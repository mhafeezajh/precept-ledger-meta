"""@governed (gate/governed.py): the tool runs only after verified records say it may (R3)."""

import asyncio
import logging
from dataclasses import replace
from typing import Any

import pytest

from common.errors import GateError
from gate.governed import EscalationStatus, GateDenied
from gate.records import Receipted, result_digest
from ledger.signing import sign_submission
from tests.gate_helpers import (
    DEMO_PAYMENT,
    SMALL_PAYMENT,
    SUPERVISOR,
    LocalEndpoint,
    Rig,
    governor,
    rig,
)
from tests.helpers import signer_for

PAID = {"payment_id": "pay-0001", "status": "sent"}


class Tool:
    """A payment tool that counts how often it really ran."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(self, **arguments: Any) -> dict[str, str]:
        self.calls.append(arguments)
        if self.fail:
            raise TimeoutError("bank rail timed out")
        return PAID


def setup(endpoint: Any = None) -> tuple[Rig, Tool, Any]:
    r = rig()
    tool = Tool()
    wrapped = governor(endpoint or LocalEndpoint(r.gate), r.clock).tool("create_payment")(tool)
    return r, tool, wrapped


async def test_an_allowed_call_runs_once_and_its_result_is_recorded() -> None:
    r, tool, create_payment = setup()
    assert await create_payment(**SMALL_PAYMENT) == PAID
    assert tool.calls == [SMALL_PAYMENT]
    assert r.ledger.types() == ["gate.decision", "gate.result"]
    result = r.ledger.appended[-1].body
    assert (result["outcome"], result["result_digest"]) == ("ok", result_digest(PAID))
    assert result["decision_record_id"] == r.ledger.appended[0].record_id


async def test_a_denied_call_never_runs() -> None:
    r, tool, create_payment = setup()
    with pytest.raises(GateDenied) as caught:
        await create_payment(**{**DEMO_PAYMENT, "amount": "300000.00"})
    assert caught.value.code == "policy.forbid"
    assert caught.value.decision is not None
    assert tool.calls == []
    assert r.ledger.types() == ["gate.decision"]


async def test_ledger_down_means_the_tool_never_runs() -> None:
    # Skill §1 R3 check: "ledger down -> tool never called".
    r, tool, create_payment = setup()
    r.ledger.down = True
    with pytest.raises(GateDenied) as caught:
        await create_payment(**SMALL_PAYMENT)
    assert caught.value.code == "gate.ledger_unavailable"
    assert tool.calls == []


class Unreachable:
    async def decide(self, tool: str, arguments: dict[str, Any]) -> Receipted:
        raise ConnectionError("gate is down")


async def test_gate_unreachable_means_the_tool_never_runs() -> None:
    _, tool, create_payment = setup(Unreachable())
    with pytest.raises(GateDenied) as caught:
        await create_payment(**SMALL_PAYMENT)
    assert caught.value.code == "gate.unreachable"
    assert tool.calls == []


async def approve_when_open(r: Rig, verdict: str = "approve") -> None:
    while not await r.store.list_open():
        await asyncio.sleep(0)
    (escalation,) = await r.store.list_open()
    await r.gate.verdict(escalation.id, SUPERVISOR, verdict)


async def test_the_demo_payment_runs_only_after_a_supervisor_approves() -> None:
    r, tool, create_payment = setup()
    call = asyncio.create_task(create_payment(**DEMO_PAYMENT))
    await asyncio.sleep(0.01)
    assert tool.calls == []  # held at escalate, before the tool
    await approve_when_open(r)
    assert await asyncio.wait_for(call, 1) == PAID
    assert tool.calls == [DEMO_PAYMENT]
    assert r.ledger.types() == ["gate.decision", "gate.verdict", "gate.result"]
    decision, verdict, result = r.ledger.appended
    assert result.body["verdict_record_id"] == verdict.record_id
    assert verdict.body["decision_record_id"] == decision.record_id


async def test_a_rejected_escalation_never_runs() -> None:
    r, tool, create_payment = setup()
    call = asyncio.create_task(create_payment(**DEMO_PAYMENT))
    await approve_when_open(r, "reject")
    with pytest.raises(GateDenied) as caught:
        await asyncio.wait_for(call, 1)
    assert caught.value.code == "gate.rejected"
    assert tool.calls == []


async def test_an_expired_escalation_never_runs() -> None:
    r, tool, create_payment = setup()
    call = asyncio.create_task(create_payment(**DEMO_PAYMENT))
    while not await r.store.list_open():
        await asyncio.sleep(0)
    (escalation,) = await r.store.list_open()
    r.clock.current = escalation.expires_at
    await r.gate.expire_due()
    with pytest.raises(GateDenied) as caught:
        await asyncio.wait_for(call, 1)
    assert caught.value.code == "gate.expired"
    assert tool.calls == []


class Tampering(LocalEndpoint):
    """A Gate (or a network in between) that returns records it should not."""

    def __init__(self, r: Rig, mode: str) -> None:
        super().__init__(r.gate)
        self.mode = mode

    async def decide(self, tool: str, arguments: dict[str, Any]) -> Receipted:
        if self.mode == "other-arguments":
            return await super().decide(tool, {**arguments, "amount": "1.00"})
        decision = await super().decide(tool, arguments)
        if self.mode == "forged-allow":
            body = {**decision.body, "decision": "allow"}
            forged = sign_submission(
                signer_for("witness-1"), producer_id="gate-1", stream=decision.submission.stream,
                record_id=decision.record_id, ts=decision.submission.ts,
                record_type="gate.decision", body=body,
            )  # fmt: skip
            return replace(decision, submission=forged)
        return decision

    async def wait(self, escalation_id: str, timeout_seconds: float) -> EscalationStatus:
        status = await super().wait(escalation_id, timeout_seconds)
        if self.mode == "approved-without-verdict":
            return EscalationStatus("approved", None)
        if self.mode == "verdict-for-another-call" and status.verdict is not None:
            other = await self.gate.decide(self.agent, "create_payment", DEMO_PAYMENT)
            assert other.escalation is not None
            return EscalationStatus("approved", await self._approve(other.escalation.id))
        return status

    async def _approve(self, escalation_id: str) -> Receipted:
        return await self.gate.verdict(escalation_id, SUPERVISOR, "approve")


@pytest.mark.parametrize(
    ("mode", "payment", "code"),
    [
        ("forged-allow", DEMO_PAYMENT, "gate.bad_record"),
        ("other-arguments", SMALL_PAYMENT, "gate.mismatch"),
        ("approved-without-verdict", DEMO_PAYMENT, "gate.no_verdict"),
        ("verdict-for-another-call", DEMO_PAYMENT, "gate.mismatch"),
    ],
)
async def test_records_that_do_not_verify_or_match_never_run_the_tool(
    mode: str, payment: dict, code: str
) -> None:
    r = rig()
    tool = Tool()
    create_payment = governor(Tampering(r, mode), r.clock).tool("create_payment")(tool)
    call = asyncio.create_task(create_payment(**payment))
    if payment is DEMO_PAYMENT and mode != "forged-allow":
        await approve_when_open(r)
    with pytest.raises(GateDenied) as caught:
        await asyncio.wait_for(call, 1)
    assert caught.value.code == code
    assert tool.calls == []


async def test_a_failing_tool_records_an_error_result_and_raises() -> None:
    r = rig()
    tool = Tool(fail=True)
    create_payment = governor(LocalEndpoint(r.gate), r.clock).tool("create_payment")(tool)
    with pytest.raises(TimeoutError):
        await create_payment(**SMALL_PAYMENT)
    result = r.ledger.appended[-1].body
    assert (result["outcome"], result["error"], result["result_digest"]) == (
        "error",
        "TimeoutError",
        None,
    )


async def test_a_result_that_cannot_be_recorded_is_returned_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    r, tool, create_payment = setup()

    async def ledger_gone(*args: Any, **kwargs: Any) -> Receipted:
        raise GateError("ledger unreachable", code="gate.ledger_unavailable")

    r.gate.record_result = ledger_gone  # type: ignore[method-assign]
    with caplog.at_level(logging.CRITICAL, logger="pl.gate.governed"):
        assert await create_payment(**SMALL_PAYMENT) == PAID  # the effect happened; not re-run
    assert len(tool.calls) == 1
    assert "not recorded" in caplog.text


async def test_arguments_that_are_not_i_json_never_reach_gate() -> None:
    _, tool, create_payment = setup(Unreachable())
    with pytest.raises(GateDenied) as caught:
        await create_payment(**{**SMALL_PAYMENT, "amount": 1.5})
    assert caught.value.code == "gate.bad_arguments"
    assert tool.calls == []


def test_non_json_results_have_no_digest() -> None:
    assert result_digest({"ok": 0.5}) is None
    assert result_digest(PAID) == result_digest(dict(reversed(PAID.items())))
