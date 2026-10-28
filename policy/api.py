"""Policy HTTP API (``/policy/v1``), served by ``pl-gate`` next to the Gate API (D73).

Every route needs an approver token with the ``compliance-officer`` role. Responses carry the
signed record and its receipt, as Gate's do. ``inject_fault`` is accepted only when the server
runs with ``DEMO_MODE=true``.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.errors import PolicyError
from common.http import read_limited
from gate.auth import APPROVER, Callers
from gate.core import Gate
from policy.draft import Drafter
from policy.pdf import MAX_PDF_BYTES
from policy.rules import MAX_YAML_BYTES
from policy.service import DRAFT, Officer, PolicyService


def _service(request: Request) -> PolicyService:
    gate: Gate = request.app.state.gate
    return PolicyService(gate, request.app.state.policy_drafter)


def _officer(request: Request) -> Officer:
    callers: Callers = request.app.state.callers
    caller = callers.authenticate(request.headers.get("authorization"), kind=APPROVER)
    return Officer(caller.name, caller.roles)


async def submit_draft(request: Request, name: str, inject_fault: bool = False) -> JSONResponse:
    officer = _officer(request)
    if inject_fault and not request.app.state.policy_demo_mode:
        raise PolicyError("inject_fault needs DEMO_MODE=true", code="policy.forbidden")
    pdf = await read_limited(request, MAX_PDF_BYTES)
    record = await _service(request).submit(pdf, name, officer, inject_fault=inject_fault)
    return JSONResponse(record.to_json(), status_code=201)


async def get_draft(draft_id: str, request: Request) -> JSONResponse:
    _officer(request)
    gate: Gate = request.app.state.gate
    return JSONResponse((await gate.read_record(draft_id, DRAFT)).to_json())


async def revise_draft(draft_id: str, request: Request) -> JSONResponse:
    officer = _officer(request)
    raw = await read_limited(request, MAX_YAML_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError("rules must be UTF-8 YAML", code="policy.yaml") from exc
    record = await _service(request).revise(draft_id, text, officer)
    return JSONResponse(record.to_json(), status_code=201)


async def approve_draft(draft_id: str, request: Request) -> JSONResponse:
    record = await _service(request).approve(draft_id, _officer(request))
    return JSONResponse(record.to_json(), status_code=201)


async def active_policy(request: Request) -> JSONResponse:
    _officer(request)
    gate: Gate = request.app.state.gate
    return JSONResponse({
        "policy_set": gate.policies.digest,
        "rules": [rule.id for rule in gate.policies.rules],
        "policy_record": gate.policy_record_id,
    })  # fmt: skip


def install_policy_routes(app: FastAPI, drafter: Drafter | None, *, demo_mode: bool) -> None:
    app.state.policy_drafter = drafter
    app.state.policy_demo_mode = demo_mode
    app.add_api_route("/policy/v1/drafts", submit_draft, methods=["POST"])
    app.add_api_route("/policy/v1/drafts/{draft_id}", get_draft, methods=["GET"])
    app.add_api_route("/policy/v1/drafts/{draft_id}/revisions", revise_draft, methods=["POST"])
    app.add_api_route("/policy/v1/drafts/{draft_id}/approve", approve_draft, methods=["POST"])
    app.add_api_route("/policy/v1/active", active_policy, methods=["GET"])
