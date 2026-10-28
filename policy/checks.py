"""Checks on drafted rules before a person reviews them (ARCHITECTURE flow B step 3, R2).

1. ``schema``: the compiled set must pass Gate's own loader and the Cedar schema.
2. ``fixture``: every fixture's request must get its expected outcome from the whole set.
3. ``untested`` / ``one_sided``: some fixture must trigger the rule and some must not, so
   both sides of its conditions are exercised.
4. ``allows_everything``: an allow rule, on its own, must not allow the riskiest request of
   its action (a new payee, a foreign currency, any amount, 3 a.m. on a Sunday).
5. ``clause_number``: every amount, count, hour or weekday in the rule must be a number its
   quoted clause states. This is what catches "$50,000" drafted from a "$5,000" clause.

Findings are for the reviewer; none of them is fixed automatically.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from common.errors import PolicyError
from gate.decide import SCHEMA_FILE, PolicySet
from policy.compiler import compile_rule, compile_rules
from policy.rules import FIELDS, PolicyRule, Value, clause_numbers, to_context

FIXTURE_AGENT = "fixture-agent"
_RISKIEST: dict[str, dict[str, Value]] = {
    "value.transfer": {"amount": Decimal(1_000_000_000), "currency": "XXX",
                       "payee_id": "unknown-payee", "payee_new": True,
                       "local_hour": 3, "local_weekday": 7},
    "comm.external": {"recipients": 1000, "external_recipients": 1000,
                      "local_hour": 3, "local_weekday": 7},
    "data.read": {"data_class": "unlisted", "local_hour": 3, "local_weekday": 7},
}  # fmt: skip


@dataclass(frozen=True)
class Finding:
    rule_id: str  # "*" for the whole set
    check: str
    detail: str


@dataclass(frozen=True)
class PolicyCheck:
    text: str  # the compiled Cedar
    digest: str | None  # Gate's policy_set digest, if the set loads
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return self.digest is not None and not self.findings


def _fixtures(rule: PolicyRule, policies: PolicySet) -> list[Finding]:
    found, triggered = [], []
    for i, fixture in enumerate(rule.tests, start=1):
        facts = to_context(rule.action, dict(fixture.facts))
        agent = fixture.agent or rule.agent or FIXTURE_AGENT
        result = policies.evaluate(agent, rule.action, "fixture", facts)
        triggered.append(rule.id in result.policy_ids)
        if result.outcome != fixture.expect:
            why = ", ".join(result.policy_ids) or result.reason
            detail = f"fixture {i}: expected {fixture.expect}, got {result.outcome} ({why})"
            found.append(Finding(rule.id, "fixture", detail))
    if not any(triggered):
        found.append(Finding(rule.id, "untested", "no fixture triggers this rule"))
    elif all(triggered):
        detail = "every fixture triggers this rule; add one just outside its conditions"
        found.append(Finding(rule.id, "one_sided", detail))
    return found


def _allows_everything(rule: PolicyRule, schema: str) -> list[Finding]:
    if rule.then != "allow":
        return []
    alone = PolicySet(compile_rule(rule), schema)
    riskiest = _RISKIEST[rule.action]
    agent = rule.agent or FIXTURE_AGENT
    result = alone.evaluate(agent, rule.action, "probe", to_context(rule.action, riskiest))
    if result.outcome != "allow":
        return []
    shown = ", ".join(f"{k}={v}" for k, v in riskiest.items())
    return [Finding(rule.id, "allows_everything", f"on its own it allows {shown}")]


def _clause_numbers(rule: PolicyRule) -> list[Finding]:
    stated = clause_numbers(rule.source.quote)
    found = []
    for c in (*rule.when, *rule.unless):
        kind = FIELDS[rule.action][c.field].kind
        if kind not in ("dollars", "int") or isinstance(c.value, bool):
            continue
        number = Decimal(c.value) if not isinstance(c.value, Decimal) else c.value
        if number != 0 and number not in stated:  # 0 encodes "none" or "any"
            listed = ", ".join(str(n) for n in sorted(stated)) or "none"
            detail = (
                f"{c.field} {c.op} {number} is not a number clause {rule.source.clause}"
                f" states (it states: {listed})"
            )
            found.append(Finding(rule.id, "clause_number", detail))
    return found


def check_rules(rules: Sequence[PolicyRule], *, schema: str | None = None) -> PolicyCheck:
    schema = SCHEMA_FILE.read_text("utf-8") if schema is None else schema
    text = compile_rules(rules)
    try:
        policies = PolicySet(text, schema)
    except PolicyError as exc:
        return PolicyCheck(text, None, (Finding("*", "schema", str(exc)),))
    findings: list[Finding] = []
    for rule in rules:
        findings += _fixtures(rule, policies)
        findings += _allows_everything(rule, schema)
        findings += _clause_numbers(rule)
    return PolicyCheck(text, policies.digest, tuple(findings))
