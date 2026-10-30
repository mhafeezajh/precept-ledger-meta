"""The Approvals app's inbox: everything held until a person signs, and what they signed (D94).

Three kinds of decision wait here: payments Gate is holding, policy drafts ready to approve,
and learner suggestions in review. Every verdict and approval goes to Gate with the chosen
persona's token; Gate checks the role, records it signed, and only then acts. The app decides
nothing.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from web import views
from web.app import console, done, render
from web.backend import WebError

router = APIRouter()


async def _queue(request: Request) -> list[dict[str, object]]:
    return await console(request).backend.escalations(request.state.session.persona)


@router.get("/", response_class=HTMLResponse)
async def inbox(request: Request) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    approval, _ = views.in_force(state.records)
    approvals = views.proposal_approvals(state.records)
    suggestions = [
        r for r in state.records
        if r.get("type") == "learn.proposal"
        and views.proposal_status(r, approvals, approval) == "waiting"
    ]  # fmt: skip
    return render(request, "pages/inbox.html", queue=await _queue(request),
                  drafts=views.drafts_to_approve(state.records, approval),
                  suggestions=suggestions, approvals=approvals)  # fmt: skip


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request) -> HTMLResponse:
    """The held payments alone, polled by the inbox so a new hold appears without a reload."""
    return render(request, "partials/queue.html", queue=await _queue(request))


@router.post("/escalations/{escalation_id}/verdict")
async def verdict(request: Request, escalation_id: str, verdict: str = Form(...)) -> Response:
    if verdict not in ("approve", "reject"):
        raise WebError("choose approve or reject", code="web.bad_request", status=400)
    persona = request.state.session.persona
    record = await console(request).backend.give_verdict(persona, escalation_id, verdict)
    response = done(request, "partials/verdict.html", "/", record=record["record"],
                    receipt=record["receipt"])  # fmt: skip
    response.headers["hx-trigger"] = "queue-changed"  # the list refreshes at once
    return response


@router.get("/decided", response_class=HTMLResponse)
async def decided(request: Request) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    mine = views.decided_by(state.records, request.state.session.persona.name)
    return render(request, "pages/decided.html", records=mine[:30], total=len(mine))
