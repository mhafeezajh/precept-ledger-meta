"""Attacks (demo mode only): tamper with a record, or rewrite history as the operator."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from web import demo, views
from web.app import console, done, render
from web.backend import WebError

router = APIRouter(prefix="/demo")
CONFIRM = "tamper"


def _demo_only(request: Request) -> None:
    if not console(request).demo_mode:
        raise WebError("attacks are shown only with DEMO_MODE=true", code="web.not_found",
                       status=404)  # fmt: skip


@router.get("", response_class=HTMLResponse)
async def demo_page(request: Request) -> HTMLResponse:
    _demo_only(request)
    state = await console(request).backend.ledger_state()
    decisions = [r for r in state.records if r.get("type") == "gate.decision"]
    payments = [r for r in decisions if (r["body"].get("resource") or {}).get("amount_cents")][:8]
    admin = console(request).extras.get("admin_database_url") is not None
    return render(request, "pages/demo.html", proof=views.seal(state.report), payments=payments,
                  admin=admin, confirm=CONFIRM)  # fmt: skip


@router.post("/tamper")
async def tamper(request: Request, seq: int = Form(...), confirm: str = Form("")) -> Response:
    _demo_only(request)
    if confirm.strip().lower() != CONFIRM:
        raise WebError(f"type {CONFIRM} to confirm the edit", code="web.bad_request", status=400)
    c = console(request)
    url = c.extras.get("admin_database_url")
    if not isinstance(url, str):
        raise WebError("set ADMIN_DATABASE_URL to use the tamper button", code="web.admin",
                       status=503)  # fmt: skip
    changed = await demo.tamper(url, c.backend.stream, seq)
    state = await c.backend.ledger_state()
    return done(request, "partials/tampered.html", "/ledger", seq=seq, changed=changed,
                proof=views.seal(state.report))  # fmt: skip


@router.post("/rogue")
async def rogue(request: Request) -> Response:
    _demo_only(request)
    run = await console(request).extras.get("rogue", demo.rogue_operator)()
    return done(request, "partials/rogue.html", "/demo", run=run)
