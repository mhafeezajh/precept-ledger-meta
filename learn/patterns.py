"""Patterns in the ledger's own history, counted by SQL (ARCHITECTURE flow E steps 1 and 2, D80).

The records come from ``claims.log.ProvenLog``: the stream's export, verified up to its latest
final checkpoint. They are loaded as they are into an in-memory SQLite database (``decision``
and ``verdict`` tables), and every number a proposal states comes from the SQL below, which is
fixed text in this file. No model sees the data before these counts exist (R2, skill §5).

1. ``reviewed``: escalated decisions in the window joined with their verdicts.
2. Fatigue filter: an approval decided in under ``fast_seconds``, or by an approver who
   approved every one of at least ``min_sample`` verdicts, is dropped as possible
   rubber-stamping. Rejections are never dropped.
3. Patterns: what is left, grouped by agent, action, payee status and business hours.
   Fewer than ``min_sample`` decisions: skipped. All approved: a loosening candidate, its
   amounts bounded by the bands the approved payments fall in. Half or more rejected or
   expired: a tightening candidate. Otherwise skipped.
"""

import dataclasses
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.clock import parse_ts

DECISION = "gate.decision"  # gate.records names, checked by a test; learn reads, never writes
VERDICT = "gate.verdict"
MIN_SAMPLE = 20
FAST_SECONDS = 5
TIGHTEN_SHARE = 0.5  # rejected or expired, of the decisions left after the filter
BUSINESS = {"local_hour": {"gte": 9, "lt": 17}, "local_weekday": {"lte": 5}}  # 09:00-16:59 Mon-Fri

_TABLES = """
CREATE TABLE decision (
  record_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, agent TEXT, tool TEXT, action TEXT,
  outcome TEXT, reason TEXT, policy_ids TEXT, amount_cents INTEGER, payee_id TEXT,
  payee_new INTEGER, local_hour INTEGER, local_weekday INTEGER);
CREATE TABLE verdict (
  record_id TEXT PRIMARY KEY, decision_record_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  verdict TEXT, approver TEXT);
"""
# Amount bands in cents: (0, 5k], (5k, 15k], (15k, 50k], (50k, 250k]. Above $250,000 has no
# band, so no loosening can reach it.
_FLAGGED = """
WITH reviewed AS (
  SELECT d.record_id AS decision_record_id, v.record_id AS verdict_record_id, d.ts_ms,
    d.agent, d.action, d.policy_ids, d.amount_cents, d.payee_id, d.payee_new, d.local_hour,
    d.local_weekday, v.verdict, v.approver, v.ts_ms - d.ts_ms AS verdict_ms,
    CASE WHEN d.local_weekday BETWEEN 1 AND 5 AND d.local_hour BETWEEN 9 AND 16
      THEN 1 ELSE 0 END AS business_hours,
    CASE WHEN d.amount_cents <= 500000 THEN 0 WHEN d.amount_cents <= 1500000 THEN 500000
      WHEN d.amount_cents <= 5000000 THEN 1500000 WHEN d.amount_cents <= 25000000 THEN 5000000
      END AS band_floor,
    CASE WHEN d.amount_cents <= 500000 THEN 500000 WHEN d.amount_cents <= 1500000 THEN 1500000
      WHEN d.amount_cents <= 5000000 THEN 5000000 WHEN d.amount_cents <= 25000000 THEN 25000000
      END AS band_ceiling
  FROM decision d JOIN verdict v ON v.decision_record_id = d.record_id
  WHERE d.outcome = 'escalate' AND d.ts_ms >= :since),
approvers AS (
  SELECT approver, COUNT(*) AS verdicts, SUM(verdict = 'approve') AS approved
  FROM reviewed WHERE approver IS NOT NULL GROUP BY approver),
flagged AS (
  SELECT r.*,
    (r.verdict = 'approve' AND r.verdict_ms < :fast_ms) AS fast,
    (r.verdict = 'approve' AND r.approver IN (
      SELECT approver FROM approvers WHERE verdicts >= :min_sample AND approved = verdicts))
      AS stamped
  FROM reviewed r)
"""
APPROVERS_SQL = """
WITH reviewed AS (
  SELECT v.verdict, v.approver FROM decision d JOIN verdict v ON v.decision_record_id = d.record_id
  WHERE d.outcome = 'escalate' AND d.ts_ms >= :since)
SELECT approver, COUNT(*) AS verdicts, SUM(verdict = 'approve') AS approved
FROM reviewed WHERE approver IS NOT NULL GROUP BY approver ORDER BY approver
"""
PATTERNS_SQL = (
    _FLAGGED
    + """
SELECT agent, action, payee_new, business_hours,
  SUM(NOT (fast OR stamped)) AS decisions,
  SUM(NOT (fast OR stamped) AND verdict = 'approve') AS approved,
  SUM(verdict = 'reject') AS rejected,
  SUM(verdict = 'expire') AS expired,
  SUM(fast) AS fast_dropped,
  SUM(stamped AND NOT fast) AS stamped_dropped,
  MIN(CASE WHEN NOT (fast OR stamped) THEN band_floor END) AS floor_cents,
  MAX(CASE WHEN NOT (fast OR stamped) THEN band_ceiling END) AS ceiling_cents,
  SUM(NOT (fast OR stamped) AND amount_cents IS NOT NULL AND band_ceiling IS NULL) AS unbanded
FROM flagged
GROUP BY agent, action, payee_new, business_hours
ORDER BY agent, action, payee_new, business_hours
"""
)
ROWS_SQL = (
    _FLAGGED
    + """
SELECT decision_record_id, verdict_record_id, amount_cents, payee_id, payee_new, local_hour,
  local_weekday, verdict, approver, verdict_ms, policy_ids, fast, stamped
FROM flagged
WHERE agent = :agent AND action = :action AND payee_new IS :payee_new
  AND business_hours = :business_hours
ORDER BY ts_ms, decision_record_id
"""
)
COUNT_NAMES = ("decisions", "approved", "rejected", "expired", "fast_dropped", "stamped_dropped")


@dataclass(frozen=True)
class Scope:
    """Which requests a pattern covers: the proposal's rules may change nothing outside it."""

    agent: str
    action: str
    payee_new: bool | None  # None for actions without a payee
    business_hours: bool
    floor_cents: int | None  # loosening only: amounts over this and up to the ceiling
    ceiling_cents: int | None

    def when(self) -> dict[str, Any]:
        """The scope as rule conditions (policy/rules.py format, amounts in dollars)."""
        out: dict[str, Any] = {}
        if self.payee_new is not None:
            out["payee_new"] = self.payee_new
        amount = {}
        if self.floor_cents:
            amount["gt"] = self.floor_cents // 100
        if self.ceiling_cents is not None:
            amount["lte"] = self.ceiling_cents // 100
        if amount:
            out["amount"] = amount
        return {**out, **BUSINESS} if self.business_hours else out

    def unless(self) -> dict[str, Any]:
        return {} if self.business_hours else dict(BUSINESS)

    def statement(self) -> str:
        """The scope in words. Proposed rules cite it, and may use only the numbers it states."""
        subject = {
            "value.transfer": "Payments to " + ("new payees" if self.payee_new else "known payees"),
            "comm.external": "Email sent outside the bank",
            "data.read": "Data reads",
        }.get(self.action, self.action)
        if self.ceiling_cents is not None:
            low = f" over ${self.floor_cents // 100:,} and" if self.floor_cents else ""
            subject += f"{low} up to ${self.ceiling_cents // 100:,}"
        if self.business_hours:
            return f"{subject} by {self.agent}, Monday to Friday from 9:00 to 17:00."
        hours = "outside business hours (Monday to Friday, 9:00 to 17:00)"
        return f"{subject} by {self.agent} {hours}."

    def numbers(self) -> set[int]:
        """Numbers the statement states, which a reason may repeat."""
        found = {1, 5, 9, 17}
        found |= {c // 100 for c in (self.floor_cents, self.ceiling_cents) if c}
        return found

    def key(self) -> str:
        payee = {None: "any", True: "new", False: "known"}[self.payee_new]
        hours = "business" if self.business_hours else "after-hours"
        return f"{self.agent}/{self.action}/{payee}/{hours}"


@dataclass(frozen=True)
class Pattern:
    scope: Scope
    direction: str | None  # "loosen", "tighten", or None when skipped
    skipped: str | None  # why there is no proposal
    counts: dict[str, int]  # COUNT_NAMES, from PATTERNS_SQL
    rows: tuple[dict[str, Any], ...]  # the decisions that count, from ROWS_SQL
    dropped: tuple[dict[str, Any], ...]  # approvals the fatigue filter set aside
    stamped_approvers: tuple[str, ...]

    @property
    def record_ids(self) -> set[str]:
        return {i for r in self.rows for i in (r["decision_record_id"], r["verdict_record_id"])}

    def policy_ids(self) -> list[str]:
        return sorted({p for r in self.rows for p in r["policy_ids"]})


@dataclass(frozen=True)
class Window:
    since: datetime
    min_sample: int = MIN_SAMPLE
    fast_seconds: int = FAST_SECONDS


def _ms(ts: str) -> int:
    return int(parse_ts(ts).timestamp() * 1000)


def _flag(value: object) -> int | None:
    return None if not isinstance(value, bool) else int(value)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def load(records: Sequence[dict[str, Any]]) -> sqlite3.Connection:
    """The proven records as two tables; values are copied as recorded, never computed."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_TABLES)
    for r in records:
        body = r.get("body") or {}
        if r.get("type") == DECISION:
            facts = body.get("resource") or {}
            db.execute(
                "INSERT INTO decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["record_id"], _ms(r["ts"]), _str(body.get("agent")), _str(body.get("tool")),
                 _str(body.get("action")), _str(body.get("decision")), _str(body.get("reason")),
                 json.dumps(body.get("policy_ids") or []), _int(facts.get("amount_cents")),
                 _str(facts.get("payee_id")), _flag(facts.get("payee_new")),
                 _int(facts.get("local_hour")), _int(facts.get("local_weekday"))),
            )  # fmt: skip
        elif r.get("type") == VERDICT and isinstance(body.get("decision_record_id"), str):
            db.execute(
                "INSERT INTO verdict VALUES (?,?,?,?,?)",
                (r["record_id"], body["decision_record_id"], _ms(r["ts"]),
                 _str(body.get("verdict")), _str(body.get("approver"))),
            )  # fmt: skip
    return db


def _params(s: Window) -> dict[str, Any]:
    since = int(s.since.timestamp() * 1000)
    return {"since": since, "fast_ms": s.fast_seconds * 1000, "min_sample": s.min_sample}


def approver_stats(db: sqlite3.Connection, s: Window) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(APPROVERS_SQL, {"since": _params(s)["since"]})]


def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["policy_ids"] = json.loads(out["policy_ids"])
    out["payee_new"] = None if out["payee_new"] is None else bool(out["payee_new"])
    out["verdict_seconds"] = round(out.pop("verdict_ms") / 1000, 3)
    return out


def _direction(
    counts: dict[str, int], scope: Scope, unbanded: int, s: Window
) -> tuple[str | None, str | None]:
    """(direction, None) for a candidate, or (None, why it is skipped)."""
    n = counts["decisions"]
    if n < s.min_sample:
        return None, f"{n} decisions after the fatigue filter; at least {s.min_sample} needed"
    not_approved = counts["rejected"] + counts["expired"]
    if not_approved == 0:
        if scope.action == "value.transfer" and (unbanded or scope.ceiling_cents is None):
            return None, "amounts above $250,000 are never loosened"
        return "loosen", None
    if not_approved >= n * TIGHTEN_SHARE:
        return "tighten", None
    return None, f"{not_approved} of {n} not approved: neither all approved nor half rejected"


def find_patterns(db: sqlite3.Connection, s: Window) -> list[Pattern]:
    params = _params(s)
    stamped = tuple(
        a["approver"]
        for a in approver_stats(db, s)
        if a["verdicts"] >= s.min_sample and a["approved"] == a["verdicts"]
    )
    patterns = []
    for g in db.execute(PATTERNS_SQL, params).fetchall():
        counts = {name: int(g[name] or 0) for name in COUNT_NAMES}
        payee_new = None if g["payee_new"] is None else bool(g["payee_new"])
        base = Scope(g["agent"], g["action"], payee_new, bool(g["business_hours"]), None, None)
        bounded = _bounded(base, g)
        direction, skipped = _direction(counts, bounded, int(g["unbanded"] or 0), s)
        scope = bounded if direction == "loosen" else base
        where = {"agent": g["agent"], "action": g["action"], "payee_new": g["payee_new"],
                 "business_hours": g["business_hours"]}  # fmt: skip
        rows = [_row(r) for r in db.execute(ROWS_SQL, {**params, **where})]
        kept = tuple(r for r in rows if not (r["fast"] or r["stamped"]))
        dropped = tuple(r for r in rows if r["fast"] or r["stamped"])
        patterns.append(Pattern(scope, direction, skipped, counts, kept, dropped, stamped))
    return patterns


def _bounded(scope: Scope, group: sqlite3.Row) -> Scope:
    """A payment scope bounded by the amount bands of the decisions that count."""
    if scope.action != "value.transfer":
        return scope
    return dataclasses.replace(
        scope, floor_cents=group["floor_cents"], ceiling_cents=group["ceiling_cents"]
    )
