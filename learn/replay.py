"""Replay the past window under a proposed rule set (ARCHITECTURE flow E step 5, R2: Cedar only).

Every ``gate.decision`` in the window recorded the exact Cedar context it was decided on
(``body.resource``). Replay evaluates that context twice, under the active set and under the
proposed one, and reports what changes. It is deterministic: the same records and sets always
give the same report.

A proposal is blocked, not just flagged, when:

- a loosening would allow any past request a person rejected or let expire;
- a loosening makes anything stricter, or a tightening makes anything looser;
- it changes nothing in the window;
- any change falls outside the pattern's scope: a past decision, or one of the active rules'
  own fixtures, whose outcome changes for a request the pattern did not cover.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from common.clock import parse_ts
from gate.decide import SCHEMA_FILE, PolicySet
from learn.patterns import DECISION, VERDICT, Scope
from policy.checks import FIXTURE_AGENT
from policy.compiler import compile_rule
from policy.rules import PolicyRule, rules_from_data, to_context

NOT_APPROVED = ("reject", "expire")


@dataclass(frozen=True)
class Change:
    decision_record_id: str
    before: str
    after: str
    verdict: str | None
    in_scope: bool


@dataclass(frozen=True)
class Replay:
    replayed: int  # decisions in the window with a recorded context
    not_replayable: int  # refused before Cedar (no context recorded)
    changes: tuple[Change, ...]
    fixture_drift: tuple[str, ...]  # active fixtures that change outside the scope
    blocked: tuple[str, ...] = field(default=())

    def count(self, before: Sequence[str], after: Sequence[str]) -> int:
        return sum(1 for c in self.changes if c.before in before and c.after in after)

    def counts(self) -> dict[str, int]:
        return {
            "escalations_removed": self.count(["escalate"], ["allow"]),
            "new_escalations": self.count(["allow"], ["escalate"]),
            "new_denials": self.count(["allow", "escalate"], ["deny"]),
            "denials_lifted": self.count(["deny"], ["allow", "escalate"]),
            "rejections_newly_allowed": sum(
                1 for c in self.changes if c.verdict in NOT_APPROVED and c.after == "allow"
            ),
            "approvals_now_denied": sum(
                1 for c in self.changes if c.verdict == "approve" and c.after == "deny"
            ),
            "outside_scope": sum(1 for c in self.changes if not c.in_scope),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "not_replayable": self.not_replayable,
            **self.counts(),
            "changed": [c.__dict__ for c in self.changes],
            "fixture_drift": list(self.fixture_drift),
            "blocked": list(self.blocked),
        }


def scope_policies(scope: Scope) -> PolicySet:
    """The scope as a one-rule set: it allows exactly the requests the pattern covers."""
    data = {
        "id": "scope",
        "source": {"doc": "scope", "page": 1, "clause": "scope", "quote": scope.statement()},
        "agent": scope.agent,
        "when": {"action": scope.action, **scope.when()},
        "unless": scope.unless(),
        "then": "allow",
        "tests": [{"expect": "allow"}],
    }
    if not data["unless"]:
        del data["unless"]
    (rule,) = rules_from_data([data])
    return PolicySet(compile_rule(rule), SCHEMA_FILE.read_text("utf-8"))


def _decisions(
    records: Sequence[dict[str, Any]], since: datetime
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    verdicts = {
        str(r["body"].get("decision_record_id")): str(r["body"].get("verdict"))
        for r in records
        if r.get("type") == VERDICT
    }
    window = [r for r in records if r.get("type") == DECISION and parse_ts(r["ts"]) >= since]
    return window, verdicts


def _drift(
    rules: Sequence[PolicyRule], before: PolicySet, after: PolicySet, scope: PolicySet
) -> list[str]:
    found = []
    for rule in rules:
        for i, fixture in enumerate(rule.tests, start=1):
            facts = to_context(rule.action, dict(fixture.facts))
            agent = fixture.agent or rule.agent or FIXTURE_AGENT
            old = before.evaluate(agent, rule.action, "fixture", facts).outcome
            new = after.evaluate(agent, rule.action, "fixture", facts).outcome
            covered = scope.evaluate(agent, rule.action, "fixture", facts).outcome == "allow"
            if old != new and not covered:
                found.append(f"{rule.id} fixture {i}: {old} becomes {new}, outside the scope")
    return found


def _blocked(direction: str, replay: Replay) -> list[str]:
    counts, reasons = replay.counts(), []
    looser = counts["escalations_removed"] + counts["denials_lifted"]
    stricter = counts["new_escalations"] + counts["new_denials"]
    if direction == "loosen":
        if counts["rejections_newly_allowed"]:
            n = counts["rejections_newly_allowed"]
            reasons.append(f"it would allow {n} past request(s) a person rejected or let expire")
        if stricter:
            reasons.append(f"a loosening that makes {stricter} past decision(s) stricter")
    elif looser:
        reasons.append(f"a tightening that makes {looser} past decision(s) looser")
    if not replay.changes:
        reasons.append("it changes no decision in the window")
    if counts["outside_scope"]:
        reasons.append(f"{counts['outside_scope']} changed decision(s) are outside the pattern")
    if replay.fixture_drift:
        reasons.append(f"{len(replay.fixture_drift)} active fixture(s) change outside the pattern")
    return reasons


def replay(
    records: Sequence[dict[str, Any]],
    since: datetime,
    *,
    base_rules: Sequence[PolicyRule],
    before: PolicySet,
    after: PolicySet,
    scope: Scope,
    direction: str,
) -> Replay:
    window, verdicts = _decisions(records, since)
    covered = scope_policies(scope)
    changes, replayed = [], 0
    for r in window:
        body = r["body"]
        facts, action = body.get("resource") or {}, body.get("action")
        if not facts or not isinstance(action, str):
            continue
        replayed += 1
        args = (str(body.get("agent")), action, str(body.get("tool")), facts)
        old, new = before.evaluate(*args).outcome, after.evaluate(*args).outcome
        if old != new:
            in_scope = covered.evaluate(*args).outcome == "allow"
            verdict = verdicts.get(r["record_id"])
            changes.append(Change(r["record_id"], old.value, new.value, verdict, in_scope))
    drift = _drift(base_rules, before, after, covered)
    result = Replay(replayed, len(window) - replayed, tuple(changes), tuple(drift))
    return Replay(
        result.replayed, result.not_replayable, result.changes, result.fixture_drift,
        tuple(_blocked(direction, result)),
    )  # fmt: skip
