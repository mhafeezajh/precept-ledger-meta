"""The auditor's read-only query, run by code over proven records (D65).

It lives on the deterministic side so that "every payment that matched" is a statement anyone
can replay: the same query over the same proven log prefix gives the same rows. Rows join each
``gate.decision`` with its ``gate.verdict``, newest first. A model only chooses the filters.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from common.clock import parse_ts

DECISION = "gate.decision"  # must match gate.records (tested); claims does not import gate
VERDICT = "gate.verdict"
BUSINESS_HOURS = range(9, 17)  # 09:00-16:59 local, Monday to Friday
MAX_ROWS = 50  # what a model is shown; the caller cuts, so the full match count is known
_CHOICES = {
    "action": {"value.transfer", "comm.external", "data.read", "any"},
    "decision": {"allow", "deny", "escalate", "any"},
    "verdict": {"approve", "reject", "expire", "none", "any"},
    "outside_business_hours": {"yes", "no", "any"},
}


@dataclass(frozen=True)
class Query:
    action: str
    decision: str
    verdict: str  # "none": no verdict recorded
    min_amount_cents: int | None
    max_amount_cents: int | None
    outside_business_hours: str
    approver: str | None
    payee_id: str | None
    since_days: int | None

    def __post_init__(self) -> None:
        for name, allowed in _CHOICES.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"unknown {name} {getattr(self, name)!r}")


def outside_hours(facts: dict[str, Any]) -> bool | None:
    hour, weekday = facts.get("local_hour"), facts.get("local_weekday")
    if not isinstance(hour, int) or not isinstance(weekday, int):
        return None
    return weekday > 5 or hour not in BUSINESS_HOURS


def build_rows(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per ``gate.decision``, joined with its ``gate.verdict``, newest first."""
    verdicts = {r["body"].get("decision_record_id"): r for r in records if r.get("type") == VERDICT}
    rows = []
    for r in records:
        if r.get("type") != DECISION:
            continue
        body, facts = r["body"], r["body"].get("resource") or {}
        verdict = verdicts.get(r["record_id"])
        rows.append({
            "decision_record_id": r["record_id"],
            "ts": r["ts"],
            "agent": body.get("agent"),
            "tool": body.get("tool"),
            "action": body.get("action"),
            "decision": body.get("decision"),
            "amount_cents": facts.get("amount_cents"),
            "payee_id": facts.get("payee_id"),
            "payee_new": facts.get("payee_new"),
            "local_hour": facts.get("local_hour"),
            "local_weekday": facts.get("local_weekday"),
            "outside_business_hours": outside_hours(facts),
            "verdict_record_id": None if verdict is None else verdict["record_id"],
            "verdict": None if verdict is None else verdict["body"].get("verdict"),
            "approver": None if verdict is None else verdict["body"].get("approver"),
        })  # fmt: skip
    return sorted(rows, key=lambda row: row["ts"], reverse=True)


def _matches(row: dict[str, Any], q: Query, since: datetime | None) -> bool:
    amount = row["amount_cents"]
    wanted_hours = {"yes": True, "no": False}.get(q.outside_business_hours)
    checks = [
        q.action == "any" or row["action"] == q.action,
        q.decision == "any" or row["decision"] == q.decision,
        q.verdict == "any" or row["verdict"] == (None if q.verdict == "none" else q.verdict),
        q.min_amount_cents is None or (isinstance(amount, int) and amount >= q.min_amount_cents),
        q.max_amount_cents is None or (isinstance(amount, int) and amount <= q.max_amount_cents),
        wanted_hours is None or row["outside_business_hours"] is wanted_hours,
        q.approver is None or row["approver"] == q.approver,
        q.payee_id is None or row["payee_id"] == q.payee_id,
        since is None or parse_ts(row["ts"]) >= since,
    ]
    return all(checks)


def run_query(
    query: Query, records: Sequence[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Every matching row, newest first; not cut to ``MAX_ROWS``."""
    since = None if query.since_days is None else now - timedelta(days=query.since_days)
    return [row for row in build_rows(records) if _matches(row, query, since)]
