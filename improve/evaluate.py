"""Scoring a learner prompt on the ledger's own history, and comparing two (D95).

Both the prompt in force and the candidate draft a proposal for the same patterns from the same
proven log. Each draft is judged by the learner's own code (``LearnService.proposal_body``:
counts against SQL, claims against records, policy checks, replay), then has the canaries
planted (``improve.canary``). The model's opinion of its prompt is never asked.

A candidate goes to review only if it is no worse than the prompt in force on every measure:
as many drafts accepted, no more failed, and every canary the current prompt's drafts catch
still caught. Whether it is better is for the two approvers to judge from the scores.
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from claims.log import ProvenLog
from common.errors import ModelOutputError
from improve.canary import CANARIES, CAUGHT, plant
from learn.draft import Drafted, LearnDrafter
from learn.patterns import Pattern
from policy.rules import PolicyRule

# How a learner run would record a draft: (state, reasons). From LearnService.proposal_body.
Assess = Callable[[Pattern, Drafted], tuple[str, list[str]]]


@dataclass(frozen=True)
class Case:
    key: str
    outcome: str  # "review" (accepted), "rejected", "blocked" or "failed"
    reasons: tuple[str, ...]
    claims: int
    assertions: int
    latency_ms: int
    canaries: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "outcome": self.outcome, "reasons": list(self.reasons[:3]),
                "claims": self.claims, "assertions": self.assertions,
                "latency_ms": self.latency_ms, "canaries": self.canaries}  # fmt: skip


@dataclass(frozen=True)
class Score:
    version: str
    cases: tuple[Case, ...]

    @property
    def accepted(self) -> int:
        return sum(c.outcome == "review" for c in self.cases)

    @property
    def failed(self) -> int:
        return sum(c.outcome == "failed" for c in self.cases)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "accepted": self.accepted,
            "failed": self.failed,
            "cases": [c.to_json() for c in self.cases],
            "latency_ms": sum(c.latency_ms for c in self.cases),
        }


async def _case(
    drafter: LearnDrafter, pattern: Pattern, rules: Sequence[PolicyRule], log: ProvenLog,
    assess: Assess,
) -> Case:  # fmt: skip
    key = pattern.scope.key()
    started = time.perf_counter()  # elapsed time only, not a timestamp
    try:
        drafted = await drafter.draft(pattern, rules, log)
    except ModelOutputError as exc:  # counted as a failure, never skipped (R4)
        elapsed = int((time.perf_counter() - started) * 1000)
        return Case(key, "failed", (exc.code,), 0, 0, elapsed)
    elapsed = int((time.perf_counter() - started) * 1000)
    state, reasons = assess(pattern, drafted)
    claims = drafted.answer.claims
    return Case(key, state, tuple(reasons), len(claims), sum(len(c.assertions) for c in claims),
                elapsed, plant(drafted, pattern, log))  # fmt: skip


async def score(
    drafter: LearnDrafter,
    patterns: Sequence[Pattern],
    rules: Sequence[PolicyRule],
    log: ProvenLog,
    assess: Assess,
) -> Score:
    cases = await asyncio.gather(*(_case(drafter, p, rules, log, assess) for p in patterns))
    return Score(drafter.prompt_version, tuple(cases))


def compare(base: Score, candidate: Score) -> list[str]:
    """Why the candidate is worse than the prompt in force; empty when it is no worse."""
    problems = []
    before = {c.key: c for c in base.cases}
    for case in candidate.cases:
        old = before.get(case.key)
        for name in CANARIES:
            if old is not None and old.canaries.get(name) == CAUGHT != case.canaries.get(name):
                problems.append(
                    f"{case.key}: a planted {name} fault would go unseen "
                    f"({case.canaries.get(name, 'no draft')}); the current prompt catches it"
                )
    total = len(candidate.cases)
    if candidate.accepted < base.accepted:
        problems.append(f"accepts fewer drafts: {candidate.accepted} of {total}, "
                        f"against {base.accepted} of {total}")  # fmt: skip
    if candidate.failed > base.failed:
        problems.append(f"more drafts failed: {candidate.failed}, against {base.failed}")
    return problems
