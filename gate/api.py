"""Gate HTTP API (``/gate/v1``). Agents decide and report results; approvers give verdicts.

Every response that lets a tool run carries the signed record and its receipt, so the
caller can check them itself (``gate.governed``). Error bodies are the shared
``{"error": {"code", "message"}}`` and never echo tool arguments (skill §7).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from common.errors import FormatError, GateError, PreceptError
from common.http import error_response, install_common_handlers, read_limited
from common.logging import log_event
from gate.auth import AGENT, APPROVER, Caller, Callers
from gate.core import Approver, Gate
from gate.escalations import Escalation
from gate.normalizer import MAX_ARGUMENT_BYTES
from ledger.format import canonical_bytes, parse_json_strict

logger = logging.getLogger("pl.gate.api")

MAX_BODY_BYTES = MAX_ARGUMENT_BYTES + 32 * 1024  # room for a delegation credential (D88)
MAX_WAIT_SECONDS = 25.0

_STATUS_BY_CODE = {
    "gate.bad_request": 400,
    "gate.unauthorized": 401,
    "gate.forbidden": 403,
    "gate.approver_not_allowed": 403,
    "gate.escalation_not_found": 404,
    "gate.record_not_found": 404,
    "gate.escalation_closed": 409,
    "gate.not_cleared": 409,
    "policy.not_clean": 409,
    "policy.stale_base": 409,
    "delegation.malformed": 400,
    "delegation.bad_signature": 400,
    "delegation.already_issued": 409,
    "delegation.not_issued": 404,
    "delegation.revoked": 409,
    "delegation.disabled": 404,
    "learn.forbidden": 403,
    "learn.owner": 403,
    "learn.not_reviewable": 409,
    "learn.already_approved": 409,
    "learn.no_active_policy": 409,
    "learn.base_mismatch": 409,
    "learn.no_model": 503,
    "learn.log_unproven": 503,
    "learn.ledger_unavailable": 503,
    "policy.forbidden": 403,
    "policy.no_drafter": 503,
    "policy.pdf_too_large": 413,
    "format.too_large": 413,
    "gate.ledger_refused": 502,
    "gate.bad_receipt": 502,
    "gate.ledger_unavailable": 503,
    "gate.store_unavailable": 503,
}


def status_for(exc: PreceptError) -> int:
    if exc.code in _STATUS_BY_CODE:
        return _STATUS_BY_CODE[exc.code]
    return 400 if exc.code.startswith(("format.", "policy.", "learn.")) else 500


def _gate(request: Request) -> Gate:
    gate: Gate = request.app.state.gate
    return gate


def _caller(request: Request, kind: str | None) -> Caller:
    callers: Callers = request.app.state.callers
    return callers.authenticate(request.headers.get("authorization"), kind=kind)


async def _json_object(
    request: Request, keys: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    # The strict ledger parser (D34): duplicate keys and floats are refused, as in arguments.
    raw = await read_limited(request, MAX_BODY_BYTES)
    try:
        data = parse_json_strict(raw)
    except FormatError as exc:  # the parser's message can quote a value from the arguments
        raise FormatError("request body is not strict JSON", code=exc.code) from None
    if not isinstance(data, dict) or not keys <= set(data) <= keys | optional:
        raise GateError(f"body must have the fields {sorted(keys)}", code="gate.bad_request")
    return data


async def decide(request: Request) -> JSONResponse:
    agent = _caller(request, AGENT)
    body = await _json_object(request, frozenset({"tool", "arguments"}), frozenset({"credential"}))
    if not isinstance(body["tool"], str) or not isinstance(body["arguments"], dict):
        raise GateError("tool must be a string and arguments an object", code="gate.bad_request")
    if (
        len(canonical_bytes(body["arguments"])) > MAX_ARGUMENT_BYTES
    ):  # the credential has its own room
        raise FormatError("arguments are too large", code="format.too_large")
    decision = await _gate(request).decide(
        agent.name, body["tool"], body["arguments"], credential=body.get("credential")
    )
    escalation = None if decision.escalation is None else decision.escalation.to_json()
    return JSONResponse(
        {
            "decision": decision.outcome.value,
            "reason": decision.reason,
            **decision.record.to_json(),
            "escalation": escalation,
        }
    )


async def issue_delegation(request: Request) -> JSONResponse:
    """A principal's signed credential, submitted by an approver; agents cannot issue (D88)."""
    _caller(request, APPROVER)
    record = await _gate(request).issue_delegation(await _json_any(request))
    return JSONResponse(record.to_json(), status_code=201)


async def revoke_delegation(request: Request) -> JSONResponse:
    _caller(request, APPROVER)
    record = await _gate(request).revoke_delegation(await _json_any(request))
    return JSONResponse(record.to_json(), status_code=201)


async def delegation_status(credential_id: str, request: Request) -> dict[str, Any]:
    _caller(request, APPROVER)
    delegations = _gate(request).delegations
    found = None if delegations is None else delegations.find(credential_id)
    if found is None:
        raise GateError("no issued credential with this id", code="gate.record_not_found")
    signed, revoked = found
    return {"credential": signed.to_json(), "credential_hash": signed.hash,
            "state": "revoked" if revoked else "active"}  # fmt: skip


async def _json_any(request: Request) -> object:
    raw = await read_limited(request, MAX_BODY_BYTES)
    try:
        return parse_json_strict(raw)
    except FormatError as exc:
        raise GateError("request body is not strict JSON", code="delegation.malformed") from exc


async def _with_brief(gate: Gate, escalation: Escalation) -> dict[str, Any]:
    brief = await gate.brief_record(escalation)
    return {**escalation.to_json(), "brief": None if brief is None else brief.body}


async def list_escalations(request: Request) -> dict[str, Any]:
    _caller(request, APPROVER)
    gate = _gate(request)
    return {"escalations": [await _with_brief(gate, e) for e in await gate.escalations.list_open()]}


def _check_visible(escalation: Escalation, caller: Caller) -> None:
    if caller.kind == AGENT and caller.name != escalation.agent:
        # Another agent's escalation does not exist, as far as this caller can tell.
        raise GateError("no such escalation", code="gate.escalation_not_found")


async def get_escalation(
    escalation_id: str, request: Request, wait: float = Query(0.0, ge=0.0, le=MAX_WAIT_SECONDS)
) -> dict[str, Any]:
    caller = _caller(request, None)
    gate = _gate(request)
    _check_visible(await gate.status(escalation_id), caller)
    escalation = await gate.wait(escalation_id, wait)
    verdict = await gate.verdict_record(escalation)
    brief = await gate.brief_record(escalation)
    return {
        "escalation": escalation.to_json(),
        "verdict": None if verdict is None else verdict.to_json(),
        "brief": None if brief is None else brief.to_json(),
    }


async def give_verdict(escalation_id: str, request: Request) -> JSONResponse:
    approver = _caller(request, APPROVER)
    body = await _json_object(request, frozenset({"verdict"}))
    if not isinstance(body["verdict"], str):
        raise GateError("verdict must be a string", code="gate.bad_request")
    record = await _gate(request).verdict(
        escalation_id, Approver(approver.name, approver.roles), body["verdict"]
    )
    return JSONResponse(record.to_json(), status_code=201)


async def record_result(request: Request) -> JSONResponse:
    agent = _caller(request, AGENT)
    body = await _json_object(
        request, frozenset({"decision_record_id", "outcome"}), frozenset({"result_digest", "error"})
    )
    outcome, digest, error = body["outcome"], body.get("result_digest"), body.get("error")
    if outcome not in ("ok", "error") or not all(
        v is None or isinstance(v, str) for v in (digest, error)
    ):
        raise GateError(
            "outcome must be ok or error; digest and error strings", code="gate.bad_request"
        )
    if not isinstance(body["decision_record_id"], str):
        raise GateError("decision_record_id must be a string", code="gate.bad_request")
    record = await _gate(request).record_result(
        agent.name, body["decision_record_id"], ok=outcome == "ok", digest=digest, error=error
    )
    return JSONResponse(record.to_json(), status_code=201)


async def health(request: Request) -> JSONResponse:
    gate = _gate(request)
    sweeper: asyncio.Task[None] | None = request.app.state.sweeper
    stopped = sweeper is not None and sweeper.done()
    body = {
        "status": "stopped" if stopped else "ok",
        "policy_set": gate.policies.digest,
        "rules": len(gate.policies.rules),
        "policy_record": gate.policy_record_id,  # None: the policy file Gate started with
        "open_escalations": len(await gate.escalations.list_open()),
        "briefs": gate.writes_briefs,
    }
    return JSONResponse(body, status_code=503 if stopped else 200)


async def _sweep(gate: Gate, interval: float) -> None:
    """Expire overdue escalations even when nobody is waiting on them."""
    while True:
        await asyncio.sleep(interval)
        try:
            expired = await gate.expire_due()
        except PreceptError as exc:  # store or ledger trouble: try again next tick
            log_event(logger, logging.WARNING, "expiry sweep failed", code=exc.code)
            continue
        if expired:
            log_event(logger, logging.INFO, "escalations expired", count=expired)


GateFactory = Callable[[], contextlib.AbstractAsyncContextManager[tuple[Gate, Callers]]]


def create_gate_app(
    open_gate: GateFactory,
    *,
    sweep_interval: float | None,
    extend: Callable[[FastAPI], None] | None = None,
) -> FastAPI:
    """``open_gate`` builds Gate and its callers at startup and releases them at shutdown.
    ``extend`` adds routes that use them (the policy API, ``policy.api``)."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_gate() as (gate, callers):
            app.state.gate, app.state.callers, app.state.sweeper = gate, callers, None
            if sweep_interval is not None:
                app.state.sweeper = asyncio.create_task(_sweep(gate, sweep_interval))
            try:
                yield
            finally:
                if app.state.sweeper is not None:
                    app.state.sweeper.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await app.state.sweeper

    app = FastAPI(title="Precept Ledger Gate", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_api_route("/gate/v1/decide", decide, methods=["POST"])
    app.add_api_route("/gate/v1/escalations", list_escalations, methods=["GET"])
    app.add_api_route("/gate/v1/escalations/{escalation_id}", get_escalation, methods=["GET"])
    app.add_api_route(
        "/gate/v1/escalations/{escalation_id}/verdict", give_verdict, methods=["POST"]
    )
    app.add_api_route("/gate/v1/results", record_result, methods=["POST"])
    app.add_api_route("/gate/v1/health", health, methods=["GET"])
    app.add_api_route("/gate/v1/delegations", issue_delegation, methods=["POST"])
    app.add_api_route("/gate/v1/delegations/revocations", revoke_delegation, methods=["POST"])
    app.add_api_route("/gate/v1/delegations/{credential_id}", delegation_status, methods=["GET"])

    @app.exception_handler(PreceptError)
    async def precept_error(request: Request, exc: PreceptError) -> JSONResponse:
        status = status_for(exc)
        if status >= 500:
            log_event(logger, logging.ERROR, "gate error", code=exc.code)
        message = (
            str(exc.args[0]) if status < 500 or isinstance(exc, GateError) else "internal error"
        )
        return error_response(status, exc.code, message)

    install_common_handlers(app, logger)
    if extend is not None:
        extend(app)
    return app
