"""``pl-web`` and ``pl-approvals``: the Precept Ledger web apps (FastAPI, Jinja, htmx).

One codebase builds two apps (D94). The Console operates and proves (agent, drafting, learner,
audit, ledger, attacks); the Approvals app is the inbox where people sign, and mounts nothing
else, so an approver's session cannot reach a drafting, admin or attack route at all.

Each app is a client of ``pl-gate`` and ``pl-ledger`` like any other: it decides nothing,
signs nothing for Gate or the log, and shows as proven only what it verified itself
(``web.backend``). Pages are server-rendered; htmx swaps fragments for actions and live
updates, and every action also works as a plain form post. See ``docs/modules/web.md``.
"""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from claims.checker import Trust
from common.config import Settings, load_settings
from common.errors import ConfigError, PreceptError
from common.logging import configure_logging, log_event
from ledger.checkpointer import origin_for
from verify.keys import TrustedKeys, WitnessPolicy
from web import views
from web.backend import Backend, WebError, load_personas
from web.nav import APPROVALS, CONSOLE, TITLES, current, navigation
from web.security import COOKIES, SecurityMiddleware, Sessions

HERE = Path(__file__).resolve().parent
logger = logging.getLogger("pl.web")
STATIC_VERSION = "5"  # bump when static files change, for cache busting


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEB_", env_file=".env", extra="ignore", frozen=True
    )

    host: str = "127.0.0.1"  # localhost only: the console acts with real Gate tokens
    port: int = 8090  # the Console
    approvals_port: int = 8094  # the Approvals app
    console_url: str = "http://127.0.0.1:8090"  # for links between the two apps
    approvals_url: str = "http://127.0.0.1:8094"
    gate_url: str = "http://127.0.0.1:8070"
    ledger_url: str = "http://127.0.0.1:8080"
    default_persona: str = "j.ortiz"
    timezone: str = "America/New_York"
    timeout_seconds: float = 200.0  # a learner run waits for one model call (up to 150 s)
    witnessed: bool = True  # proven means cosigned by the witness quorum
    secure_cookie: bool = False  # true behind HTTPS
    session_secret: str = ""  # empty: a fresh key per start (sessions end on restart)
    # The demo tamper button's superuser connection; used only with DEMO_MODE=true.
    admin_database_url: SecretStr | None = Field(
        default=None, validation_alias="ADMIN_DATABASE_URL"
    )


@dataclass
class Console:
    """Everything a route needs, built once at startup."""

    backend_: Backend | None  # set when the app starts (it owns the HTTP clients)
    sessions: Sessions
    zone: ZoneInfo
    demo_mode: bool
    settings: Settings | None = None  # for features that build their own clients (agent, audit)
    web: WebSettings = field(default_factory=WebSettings)
    extras: dict[str, Any] = field(default_factory=dict)  # feature state (agent runs, ...)
    app: str = CONSOLE  # which of the two apps this is

    @property
    def backend(self) -> Backend:
        if self.backend_ is None:
            raise WebError("the console is still starting", code="web.starting", status=503)
        return self.backend_


templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters.update(
    dollars=views.dollars, tone=views.tone, summary=views.summary, outcome=views.outcome,
    type_label=lambda t: views.TYPE_LABELS.get(str(t), str(t)),
)  # fmt: skip


def console(request: Request) -> Console:
    found: Console = request.app.state.console
    return found


def render(request: Request, name: str, status: int = 200, **values: Any) -> HTMLResponse:
    c = console(request)
    page = current(request.url.path, c.app)
    base = {
        "session": request.state.session,
        "personas": c.sessions.personas,
        "nav": navigation(c.app, demo_mode=c.demo_mode),
        "page": page,
        "app_name": c.app,
        "app_title": TITLES[c.app],
        "console_url": c.web.console_url,
        "approvals_url": c.web.approvals_url,
        # record links go to the Console's ledger pages; the Approvals app has none
        "record_base": c.web.console_url if c.app == APPROVALS else "",
        "demo_mode": c.demo_mode,
        "static_version": STATIC_VERSION,
        "when": lambda ts: views.local_time(ts, c.zone),
        "zone": c.zone.key,
    }
    return templates.TemplateResponse(request, name, {**base, **values}, status_code=status)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def done(request: Request, fragment: str, back: str, **values: Any) -> Response:
    """The result of an action: a fragment for htmx, or a redirect back for a plain form."""
    if is_htmx(request):
        return render(request, fragment, **values)
    return RedirectResponse(back, status_code=303)


async def _web_error(request: Request, exc: Exception) -> Response:
    if isinstance(exc, WebError):
        error = exc
    elif isinstance(exc, PreceptError):
        error = WebError(str(exc.args[0]), code=exc.code, status=400)
    else:  # a bug: logged with its type only, never shown with details
        log_event(logger, logging.ERROR, "page failed", path=request.url.path,
                  error=type(exc).__name__)  # fmt: skip
        error = WebError("Something went wrong on this page.", code="web.internal", status=500)
    template = "partials/error.html" if is_htmx(request) else "pages/error.html"
    response = render(request, template, status=error.status, error=error)
    if is_htmx(request):  # into the notice area at the top, even on an error status
        response.headers["hx-retarget"] = "#flash"
        response.headers["hx-reswap"] = "innerHTML"
    return response


async def choose_persona(request: Request) -> Response:
    form = await request.form()
    name = str(form.get("persona", ""))
    c = console(request)
    response = RedirectResponse(str(form.get("back") or "/"), status_code=303)
    if name in c.sessions.personas:
        response.set_cookie(COOKIES[c.app], c.sessions.issue(name), httponly=True,
                            samesite="strict", secure=c.web.secure_cookie, path="/")  # fmt: skip
    return response


def create_web_app(made: Console, *, lifespan: Any = None) -> FastAPI:
    from web.routes import agent, approvals, audit, demo, improve, learn, ledger, overview, policy

    routers = {  # each app mounts only its own routes (D94)
        CONSOLE: (overview.router, agent.router, policy.router, learn.router, improve.router,
                  ledger.router, audit.router, demo.router),
        APPROVALS: (approvals.router, policy.review, learn.review, improve.review),
    }[made.app]  # fmt: skip
    app = FastAPI(title=f"Precept Ledger {TITLES[made.app]}", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)  # fmt: skip
    app.state.console = made
    app.add_middleware(SecurityMiddleware, sessions=made.sessions,
                       secure_cookie=made.web.secure_cookie, cookie=COOKIES[made.app])  # fmt: skip
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.add_api_route("/session/persona", choose_persona, methods=["POST"])
    for router in routers:
        app.include_router(router)
    app.add_exception_handler(PreceptError, _web_error)
    app.add_exception_handler(Exception, _web_error)
    return app


def _trust(settings: Settings, web: WebSettings) -> Trust:
    try:
        keys = TrustedKeys.from_json((settings.keys_dir / "trusted.json").read_bytes())
        witnesses = None
        if web.witnessed:
            raw = (settings.keys_dir / "witnesses.txt").read_bytes()
            witnesses = WitnessPolicy.from_text(raw, settings.witness_quorum)
    except (OSError, PreceptError) as exc:
        raise ConfigError(f"console keys: {exc}", code="config.keys") from exc
    return Trust(keys, origin_for(settings.ledger_origin, settings.ledger_stream), witnesses)


def _features(settings: Settings, web: WebSettings, trust: Trust) -> dict[str, Any]:
    """The agent runner, the auditor and the admin connection, when they are configured."""
    from agent.cli import open_agent
    from assist.auditor import Auditor, LedgerRecorder
    from assist.evidence import HttpLedgerReader
    from assist.llm import LlmClient, model_config
    from common.clock import SystemClock
    from gate.ledger_client import HttpLedgerClient
    from ledger.receipts import LOG_KEY_OWNER
    from ledger.signing import KeyRing, Signer
    from web.agent_runs import AgentRuns

    extras: dict[str, Any] = {}
    config = model_config(settings)
    if config is not None:
        extras["agent_runs"] = AgentRuns(lambda: open_agent(settings))
        assist_key = settings.keys_dir / "assist-1.pem"
        log_keys = KeyRing((LOG_KEY_OWNER, raw) for raw in trust.keys.log_keys)
        stream = settings.ledger_stream

        async def ask(question: str, inject_fault: bool) -> Any:
            clock = SystemClock()
            async with httpx.AsyncClient(timeout=web.timeout_seconds) as http:
                recorder = None
                if assist_key.is_file():  # the audit is recorded as assist.qa, as in demo-audit
                    recorder = LedgerRecorder(
                        signer=Signer.from_pem_file(assist_key), producer_id="assist-1",
                        stream=stream, ledger=HttpLedgerClient(web.ledger_url, http),
                        log_keys=log_keys, clock=clock,
                    )  # fmt: skip
                auditor = Auditor(
                    LlmClient(config), HttpLedgerReader(web.ledger_url, http, stream),
                    trust, recorder, clock=clock, timezone=ZoneInfo(web.timezone),
                )  # fmt: skip
                return await auditor.ask(question, inject_fault=inject_fault)

        extras["audit"] = ask
    if settings.demo_mode and web.admin_database_url is not None:
        extras["admin_database_url"] = web.admin_database_url.get_secret_value()
    return extras


def app_from_settings(app: str = CONSOLE) -> FastAPI:
    settings, web = load_settings(), WebSettings()
    configure_logging(settings.log_level)
    personas = load_personas(settings.keys_dir)
    approvers = {n: p for n, p in personas.items() if p.kind == "approver"}
    secret = web.session_secret.encode() or secrets.token_bytes(32)
    sessions = Sessions(secret, approvers, web.default_persona)
    trust = _trust(settings, web)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            httpx.AsyncClient(base_url=web.gate_url, timeout=web.timeout_seconds) as gate,
            httpx.AsyncClient(base_url=web.ledger_url, timeout=web.timeout_seconds) as ledger,
        ):
            app.state.console.backend_ = Backend(
                gate, ledger, stream=settings.ledger_stream, trust=trust, personas=personas
            )
            yield

    made = Console(
        None,
        sessions,
        ZoneInfo(web.timezone),
        settings.demo_mode,
        settings,
        web,
        _features(settings, web, trust) if app == CONSOLE else {},  # no agent, auditor or admin
        app,
    )
    return create_web_app(made, lifespan=lifespan)


def approvals_from_settings() -> FastAPI:
    return app_from_settings(APPROVALS)


def main() -> None:
    web = WebSettings()
    uvicorn.run("web.app:app_from_settings", factory=True, host=web.host, port=web.port,
                log_config=None, access_log=False)  # fmt: skip


def approvals_main() -> None:
    web = WebSettings()
    uvicorn.run("web.app:approvals_from_settings", factory=True, host=web.host,
                port=web.approvals_port, log_config=None, access_log=False)  # fmt: skip
