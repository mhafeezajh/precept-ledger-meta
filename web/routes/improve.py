"""Improve the learner: revisions of the learner's prompt, their scores and their review (D95).

Runs are in the Console (``router``); approvals are in the Approvals app (``review``), like the
learner's. Pages are read from the verified export, so anyone may see what changed and why;
running and approving go to ``pl-gate`` (``/improve/v1``) under the chosen persona's token,
and Gate refuses anyone without the compliance-officer role.
"""

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from web import views
from web.app import console, done, is_htmx, render
from web.backend import LedgerState, WebError

router = APIRouter(prefix="/improve")  # the Console
review = APIRouter(prefix="/improve")  # the Approvals app
CANARY_LABELS = {"caught": ("allow", "Caught"), "missed": ("deny", "Missed"),
                 "unplantable": ("deny", "Nothing to check"),
                 "not_applicable": ("neutral", "Not a payment")}  # fmt: skip


def _proposals(state: LedgerState) -> list[dict[str, Any]]:
    return [r for r in state.records if r.get("type") == "improve.proposal"]


@router.get("", response_class=HTMLResponse)
async def improve_page(request: Request) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    approvals = views.improvement_approvals(state.records)
    in_force = views.improvement_in_force(state.records)
    proposals = _proposals(state)
    states = {p["record_id"]: views.improvement_status(p, approvals, in_force) for p in proposals}
    officer = "compliance-officer" in request.state.session.persona.roles
    return render(request, "pages/improve.html", history=proposals[:20], states=states,
                  approvals=approvals, in_force=state.by_id.get(in_force or ""),
                  officer=officer)  # fmt: skip


@router.post("/run")
async def run(request: Request, inject_fault: str = Form("")) -> Response:
    fault = bool(inject_fault) and console(request).demo_mode
    persona = request.state.session.persona
    result = await console(request).backend.run_improver(persona, inject_fault=fault)
    path = f"/improve/proposals/{result['proposal']['record']['record_id']}"
    if is_htmx(request):
        return Response(status_code=204, headers={"hx-location": path})
    return RedirectResponse(path, status_code=303)


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
@review.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_page(request: Request, proposal_id: str) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    record = state.by_id.get(proposal_id)
    if record is None or record.get("type") != "improve.proposal":
        raise WebError("No such prompt change.", code="web.not_found", status=404)
    body = record["body"]
    approvals = [
        r for r in reversed(state.records)
        if r.get("type") == "improve.approve" and r["body"].get("proposal_record_id") == proposal_id
    ]  # fmt: skip
    counts = views.improvement_approvals(state.records)
    # not "status": render() takes that as the HTTP status code
    standing = views.improvement_status(record, counts, views.improvement_in_force(state.records))
    current, candidate = body["scores"]["current"], body["scores"]["candidate"]
    cases = [(old, new) for old, new in zip(current["cases"], candidate["cases"], strict=True)]
    return render(request, "pages/improvement.html", record=record, body=body,
                  approvals=approvals, standing=standing, current=current, candidate=candidate,
                  cases=cases, canary_labels=CANARY_LABELS,
                  diff=views.prompt_diff(body["base"].get("instructions", ""),
                                         body["instructions"]))  # fmt: skip


@review.post("/proposals/{proposal_id}/approve")
async def approve(request: Request, proposal_id: str) -> Response:
    persona = request.state.session.persona
    result = await console(request).backend.approve_improvement(persona, proposal_id)
    return done(request, "partials/improve_approved.html", f"/improve/proposals/{proposal_id}",
                approval=result["approval"]["record"], in_force=result["in_force"])  # fmt: skip
