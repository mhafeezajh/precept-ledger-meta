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
from collections.abc import Iterable, Mapping, Sequence
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
from gate.decide import SCHEMA_FILE, PolicySet
from learn.patterns import COUNT_NAMES, Pattern, Scope
from learn.replay import scope_policies
from policy.checks import FIXTURE_AGENT
from policy.compiler import compile_rules
from policy.draft import ConditionOut, FixtureOut, Short
from policy.rules import PolicyRule, rule_data, rules_from_data, to_context

PROMPT = Path(__file__).resolve().parent.parent / "assist" / "prompts" / "learn_proposal.md"
LEARNED_DOC = "learner"
_SCOPE_WORDS = ("known-payee", "new-payee", "business-hours", "after-hours", "payee_new",
                "amount_cents", "local_hour", "local_weekday")  # fmt: skip
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
    answered = {r.id for r in answer.rules}
    for rule in base:  # left out, its fixture would flip and replay would block (D90)
        skip = rule.action != pattern.scope.action or rule.id in answered
        if not skip and (moved := fixtures_to_move(rule, pattern)):
            problems.append(f"replace {rule.id} to move its fixtures {moved} outside the scope")
    return problems


def _count_problems(stated: dict[str, int], pattern: Pattern) -> list[str]:
    return [
        f"count {name}: stated {value}, SQL counted {pattern.counts[name]}"
        for name, value in stated.items()
        if pattern.counts[name] != value
    ]


def fixtures_to_move(rule: PolicyRule, pattern: Pattern) -> list[int]:
    """Numbers of the rule's fixtures inside the scope that expect another outcome than the
    change gives there: a replacement must move them just outside it. Live, the model missed
    them at every reasoning effort; code knows them exactly (scope_policies, as replay uses)."""
    covered = scope_policies(pattern.scope)
    outcome = "allow" if pattern.direction == "loosen" else "deny"
    moved = []
    for i, fixture in enumerate(rule.tests, start=1):
        agent = fixture.agent or rule.agent or FIXTURE_AGENT
        facts = to_context(rule.action, dict(fixture.facts))
        inside = covered.evaluate(agent, rule.action, "fixture", facts).outcome == "allow"
        if inside and fixture.expect != outcome:
            moved.append(i)
    return moved


def reason_names(scope: Scope, rule_ids: Iterable[str]) -> set[str]:
    """Names the reason may use without a claim: the rule ids, the scope's agent and action,
    and the learner's own vocabulary (count names, condition fields, the scope's words), in
    both spellings. Payee ids and approvers still need an assertion (R5)."""
    words = {*COUNT_NAMES, *_SCOPE_WORDS}
    spelled = {w.replace("_", "-") for w in words} | {w.replace("-", "_") for w in words}
    return {*rule_ids, scope.agent, scope.action, *spelled}


def _row_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One counted row as its two records, each field under the path the claim checker reads
    and with the value the record holds: live, the model put the verdict on the decision."""
    decision: dict[str, Any] = {"record_id": row["decision_record_id"], "body.decision": "escalate"}
    for field in ("amount_cents", "payee_id", "payee_new", "local_hour", "local_weekday"):
        if row[field] is not None:
            decision[f"body.resource.{field}"] = row[field]
    verdict: dict[str, Any] = {
        "record_id": row["verdict_record_id"],
        "body.verdict": row["verdict"],
    }
    if row["approver"] is not None:
        verdict["body.approver"] = row["approver"]
    return {"decision_record": decision, "verdict_record": verdict,
            "seconds_to_verdict": row["verdict_seconds"]}  # fmt: skip


def held_by(pattern: Pattern, base_rules: Sequence[PolicyRule], log: ProvenLog) -> list[str]:
    """The active escalate rules that hold the pattern's recorded requests now. The history
    names the rules in force then; after a new policy is approved those ids may be gone, and
    live the model, told to replace rules that no longer existed, ran out of time (D90)."""
    policies = PolicySet(compile_rules(base_rules), SCHEMA_FILE.read_text("utf-8"))
    escalate = {r.id for r in base_rules if r.then == "escalate"}
    held: set[str] = set()
    for row in pattern.rows:
        body = (log.get(row["decision_record_id"]) or {}).get("body") or {}
        args = (str(body.get("agent")), str(body.get("action")), str(body.get("tool")))
        held.update(escalate & set(policies.evaluate(*args, body.get("resource") or {}).policy_ids))
    return sorted(held)


def model_input(pattern: Pattern, base_rules: Sequence[PolicyRule], log: ProvenLog) -> str:
    scope = pattern.scope
    view = {
        "direction": pattern.direction,
        "scope": {"statement": scope.statement(), "agent": scope.agent, "action": scope.action,
                  "when": scope.when(), "unless": scope.unless()},
        "counts": pattern.counts,
        "policies_matched": held_by(pattern, base_rules, log),
        "active_rules": [{**rule_data(r), "fixtures_to_move": fixtures_to_move(r, pattern)}
                         for r in base_rules if r.action == scope.action],
        "rows": [_row_view(row) for row in pattern.rows[:MAX_ROWS]],
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
             {"role": "user", "content": model_input(pattern, base_rules, log)}],
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
        names = reason_names(pattern.scope, [r.id for r in merged])
        reason = check_summary(answer.reason, results, numbers=numbers, names=names)
        return Drafted(
            answer, changed, merged, stated, tuple(_count_problems(stated, pattern)), results,
            tuple(reason), self.model, self.prompt_version, injected,
        )  # fmt: skip
