"""Overview: is everything up, is the log proven, and what happened most recently."""

from collections import Counter
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web import views
from web.app import console, render
from web.backend import WebError

router = APIRouter()
RECENT = 12


async def _status(call: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return await call(), None
    except WebError as exc:
        return None, exc.message


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    backend = console(request).backend
    gate, gate_error = await _status(backend.gate_health)
    ledger, ledger_error = await _status(backend.ledger_health)
    state, seal = None, None
    if ledger is not None:
        try:
            state = await backend.ledger_state()
            seal = views.seal(state.report)
        except WebError as exc:
            ledger_error = exc.message
    decisions = [r for r in (state.records if state else ()) if r.get("type") == "gate.decision"]
    outcomes = Counter(str(r["body"].get("decision")) for r in decisions)
    return render(
        request, "pages/overview.html", gate=gate, gate_error=gate_error, ledger=ledger,
        ledger_error=ledger_error, proof=seal, state=state, outcomes=outcomes,
        recent=decisions[:RECENT],
    )  # fmt: skip
