"""Muse Wonder proposes a revised learner prompt from the learner's own failures (D95).

The only model call in ``improve/``. The model gets the learner's current instructions and the
reasons code gave for its recent rejected or blocked proposals, and answers with revised
instructions, the changes and why. It writes text; code tests it (``improve.evaluate``) and
people decide.

Code checks the revision before it is evaluated (one retry, then ``ModelOutputError``): every
``KEEP`` sentence is still there, the text changed, and it is at most half as long again.

``plant_shortcut`` (demo only) swaps the claims instruction for one that asks for a single
claim asserting only a verdict: shorter, faster, fewer rejections, and fewer checks. It shows
the canaries refusing a prompt that scores better by saying less.
"""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.brief import load_prompt
from assist.llm import LlmClient
from learn.prompt import PromptInForce, missing_guards

PROMPT = Path(__file__).resolve().parent.parent / "assist" / "prompts" / "improve_prompt.md"
MAX_GROWTH = 1.5
SHORTCUT = (
    "- claims: one short claim about the pattern as a whole, citing one row's verdict_record "
    "and asserting only its body.verdict. Keep it brief: fewer claims mean fewer rejections."
)
_CLAIMS = re.compile(r"^- claims:.*?(?=\n\n|\Z)", re.DOTALL | re.MULTILINE)


class PromptRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: Annotated[str, StringConstraints(min_length=200, max_length=16_000)]
    changes: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=200)]],
        Field(min_length=1, max_length=6),
    ]
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=600)]


def revision_problems(instructions: str, current: str) -> list[str]:
    problems = [f"keep this sentence exactly: {k!r}" for k in missing_guards(instructions)]
    if instructions.strip() == current.strip():
        problems.append("the instructions are unchanged")
    if len(instructions) > len(current) * MAX_GROWTH:
        problems.append(f"at most {int(len(current) * MAX_GROWTH)} characters")
    return problems


def plant_shortcut(instructions: str) -> str:
    """Demo only: the claims instruction replaced by one that asks for less evidence."""
    replaced, count = _CLAIMS.subn(SHORTCUT, instructions, count=1)
    return replaced if count else f"{instructions}\n\n{SHORTCUT}"


class Proposer:
    def __init__(self, llm: LlmClient, prompt_file: Path = PROMPT) -> None:
        self._llm = llm
        self.prompt_version, self._instructions = load_prompt(prompt_file)

    @property
    def model(self) -> str:
        return self._llm.config.route("wonder").model

    async def revise(
        self, current: PromptInForce, failures: Sequence[dict[str, Any]]
    ) -> PromptRevision:
        user = (
            "Current instructions:\n<<<\n" + current.instructions + "\n>>>\n\n"
            "Recent failures:\n" + json.dumps(list(failures), indent=1)
        )  # model input, never hashed
        return await self._llm.complete_structured(
            "wonder", PromptRevision,
            [{"role": "system", "content": self._instructions},
             {"role": "user", "content": user}],
            prompt_version=self.prompt_version,
            check=lambda r: revision_problems(r.instructions, current.instructions),
        )  # fmt: skip
