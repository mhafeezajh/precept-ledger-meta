"""Learner: proposals from the ledger's history, their evidence and their review (beat 6).

Runs and approvals happen in ``pl-gate`` (``/learn/v1``) under the chosen persona's token.
A loosening needs two compliance officers, a tightening one; the approval that completes a
review returns a policy draft, approved on the Policy page like any other.
"""

from collections import Counter
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from policy.rules import parse_rules
from web import views
from web.app import console, done, render

router = APIRouter(prefix="/learn")
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
    approvals = Counter(
        str(r["body"].get("proposal_record_id"))
        for r in state.records
        if r.get("type") == "learn.approve"
    )
    officer = "compliance-officer" in request.state.session.persona.roles
    approval, _ = views.in_force(state.records)
    states = {p["record_id"]: _status(p, approvals, approval) for p in proposals}
    waiting = [p for p in proposals if states[p["record_id"]] == "waiting"]
    return render(request, "pages/learn.html", waiting=waiting, history=proposals[:20],
                  states=states, approvals=approvals, officer=officer)  # fmt: skip


def _status(
    proposal: dict[str, Any], approvals: Counter[str], approval: dict[str, Any] | None
) -> str:
    """waiting, approved, out_of_date (drafted against a policy since replaced: approving it
    would fail Gate's stale-base check, D81), rejected or blocked."""
    body = proposal["body"]
    if body.get("state") != "review":
        return str(body.get("state"))
    if approvals.get(proposal["record_id"], 0) >= body.get("approvals_needed", 1):
        return "approved"
    base = (body.get("base") or {}).get("policy_record_id")
    current = approval["record_id"] if approval else None
    return "waiting" if base == current else "out_of_date"


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
async def proposal_page(request: Request, proposal_id: str) -> HTMLResponse:
    persona = request.state.session.persona
    found = await console(request).backend.proposal(persona, proposal_id)
    return render(request, "pages/proposal.html", conditions=views.conditions,
                  **_proposal_view(found))  # fmt: skip


@router.post("/proposals/{proposal_id}/approve")
async def approve(request: Request, proposal_id: str) -> Response:
    persona = request.state.session.persona
    result = await console(request).backend.approve_proposal(persona, proposal_id)
    return done(request, "partials/proposal_approved.html", f"/learn/proposals/{proposal_id}",
                approval=result["approval"]["record"], draft=result["policy_draft"])  # fmt: skip
