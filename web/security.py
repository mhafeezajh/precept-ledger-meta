"""Sessions, CSRF and response headers for the console.

- The session cookie names the person who signed in (D96), with a random nonce, signed with
  HMAC-SHA256 under a key that exists only in this process (or ``WEB_SESSION_SECRET``). It is
  ``HttpOnly`` and ``SameSite=Strict``; the person's Gate token never leaves the server. Before
  sign-in the cookie names nobody, so the sign-in form has a CSRF token too; signing in or out
  issues a new nonce, so a session planted before sign-in is useless after it.
- Every unsafe request must carry the session's CSRF token, as a form field or the
  ``X-CSRF-Token`` header htmx sends. The token is an HMAC of the nonce, so it is per session.
- Every response gets a strict Content Security Policy (scripts and styles from this origin
  only, no inline script, no framing) and the usual hardening headers.
"""

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from web.backend import Persona, WebError

COOKIE = "pl_session"
# Browsers share cookies across ports on one host, so each app names its own session cookie;
# otherwise switching windows would read the other app's cookie and reset the persona (D94).
COOKIES = {"console": COOKIE, "approvals": "pl_approvals_session"}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}


@dataclass(frozen=True)
class Session:
    persona: Persona
    csrf: str


class Sessions:
    """Signed session cookies. ``personas`` is everyone who can sign in (``web.logins``)."""

    def __init__(self, secret: bytes, personas: dict[str, Persona]) -> None:
        self._secret, self.personas = secret, personas

    def _mac(self, text: str) -> str:
        return hmac.new(self._secret, text.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self, persona: str | None) -> str:
        """A new session for this person, or for nobody yet (None)."""
        if persona is not None and persona not in self.personas:
            raise WebError("unknown persona", code="web.persona", status=400)
        value = f"{persona or ''}|{secrets.token_urlsafe(16)}"
        return f"{value}|{self._mac(value)}"

    def _parts(self, cookie: str | None) -> tuple[str, str] | None:
        if not cookie or cookie.count("|") != 2:
            return None
        persona, nonce, mac = cookie.split("|")
        if not hmac.compare_digest(mac, self._mac(f"{persona}|{nonce}")):
            return None
        return persona, nonce

    def read(self, cookie: str | None) -> Session | None:
        """The signed-in session, or None (no cookie, a forged one, or nobody signed in)."""
        parts = self._parts(cookie)
        found = self.personas.get(parts[0]) if parts else None
        return None if parts is None or found is None else Session(found, self._csrf(parts[1]))

    def csrf(self, cookie: str | None) -> str | None:
        """The CSRF token of any genuine session, signed in or not."""
        parts = self._parts(cookie)
        return None if parts is None else self._csrf(parts[1])

    def _csrf(self, nonce: str) -> str:
        return self._mac(f"csrf|{nonce}")


class SecurityMiddleware(BaseHTTPMiddleware):
    """Attach the session (issuing one for nobody yet if needed), refuse unsafe requests
    without the session's CSRF token, and set the security headers. Whether a page needs
    sign-in, and which roles may open it, is the routes' guard (``web.app.guard``)."""

    def __init__(
        self, app: object, sessions: Sessions, *, secure_cookie: bool, cookie: str = COOKIE
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # Starlette's ASGIApp type
        self._sessions, self._secure, self._cookie = sessions, secure_cookie, cookie

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cookie = request.cookies.get(self._cookie)
        csrf, issued = self._sessions.csrf(cookie), None
        if csrf is None:
            issued = self._sessions.issue(None)
            csrf = self._sessions.csrf(issued)
        request.state.csrf = csrf
        request.state.session = None if issued else self._sessions.read(cookie)
        if request.method not in SAFE_METHODS and not await _csrf_ok(request, csrf):
            response: Response = Response("Refused: the form expired. Reload the page.",
                                          status_code=403, media_type="text/plain")  # fmt: skip
        else:
            response = await call_next(request)
        if issued is not None:
            self.set_cookie(response, issued)
        response.headers.update(HEADERS)
        return response

    def set_cookie(self, response: Response, value: str) -> None:
        response.set_cookie(self._cookie, value, httponly=True, samesite="strict",
                            secure=self._secure, path="/")  # fmt: skip


async def _csrf_ok(request: Request, csrf: str | None) -> bool:
    if csrf is None:
        return False
    sent = request.headers.get("x-csrf-token")
    if sent is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        # body() first: BaseHTTPMiddleware replays a cached body to the route, but not a form
        # parsed here, so reading only form() left the route an empty form
        await request.body()
        form = await request.form()
        value = form.get("csrf")
        sent = value if isinstance(value, str) else None
    return sent is not None and hmac.compare_digest(sent, csrf)
