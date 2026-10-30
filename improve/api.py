"""Improver HTTP API (``/improve/v1``), served by ``pl-gate`` next to the learner's (D95).

Every route needs an approver token with the ``compliance-officer`` role. The improver reads
the same verified export as the learner, through the learner's ``ExportView`` and service.
``inject_fault`` (the shortcut revision) is accepted only with ``DEMO_MODE=true``.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.errors import ImproveError
from gate.core import Gate
from improve.propose import Proposer
from improve.service import ImproveService
from learn.api import learn_service, officer_of
from learn.prompt import IMPROVE_PROPOSAL


def _service(request: Request) -> ImproveService:
    state = request.app.state
    return ImproveService(
        learn_service(request), state.gate, state.learn_view, state.improve_proposer,
        state.learn_drafter, clock=state.learn_clock, lock=state.improve_lock,
    )  # fmt: skip


async def run_improver(request: Request, inject_fault: bool = False) -> JSONResponse:
    officer = officer_of(request)
    if inject_fault and not request.app.state.learn_demo_mode:
        raise ImproveError("inject_fault needs DEMO_MODE=true", code="improve.forbidden")
    proposal = await _service(request).run(officer, inject_fault=inject_fault)
    return JSONResponse({"proposal": proposal.to_json()}, status_code=201)


async def current_prompt(request: Request) -> JSONResponse:
    officer_of(request)
    found = await _service(request).current()
    return JSONResponse({"version": found.version, "sha256": found.sha256,
                         "record_id": found.record_id,
                         "instructions": found.instructions})  # fmt: skip


async def get_proposal(proposal_id: str, request: Request) -> JSONResponse:
    officer_of(request)
    gate: Gate = request.app.state.gate
    proposal = await gate.read_record(proposal_id, IMPROVE_PROPOSAL)
    approvals = await _service(request).approvals(proposal_id)
    return JSONResponse({
        "proposal": proposal.to_json(), "approvals": [a.to_json() for a in approvals],
    })  # fmt: skip


async def approve_proposal(proposal_id: str, request: Request) -> JSONResponse:
    result = await _service(request).approve(proposal_id, officer_of(request))
    return JSONResponse({"approval": result.approval.to_json(), "in_force": result.in_force},
                        status_code=201)  # fmt: skip


def install_improve_routes(app: FastAPI, proposer: Proposer | None) -> None:
    """After ``install_learn_routes``: the improver tests prompts with the learner's drafter."""
    app.state.improve_proposer = proposer
    app.state.improve_lock = asyncio.Lock()
    app.add_api_route("/improve/v1/run", run_improver, methods=["POST"])
    app.add_api_route("/improve/v1/prompt", current_prompt, methods=["GET"])
    app.add_api_route("/improve/v1/proposals/{proposal_id}", get_proposal, methods=["GET"])
    app.add_api_route(
        "/improve/v1/proposals/{proposal_id}/approve", approve_proposal, methods=["POST"]
    )
