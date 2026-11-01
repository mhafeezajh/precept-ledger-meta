"""Who may sign in to the web apps, and with which roles (D96).

``KEYS_DIR/web-logins.txt`` has one line per person: ``name roles scrypt$n$r$p$salt$hash``.
Passwords are kept only as salted scrypt hashes; ``make dev-keys`` writes the plain demo
passwords to ``web-passwords.txt`` beside it (owner-only), never into the repo or a log.

- A **Gate person** (a supervisor or compliance officer) must be a Gate approver with exactly
  the roles Gate gives them, or the app does not start: the screens must never promise what
  Gate would refuse. They act with their own Gate token, on the server.
- A **web-only person** (the examiner, the demo admin) has no Gate token and can never act
  through Gate; they only read what the app verified.

Five wrong passwords for one name within 15 minutes lock that name until the window passes.
Which screens each role sees is in ``web.nav``.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from common.errors import ConfigError
from web.backend import Persona, WebError

SUPERVISOR, OFFICER = "payments-supervisor", "compliance-officer"  # Gate's roles
EXAMINER, ADMIN = "examiner", "demo-admin"  # web-only: no Gate token
GATE_ROLES = frozenset({SUPERVISOR, OFFICER})
WEB_ONLY_ROLES = frozenset({EXAMINER, ADMIN})
WEB_KIND = "web"  # Persona.kind of a web-only person
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1  # 16 MiB per check (OWASP's minimum for scrypt)
LIMIT, WINDOW_SECONDS = 5, 15 * 60


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    """True only for the right password; a malformed hash is refused like a wrong password."""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt, expected = base64.b64decode(parts[4], validate=True), base64.b64decode(parts[5])
        got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                             maxmem=64 * 1024 * 1024)  # fmt: skip
    except (ValueError, binascii.Error):
        return False
    return len(expected) > 0 and hmac.compare_digest(got, expected)


@dataclass(frozen=True)
class Login:
    name: str
    roles: frozenset[str]
    password_hash: str = field(repr=False)


def parse_logins(text: str) -> dict[str, Login]:
    logins: dict[str, Login] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ConfigError(f"web logins line {number}: expected: name roles password-hash",
                              code="config.logins")  # fmt: skip
        name, roles_text, stored = parts
        roles = frozenset(roles_text.split(","))
        if not roles <= GATE_ROLES | WEB_ONLY_ROLES:
            raise ConfigError(f"web logins line {number}: unknown role", code="config.logins")
        if not stored.startswith("scrypt$") or stored.count("$") != 5:
            raise ConfigError(f"web logins line {number}: not a scrypt password hash",
                              code="config.logins")  # fmt: skip
        if name in logins:
            raise ConfigError(f"web logins: {name} is listed twice", code="config.logins")
        logins[name] = Login(name, roles, stored)
    return logins


def load_logins(path: Path) -> dict[str, Login]:
    try:
        return parse_logins(path.read_text("utf-8"))
    except OSError as exc:  # fail closed: no file, nobody signs in
        raise ConfigError(f"no web logins at {path}: run make dev-keys",
                          code="config.logins") from exc  # fmt: skip


def people(logins: Mapping[str, Login], gate: Mapping[str, Persona]) -> dict[str, Persona]:
    """Everyone who can sign in, as the persona the app acts for. Refuses a login whose roles
    differ from Gate's, so a screen never offers what Gate would refuse."""
    found: dict[str, Persona] = {}
    for name, login in logins.items():
        persona = gate.get(name)
        if login.roles <= WEB_ONLY_ROLES and persona is None:
            found[name] = Persona(name, WEB_KIND, login.roles, "")
        elif persona is not None and persona.kind == "approver" and persona.roles == login.roles:
            found[name] = persona
        else:
            raise ConfigError(f"web logins: {name}'s roles do not match Gate's callers",
                              code="config.logins")  # fmt: skip
    return found


@cache
def _decoy() -> str:
    return hash_password(secrets.token_urlsafe(16))


class SignIn:
    """Checks passwords, and locks a name after ``LIMIT`` failures within the window."""

    def __init__(
        self, logins: Mapping[str, Login], *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._logins, self._clock = dict(logins), clock
        self._failures: dict[str, list[float]] = {}  # known names only, so it stays bounded

    def check(self, name: str, password: str) -> Login | None:
        now = self._clock()
        recent = [t for t in self._failures.get(name, []) if now - t < WINDOW_SECONDS]
        if len(recent) >= LIMIT:
            raise WebError("Too many attempts for this name. Try again in 15 minutes.",
                           code="web.locked", status=429)  # fmt: skip
        login = self._logins.get(name)
        # an unknown name costs the same scrypt work, so timing does not reveal who exists
        ok = check_password(password, login.password_hash if login else _decoy())
        if login is None or not ok:
            if login is not None:
                self._failures[name] = [*recent, now]
            return None
        self._failures.pop(name, None)
        return login
