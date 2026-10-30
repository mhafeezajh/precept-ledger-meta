"""Learner: proposals from the ledger's history, their evidence and their review (beat 6).

Runs are in the Console (``router``); approvals are in the Approvals app (``review``), D94.
Both happen in ``pl-gate`` (``/learn/v1``) under the chosen persona's token. A loosening needs
two compliance officers, a tightening one; the approval that completes a review returns a
policy draft, approved in the Approvals app like any other.
"""

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from policy.rules import parse_rules
from web import views
from web.app import console, done, render

router = APIRouter(prefix="/learn")  # the Console
review = APIRouter(prefix="/learn")  # the Approvals app
REPLAY_LABELS = (
    ("escalations_removed", "holds no longer needed"),
    ("new_denials", "requests now denied"),
    ("new_escalations", "requests now held"),
    ("rejections_newly_allowed", "past rejections now allowed"),
    ("approvals_now_denied", "past approvals now denied"),
    ("outside_scope", "changes outside the pattern"),
)


@router.get("", response_class=HTMLResponse)
async def learn_page(request: Request) -> HTMLResponse:
    state = await console(request).backend.ledger_state()
    proposals = [r for r in state.records if r.get("type") == "learn.proposal"]
    approvals = views.proposal_approvals(state.records)
    officer = "compliance-officer" in request.state.session.persona.roles
    approval, _ = views.in_force(state.records)
    states = {p["record_id"]: views.proposal_status(p, approvals, approval) for p in proposals}
    waiting = [p for p in proposals if states[p["record_id"]] == "waiting"]
    return render(request, "pages/learn.html", waiting=waiting, history=proposals[:20],
                  states=states, approvals=approvals, officer=officer)  # fmt: skip


@router.post("/run")
async def run(request: Request, inject_fault: str = Form("")) -> Response:
    fault = bool(inject_fault) and console(request).demo_mode
    persona = request.state.session.persona
    result = await console(request).backend.run_learner(persona, inject_fault=fault)
    return done(request, "partials/learn_run.html", "/learn", result=result)


def _proposal_view(found: dict[str, Any]) -> dict[str, Any]:
    body = found["proposal"]["record"]["body"]
    rules = list(parse_rules(body["rules_yaml"])) if body["rules_yaml"] else []
    replay = body.get("replay") or {}
    return {
        "record": found["proposal"]["record"],
        "body": body,
        "rules": rules,
        "replay": [(label, replay[key]) for key, label in REPLAY_LABELS if key in replay],
        "approvals": [a["record"] for a in found["approvals"]],
    }


@router.get("/proposals/{proposal_id}", response_class=HTMLResponse)
@review.get("/proposals/{proposal_id}", response_class=HTMLResponse)
async def proposal_page(request: Request, proposal_id: str) -> HTMLResponse:
    persona = request.state.session.persona
    found = await console(request).backend.proposal(persona, proposal_id)
    return render(request, "pages/proposal.html", conditions=views.conditions,
                  **_proposal_view(found))  # fmt: skip


@review.post("/proposals/{proposal_id}/approve")
async def approve(request: Request, proposal_id: str) -> Response:
    persona = request.state.session.persona
    result = await console(request).backend.approve_proposal(persona, proposal_id)
    return done(request, "partials/proposal_approved.html", f"/learn/proposals/{proposal_id}",
                approval=result["approval"]["record"], draft=result["policy_draft"])  # fmt: skip
