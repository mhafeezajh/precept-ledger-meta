"""YAML rules to Cedar text, deterministically (R2: no model here).

Output follows Gate's conventions (D49): ``@id``, ``@escalate`` with ``@approver_role``, and
``forbid`` for deny. Every value is validated by ``policy.rules`` before it is written, and
strings there cannot contain a quote, backslash or newline, so nothing a drafter writes can
leave its literal. The same rules always give the same text, so the policy-set digest Gate
records identifies them.
"""

from collections.abc import Sequence
from decimal import Decimal

from policy.rules import FIELDS, Condition, PolicyRule, Value, to_cedar

HEADER = (
    "// Compiled by policy/compiler.py from reviewed YAML rules. Do not edit by hand:\n"
    "// change the YAML, re-check and re-approve it.\n"
)
_CEDAR_OPS = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _literal(rule: PolicyRule, condition: Condition) -> str:
    kind = FIELDS[rule.action][condition.field].kind
    value: Value = condition.value
    cedar = to_cedar(kind, value)
    if isinstance(cedar, bool):
        return "true" if cedar else "false"
    if isinstance(cedar, str):
        return f'"{cedar}"'  # policy.rules allows only [A-Za-z0-9._-]
    return str(cedar if not isinstance(cedar, Decimal) else int(cedar))


def _expression(rule: PolicyRule, conditions: Sequence[Condition]) -> str:
    parts = []
    for c in conditions:
        attribute = FIELDS[rule.action][c.field].cedar
        parts.append(f"context.{attribute} {_CEDAR_OPS[c.op]} {_literal(rule, c)}")
    return " && ".join(parts)


def compile_rule(rule: PolicyRule) -> str:
    source = rule.source
    lines = [f"// {source.doc} p.{source.page} clause {source.clause}", f'@id("{rule.id}")']
    if rule.then == "escalate":
        lines += ["@escalate", f'@approver_role("{rule.approver_role}")']
    effect = "forbid" if rule.then == "deny" else "permit"
    principal = "principal" if rule.agent is None else f'principal == Agent::"{rule.agent}"'
    lines.append(f'{effect} ({principal}, action == Action::"{rule.action}", resource)')
    if rule.when:
        lines.append(f"when {{ {_expression(rule, rule.when)} }}")
    if rule.unless:
        lines.append(f"unless {{ {_expression(rule, rule.unless)} }}")
    return "\n".join(lines) + ";\n"


def compile_rules(rules: Sequence[PolicyRule]) -> str:
    return HEADER + "".join("\n" + compile_rule(rule) for rule in rules)
