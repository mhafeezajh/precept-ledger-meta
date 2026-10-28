"""Policy rules as reviewable YAML, each citing the clause it comes from (ARCHITECTURE 2.4, D68).

A rule names one action, the conditions that make it apply (``when``, and optionally
``unless``), what follows (``allow``, ``escalate`` with an approver role, or ``deny``), and
fixtures: requests with the outcome the whole rule set must give. Field names and units are
the policy's, not Cedar's: ``amount`` is in dollars and compiles to ``amount_cents`` (D50).

Rules are hostile input (a model drafts them from a PDF): only known fields and operators,
values of the field's type, floats refused, and strings limited to characters that cannot
leave a Cedar string literal or comment.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import yaml

from common.errors import PolicyError

Kind = Literal["dollars", "int", "bool", "str"]
Value = Decimal | int | bool | str
OUTCOMES = ("allow", "escalate", "deny")
OPS = ("eq", "ne", "gt", "gte", "lt", "lte")
ORDERED = frozenset({"gt", "gte", "lt", "lte"})
MAX_YAML_BYTES = 256 * 1024
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")  # gate.decide accepts the same @id values
_STRING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # no quote, backslash or space
_DOC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")  # goes into a Cedar comment
_CLAUSE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.()-]{0,15}$")
_DOLLARS = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
_RULE_KEYS = {"id", "source", "agent", "when", "unless", "then", "approver_role", "tests"}
_REQUIRED = {"id", "source", "when", "then", "tests"}


@dataclass(frozen=True)
class FieldSpec:
    cedar: str  # the Cedar context attribute (gate/schema.cedarschema)
    kind: Kind


_HOURS = {"local_hour": FieldSpec("local_hour", "int"),
          "local_weekday": FieldSpec("local_weekday", "int")}  # fmt: skip
FIELDS: dict[str, dict[str, FieldSpec]] = {
    "value.transfer": {
        "amount": FieldSpec("amount_cents", "dollars"),
        "currency": FieldSpec("currency", "str"),
        "payee_id": FieldSpec("payee_id", "str"),
        "payee_new": FieldSpec("payee_new", "bool"),
        **_HOURS,
    },
    "comm.external": {
        "recipients": FieldSpec("recipients", "int"),
        "external_recipients": FieldSpec("external_recipients", "int"),
        **_HOURS,
    },
    "data.read": {"data_class": FieldSpec("data_class", "str"), **_HOURS},
}
_WEEKDAY_10AM = {"local_hour": 10, "local_weekday": 2}  # Tuesday, inside business hours
DEFAULTS: dict[str, dict[str, Value]] = {  # fixture fields a test leaves out
    "value.transfer": {"amount": Decimal(100), "currency": "USD", "payee_id": "fixture-payee",
                       "payee_new": False, **_WEEKDAY_10AM},
    "comm.external": {"recipients": 1, "external_recipients": 0, **_WEEKDAY_10AM},
    "data.read": {"data_class": "vendor", **_WEEKDAY_10AM},
}  # fmt: skip


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Value  # in policy units: dollars as Decimal


@dataclass(frozen=True)
class Fixture:
    facts: tuple[tuple[str, Value], ...]
    expect: str
    agent: str | None


@dataclass(frozen=True)
class Source:
    doc: str
    page: int
    clause: str
    quote: str  # the clause text the rule implements; numbers in the rule must appear in it


@dataclass(frozen=True)
class PolicyRule:
    id: str
    source: Source
    agent: str | None  # None: any agent
    action: str
    when: tuple[Condition, ...]
    unless: tuple[Condition, ...]
    then: str
    approver_role: str | None
    tests: tuple[Fixture, ...]


def _bad(message: str, code: str) -> PolicyError:
    return PolicyError(message, code=code)


def _value(kind: Kind, raw: object, where: str) -> Value:
    if kind == "dollars":
        # A float such as 5000.1 is not exact; whole dollars or a "5000.10" string only.
        if isinstance(raw, (int, Decimal)) and not isinstance(raw, bool) and raw >= 0:
            raw = format(raw, "f") if isinstance(raw, Decimal) else str(raw)  # parsed, or whole
        if isinstance(raw, str) and _DOLLARS.fullmatch(raw):
            return Decimal(raw)
    elif kind == "int":
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 1_000_000_000:
            return raw
    elif kind == "bool":
        if isinstance(raw, bool):
            return raw
    elif isinstance(raw, str) and _STRING.fullmatch(raw):
        return raw
    raise _bad(f"{where}: {raw!r} is not a valid {kind} value", "policy.rule_value")


def _spec(action: str, name: object, where: str) -> FieldSpec:
    spec = FIELDS[action].get(name) if isinstance(name, str) else None
    if spec is None:
        raise _bad(f"{where}: {action} has no field {name!r}", "policy.rule_field")
    return spec


def _conditions(action: str, raw: object, where: str) -> tuple[Condition, ...]:
    if not isinstance(raw, dict):
        raise _bad(f"{where} must be a mapping", "policy.rule_shape")
    out = []
    for name, test in raw.items():
        spec = _spec(action, name, where)
        pairs = test.items() if isinstance(test, dict) else [("eq", test)]
        for op, value in pairs:
            if op not in OPS or (op in ORDERED and spec.kind not in ("dollars", "int")):
                raise _bad(f"{where}: operator {op!r} on {name}", "policy.rule_op")
            out.append(Condition(name, op, _value(spec.kind, value, f"{where}.{name}")))
    return tuple(out)


def _facts(action: str, raw: Mapping[Any, Any], where: str) -> dict[str, Value]:
    return {
        name: _value(_spec(action, name, where).kind, value, f"{where}.{name}")
        for name, value in raw.items()
    }


def to_context(action: str, facts: Mapping[str, object]) -> dict[str, Any]:
    """A Cedar context for ``action``: defaults, overridden by ``facts``, in Cedar units."""
    merged = {**DEFAULTS[action], **_facts(action, facts, "facts")}
    context: dict[str, Any] = {}
    for name, value in merged.items():
        spec = FIELDS[action][name]
        context[spec.cedar] = to_cedar(spec.kind, value)
    return context


def to_cedar(kind: Kind, value: Value) -> int | bool | str:
    if isinstance(value, Decimal):  # only dollars are Decimal (_value)
        return int(value * 100) if kind == "dollars" else int(value)
    return value


def _text(raw: object, pattern: re.Pattern[str], where: str) -> str:
    if not isinstance(raw, str) or not pattern.fullmatch(raw):
        raise _bad(f"{where}: {raw!r} is not allowed", "policy.rule_value")
    return raw


def _source(raw: object, where: str) -> Source:
    if not isinstance(raw, dict) or set(raw) != {"doc", "page", "clause", "quote"}:
        raise _bad(f"{where}.source needs doc, page, clause and quote", "policy.rule_shape")
    page, quote = raw["page"], raw["quote"]
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 10_000:
        raise _bad(f"{where}.source.page must be a page number", "policy.rule_value")
    if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 2000:
        raise _bad(f"{where}.source.quote must be the clause text", "policy.rule_value")
    doc = _text(raw["doc"], _DOC, f"{where}.source.doc")
    clause = _text(str(raw["clause"]), _CLAUSE, f"{where}.source.clause")
    return Source(doc, page, clause, " ".join(quote.split()))


def _fixtures(action: str, raw: object, where: str) -> tuple[Fixture, ...]:
    if not isinstance(raw, list) or not raw:
        raise _bad(f"{where}.tests needs at least one fixture", "policy.rule_shape")
    out = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or item.get("expect") not in OUTCOMES:
            raise _bad(
                f"{where}.tests[{i}] needs expect: allow, escalate or deny", "policy.rule_shape"
            )
        rest = {k: v for k, v in item.items() if k not in ("expect", "agent")}
        agent = item.get("agent")
        if agent is not None:
            agent = _text(agent, _STRING, f"{where}.tests[{i}].agent")
        facts = _facts(action, rest, f"{where}.tests[{i}]")
        out.append(Fixture(tuple(facts.items()), item["expect"], agent))
    return tuple(out)


def _rule(raw: object, index: int) -> PolicyRule:
    where = f"rule {index}"
    if not isinstance(raw, dict) or not set(raw) >= _REQUIRED or set(raw) - _RULE_KEYS:
        raise _bad(
            f"{where}: needs {sorted(_REQUIRED)}, allows {sorted(_RULE_KEYS)}", "policy.rule_shape"
        )
    rule_id = _text(raw["id"], _ID, f"{where}.id")
    where = rule_id
    when = raw["when"]
    if not isinstance(when, dict) or when.get("action") not in FIELDS:
        action = when.get("action") if isinstance(when, dict) else None
        raise _bad(f"{where}: unknown action {action!r}", "policy.rule_action")
    action = when["action"]
    then, role = raw["then"], raw.get("approver_role")
    if then not in OUTCOMES or (then == "escalate") != (role is not None):
        raise _bad(
            f"{where}: then must be allow, escalate (with approver_role) or deny",
            "policy.rule_shape",
        )
    agent = raw.get("agent")
    return PolicyRule(
        id=rule_id,
        source=_source(raw["source"], where),
        agent=None if agent is None else _text(agent, _STRING, f"{where}.agent"),
        action=action,
        when=_conditions(action, {k: v for k, v in when.items() if k != "action"}, f"{where}.when"),
        unless=_conditions(action, raw.get("unless") or {}, f"{where}.unless"),
        then=then,
        approver_role=None if role is None else _text(role, _ID, f"{where}.approver_role"),
        tests=_fixtures(action, raw["tests"], where),
    )


def parse_rules(text: str) -> tuple[PolicyRule, ...]:
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise _bad("rules file is too large", "policy.yaml")
    try:
        raw = yaml.safe_load(text)  # safe_load: plain data only, never Python objects
    except yaml.YAMLError as exc:
        raise _bad(f"not valid YAML: {exc}", "policy.yaml") from exc
    if raw == [] or raw is None:
        raise _bad("no rules", "policy.empty")
    if not isinstance(raw, list):
        raise _bad("rules must be a YAML list", "policy.rule_shape")
    rules = tuple(_rule(item, i) for i, item in enumerate(raw, start=1))
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise _bad("rule ids must be unique", "policy.duplicate_id")
    return rules


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?|\b(\d{1,2}):(\d{2})\b", re.I)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def clause_numbers(text: str) -> set[Decimal]:
    """Numbers a clause states: amounts, counts, hours (5:00 p.m. is 17) and weekdays."""
    found: set[Decimal] = set()

    def hour(match: re.Match[str]) -> str:
        if match.group(4) is not None:
            found.add(Decimal(int(match.group(4))))
        else:
            h = int(match.group(1)) % 12
            found.add(Decimal(h + 12 if match.group(3).lower() == "p" else h))
        return " "

    rest = _TIME.sub(hour, text)
    lowered = rest.lower()
    found |= {Decimal(12) for word in ("noon",) if word in lowered}
    found |= {Decimal(i) for i, day in enumerate(_WEEKDAYS, start=1) if day in lowered}
    found |= {Decimal(n.replace(",", "")) for n in _NUMBER.findall(rest)}
    return found
