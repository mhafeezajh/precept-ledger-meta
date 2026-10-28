"""Approval briefs (assist/brief.py) and Gate's brief hook. Model calls are faked here; the one
real call is in test_a_real_brief_from_muse_spark, marked live."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from assist.brief import (
    Brief,
    BriefPoint,
    BriefWriter,
    build_context,
    check_brief,
    dollars,
    load_prompt,
)
from assist.llm import LlmClient, ModelConfig, Route
from common.errors import ModelOutputError
from gate.core import BRIEF
from gate.escalations import Escalation
from gate.records import Receipted
from tests.gate_helpers import AGENT, DEMO_PAYMENT, SMALL_PAYMENT, SUPERVISOR, Rig, rig
from tests.unit.test_assist_llm import FakeCompletion, no_sleep


async def seeded() -> tuple[Rig, Receipted, Escalation]:
    """Three small payments, one approved $9,000 payment to acme-paper, then the demo payment."""
    r = rig()
    for amount in ("120.00", "480.00", "2500.00"):
        await r.gate.decide(AGENT, "create_payment", {**SMALL_PAYMENT, "amount": amount})
    big = await r.gate.decide(AGENT, "create_payment", {**SMALL_PAYMENT, "amount": "9000.00"})
    assert big.escalation is not None
    await r.gate.verdict(big.escalation.id, SUPERVISOR, "approve")
    demo = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert demo.escalation is not None
    return r, demo.record, demo.escalation


async def context_for(r: Rig, decision: Receipted, escalation: Escalation) -> Any:
    return build_context(
        escalation, decision, await r.ledger.store.read_raw(decision.submission.stream)
    )


async def test_the_context_is_computed_by_code_from_the_ledger() -> None:
    r, decision, escalation = await seeded()
    context = await context_for(r, decision, escalation)
    data = context.data
    assert data["facts"]["amount"] == "$48,000.00"
    assert "amount_cents" not in data["facts"]
    assert data["decision"]["policies_matched"] == ["pay-new-payee-01", "pay-over-5k-01"]
    assert data["after_business_hours"] is True  # 17:14 New York
    history = data["agent_payments_last_30_days"]
    assert (history["count"], history["median_amount"], history["largest_amount"]) == (
        4, "$1,490.00", "$9,000.00"
    )  # fmt: skip
    assert data["payee_history"] == {
        "earlier_decisions": 0,
        "earlier_approvals": 0,
        "record_ids": [],
    }
    assert decision.record_id in context.record_ids
    assert history["largest_record_id"] in context.record_ids


async def test_a_known_payee_shows_its_approvals() -> None:
    r, _, _ = await seeded()
    again = await r.gate.decide(AGENT, "create_payment", {**SMALL_PAYMENT, "amount": "7000.00"})
    assert again.escalation is not None
    context = await context_for(r, again.record, again.escalation)
    payee = context.data["payee_history"]
    assert (payee["earlier_decisions"], payee["earlier_approvals"]) == (4, 1)
    assert len(payee["record_ids"]) == 5


def good_brief(context: Any) -> Brief:
    decision_id = context.data["decision"]["record_id"]
    largest_id = context.data["agent_payments_last_30_days"]["largest_record_id"]
    return Brief(
        headline="The agent wants to pay $48,000.00 to northwind-supply, a new payee.",
        unusual=[
            BriefPoint(
                text="northwind-supply has no earlier payments or approvals.",
                record_ids=[decision_id],
            ),
            BriefPoint(
                text="The amount is far above the agent's largest payment in 30 days, $9,000.00.",
                record_ids=[decision_id, largest_id],
            ),
        ],
        approver_checks=["Confirm the vendor's bank details by phone using a number on file."],
    )


async def test_a_brief_grounded_in_the_context_passes() -> None:
    r, decision, escalation = await seeded()
    context = await context_for(r, decision, escalation)
    assert check_brief(good_brief(context), context) == []


UNKNOWN_ID = "01JB6X7Q2M9ZK3T8V4N5R6S7W0"
DECISION_ID = "<decision id>"  # replaced by the real one in the test
BAD_BRIEFS = [
    ("unusual", [BriefPoint(text="New payee.", record_ids=[UNKNOWN_ID])], "not in the context"),
    ("unusual", [BriefPoint(text="New payee.", record_ids=[])], "cites no record"),
    ("headline", "Pays $50,000.00 to a new payee.", "numbers not in the context"),
    ("headline", "That is 400% of the usual payment.", "numbers not in the context"),
    ("headline", f"See {DECISION_ID}.", "belong in record_ids"),
    ("approver_checks", ["We recommend approval."], "recommend a verdict"),
    ("approver_checks", ["This should be rejected."], "recommend a verdict"),
]


@pytest.mark.parametrize(("field", "value", "problem"), BAD_BRIEFS)
async def test_briefs_that_invent_or_decide_are_rejected(
    field: str, value: Any, problem: str
) -> None:
    r, decision, escalation = await seeded()
    context = await context_for(r, decision, escalation)
    if isinstance(value, str):
        value = value.replace(DECISION_ID, decision.record_id)
    problems = check_brief(good_brief(context).model_copy(update={field: value}), context)
    assert any(problem in p for p in problems), problems


def llm(tmp_path: Path, fake: FakeCompletion) -> LlmClient:
    config = ModelConfig(
        profile="cloud",
        routes={"cloud": {"spark": Route("meta/muse-spark-1.3", None, "test-key")}},
        timeout_seconds=5,
        call_log=tmp_path / "model_calls.csv",
    )
    return LlmClient(config, completion=fake, sleep=no_sleep)


async def test_the_writer_sends_only_the_context_and_returns_a_record_body(tmp_path: Path) -> None:
    r, decision, escalation = await seeded()
    context = await context_for(r, decision, escalation)
    fake = FakeCompletion(good_brief(context).model_dump_json())
    body = await BriefWriter(llm(tmp_path, fake), r.ledger.store).write(escalation, decision)
    assert (body["escalation_id"], body["decision_record_id"]) == (
        escalation.id,
        decision.record_id,
    )
    assert (body["model"], body["prompt_version"]) == ("meta/muse-spark-1.3", "approval_brief/1")
    assert set(body["context_record_ids"]) == set(context.record_ids)
    sent = fake.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "Do not recommend approving or rejecting" in sent[0]["content"]
    assert json.loads(sent[1]["content"].removeprefix("Context:\n")) == context.data
    assert "4800000" not in sent[1]["content"]


async def test_an_invented_brief_gets_one_retry_then_no_brief(tmp_path: Path) -> None:
    r, decision, escalation = await seeded()
    context = await context_for(r, decision, escalation)
    invented = (
        good_brief(context).model_copy(update={"headline": "Pays $50,000.00."}).model_dump_json()
    )
    fake = FakeCompletion(invented, good_brief(context).model_dump_json())
    await BriefWriter(llm(tmp_path, fake), r.ledger.store).write(escalation, decision)
    assert "numbers not in the context" in fake.calls[1]["messages"][-1]["content"]
    with pytest.raises(ModelOutputError):
        await BriefWriter(llm(tmp_path, FakeCompletion(invented, invented)), r.ledger.store).write(
            escalation, decision
        )


class StaticWriter:
    def __init__(self, body: dict[str, Any] | Exception | None = None) -> None:
        self.body = body

    async def write(self, escalation: Escalation, decision: Receipted) -> dict[str, Any]:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body or {
            "escalation_id": escalation.id,
            "decision_record_id": decision.record_id,
            "headline": "Held: new payee.",
        }


async def test_gate_records_the_brief_and_the_verdict_names_it() -> None:
    r = rig()
    r.gate._briefs = StaticWriter()  # type: ignore[assignment]
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    await r.gate.drain_briefs()
    escalation = await r.gate.status(decision.escalation.id)
    brief = await r.gate.brief_record(escalation)
    assert brief is not None
    assert brief.submission.type == BRIEF
    assert brief.body["headline"] == "Held: new payee."
    verdict = await r.gate.verdict(escalation.id, SUPERVISOR, "approve")
    assert verdict.body["brief_record_id"] == brief.record_id
    assert r.ledger.types() == ["gate.decision", BRIEF, "gate.verdict"]


@pytest.mark.parametrize(
    "writer",
    [
        StaticWriter(ModelOutputError("down", code="model.unavailable")),
        StaticWriter({"escalation_id": "other"}),
    ],
)
async def test_a_failed_brief_changes_nothing_but_the_brief(writer: StaticWriter) -> None:
    r = rig()
    r.gate._briefs = writer  # type: ignore[assignment]
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    await r.gate.drain_briefs()
    escalation = await r.gate.status(decision.escalation.id)
    assert (escalation.state, escalation.brief_record_id) == ("open", None)
    assert await r.gate.brief_record(escalation) is None
    assert r.ledger.types() == ["gate.decision"]
    await r.gate.verdict(escalation.id, SUPERVISOR, "approve")  # the approver decides without one


async def test_a_brief_is_attached_once(tmp_path: Path) -> None:
    r = rig()
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    assert await r.store.attach_brief(decision.escalation.id, "01JB6X7Q2M9ZK3T8V4N5R6S7W0")
    assert not await r.store.attach_brief(decision.escalation.id, "01JB6X7Q2M9ZK3T8V4N5R6S7W1")
    assert not await r.store.attach_brief(
        "01JB6X7Q2M9ZK3T8V4N5R6S7W2", "01JB6X7Q2M9ZK3T8V4N5R6S7W0"
    )


def test_the_prompt_file_has_a_versioned_header() -> None:
    version, instructions = load_prompt()
    assert version == "approval_brief/1"
    assert instructions.startswith("You write approval briefs")


def test_dollars_formats_cents_exactly() -> None:
    assert [dollars(c) for c in (1, 100, 4_800_000, 123_456_789)] == [
        "$0.01", "$1.00", "$48,000.00", "$1,234,567.89"
    ]  # fmt: skip


@pytest.mark.live
async def test_a_real_brief_from_muse_spark(tmp_path: Path) -> None:
    """Calls the Meta Model API (Standard mode). Needs META_API_KEY; run with -m live."""
    key = os.environ.get("META_API_KEY")
    if not key:
        pytest.skip("META_API_KEY is not set")
    config = ModelConfig(
        profile="cloud",
        routes={"cloud": {"spark": Route("meta/muse-spark-1.3", "https://api.meta.ai/v1", key)}},
        timeout_seconds=60,
        call_log=tmp_path / "model_calls.csv",
    )
    r, decision, escalation = await seeded()
    body = await BriefWriter(LlmClient(config), r.ledger.store).write(escalation, decision)
    print(json.dumps(body, indent=2))
    assert body["unusual"]


async def test_approvers_see_the_brief_in_the_queue_and_on_the_escalation() -> None:
    from tests.unit.test_gate_api import AGENT_TOKEN, SUPERVISOR_TOKEN, stack

    async with stack() as s:
        s.rig.gate._briefs = StaticWriter()  # type: ignore[assignment]
        decided = await s.call(
            "POST", "/gate/v1/decide", AGENT_TOKEN,
            json={"tool": "create_payment", "arguments": DEMO_PAYMENT},
        )  # fmt: skip
        await s.rig.gate.drain_briefs()
        queue = (await s.call("GET", "/gate/v1/escalations", SUPERVISOR_TOKEN)).json()
        assert queue["escalations"][0]["brief"]["headline"] == "Held: new payee."
        one = await s.call(
            "GET", f"/gate/v1/escalations/{decided.json()['escalation']['id']}", AGENT_TOKEN
        )
        assert one.json()["brief"]["record"]["type"] == BRIEF
