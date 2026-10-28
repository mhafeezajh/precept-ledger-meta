"""Muse Wonder drafts rules from a policy document (ARCHITECTURE flow B step 2, R2).

The model gets the numbered clauses and returns ``DraftRules``: for each rule, the clause it
implements and a quote from it, conditions, the outcome and fixtures. Code then anchors every
rule: the document name, page and character span come from ``policy.pdf``, never from the model
(D68), and a quote that is not inside its clause is a structural problem the model gets one
retry to fix. The checks in ``policy.checks`` (fixtures, allows everything, clause numbers) are
not retried: they are for the reviewer, which is how beat 1 shows a bad rule being caught.

``inject_fault`` (demo only) multiplies the first amount threshold by ten, the way a drafter
might misread "$5,000" as "$50,000", and the draft says which rule it changed.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.brief import load_prompt
from assist.llm import LlmClient
from common.errors import PolicyError
from policy.checks import PolicyCheck, check_rules
from policy.pdf import Document
from policy.rules import PolicyRule, rules_from_data

PROMPT = Path(__file__).resolve().parent.parent / "assist" / "prompts" / "policy_draft.md"
MAX_RULES = 40
Short = Annotated[str, StringConstraints(max_length=64)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConditionOut(_Strict):
    field: Short
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: str | int | bool  # amounts in dollars: 5000 or "5000.00", never a float


class FactOut(_Strict):
    field: Short
    value: str | int | bool


class FixtureOut(_Strict):
    facts: Annotated[list[FactOut], Field(max_length=8)]
    agent: Short | None
    expect: Literal["allow", "escalate", "deny"]


class DraftRuleOut(_Strict):
    id: Short
    clause: Annotated[str, StringConstraints(max_length=16)]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    agent: Short | None
    action: Literal["value.transfer", "comm.external", "data.read"]
    when: Annotated[list[ConditionOut], Field(max_length=8)]
    unless: Annotated[list[ConditionOut], Field(max_length=8)]
    then: Literal["allow", "escalate", "deny"]
    approver_role: Short | None
    tests: Annotated[list[FixtureOut], Field(min_length=1, max_length=6)]


class DraftRules(_Strict):
    rules: Annotated[list[DraftRuleOut], Field(min_length=1, max_length=MAX_RULES)]


@dataclass(frozen=True)
class Draft:
    document: Document
    rules: tuple[PolicyRule, ...]
    report: PolicyCheck
    model: str
    prompt_version: str
    injected: str | None  # the rule inject_fault changed


def _conditions(items: Sequence[ConditionOut]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for c in items:
        out.setdefault(c.field, {})[c.op] = c.value
    return out


def anchored(answer: DraftRules, document: Document) -> tuple[list[dict[str, Any]], list[str]]:
    """The answer as rule data with code-computed sources, and what is structurally wrong."""
    data, problems = [], []
    for rule in answer.rules:
        span = document.locate(rule.clause, rule.quote)
        clause = document.clause(rule.clause)
        if clause is None or span is None:
            problems.append(f"rule {rule.id}: the quote is not in clause {rule.clause!r}")
            continue
        quote = document.text[span[0] : span[1]]
        source = {"doc": document.name, "page": clause.page, "clause": clause.id,
                  "quote": quote, "span": list(span)}  # fmt: skip
        item: dict[str, Any] = {"id": rule.id, "source": source, "agent": rule.agent,
                                "when": {"action": rule.action, **_conditions(rule.when)},
                                "then": rule.then, "tests": []}  # fmt: skip
        if rule.unless:
            item["unless"] = _conditions(rule.unless)
        if rule.approver_role is not None:
            item["approver_role"] = rule.approver_role
        for t in rule.tests:
            fixture: dict[str, Any] = {f.field: f.value for f in t.facts}
            if t.agent is not None:
                fixture["agent"] = t.agent
            item["tests"].append({**fixture, "expect": t.expect})
        data.append(item)
    if not problems:
        try:
            rules_from_data(data)
        except PolicyError as exc:
            problems.append(str(exc.args[0]))
    return data, problems


def inject_wrong_threshold(
    rules: Sequence[PolicyRule],
) -> tuple[tuple[PolicyRule, ...], str | None]:
    """Demo only: the first amount threshold times ten ($5,000 becomes $50,000)."""
    out = list(rules)
    for i, rule in enumerate(out):
        for j, c in enumerate(rule.when):
            if c.field == "amount" and isinstance(c.value, Decimal) and c.value > 0:
                when = list(rule.when)
                when[j] = replace(c, value=c.value * 10)
                out[i] = replace(rule, when=tuple(when))
                return tuple(out), rule.id
    return tuple(out), None


class Drafter:
    def __init__(self, llm: LlmClient, prompt_file: Path = PROMPT) -> None:
        self._llm = llm
        self.prompt_version, self._instructions = load_prompt(prompt_file)

    async def draft(self, document: Document, *, inject_fault: bool = False) -> Draft:
        clauses = "\n".join(f"[{c.id}] (page {c.page}) {c.text}" for c in document.clauses)
        answer = await self._llm.complete_structured(
            "wonder", DraftRules,
            [{"role": "system", "content": self._instructions},
             {"role": "user", "content": f"Document: {document.name}\nClauses:\n{clauses}"}],
            prompt_version=self.prompt_version, check=lambda a: anchored(a, document)[1],
        )  # fmt: skip
        rules = rules_from_data(anchored(answer, document)[0])
        injected = None
        if inject_fault:
            rules, injected = inject_wrong_threshold(rules)
        model = self._llm.config.route("wonder").model
        return Draft(document, rules, check_rules(rules), model, self.prompt_version, injected)
