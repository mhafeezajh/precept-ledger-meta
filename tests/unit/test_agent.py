"""The payments agent (agent/): a LangGraph loop whose every tool call goes through Gate.

The planner is the mock profile's scripted model (or a fake), Gate is the real Gate over an
in-memory ledger, and the bank is the demo bank. What is checked is what beat 2 claims: nothing
reaches the bank without a verified Gate record, a held payment waits for a human, and a
denial, a rejection, an unreachable Gate or a failed model call ends the run with nothing paid.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent.bank import AP_EMAIL, DemoBank, ToolFailed, load_vendors
from agent.graph import PaymentsAgent, StepRecord
from agent.steps import AgentStep, StepPlanner, check_step, render_task
from assist.llm import LlmClient, ModelConfig, Route, strict_json_schema
from assist.mock import MOCK_MODEL
from common.errors import ModelOutputError
from tests.gate_helpers import SUPERVISOR, LocalEndpoint, Rig, governor, rig

ROOT = Path(__file__).resolve().parents[2]
TASK = "Pay Northwind Supply $48,000.00 for invoice INV-4471."


def mock_llm(tmp_path: Path) -> LlmClient:
    route = Route(MOCK_MODEL, None, None)
    return LlmClient(ModelConfig("mock", {"mock": {"spark": route}}, 5.0, tmp_path / "calls.csv"))


def bank() -> DemoBank:
    return DemoBank(load_vendors(ROOT / "deploy" / "demo" / "vendors.json"))


def build(tmp_path: Path, r: Rig, *, planner: Any = None, max_steps: int = 8) -> PaymentsAgent:
    return PaymentsAgent(
        planner or StepPlanner(mock_llm(tmp_path)), governor(LocalEndpoint(r.gate), r.clock),
        bank(), max_steps=max_steps,
    )  # fmt: skip


async def decide_when_open(r: Rig, verdict: str = "approve", seconds: float = 2) -> None:
    for _ in range(int(seconds * 1000)):  # bounded: a run that never escalates fails, not hangs
        if await r.store.list_open():
            break
        await asyncio.sleep(0.001)
    (escalation,) = await r.store.list_open()
    await r.gate.verdict(escalation.id, SUPERVISOR, verdict)


def outcomes(steps: tuple[StepRecord, ...]) -> list[tuple[str, str, str | None]]:
    return [(s.tool, s.outcome, s.code) for s in steps]


async def test_beat_2_lookup_then_a_held_payment_then_an_internal_email(tmp_path: Path) -> None:
    r = rig()
    agent = build(tmp_path, r)
    run = asyncio.create_task(agent.run(TASK))
    await decide_when_open(r)
    result = await asyncio.wait_for(run, 2)
    assert outcomes(result.steps) == [
        ("lookup_vendor", "ok", None), ("create_payment", "ok", None), ("send_email", "ok", None)
    ]  # fmt: skip
    lookup, payment, email = result.steps
    assert (lookup.decision, payment.decision, email.decision) == ("allow", "escalate", "allow")
    assert payment.arguments == {"payee_id": "northwind-supply", "amount": "48000.00",
                                 "currency": "USD", "memo": "Invoice INV-4471"}  # fmt: skip
    assert payment.verdict_record_id is not None
    assert email.arguments["to"] == [AP_EMAIL]
    assert [p["payee_id"] for p in agent.bank.payments] == ["northwind-supply"]
    assert len(agent.bank.outbox) == 1
    assert r.ledger.types() == [
        "gate.decision", "gate.result",  # lookup
        "gate.decision", "gate.verdict", "gate.result",  # payment, held then approved
        "gate.decision", "gate.result",  # email
    ]  # fmt: skip
    assert result.stopped is None
    assert "$48,000.00" in result.reply
    assert agent.bank.payments[0]["payment_id"] in result.reply


async def test_a_rejected_payment_is_never_made_and_the_agent_says_so(tmp_path: Path) -> None:
    r = rig()
    agent = build(tmp_path, r)
    run = asyncio.create_task(agent.run(TASK))
    await decide_when_open(r, "reject")
    result = await asyncio.wait_for(run, 2)
    assert outcomes(result.steps) == [
        ("lookup_vendor", "ok", None), ("create_payment", "denied", "gate.rejected")
    ]  # fmt: skip
    assert result.steps[1].decision_record_id is not None
    assert agent.bank.payments == []
    assert agent.bank.outbox == []
    assert "not made" in result.reply
    assert "gate.rejected" in result.reply


async def test_a_payment_over_the_cap_is_denied_without_asking_anyone(tmp_path: Path) -> None:
    r = rig()
    agent = build(tmp_path, r)
    result = await asyncio.wait_for(agent.run("Pay Acme Paper $300,000 for invoice INV-9."), 2)
    assert outcomes(result.steps)[1] == ("create_payment", "denied", "policy.forbid")
    assert result.steps[1].decision == "deny"
    assert agent.bank.payments == []
    assert await r.store.list_open() == []


class Unreachable:
    async def decide(self, tool: str, arguments: dict[str, Any]) -> Any:
        raise ConnectionError("gate is down")


async def test_with_gate_unreachable_no_tool_runs(tmp_path: Path) -> None:
    r = rig()
    agent = PaymentsAgent(StepPlanner(mock_llm(tmp_path)), governor(Unreachable(), r.clock), bank())
    result = await agent.run(TASK)
    assert outcomes(result.steps) == [("lookup_vendor", "denied", "gate.unreachable")]
    assert (agent.bank.lookups, agent.bank.payments) == ([], [])
    assert "gate.unreachable" in result.reply


class Script:
    """A planner that answers from a list (or repeats the last answer)."""

    def __init__(self, *steps: dict[str, Any]) -> None:
        self.steps = [AgentStep.model_validate(step(**s)) for s in steps]
        self.seen: list[int] = []

    async def next_step(self, task: str, steps: tuple[StepRecord, ...]) -> AgentStep:
        self.seen.append(len(steps))
        return self.steps[min(len(self.seen) - 1, len(self.steps) - 1)]


def step(tool: str, args: dict[str, Any] | None = None, reply: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"note": "", "tool": tool, "lookup_vendor": None,
                           "create_payment": None, "send_email": None, "reply": reply}  # fmt: skip
    if args is not None:
        out[tool] = args
    return out


PAY_ACME = {"payee_id": "acme-paper", "amount": "120.00", "currency": "USD", "memo": ""}


async def test_the_run_stops_at_the_step_limit(tmp_path: Path) -> None:
    r = rig()
    planner = Script(*({"tool": "lookup_vendor", "args": {"query": f"q{i}"}} for i in range(9)))
    result = await build(tmp_path, r, planner=planner, max_steps=3).run("Look around.")
    assert len(result.steps) == 3
    assert result.stopped == "agent.step_limit"


async def test_the_same_call_is_never_sent_twice(tmp_path: Path) -> None:
    r = rig()
    planner = Script({"tool": "create_payment", "args": PAY_ACME},
                     {"tool": "create_payment", "args": PAY_ACME},
                     {"tool": "finish", "reply": "Done."})  # fmt: skip
    agent = build(tmp_path, r, planner=planner)
    result = await agent.run("Pay Acme Paper $120.")
    assert outcomes(result.steps) == [
        ("create_payment", "ok", None), ("create_payment", "skipped", "agent.duplicate_call")
    ]  # fmt: skip
    assert len(agent.bank.payments) == 1
    assert r.ledger.types() == ["gate.decision", "gate.result"]  # the repeat never reached Gate
    assert result.reply == "Done."


async def test_a_tool_failure_is_recorded_and_reported(tmp_path: Path) -> None:
    r = rig()
    r.payees.known = frozenset({"acme-paper", "nobody-co"})  # Gate allows it; the bank does not
    pay = {**PAY_ACME, "payee_id": "nobody-co"}
    planner = Script({"tool": "create_payment", "args": pay}, {"tool": "finish", "reply": "No."})
    agent = build(tmp_path, r, planner=planner)
    result = await agent.run("Pay Nobody $120.")
    assert outcomes(result.steps) == [("create_payment", "error", "bank.unknown_payee")]
    assert r.ledger.appended[-1].body["outcome"] == "error"
    assert agent.bank.payments == []


class Broken:
    async def next_step(self, task: str, steps: tuple[StepRecord, ...]) -> AgentStep:
        raise ModelOutputError("model output failed validation twice", code="model.bad_output")


async def test_a_failed_model_call_ends_the_run_with_nothing_done(tmp_path: Path) -> None:
    r = rig()
    agent = build(tmp_path, r, planner=Broken())
    result = await agent.run(TASK)
    assert (result.steps, result.stopped) == ((), "model.bad_output")
    assert r.ledger.appended == []


async def test_the_observer_sees_each_step_and_the_hold(tmp_path: Path) -> None:
    r = rig()
    events: list[str] = []

    class Watch:
        def started(self, n: int, step: AgentStep) -> None:
            events.append(f"start {n} {step.tool}")

        def held(self, n: int, decision_record_id: str) -> None:
            events.append(f"held {n}")

        def finished(self, record: StepRecord) -> None:
            events.append(f"done {record.n} {record.outcome}")

    agent = build(tmp_path, r)
    run = asyncio.create_task(agent.run(TASK, observer=Watch()))
    await decide_when_open(r)
    await asyncio.wait_for(run, 2)
    assert events == ["start 1 lookup_vendor", "done 1 ok", "start 2 create_payment", "held 2",
                      "done 2 ok", "start 3 send_email", "done 3 ok"]  # fmt: skip


async def test_tasks_are_bounded(tmp_path: Path) -> None:
    agent = build(tmp_path, rig())
    with pytest.raises(ValueError, match="task"):
        await agent.run("x" * 2001)
    with pytest.raises(ValueError, match="task"):
        await agent.run("   ")


def test_the_step_schema_fits_metas_strict_subset() -> None:
    strict_json_schema(AgentStep)


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (step("create_payment"), "create_payment needs its arguments"),
        (
            {
                **step("lookup_vendor", {"query": "a"}),
                "send_email": {"to": ["a@b.co"], "subject": "", "body": ""},
            },
            "only lookup_vendor's arguments",
        ),
        (step("finish"), "finish needs a reply"),
        ({**step("finish", reply="ok"), "lookup_vendor": {"query": "a"}}, "finish takes no"),
    ],
)
def test_check_step_wants_exactly_the_chosen_tools_arguments(
    data: dict[str, Any], problem: str
) -> None:
    problems = check_step(AgentStep.model_validate(data))
    assert any(problem in p for p in problems), problems


def test_a_good_step_has_no_problems() -> None:
    assert check_step(AgentStep.model_validate(step("lookup_vendor", {"query": "acme"}))) == []
    assert check_step(AgentStep.model_validate(step("finish", reply="All done."))) == []


def test_the_model_sees_the_task_and_every_step_so_far() -> None:
    record = StepRecord(
        n=1, note="find it", tool="lookup_vendor", arguments={"query": "acme"}, outcome="ok",
        result={"vendors": []}, code=None, decision="allow", decision_record_id="01X",
        verdict_record_id=None,
    )  # fmt: skip
    text = render_task(TASK, (record,))
    assert text.startswith(f"Task:\n{TASK}\n\nSteps so far:\n")
    (shown,) = json.loads(text.split("Steps so far:\n", 1)[1])
    assert shown == {"n": 1, "tool": "lookup_vendor", "arguments": {"query": "acme"},
                     "outcome": "ok", "result": {"vendors": []}, "gate": {
                         "decision": "allow", "decision_record_id": "01X"}}  # fmt: skip


async def test_the_bank_finds_vendors_and_refuses_unknown_payees() -> None:
    demo = bank()
    found = await demo.call("lookup_vendor", {"query": "northwind"})
    (vendor,) = found["vendors"]
    assert vendor["id"] == "northwind-supply"
    assert vendor["account"].startswith("****")
    assert len(vendor["account"]) == 8
    assert (await demo.call("lookup_vendor", {"query": "zzz"})) == {"vendors": []}
    with pytest.raises(ToolFailed) as caught:
        await demo.call("create_payment", {**PAY_ACME, "payee_id": "nobody-co"})
    assert caught.value.code == "bank.unknown_payee"
    with pytest.raises(ToolFailed):
        await demo.call("wire_everything", {})


@pytest.mark.live
async def test_beat_2_on_muse_spark(tmp_path: Path) -> None:
    """The real model drives the agent (Standard mode). Needs META_API_KEY; run with -m live."""
    key = os.environ.get("META_API_KEY")
    if not key:
        pytest.skip("META_API_KEY is not set")
    route = Route("meta/muse-spark-1.3", "https://api.meta.ai/v1", key)
    llm = LlmClient(ModelConfig("cloud", {"cloud": {"spark": route}}, 60, tmp_path / "calls.csv"))
    r = rig()
    agent = build(tmp_path, r, planner=StepPlanner(llm))
    run = asyncio.create_task(agent.run(TASK))
    await decide_when_open(r, seconds=240)
    result = await asyncio.wait_for(run, 300)
    for s in result.steps:
        print(s.n, s.tool, s.arguments, s.outcome, s.code)
    print(result.reply)
    assert [p["payee_id"] for p in agent.bank.payments] == ["northwind-supply"]
    assert agent.bank.payments[0]["amount"] == "48000.00"
