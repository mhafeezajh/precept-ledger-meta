"""Muse Spark drafts a proposal for one pattern (ARCHITECTURE flow E steps 3 and 4).

The only model call in ``learn/``. The model gets the pattern code found: its scope as rule
conditions and in words, the SQL counts, the rows that count and the active rules for the
action. It returns rule changes (new rules, or replacements of active rules by id), a reason,
the counts restated, and claims citing records. It writes text; it decides nothing.

Code then:

- sets every changed rule's ``source``: the scope statement, so ``policy.checks`` flags any
  number in a rule that the scope does not state (a model cannot widen "$15,000" to "$50,000");
  a replaced rule keeps its clause and quote, with the learned exception added;
- compares each restated count with the SQL count: any mismatch rejects the proposal;
- checks every claim with ``claims.checker`` against the proven log: any failure rejects it;
- lets the reason state only SQL counts, the scope's numbers, accepted claims' values and the
  ids of the rules involved.

Structure (rule shape, record ids that are not the pattern's) gets one retry; the counts and
claims do not, so a model that inflates a count is caught, not coached. ``inject_fault`` (demo
only) adds 10 to the first restated count, to show that rejection on demand.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.auditor import ClaimOut, to_claim
from assist.brief import load_prompt
from assist.llm import LlmClient
from claims.checker import ClaimResult, check_claims_in_log, check_summary
from claims.log import ProvenLog
from common.errors import PolicyError
from learn.patterns import COUNT_NAMES, Pattern
from policy.draft import ConditionOut, FixtureOut, Short
from policy.rules import PolicyRule, rule_data, rules_from_data

PROMPT = Path(__file__).resolve().parent.parent / "assist" / "prompts" / "learn_proposal.md"
LEARNED_DOC = "learner"
MAX_ROWS = 12  # rows the model sees; the counts cover all of them


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LearnRuleOut(_Strict):
    id: Short
    agent: Short | None
    action: Literal["value.transfer", "comm.external", "data.read"]
    when: Annotated[list[ConditionOut], Field(max_length=8)]
    unless: Annotated[list[ConditionOut], Field(max_length=8)]
    then: Literal["allow", "escalate", "deny"]
    approver_role: Short | None
    tests: Annotated[list[FixtureOut], Field(min_length=2, max_length=6)]


class CountOut(_Strict):
    name: Literal["decisions", "approved", "rejected", "expired", "fast_dropped", "stamped_dropped"]
    value: Annotated[int, Field(ge=0)]


class LearnerProposal(_Strict):
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    rules: Annotated[list[LearnRuleOut], Field(min_length=1, max_length=4)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    counts: Annotated[list[CountOut], Field(min_length=1, max_length=len(COUNT_NAMES))]
    claims: Annotated[list[ClaimOut], Field(min_length=1, max_length=6)]


@dataclass(frozen=True)
class Drafted:
    answer: LearnerProposal
    changed: tuple[PolicyRule, ...]  # with sources set by code
    merged: tuple[PolicyRule, ...]  # the active rules with the changes applied
    stated: dict[str, int]  # the counts as the model restated them (after any injected fault)
    count_problems: tuple[str, ...]
    claims: tuple[ClaimResult, ...]
    reason_problems: tuple[str, ...]
    model: str
    prompt_version: str
    injected: str | None

    @property
    def rejected(self) -> list[str]:
        """Why the model's account of the history does not hold; empty when it does."""
        reasons = list(self.count_problems)
        reasons += [
            f"claim {i} rejected: {'; '.join(r.reasons)}"
            for i, r in enumerate(self.claims, start=1)
            if not r.accepted
        ]
        return reasons + [f"reason: {p}" for p in self.reason_problems]


def _conditions(items: Sequence[ConditionOut]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for c in items:
        out.setdefault(c.field, {})[c.op] = c.value
    return out


def _source(rule_id: str, base: dict[str, PolicyRule], statement: str) -> dict[str, Any]:
    earlier = base.get(rule_id)
    if earlier is None:
        return {"doc": LEARNED_DOC, "page": 1, "clause": "learned", "quote": statement}
    s = earlier.source  # the clause stays cited; the span no longer matches the quote
    quote = f"{s.quote} Learned exception: {statement}"
    return {"doc": s.doc, "page": s.page, "clause": s.clause, "quote": quote}


def rule_changes(
    answer: LearnerProposal, pattern: Pattern, base_rules: Sequence[PolicyRule]
) -> tuple[tuple[PolicyRule, ...], tuple[PolicyRule, ...], list[str]]:
    """(changed rules, merged set, structural problems). Sources are set here, by code."""
    base = {r.id: r for r in base_rules}
    statement, data, problems = pattern.scope.statement(), [], []
    for rule in answer.rules:
        if rule.action != pattern.scope.action:
            problems.append(f"rule {rule.id}: action must be {pattern.scope.action}")
        item: dict[str, Any] = {
            "id": rule.id, "source": _source(rule.id, base, statement), "agent": rule.agent,
            "when": {"action": rule.action, **_conditions(rule.when)}, "then": rule.then,
            "tests": [{**{f.field: f.value for f in t.facts},
                       **({"agent": t.agent} if t.agent else {}), "expect": t.expect}
                      for t in rule.tests],
        }  # fmt: skip
        if rule.unless:
            item["unless"] = _conditions(rule.unless)
        if rule.approver_role is not None:
            item["approver_role"] = rule.approver_role
        data.append(item)
    try:
        changed = rules_from_data(data)
    except PolicyError as exc:
        return (), tuple(base_rules), [*problems, str(exc.args[0])]
    return changed, merge_rules(base_rules, changed), problems


def merge_rules(
    base: Sequence[PolicyRule], changed: Sequence[PolicyRule]
) -> tuple[PolicyRule, ...]:
    """``base`` with each changed rule replacing the rule of the same id, new ones appended.
    A proposal can add or replace rules; it cannot remove one."""
    replaced = {r.id: r for r in changed}
    return tuple(replaced.pop(r.id, r) for r in base) + tuple(replaced.values())


def _structure(answer: LearnerProposal, pattern: Pattern, base: Sequence[PolicyRule]) -> list[str]:
    problems = rule_changes(answer, pattern, base)[2]
    names = [c.name for c in answer.counts]
    if len(set(names)) != len(names):
        problems.append("each count may be stated once")
    allowed = pattern.record_ids
    cited = {
        i for c in answer.claims for i in [*c.record_ids, *(a.record_id for a in c.assertions)]
    }
    if unknown := sorted(cited - allowed):
        problems.append(f"record ids that are not in the pattern's rows: {unknown}")
    return problems


def _count_problems(stated: dict[str, int], pattern: Pattern) -> list[str]:
    return [
        f"count {name}: stated {value}, SQL counted {pattern.counts[name]}"
        for name, value in stated.items()
        if pattern.counts[name] != value
    ]


def model_input(pattern: Pattern, base_rules: Sequence[PolicyRule]) -> str:
    scope = pattern.scope
    view = {
        "direction": pattern.direction,
        "scope": {"statement": scope.statement(), "agent": scope.agent, "action": scope.action,
                  "when": scope.when(), "unless": scope.unless()},
        "counts": pattern.counts,
        "policies_matched": pattern.policy_ids(),
        "active_rules": [rule_data(r) for r in base_rules if r.action == scope.action],
        "rows": [{k: row[k] for k in ("decision_record_id", "verdict_record_id", "amount_cents",
                                      "payee_id", "payee_new", "local_hour", "local_weekday",
                                      "verdict", "approver", "verdict_seconds")}
                 for row in pattern.rows[:MAX_ROWS]],
    }  # fmt: skip
    return "Pattern:\n" + json.dumps(view, indent=1, default=str)  # model input, never hashed


class LearnDrafter:
    def __init__(self, llm: LlmClient, prompt_file: Path = PROMPT) -> None:
        self._llm = llm
        self.prompt_version, self._instructions = load_prompt(prompt_file)

    @property
    def model(self) -> str:
        return self._llm.config.route("spark").model

    async def draft(
        self,
        pattern: Pattern,
        base_rules: Sequence[PolicyRule],
        log: ProvenLog,
        *,
        inject_fault: bool = False,
    ) -> Drafted:
        answer = await self._llm.complete_structured(
            "spark", LearnerProposal,
            [{"role": "system", "content": self._instructions},
             {"role": "user", "content": model_input(pattern, base_rules)}],
            prompt_version=self.prompt_version,
            check=lambda a: _structure(a, pattern, base_rules),
        )  # fmt: skip
        changed, merged, _ = rule_changes(answer, pattern, base_rules)
        stated: dict[str, int] = {c.name: c.value for c in answer.counts}
        injected = None
        if inject_fault:  # demo only: the model "remembers" ten more decisions than SQL found
            name = answer.counts[0].name
            stated[name] += 10
            injected = f"counts.{name}"
        results = tuple(check_claims_in_log([to_claim(c) for c in answer.claims], log))
        numbers = {*pattern.counts.values(), *pattern.scope.numbers()}
        names = {r.id for r in merged} | {pattern.scope.agent, pattern.scope.action}
        reason = check_summary(answer.reason, results, numbers=numbers, names=names)
        return Drafted(
            answer, changed, merged, stated, tuple(_count_problems(stated, pattern)), results,
            tuple(reason), self.model, self.prompt_version, injected,
        )  # fmt: skip
