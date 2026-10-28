"""Gate HTTP API and client: auth, every route, and demo beat 2 end to end over HTTP."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from common.errors import ConfigError, GateError
from gate.api import create_gate_app
from gate.auth import Callers, token_hash
from gate.client import HttpGateClient
from gate.core import Gate
from gate.governed import GateDenied
from tests.gate_helpers import DEMO_PAYMENT, SMALL_PAYMENT, Rig, governor, rig

# TEST ONLY tokens.
AGENT_TOKEN = "test-only-agent-token-payments-ops"
OTHER_AGENT_TOKEN = "test-only-agent-token-marketing"
SUPERVISOR_TOKEN = "test-only-approver-token-j-ortiz"
CLERK_TOKEN = "test-only-approver-token-a-chen"
CALLERS_TEXT = f"""# kind name roles sha256(token)
agent payments-ops - {token_hash(AGENT_TOKEN)}
agent marketing-bot - {token_hash(OTHER_AGENT_TOKEN)}
approver j.ortiz payments-supervisor {token_hash(SUPERVISOR_TOKEN)}
approver a.chen payments-clerk {token_hash(CLERK_TOKEN)}
"""


@dataclass
class Stack:
    rig: Rig
    http: httpx.AsyncClient

    async def call(
        self, method: str, path: str, token: str | None, **kwargs: Any
    ) -> httpx.Response:
        headers = {} if token is None else {"authorization": f"Bearer {token}"}
        return await self.http.request(method, path, headers=headers, **kwargs)

    def client(self, token: str = AGENT_TOKEN) -> HttpGateClient:
        return HttpGateClient("http://gate", token, self.http)


@asynccontextmanager
async def stack(sweep_interval: float | None = None) -> AsyncIterator[Stack]:
    r = rig()

    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield r.gate, Callers.from_text(CALLERS_TEXT)

    app = create_gate_app(open_gate, sweep_interval=sweep_interval)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gate") as http,
    ):
        yield Stack(r, http)


def code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def test_beat_2_the_payment_runs_only_after_approval_over_http() -> None:
    async with stack() as s:
        paid: list[dict[str, Any]] = []

        async def create_payment(**arguments: Any) -> dict[str, str]:
            paid.append(arguments)
            return {"payment_id": "pay-0001"}

        tool = governor(s.client(), s.rig.clock).tool("create_payment")(create_payment)
        call = asyncio.create_task(tool(**DEMO_PAYMENT))
        queue: list[dict[str, Any]] = []
        while not queue:
            queue = (await s.call("GET", "/gate/v1/escalations", SUPERVISOR_TOKEN)).json()[
                "escalations"
            ]
            await asyncio.sleep(0.01)
        assert paid == []
        (escalation,) = queue
        assert escalation["approver_roles"] == ["payments-supervisor"]
        verdict = await s.call(
            "POST", f"/gate/v1/escalations/{escalation['id']}/verdict", SUPERVISOR_TOKEN,
            json={"verdict": "approve"},
        )  # fmt: skip
        assert verdict.status_code == 201
        assert await asyncio.wait_for(call, 2) == {"payment_id": "pay-0001"}
        assert paid == [DEMO_PAYMENT]
        assert s.rig.ledger.types() == ["gate.decision", "gate.verdict", "gate.result"]


async def test_decide_returns_the_signed_record_and_receipt() -> None:
    async with stack() as s:
        response = await s.call(
            "POST",
            "/gate/v1/decide",
            AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": SMALL_PAYMENT},
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["decision"], body["reason"], body["escalation"]) == (
            "allow",
            "policy.permit",
            None,
        )
        assert body["record"]["body"]["agent"] == "payments-ops"  # from the token, not the body
        assert body["receipt"]["record_id"] == body["record"]["record_id"]


@pytest.mark.parametrize(
    ("method", "path", "token", "status", "error"),
    [
        ("POST", "/gate/v1/decide", None, 401, "gate.unauthorized"),
        ("POST", "/gate/v1/decide", "not-a-known-token", 401, "gate.unauthorized"),
        ("POST", "/gate/v1/decide", SUPERVISOR_TOKEN, 403, "gate.forbidden"),
        ("GET", "/gate/v1/escalations", AGENT_TOKEN, 403, "gate.forbidden"),
        ("POST", "/gate/v1/results", SUPERVISOR_TOKEN, 403, "gate.forbidden"),
        (
            "POST",
            "/gate/v1/escalations/01JB6X7Q2M9ZK3T8V4N5R6S7W0/verdict",
            AGENT_TOKEN,
            403,
            "gate.forbidden",
        ),
    ],
)
async def test_each_route_admits_only_its_callers(
    method: str, path: str, token: str | None, status: int, error: str
) -> None:
    async with stack() as s:
        response = await s.call(method, path, token, json={})
    assert (response.status_code, code(response)) == (status, error)


async def test_an_agent_token_can_never_approve_its_own_escalation() -> None:
    async with stack() as s:
        decided = await s.call(
            "POST",
            "/gate/v1/decide",
            AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": DEMO_PAYMENT},
        )
        escalation_id = decided.json()["escalation"]["id"]
        path = f"/gate/v1/escalations/{escalation_id}/verdict"
        assert (
            await s.call("POST", path, AGENT_TOKEN, json={"verdict": "approve"})
        ).status_code == 403
        clerk = await s.call("POST", path, CLERK_TOKEN, json={"verdict": "approve"})
        assert (clerk.status_code, code(clerk)) == (403, "gate.approver_not_allowed")
        assert (
            await s.call("POST", path, SUPERVISOR_TOKEN, json={"verdict": "reject"})
        ).status_code == 201
        again = await s.call("POST", path, SUPERVISOR_TOKEN, json={"verdict": "approve"})
        assert (again.status_code, code(again)) == (409, "gate.escalation_closed")


async def test_agents_see_only_their_own_escalations() -> None:
    async with stack() as s:
        decided = await s.call(
            "POST",
            "/gate/v1/decide",
            AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": DEMO_PAYMENT},
        )
        path = f"/gate/v1/escalations/{decided.json()['escalation']['id']}"
        own = await s.call("GET", path, AGENT_TOKEN)
        assert (own.status_code, own.json()["escalation"]["state"], own.json()["verdict"]) == (
            200,
            "open",
            None,
        )
        other = await s.call("GET", path, OTHER_AGENT_TOKEN)
        assert (other.status_code, code(other)) == (404, "gate.escalation_not_found")
        assert (await s.call("GET", path + "?wait=99", AGENT_TOKEN)).status_code == 422


@pytest.mark.parametrize(
    ("content", "status", "error"),
    [
        (b'{"tool": "create_payment", "arguments": {"amount": 1.5}}', 400, "format.float"),
        (b'{"tool": "create_payment"}', 400, "gate.bad_request"),
        (b'{"tool": 5, "arguments": {}}', 400, "gate.bad_request"),
        (b'{"tool": "a", "tool": "b", "arguments": {}}', 400, "format.duplicate_key"),
        (b'{"tool": "x", "arguments": {"p": "' + b"y" * 20_000 + b'"}}', 413, "format.too_large"),
    ],
)
async def test_malformed_decide_bodies_are_refused(content: bytes, status: int, error: str) -> None:
    async with stack() as s:
        response = await s.call("POST", "/gate/v1/decide", AGENT_TOKEN, content=content)
    assert response.status_code == status
    assert code(response).startswith(error.split(".")[0])
    assert "1.5" not in response.text


async def test_results_need_a_cleared_decision_and_a_valid_body() -> None:
    async with stack() as s:
        denied = await s.call(
            "POST", "/gate/v1/decide", AGENT_TOKEN, json={"tool": "delete_ledger", "arguments": {}}
        )
        record_id = denied.json()["record"]["record_id"]
        result = await s.call(
            "POST",
            "/gate/v1/results",
            AGENT_TOKEN,
            json={"decision_record_id": record_id, "outcome": "ok"},
        )
        assert (result.status_code, code(result)) == (409, "gate.not_cleared")
        for body in (
            {"decision_record_id": record_id, "outcome": "maybe"},
            {"decision_record_id": 5, "outcome": "ok"},
        ):
            response = await s.call("POST", "/gate/v1/results", AGENT_TOKEN, json=body)
            assert (response.status_code, code(response)) == (400, "gate.bad_request")


async def test_ledger_down_is_a_503_and_the_tool_never_runs() -> None:
    async with stack() as s:
        s.rig.ledger.down = True
        response = await s.call(
            "POST",
            "/gate/v1/decide",
            AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": SMALL_PAYMENT},
        )
        assert (response.status_code, code(response)) == (503, "gate.ledger_unavailable")
        ran: list[int] = []

        async def create_payment(**arguments: Any) -> None:
            ran.append(1)

        tool = governor(s.client(), s.rig.clock).tool("create_payment")(create_payment)
        with pytest.raises(GateDenied) as caught:
            await tool(**SMALL_PAYMENT)
        assert caught.value.code == "gate.ledger_unavailable"
        assert ran == []


async def test_the_sweeper_expires_overdue_escalations_and_health_reports() -> None:
    async with stack(sweep_interval=0.01) as s:
        decided = await s.call(
            "POST",
            "/gate/v1/decide",
            AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": DEMO_PAYMENT},
        )
        health = (await s.call("GET", "/gate/v1/health", None)).json()
        assert (health["status"], health["open_escalations"], health["rules"]) == ("ok", 1, 8)
        s.rig.clock.current = s.rig.clock.current.replace(year=2027)
        for _ in range(200):
            if not await s.rig.store.list_open():
                break
            await asyncio.sleep(0.01)
        state = await s.call(
            "GET", f"/gate/v1/escalations/{decided.json()['escalation']['id']}", AGENT_TOKEN
        )
        assert state.json()["escalation"]["state"] == "expired"
        assert state.json()["verdict"]["record"]["body"]["verdict"] == "expire"


def broken(status: int = 200, content: bytes = b"{}") -> HttpGateClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    return HttpGateClient("http://gate", AGENT_TOKEN, httpx.AsyncClient(transport=transport))


def unreachable() -> HttpGateClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    return HttpGateClient(
        "http://gate", AGENT_TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(fail))
    )


@pytest.mark.parametrize(
    ("client", "error"),
    [
        (unreachable(), "gate.unreachable"),
        (broken(content=b"[]"), "gate.bad_response"),
        (broken(content=b"not json"), "gate.bad_response"),
        (broken(content=b"{" + b" " * 300_000 + b"}"), "gate.bad_response"),
        (broken(500, b'{"error": {"code": "gate.ledger_unavailable"}}'), "gate.ledger_unavailable"),
        (broken(500, b'{"error": {"code": "<script>"}}'), "gate.http_500"),
        (broken(content=b'{"record": {}, "receipt": {}}'), "gate.bad_record"),
    ],
)
async def test_the_client_turns_every_failure_into_a_gate_error(
    client: HttpGateClient, error: str
) -> None:
    with pytest.raises(GateError) as caught:
        await client.decide("create_payment", SMALL_PAYMENT)
    assert caught.value.code == error


@pytest.mark.parametrize(
    ("client", "escalation_id"),
    [
        (broken(), "../../decide"),
        (broken(content=b'{"escalation": {}}'), "01JB6X7Q2M9ZK3T8V4N5R6S7W0"),
        (
            broken(content=b'{"escalation": {"state": "done"}, "verdict": null}'),
            "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
        ),
    ],
)
async def test_the_client_refuses_odd_escalation_answers(
    client: HttpGateClient, escalation_id: str
) -> None:
    with pytest.raises(GateError) as caught:
        await client.wait(escalation_id, 1)
    assert caught.value.code == "gate.bad_response"


def test_the_client_never_shows_its_token() -> None:
    assert AGENT_TOKEN not in repr(broken())


@pytest.mark.parametrize(
    "text",
    [
        "agent payments-ops -",
        f"robot x - {token_hash('a')}",
        f"agent payments-ops payments-supervisor {token_hash('a')}",
        f"approver j.ortiz Bad_Role {token_hash('a')}",
        "approver j.ortiz r NOTHEX",
        f"agent a - {token_hash('a')}\nagent a - {token_hash('b')}",
        f"agent a - {token_hash('a')}\nagent b - {token_hash('a')}",
    ],
)
def test_a_bad_callers_file_is_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        Callers.from_text(text)


def test_bearer_parsing() -> None:
    callers = Callers.from_text(CALLERS_TEXT)
    assert len(callers) == 4
    assert callers.authenticate(f"bearer {AGENT_TOKEN}", kind=None).name == "payments-ops"
    with pytest.raises(GateError):
        callers.authenticate(f"Basic {AGENT_TOKEN}", kind=None)
