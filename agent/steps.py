"""One agent step: Muse Spark picks the next tool call (or finishes) as a typed ``AgentStep``.

The step comes back through ``assist.llm.complete_structured`` like every other model output
(R6, D78): Meta's strict JSON schema, then ``check_step``, then one retry with the problems
fed back. Each call sends the task and every step so far, so the model holds no state of its
own and the prompt always shows what Gate said about each earlier call.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.brief import load_prompt
from assist.llm import LlmClient

PROMPT = Path(__file__).resolve().parent.parent / "assist" / "prompts" / "agent_step.md"
TOOLS = ("lookup_vendor", "create_payment", "send_email")


Id = Annotated[str, StringConstraints(max_length=64)]
Line = Annotated[str, StringConstraints(max_length=300)]
Address = Annotated[str, StringConstraints(max_length=254)]
Body = Annotated[str, StringConstraints(max_length=2000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LookupArgs(_Strict):
    query: Annotated[str, StringConstraints(max_length=100)]


class PaymentArgs(_Strict):
    payee_id: Id
    amount: Annotated[
        str, StringConstraints(max_length=20)
    ]  # "48000.00"; Gate refuses anything else (gate.bad_arguments)
    currency: Annotated[str, StringConstraints(max_length=3)]
    memo: Line


class EmailArgs(_Strict):
    to: Annotated[list[Address], Field(min_length=1, max_length=5)]
    subject: Line
    body: Body


class AgentStep(_Strict):
    note: Line  # why this step, one line for the run log
    tool: Literal["lookup_vendor", "create_payment", "send_email", "finish"]
    lookup_vendor: LookupArgs | None
    create_payment: PaymentArgs | None
    send_email: EmailArgs | None
    reply: Body  # the answer to the user, when tool is finish

    def arguments(self) -> dict[str, Any]:
        args = getattr(self, self.tool, None) if self.tool in TOOLS else None
        return {} if args is None else args.model_dump()


def check_step(step: AgentStep) -> list[str]:
    """Exactly the chosen tool's arguments, and a reply when finishing."""
    given = [t for t in TOOLS if getattr(step, t) is not None]
    if step.tool == "finish":
        problems = [] if step.reply.strip() else ["finish needs a reply"]
        if given:
            problems.append(f"finish takes no tool arguments (got {', '.join(given)})")
        return problems
    if getattr(step, step.tool) is None:
        return [f"{step.tool} needs its arguments"]
    if given != [step.tool]:
        return [f"give only {step.tool}'s arguments; set the others to null"]
    return []


@dataclass(frozen=True)
class StepRecord:
    """What happened to one step: Gate's decision and the tool's result or failure code."""

    n: int
    note: str
    tool: str
    arguments: dict[str, Any]
    outcome: str  # ok, denied (by Gate), error (the tool failed), skipped (a repeat)
    result: Any
    code: str | None
    decision: str | None  # allow, escalate or deny, when Gate decided
    decision_record_id: str | None
    verdict_record_id: str | None

    def to_json(self) -> dict[str, Any]:
        """What the model sees of this step (not its own note)."""
        out: dict[str, Any] = {"n": self.n, "tool": self.tool, "arguments": self.arguments,
                               "outcome": self.outcome}  # fmt: skip
        if self.result is not None:
            out["result"] = self.result
        if self.code is not None:
            out["code"] = self.code
        if self.decision is not None:
            gate = {"decision": self.decision, "decision_record_id": self.decision_record_id}
            if self.verdict_record_id is not None:
                gate["verdict_record_id"] = self.verdict_record_id
            out["gate"] = gate
        return out


def render_task(task: str, steps: Sequence[StepRecord]) -> str:
    shown = json.dumps([s.to_json() for s in steps], indent=1)  # model input, never hashed
    return f"Task:\n{task}\n\nSteps so far:\n{shown}"


class StepPlanner:
    def __init__(self, llm: LlmClient, prompt_file: Path = PROMPT) -> None:
        self._llm = llm
        self.prompt_version, self._instructions = load_prompt(prompt_file)

    @property
    def model(self) -> str:
        return self._llm.config.route("spark").model

    async def next_step(self, task: str, steps: Sequence[StepRecord]) -> AgentStep:
        return await self._llm.complete_structured(
            "spark", AgentStep,
            [{"role": "system", "content": self._instructions},
             {"role": "user", "content": render_task(task, steps)}],
            prompt_version=self.prompt_version, check=check_step,
        )  # fmt: skip
