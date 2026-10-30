"""What a page shows, computed from records: pure functions, no I/O, tested directly.

Pages never invent a fact. Every sentence here is built from a record's own fields, and the
seal is built only from the console's own verification of the export (``verify_bundle``).
"""

import base64
import difflib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.clock import parse_ts
from common.errors import PolicyError
from policy.rules import Condition, PolicyRule, parse_rules
from verify.checks import Report

# A newer checkpoint waiting for cosignatures is normal while the log runs (claims.log).
PENDING_CHECKS = frozenset({"below_quorum"})
TYPE_LABELS = {
    "gate.decision": "Decision",
    "gate.verdict": "Verdict",
    "gate.result": "Tool ran",
    "gate.delegation.issue": "Delegation issued",
    "gate.delegation.revoke": "Delegation revoked",
    "assist.brief": "Approval brief",
    "assist.qa": "Audit answer",
    "policy.draft": "Policy draft",
    "policy.approve": "Policy approved",
    "learn.proposal": "Learner proposal",
    "learn.approve": "Proposal approval",
    "improve.proposal": "Prompt change",
    "improve.approve": "Prompt change approval",
}
OUTCOMES = {  # a value in a record -> the tone a page gives it
    "allow": "allow", "approve": "allow", "ok": "allow", "review": "escalate",
    "escalate": "escalate", "deny": "deny", "reject": "deny", "expire": "deny",
    "error": "deny", "rejected": "deny", "blocked": "deny",
}  # fmt: skip


def dollars(cents: object) -> str:
    if not isinstance(cents, int) or isinstance(cents, bool):
        return ""
    return f"${cents // 100:,}.{cents % 100:02d}"


def local_time(ts: str, zone: ZoneInfo) -> str:
    """``Wed 28 Oct, 17:04`` in the bank's time zone."""
    moment: datetime = parse_ts(ts).astimezone(zone)
    return f"{moment:%a} {moment.day} {moment:%b}, {moment:%H:%M}"


def tone(value: object) -> str:
    return OUTCOMES.get(str(value), "neutral")


def outcome(record: dict[str, Any]) -> str | None:
    """The record's one outcome word, for its badge, or None."""
    body = record.get("body") or {}
    for key in ("decision", "verdict", "outcome", "state"):
        if isinstance(body.get(key), str):
            return str(body[key])
    approvals = {
        "policy.approve": "approve",
        "learn.approve": "approve",
        "improve.approve": "approve",
    }
    return approvals.get(str(record.get("type")))


def summary(record: dict[str, Any]) -> str:
    """One plain sentence about the record, from its own fields."""
    body, kind = record.get("body") or {}, record.get("type")
    facts = body.get("resource") or {}
    if kind == "gate.decision":
        what = f"{body.get('agent')} called {body.get('tool')}"
        if facts.get("amount_cents") is not None:
            what += f": {dollars(facts['amount_cents'])} to {facts.get('payee_id')}"
            what += " (new payee)" if facts.get("payee_new") else ""
        return what
    if kind == "gate.verdict":
        who = body.get("approver") or "nobody, before the deadline"
        return f"{body.get('tool')} by {body.get('agent')}: {body.get('verdict')} by {who}"
    if kind == "gate.result":
        return f"{body.get('tool')} ran for {body.get('agent')}"
    if kind == "assist.brief":
        return str(body.get("headline", "Approval brief"))
    if kind == "assist.qa":
        return str(body.get("question", "Audit question"))
    if kind == "gate.delegation.issue":
        return f"{body.get('principal_id')} delegated to {body.get('agent_id')}"
    if kind == "gate.delegation.revoke":
        return f"Credential {body.get('credential_id')} revoked"
    if kind == "policy.draft":
        rules = str(body.get("rules_yaml", "")).count("\n- id:") + 1
        by = body.get("author") or "drafter"
        return f"{rules} rules drafted by {by}, {len(body.get('findings') or [])} findings"
    if kind == "policy.approve":
        return f"{len(body.get('rules') or [])} rules approved by {body.get('approver')}"
    if kind == "learn.proposal":
        return f"{str(body.get('direction', '')).capitalize()}: {body.get('title')}"
    if kind == "learn.approve":
        count = f"{body.get('approval')} of {body.get('approvals_needed')}"
        return f"Approval {count} by {body.get('approver')}"
    if kind == "improve.proposal":
        return (
            f"Learner prompt {body.get('version')}, from {(body.get('base') or {}).get('version')}"
        )
    if kind == "improve.approve":
        count = f"{body.get('approval')} of {body.get('approvals_needed')}"
        return f"Prompt change approval {count} by {body.get('approver')}"
    return str(kind)


@dataclass(frozen=True)
class Seal:
    """The console's verdict on the whole log, for the proof seal."""

    state: str  # "proven", "pending" or "broken"
    title: str
    detail: str
    seq: int | None = None  # the first broken record


def root_text(root: bytes) -> str:
    return base64.b64encode(root).decode()[:12] + "…"


def seal(report: Report) -> Seal:
    serious = [f for f in report.findings if f.check not in PENDING_CHECKS]
    if serious:
        first = min(serious, key=lambda f: (f.seq is None, f.seq or 0))
        where = f"record {first.seq}" if first.seq is not None else "the log"
        return Seal("broken", f"Verification failed at {where}",
                    f"{first.check.replace('_', ' ')}: {first.detail}", first.seq)  # fmt: skip
    head = report.final or report.latest
    if head is None:
        return Seal("pending", "Not yet checkpointed", f"{report.records} records, no checkpoint")
    if report.quorum is not None and report.final is None:
        waiting = f"Checkpoint {head.size} has fewer than {report.quorum} cosignatures"
        return Seal("pending", "Waiting for witnesses", waiting)
    witnesses = ", ".join(report.final_cosigners)
    detail = f"Root {root_text(head.root)}"
    if witnesses:
        detail = f"Cosigned by {witnesses}. {detail}"
    if report.records > head.size:
        detail += f". {report.records - head.size} newer records wait for the next checkpoint"
    return Seal("proven", f"Proven through record {head.size}", detail)


_WEEKDAYS = ("", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_FIELDS = {"amount": "amount", "currency": "currency", "payee_id": "payee", "local_hour": "hour",
           "local_weekday": "weekday", "recipients": "recipients", "data_class": "data",
           "external_recipients": "recipients outside the bank"}  # fmt: skip
_OPS = {"eq": "is", "ne": "is not", "gt": "over", "gte": "at least", "lt": "under",
        "lte": "up to"}  # fmt: skip


def condition(c: Condition) -> str:
    """``amount over $5,000``, ``known payee``, ``weekday up to Friday``."""
    if c.field == "payee_new":
        new = bool(c.value) == (c.op == "eq")
        return "new payee" if new else "known payee"
    value: object = c.value
    if c.field == "amount":
        value = f"${c.value:,}"
    elif c.field == "local_weekday" and isinstance(c.value, int) and 1 <= c.value <= 7:
        value = _WEEKDAYS[c.value]
    elif c.field == "local_hour":
        value = f"{c.value}:00"
    return f"{_FIELDS.get(c.field, c.field)} {_OPS[c.op]} {value}"


def conditions(rule: PolicyRule) -> tuple[list[str], list[str]]:
    """The rule's when and unless conditions in words."""
    return [condition(c) for c in rule.when], [condition(c) for c in rule.unless]


def in_force(records: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[PolicyRule]]:
    """The latest policy approval and its rules: what Gate enforces, since Gate restores the
    latest approval at startup (D75). Read from the verified ledger, so anyone may see it."""
    approval = next((r for r in records if r.get("type") == "policy.approve"), None)
    if approval is None:
        return None, []
    draft_id = approval["body"].get("draft_record_id")
    draft = next((r for r in records if r.get("record_id") == draft_id), None)
    try:
        return approval, list(parse_rules(str((draft or {}).get("body", {}).get("rules_yaml", ""))))
    except PolicyError:  # shown as no rules, never as rules that were not approved (R4)
        return approval, []


# --- the Approvals app: what waits for a signature, and what a person already signed --------

DECISION_TYPES = ("gate.verdict", "policy.approve", "learn.approve", "improve.approve")


def proposal_approvals(records: Sequence[dict[str, Any]]) -> Counter[str]:
    """Approvals given so far, by learner proposal."""
    return Counter(
        str(r["body"].get("proposal_record_id"))
        for r in records
        if r.get("type") == "learn.approve"
    )


def proposal_status(
    proposal: dict[str, Any], approvals: Counter[str], approval: dict[str, Any] | None
) -> str:
    """waiting, approved, out_of_date (drafted against a policy since replaced: approving it
    would fail Gate's stale-base check, D81), rejected or blocked."""
    body = proposal["body"]
    if body.get("state") != "review":
        return str(body.get("state"))
    if approvals.get(proposal["record_id"], 0) >= body.get("approvals_needed", 1):
        return "approved"
    base = (body.get("base") or {}).get("policy_record_id")
    current = approval["record_id"] if approval else None
    return "waiting" if base == current else "out_of_date"


def drafts_to_approve(
    records: Sequence[dict[str, Any]], approval: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Policy drafts a compliance officer can approve now: newer than the rules in force, not
    revised since, and without findings (Gate refuses those; they are fixed in the Console).
    ``records`` is newest first."""
    newer: list[dict[str, Any]] = []
    for record in records:
        if approval is not None and record.get("record_id") == approval["record_id"]:
            break
        if record.get("type") == "policy.draft":
            newer.append(record)
    revised = {d["body"].get("revises") for d in newer}
    return [d for d in newer if d["record_id"] not in revised and not d["body"].get("findings")]


def decided_by(records: Sequence[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Verdicts and approvals signed by ``name``, newest first."""
    return [
        r for r in records
        if r.get("type") in DECISION_TYPES and (r.get("body") or {}).get("approver") == name
    ]  # fmt: skip


# --- the improver (D95): the learner's prompt --------------------------------------------------


def improvement_approvals(records: Sequence[dict[str, Any]]) -> Counter[str]:
    return Counter(
        str(r["body"].get("proposal_record_id"))
        for r in records
        if r.get("type") == "improve.approve"
    )


def improvement_in_force(records: Sequence[dict[str, Any]]) -> str | None:
    """The fully approved prompt change the learner uses now, or None for the prompt file.
    ``records`` is newest first. For display: the learner itself verifies it on every run."""
    final = next(
        (r for r in records if r.get("type") == "improve.approve"
         and r["body"].get("approval") == r["body"].get("approvals_needed")),
        None,
    )  # fmt: skip
    return None if final is None else str(final["body"].get("proposal_record_id"))


def improvement_status(
    proposal: dict[str, Any], approvals: Counter[str], in_force: str | None
) -> str:
    """waiting, in_force, replaced (approved, since replaced by a newer change), out_of_date
    (scored against a prompt since replaced: Gate refuses it as stale) or blocked."""
    body = proposal["body"]
    if body.get("state") != "review":
        return str(body.get("state"))
    if approvals.get(proposal["record_id"], 0) >= body.get("approvals_needed", 2):
        return "in_force" if proposal["record_id"] == in_force else "replaced"
    base = (body.get("base") or {}).get("record_id")
    return "waiting" if base == in_force else "out_of_date"


def prompt_diff(old: str, new: str) -> list[tuple[str, str]]:
    """The change as (kind, line) pairs, kind one of add, del, hunk, same."""
    out = []
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1)
    for line in list(lines)[2:]:  # skip the --- and +++ header lines
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "same")
        out.append((kind, line if kind == "hunk" else line[1:]))
    return out
