"""Gate core (gate/core.py): signed decisions, write-before-effect, verdicts, expiry, results."""

import asyncio
from datetime import timedelta

import pytest

from common.errors import GateError
from gate.core import Approver, GateDecision
from gate.decide import Outcome
from gate.escalations import APPROVED, EXPIRED, OPEN, REJECTED, Escalation
from gate.records import Receipted
from tests.gate_helpers import (
    AGENT,
    CLERK,
    DEMO_PAYMENT,
    POLICIES,
    SMALL_PAYMENT,
    SUPERVISOR,
    TIMEOUT,
    Rig,
    rig,
)
from tests.helpers import signer_for

DIGEST = "sha256:" + "ab" * 32


async def test_an_allowed_call_has_a_verified_decision_record() -> None:
    r = rig()
    decision = await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
    assert (decision.outcome, decision.reason, decision.escalation) == (
        Outcome.ALLOW,
        "policy.permit",
        None,
    )
    body = decision.record.body
    assert body["resource"]["amount_cents"] == 12_000
    assert body["policy_ids"] == ["pay-known-payee-01"]
    assert body["policy_set"] == POLICIES.digest
    assert body["args_digest"].startswith("sha256:")
    assert "memo" not in body["resource"]
    stored, receipt = await r.ledger.get(decision.record.record_id)
    assert receipt == decision.record.receipt
    assert stored.submission == decision.record.submission


async def test_the_demo_payment_escalates_and_opens_an_approval_request() -> None:
    r = rig()
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.outcome is Outcome.ESCALATE
    escalation = decision.escalation
    assert escalation is not None
    assert escalation.state == OPEN
    assert escalation.decision_record_id == decision.record.record_id
    assert escalation.approver_roles == ("payments-supervisor",)
    assert escalation.expires_at - escalation.opened_at == TIMEOUT
    signed = decision.record.body["escalation"]
    assert signed["id"] == escalation.id
    assert signed["approver_roles"] == ["payments-supervisor"]
    assert await r.store.list_open() == [escalation]


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        ("delete_ledger", {}, "gate.unmapped_tool"),
        ("create_payment", {**DEMO_PAYMENT, "amount": 1.5}, "gate.bad_arguments"),
        ("create_payment", {**DEMO_PAYMENT, "amount": "300000.00"}, "policy.forbid"),
    ],
)
async def test_refused_calls_are_recorded_as_denies(
    tool: str, arguments: dict, reason: str
) -> None:
    r = rig()
    decision = await r.gate.decide(AGENT, tool, arguments)
    assert (decision.outcome, decision.reason) == (Outcome.DENY, reason)
    assert decision.record.body["decision"] == "deny"
    assert r.ledger.types() == ["gate.decision"]


async def test_no_receipt_means_no_decision() -> None:
    r = rig()
    r.ledger.down = True
    with pytest.raises(GateError) as caught:
        await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert caught.value.code == "gate.ledger_unavailable"
    assert await r.store.list_open() == []  # nothing an approver could approve


async def test_a_ledger_that_refuses_the_record_is_a_deny() -> None:
    r = rig()
    r.ledger.ledger._producers = type(r.ledger.ledger._producers)()  # trusts no producer
    with pytest.raises(GateError) as caught:
        await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
    assert caught.value.code == "gate.ledger_refused"


async def test_a_receipt_signed_by_another_key_is_rejected() -> None:
    r = rig(log_signer=signer_for("witness-1"))
    with pytest.raises(GateError) as caught:
        await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
    assert caught.value.code == "gate.bad_receipt"


@pytest.mark.parametrize(("agent", "tool"), [("Payments Ops", "create_payment"), (AGENT, "x" * 70)])
async def test_malformed_names_are_refused_before_anything_is_recorded(
    agent: str, tool: str
) -> None:
    r = rig()
    with pytest.raises(GateError) as caught:
        await r.gate.decide(agent, tool, {})
    assert caught.value.code == "gate.bad_request"
    assert r.ledger.appended == []


async def escalated(r: Rig | None = None) -> tuple[Rig, GateDecision, Escalation]:
    r = r or rig()
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    return r, decision, decision.escalation


async def test_a_supervisor_approves_and_the_verdict_is_a_ledger_record() -> None:
    r, decision, escalation = await escalated()
    verdict = await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    assert verdict.body == {
        "decision_record_id": decision.record.record_id,
        "escalation_id": escalation.id,
        "verdict": "approve",
        "approver": "j.ortiz",
        "agent": AGENT,
        "tool": "create_payment",
        "action": "value.transfer",
        "brief_record_id": None,
    }
    closed = await r.gate.status(escalation.id)
    assert (closed.state, closed.approver, closed.verdict_record_id) == (
        APPROVED,
        "j.ortiz",
        verdict.record_id,
    )
    assert await r.gate.verdict_record(closed) == verdict


async def test_an_escalation_closes_exactly_once() -> None:
    r, _, escalation = await escalated()
    await r.gate.verdict(escalation.id, SUPERVISOR, "reject")
    with pytest.raises(GateError) as caught:
        await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    assert caught.value.code == "gate.escalation_closed"
    assert (await r.gate.status(escalation.id)).state == REJECTED


async def test_two_approvers_at_once_produce_one_verdict() -> None:
    r, _, escalation = await escalated()
    other = Approver("m.lee", SUPERVISOR.roles)
    results = await asyncio.gather(
        r.gate.verdict(escalation.id, SUPERVISOR, "approve"),
        r.gate.verdict(escalation.id, other, "reject"),
        return_exceptions=True,
    )
    assert sum(isinstance(x, Receipted) for x in results) == 1
    assert r.ledger.types().count("gate.verdict") == 1


async def test_an_approver_without_the_role_cannot_approve() -> None:
    r, _, escalation = await escalated()
    with pytest.raises(GateError) as caught:
        await r.gate.verdict(escalation.id, CLERK, "approve")
    assert caught.value.code == "gate.approver_not_allowed"
    assert (await r.gate.status(escalation.id)).state == OPEN


@pytest.mark.parametrize(
    ("escalation_id", "verdict"), [("01JB6X7Q2M9ZK3T8V4N5R6S7W0", "approve"), ("../x", "approve")]
)
async def test_unknown_escalations_are_not_found(escalation_id: str, verdict: str) -> None:
    with pytest.raises(GateError) as caught:
        await rig().gate.verdict(escalation_id, SUPERVISOR, verdict)
    assert caught.value.code == "gate.escalation_not_found"


async def test_a_verdict_must_be_approve_or_reject() -> None:
    r, _, escalation = await escalated()
    with pytest.raises(GateError) as caught:
        await r.gate.verdict(escalation.id, SUPERVISOR, "expire")
    assert caught.value.code == "gate.bad_request"


async def test_a_verdict_the_ledger_cannot_record_leaves_the_escalation_open() -> None:
    r, _, escalation = await escalated()
    r.ledger.down = True
    with pytest.raises(GateError):
        await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    r.ledger.down = False
    assert (await r.gate.status(escalation.id)).state == OPEN
    await r.gate.verdict(escalation.id, SUPERVISOR, "approve")  # the approver can retry


async def test_no_verdict_before_the_timeout_is_a_recorded_deny() -> None:
    r, _, escalation = await escalated()
    r.clock.current = escalation.expires_at
    with pytest.raises(GateError) as caught:
        await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    assert caught.value.code == "gate.escalation_closed"
    closed = await r.gate.status(escalation.id)
    assert closed.state == EXPIRED
    record = await r.gate.verdict_record(closed)
    assert record is not None
    assert (record.body["verdict"], record.body["approver"]) == ("expire", None)


async def test_expiry_holds_even_when_it_cannot_be_recorded() -> None:
    r, _, escalation = await escalated()
    r.clock.current = escalation.expires_at + timedelta(seconds=1)
    r.ledger.down = True
    assert await r.gate.expire_due() == 1
    closed = await r.store.get(escalation.id)
    assert closed is not None
    assert (closed.state, closed.verdict_record_id) == (EXPIRED, None)
    assert await r.gate.verdict_record(closed) is None


async def test_a_waiter_wakes_when_the_verdict_lands() -> None:
    r, _, escalation = await escalated()
    waiter = asyncio.create_task(r.gate.wait(escalation.id, 5))
    await asyncio.sleep(0)
    await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    assert (await asyncio.wait_for(waiter, 1)).state == APPROVED


async def test_a_wait_returns_the_open_escalation_when_it_times_out() -> None:
    r, _, escalation = await escalated()
    assert (await r.gate.wait(escalation.id, 0.01)).state == OPEN
    assert (await r.gate.wait(escalation.id, 0)).state == OPEN


async def test_results_are_recorded_for_allowed_and_approved_calls() -> None:
    r = rig()
    allowed = await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
    result = await r.gate.record_result(
        AGENT, allowed.record.record_id, ok=True, digest=DIGEST, error=None
    )
    assert result.body["verdict_record_id"] is None
    assert (result.body["outcome"], result.body["result_digest"]) == ("ok", DIGEST)

    _, decision, escalation = await escalated(r)
    verdict = await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    result = await r.gate.record_result(
        AGENT, decision.record.record_id, ok=False, digest=None, error="TimeoutError"
    )
    assert result.body["verdict_record_id"] == verdict.record_id
    assert (result.body["outcome"], result.body["error"]) == ("error", "TimeoutError")


async def test_a_result_needs_a_decision_that_let_the_tool_run() -> None:
    r = rig()
    denied = await r.gate.decide(AGENT, "delete_ledger", {})
    _, pending, _ = await escalated(r)
    for decision in (denied, pending):
        with pytest.raises(GateError) as caught:
            await r.gate.record_result(
                AGENT, decision.record.record_id, ok=True, digest=DIGEST, error=None
            )
        assert caught.value.code == "gate.not_cleared"
    with pytest.raises(GateError) as caught:
        await r.gate.record_result(
            "other-agent", pending.record.record_id, ok=True, digest=None, error=None
        )
    assert caught.value.code == "gate.forbidden"


@pytest.mark.parametrize(
    ("decision_id", "digest", "error"),
    [("not-a-ulid", None, None), ("01JB6X7Q2M9ZK3T8V4N5R6S7W0", "md5:00", None),
     ("01JB6X7Q2M9ZK3T8V4N5R6S7W0", None, "has spaces")],
)  # fmt: skip
async def test_malformed_results_are_refused(
    decision_id: str, digest: str | None, error: str | None
) -> None:
    with pytest.raises(GateError) as caught:
        await rig().gate.record_result(AGENT, decision_id, ok=True, digest=digest, error=error)
    assert caught.value.code == "gate.bad_request"


async def test_a_result_for_a_record_gate_did_not_write_is_refused() -> None:
    r = rig()
    with pytest.raises(GateError) as caught:
        await r.gate.record_result(
            AGENT, "01JB6X7Q2M9ZK3T8V4N5R6S7W0", ok=True, digest=None, error=None
        )
    assert caught.value.code == "gate.record_not_found"
