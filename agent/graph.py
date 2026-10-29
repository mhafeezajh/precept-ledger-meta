"""The payments-ops agent: a LangGraph loop of think (Muse Spark) and act (through Gate).

``think`` asks the planner for the next ``AgentStep``. ``act`` runs it through
``gate.governed.Governor``, which lets the bank see the call only after Gate's decision record,
and, for an escalation, a supervisor's approval, are signed, receipted and verified (R3). What
Gate said goes back into the next prompt, so the model sees a denial or a rejection as a step
that did not happen. The model never touches the bank or the ledger itself.

The loop fails closed (R4): a model call that fails twice, too many steps, or a repeat of a
call already sent ends the run; none of them can make a tool run. LangSmith tracing is forced
off before LangGraph loads: it would send the agent's state to an outside service (D78).
"""

import operator
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

os.environ["LANGSMITH_TRACING"] = "false"  # D78: nothing leaves the machine except model calls
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from langchain_core.runnables import RunnableConfig  # tracing must be off first
from langgraph.graph import END, START, StateGraph

from agent.bank import DemoBank, ToolFailed
from agent.steps import AgentStep, StepRecord
from common.errors import ModelOutputError
from gate.governed import GateDenied, Governor
from gate.normalizer import args_digest
from gate.records import Receipted

MAX_STEPS = 8
MAX_TASK_CHARS = 2000

__all__ = ["AgentRun", "Observer", "PaymentsAgent", "Planner", "StepRecord"]


class Planner(Protocol):
    async def next_step(self, task: str, steps: Sequence[StepRecord]) -> AgentStep: ...


class Observer(Protocol):
    """Progress for a person watching: the CLI prints it, the web page will stream it."""

    def started(self, n: int, step: AgentStep) -> None: ...

    def held(self, n: int, decision_record_id: str) -> None: ...

    def finished(self, record: StepRecord) -> None: ...


class _Quiet:
    def started(self, n: int, step: AgentStep) -> None:
        pass

    def held(self, n: int, decision_record_id: str) -> None:
        pass

    def finished(self, record: StepRecord) -> None:
        pass


@dataclass(frozen=True)
class AgentRun:
    task: str
    steps: tuple[StepRecord, ...]
    reply: str
    stopped: str | None  # why the run ended early: agent.step_limit or a model.* code


class State(TypedDict):
    task: str
    steps: Annotated[list[StepRecord], operator.add]
    pending: AgentStep | None
    reply: str
    stopped: str | None


class PaymentsAgent:
    def __init__(
        self, planner: Planner, governor: Governor, bank: DemoBank, *, max_steps: int = MAX_STEPS
    ) -> None:
        self._planner = planner
        self._governor = governor
        self.bank = bank
        self._max_steps = max_steps
        graph = StateGraph(State)
        graph.add_node("think", self._think)
        graph.add_node("act", self._act)
        graph.add_edge(START, "think")
        graph.add_conditional_edges("think", _next, ["act", END])
        graph.add_edge("act", "think")
        self._graph = graph.compile()

    async def run(self, task: str, *, observer: Observer | None = None) -> AgentRun:
        task = task.strip()
        if not task or len(task) > MAX_TASK_CHARS:
            raise ValueError(f"the task must be 1 to {MAX_TASK_CHARS} characters")
        start: State = {"task": task, "steps": [], "pending": None, "reply": "", "stopped": None}
        config: RunnableConfig = {
            "recursion_limit": 2 * self._max_steps + 4,
            "configurable": {"observer": observer or _Quiet()},
        }
        final = await self._graph.ainvoke(start, config)
        return AgentRun(task, tuple(final["steps"]), final["reply"], final["stopped"])

    async def _think(self, state: State) -> dict[str, Any]:
        steps = state["steps"]
        if len(steps) >= self._max_steps:
            reply = f"I stopped after {len(steps)} steps without finishing the task."
            return {"pending": None, "reply": reply, "stopped": "agent.step_limit"}
        try:
            step = await self._planner.next_step(state["task"], steps)
        except ModelOutputError as exc:  # no step means nothing runs (R4)
            reply = f"I could not plan the next step ({exc.code}); nothing more was done."
            return {"pending": None, "reply": reply, "stopped": exc.code}
        if step.tool == "finish":
            return {"pending": None, "reply": step.reply}
        return {"pending": step}

    async def _act(self, state: State, config: RunnableConfig) -> dict[str, Any]:
        observer: Observer = config.get("configurable", {})["observer"]
        step, n = state["pending"], len(state["steps"]) + 1
        if step is None:  # _next routes here only with a pending step
            raise RuntimeError("act without a pending step")
        observer.started(n, step)
        record = await self._call(n, step, state["steps"], observer)
        observer.finished(record)
        return {"steps": [record], "pending": None}

    async def _call(
        self, n: int, step: AgentStep, earlier: Sequence[StepRecord], observer: Observer
    ) -> StepRecord:
        tool, arguments = step.tool, step.arguments()

        def record(outcome: str, **fields: Any) -> StepRecord:
            base = {"result": None, "code": None, "decision": None, "decision_record_id": None,
                    "verdict_record_id": None}  # fmt: skip
            return StepRecord(n, step.note, tool, arguments, outcome, **{**base, **fields})

        if _already_sent(tool, arguments, earlier):
            # The same call again would be a second payment or a second ask of an approver.
            return record("skipped", code="agent.duplicate_call")
        try:
            cleared = await self._governor.run_cleared(
                tool, arguments, lambda: self.bank.call(tool, arguments),
                on_hold=lambda decision: observer.held(n, decision.record_id),
            )  # fmt: skip
        except GateDenied as exc:
            return record("denied", code=exc.code, **_gate_fields(exc.decision, None))
        except ToolFailed as exc:  # the governor recorded the error result before re-raising
            return record("error", code=exc.code)
        return record(
            "ok", result=cleared.result, **_gate_fields(cleared.decision, cleared.verdict)
        )


def _next(state: State) -> str:
    return "act" if state["pending"] is not None else END


def _already_sent(tool: str, arguments: dict[str, Any], earlier: Sequence[StepRecord]) -> bool:
    digest = args_digest(arguments)
    return any(
        s.tool == tool and s.outcome != "skipped" and args_digest(s.arguments) == digest
        for s in earlier
    )


def _gate_fields(decision: Receipted | None, verdict: Receipted | None) -> dict[str, Any]:
    if decision is None:
        return {}
    return {
        "decision": decision.body.get("decision"),
        "decision_record_id": decision.record_id,
        "verdict_record_id": None if verdict is None else verdict.record_id,
    }
