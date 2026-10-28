"""Cedar decides: allow, deny or escalate for one normalized tool call (R2: no model here).

Cedar has two effects, permit and forbid. Gate adds the third outcome with annotations (D49):
a permit tagged ``@escalate`` allows the call only after a person holding its
``@approver_role`` approves. Any forbid wins, no matching permit means deny, and any Cedar
error or ``NoDecision`` means deny, because Cedar skips a policy that errors and may then
answer Allow (R4).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import cedarpy

from common.errors import PolicyError

SCHEMA_FILE = Path(__file__).with_name("schema.cedarschema")
ANNOTATIONS = frozenset({"id", "escalate", "approver_role"})
_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ROLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Rule:
    id: str
    effect: str  # "permit" or "forbid"
    escalate: bool
    approver_role: str | None


@dataclass(frozen=True)
class Evaluation:
    outcome: Outcome
    reason: str  # policy.permit, policy.escalate, policy.forbid, policy.no_permit, gate.*
    policy_ids: tuple[str, ...] = ()
    approver_roles: tuple[str, ...] = ()

    @classmethod
    def deny(cls, reason: str, policy_ids: tuple[str, ...] = ()) -> "Evaluation":
        return cls(Outcome.DENY, reason, policy_ids)


def _refuse(message: str, code: str) -> PolicyError:
    return PolicyError(message, code=code)


def _rule(annotations: dict[str, str | None], effect: str) -> Rule:
    unknown = sorted(set(annotations) - ANNOTATIONS)
    if unknown:  # a typo such as @escalte must not turn an escalation into a plain permit
        raise _refuse(f"unknown annotation(s) {unknown}", "policy.bad_annotation")
    rule_id = annotations.get("id")
    if rule_id is None:
        raise _refuse("every policy needs an @id", "policy.missing_id")
    if not _RULE_ID.fullmatch(rule_id):
        raise _refuse(f"invalid policy id {rule_id!r}", "policy.bad_id")
    escalate = "escalate" in annotations
    role = annotations.get("approver_role")
    if escalate and annotations["escalate"] is not None:
        raise _refuse(f"{rule_id}: @escalate takes no value", "policy.bad_annotation")
    if escalate and effect != "permit":
        raise _refuse(f"{rule_id}: only a permit can escalate", "policy.escalate_on_forbid")
    if escalate and role is None:
        raise _refuse(f"{rule_id}: @escalate needs @approver_role", "policy.missing_approver_role")
    if role is not None and (not escalate or not _ROLE.fullmatch(role)):
        raise _refuse(f"{rule_id}: invalid @approver_role", "policy.bad_annotation")
    return Rule(rule_id, effect, escalate, role)


def _parse_rules(text: str) -> dict[str, Rule]:
    """Cedar's own parse of the text, keyed by Cedar's internal ids (``policy0``, ...)."""
    try:
        parsed = json.loads(cedarpy.policies_to_json_str(text))
    except ValueError as exc:
        raise _refuse(f"policy text does not parse: {exc}", "policy.syntax") from exc
    if parsed.get("templates") or parsed.get("templateLinks"):
        raise _refuse("policy templates are not supported", "policy.template")
    static: dict[str, Any] = parsed.get("staticPolicies") or {}
    if not static:
        raise _refuse("policy set is empty", "policy.empty")
    rules = {key: _rule(p.get("annotations") or {}, p["effect"]) for key, p in static.items()}
    ids = [rule.id for rule in rules.values()]
    if len(set(ids)) != len(ids):  # Cedar itself accepts repeated @id values
        raise _refuse("policy ids must be unique", "policy.duplicate_id")
    return rules


class PolicySet:
    """A validated, immutable policy set. ``digest`` is what decision records cite."""

    def __init__(self, text: str, schema: str | None) -> None:
        self._rules = _parse_rules(text)
        self._schema = None
        if schema is not None:
            try:
                self._schema = cedarpy.Schema.from_str(schema)
                result = cedarpy.validate_policies(text, self._schema)
            except ValueError as exc:
                raise _refuse(f"schema does not parse: {exc}", "policy.schema") from exc
            if not result.validation_passed:
                errors = "; ".join(str(e) for e in result.errors)
                raise _refuse(f"policies fail the schema: {errors}", "policy.schema")
        self._policies = cedarpy.PolicySet.from_str(text)
        self.digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(self._rules.values())

    def evaluate(self, agent: str, action: str, tool: str, facts: dict[str, Any]) -> Evaluation:
        request = {
            # Dict form: ids are passed as data, never spliced into Cedar syntax.
            "principal": {"type": "Agent", "id": agent},
            "action": {"type": "Action", "id": action},
            "resource": {"type": "Tool", "id": tool},
            "context": facts,
        }
        result = cedarpy.is_authorized(request, self._policies, [], schema=self._schema)
        if result.diagnostics.errors or result.decision not in (
            cedarpy.Decision.Allow, cedarpy.Decision.Deny
        ):  # fmt: skip
            return Evaluation.deny("gate.policy_error")
        matched = sorted(
            (self._rules[key] for key in result.diagnostics.reasons), key=lambda r: r.id
        )
        ids = tuple(rule.id for rule in matched)
        if result.decision is cedarpy.Decision.Deny:
            return Evaluation.deny("policy.forbid" if ids else "policy.no_permit", ids)
        roles = tuple(sorted({r.approver_role for r in matched if r.approver_role is not None}))
        if roles:
            return Evaluation(Outcome.ESCALATE, "policy.escalate", ids, roles)
        return Evaluation(Outcome.ALLOW, "policy.permit", ids)


def load_policy_set(path: Path, schema_path: Path = SCHEMA_FILE) -> PolicySet:
    try:
        text, schema = path.read_text("utf-8"), schema_path.read_text("utf-8")
    except OSError as exc:
        raise _refuse(f"cannot read policies: {exc}", "policy.unreadable") from exc
    return PolicySet(text, schema)
