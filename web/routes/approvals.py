"""Approvals: what Gate is holding, with its brief, and the person's verdict.

The verdict goes to Gate with the chosen persona's token; Gate checks the role, records a
signed ``gate.verdict`` and only then lets the held call run. The console decides nothing.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from web.app import console, done, render
from web.backend import WebError

router = APIRouter(prefix="/approvals")


async def _queue(request: Request) -> list[dict[str, object]]:
    return await console(request).backend.escalations(request.state.session.persona)


@router.get("", response_class=HTMLResponse)
async def approvals(request: Request) -> HTMLResponse:
    return render(request, "pages/approvals.html", queue=await _queue(request))


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request) -> HTMLResponse:
    """The list alone, polled by the page so a new hold appears without a reload."""
    return render(request, "partials/queue.html", queue=await _queue(request))


@router.post("/{escalation_id}/verdict")
async def verdict(request: Request, escalation_id: str, verdict: str = Form(...)) -> Response:
    if verdict not in ("approve", "reject"):
        raise WebError("choose approve or reject", code="web.bad_request", status=400)
    persona = request.state.session.persona
    record = await console(request).backend.give_verdict(persona, escalation_id, verdict)
    response = done(request, "partials/verdict.html", "/approvals", record=record["record"],
                    receipt=record["receipt"])  # fmt: skip
    response.headers["hx-trigger"] = "queue-changed"  # the list refreshes at once
    return response
