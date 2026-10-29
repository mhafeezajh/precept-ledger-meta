"""Policy Learner HTTP API (``/learn/v1``), served by ``pl-gate`` next to the policy API.

Every route needs an approver token with the ``compliance-officer`` role. A run reads the
stream's export from the ledger API and verifies it before anything is counted
(``ExportView``); approvals are read from the same export and each one is verified against
Gate's key and its receipt. ``inject_fault`` is accepted only with ``DEMO_MODE=true``.
"""

import asyncio
import re
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from assist.evidence import HttpLedgerReader
from claims.checker import Trust
from claims.log import ProvenLog, prove_log
from common.clock import Clock, SystemClock
from common.errors import LearnError, PreceptError
from gate.auth import APPROVER, Callers
from gate.core import Gate
from learn.draft import LearnDrafter
from learn.patterns import FAST_SECONDS, MIN_SAMPLE
from learn.service import PROPOSAL, LearnService, LedgerView
from ledger.format import parse_canonical
from policy.service import Officer
from verify.bundle import Bundle

_WINDOW = re.compile(r"^(\d{1,2})d?$")


class LearnSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEARN_", env_file=".env", extra="ignore", frozen=True
    )

    # agent=owner pairs, comma-separated: an owner may not approve loosening their agent's rules
    agent_owners: str = "payments-ops=j.ortiz"
    min_sample: int = MIN_SAMPLE
    fast_seconds: int = FAST_SECONDS
    export_timeout_seconds: float = 30.0
    # Count only records under a checkpoint cosigned by the witness quorum (KEYS_DIR/witnesses.txt).
    witnessed: bool = True

    def owners(self) -> dict[str, str]:
        pairs = (p.split("=", 1) for p in self.agent_owners.split(",") if "=" in p)
        return {agent.strip(): owner.strip() for agent, owner in pairs}


class ExportView:
    """The stream's export from the ledger API: proven for counting, raw for approvals."""

    def __init__(
        self,
        base_url: str,
        stream: str,
        trust: Trust,
        timeout: float,
        http: httpx.AsyncClient | None = None,  # tests: a client on the ASGI app
    ) -> None:
        self._base, self._stream, self._trust, self._timeout = base_url, stream, trust, timeout
        self._http = http

    async def _export(self) -> Bundle:
        try:
            if self._http is not None:
                return await HttpLedgerReader(self._base, self._http, self._stream).export()
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                return await HttpLedgerReader(self._base, http, self._stream).export()
        except PreceptError as exc:
            raise LearnError(str(exc.args[0]), code="learn.ledger_unavailable") from exc

    async def proven(self) -> ProvenLog:
        log = prove_log(await self._export(), self._trust)
        if isinstance(log, str):  # R4: nothing is learned from a log that does not verify
            raise LearnError(log, code="learn.log_unproven")
        return log

    async def records(self) -> list[dict[str, Any]]:
        out = []
        for line in (await self._export()).records:
            value = parse_canonical(line)
            if isinstance(value, dict):
                out.append(value)
        return out


def _officer(request: Request) -> Officer:
    callers: Callers = request.app.state.callers
    caller = callers.authenticate(request.headers.get("authorization"), kind=APPROVER)
    return Officer(caller.name, caller.roles)


def _service(request: Request) -> LearnService:
    state = request.app.state
    gate: Gate = state.gate
    settings: LearnSettings = state.learn_settings
    return LearnService(
        gate, state.learn_drafter, state.learn_view, clock=state.learn_clock,
        owners=settings.owners(), lock=state.learn_lock, min_sample=settings.min_sample,
        fast_seconds=settings.fast_seconds,
    )  # fmt: skip


async def run_learner(
    request: Request, window: str = "7d", inject_fault: bool = False
) -> JSONResponse:
    officer = _officer(request)
    if inject_fault and not request.app.state.learn_demo_mode:
        raise LearnError("inject_fault needs DEMO_MODE=true", code="learn.forbidden")
    match = _WINDOW.fullmatch(window)
    if match is None:
        raise LearnError("window is a number of days, e.g. 7d", code="learn.window")
    days = int(match.group(1))
    result = await _service(request).run(officer, window_days=days, inject_fault=inject_fault)
    return JSONResponse(result.to_json(), status_code=201)


async def get_proposal(proposal_id: str, request: Request) -> JSONResponse:
    _officer(request)
    service = _service(request)
    gate: Gate = request.app.state.gate
    proposal = await gate.read_record(proposal_id, PROPOSAL)
    approvals = await service.approvals(proposal_id)
    return JSONResponse({
        "proposal": proposal.to_json(), "approvals": [a.to_json() for a in approvals],
    })  # fmt: skip


async def approve_proposal(proposal_id: str, request: Request) -> JSONResponse:
    result = await _service(request).approve(proposal_id, _officer(request))
    draft = None if result.policy_draft is None else result.policy_draft.to_json()
    return JSONResponse(
        {"approval": result.approval.to_json(), "policy_draft": draft}, status_code=201
    )


def install_learn_routes(
    app: FastAPI,
    drafter: LearnDrafter | None,
    view: LedgerView,
    settings: LearnSettings,
    *,
    demo_mode: bool,
    clock: Clock | None = None,
) -> None:
    app.state.learn_drafter = drafter
    app.state.learn_clock = clock or SystemClock()
    app.state.learn_view = view
    app.state.learn_settings = settings
    app.state.learn_lock = asyncio.Lock()
    app.state.learn_demo_mode = demo_mode
    app.add_api_route("/learn/v1/run", run_learner, methods=["POST"])
    app.add_api_route("/learn/v1/proposals/{proposal_id}", get_proposal, methods=["GET"])
    app.add_api_route(
        "/learn/v1/proposals/{proposal_id}/approve", approve_proposal, methods=["POST"]
    )
