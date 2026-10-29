"""What a page shows, computed from records: pure functions, no I/O, tested directly.

Pages never invent a fact. Every sentence here is built from a record's own fields, and the
seal is built only from the console's own verification of the export (``verify_bundle``).
"""

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from common.clock import parse_ts
from policy.rules import Condition, PolicyRule
from verify.checks import Report

# A newer checkpoint waiting for cosignatures is normal while the log runs (claims.log).
PENDING_CHECKS = frozenset({"below_quorum"})
TYPE_LABELS = {
    "gate.decision": "Decision",
    "gate.verdict": "Verdict",
    "gate.result": "Tool ran",
    "assist.brief": "Approval brief",
    "assist.qa": "Audit answer",
    "policy.draft": "Policy draft",
    "policy.approve": "Policy approved",
    "learn.proposal": "Learner proposal",
    "learn.approve": "Proposal approval",
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
    return {"policy.approve": "approve", "learn.approve": "approve"}.get(str(record.get("type")))


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
