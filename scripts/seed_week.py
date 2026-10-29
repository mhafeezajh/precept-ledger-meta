"""Seed one simulated week of Harbor Federal decisions into the running ledger (beats 3 and 5).

The plan is deterministic (a fixed seed) and is played through the real Gate code: the
normalizer, Cedar, signed decision and verdict records, receipts checked. Only the clock is
simulated, so each record's ``ts``, local hour and weekday describe the simulated moment, while
its receipt's ``received_at`` is the real time it reached the ledger. Records are therefore
backdated by design, and ``pl-verify --tsa-roots`` reports them as ``late_timestamp`` when
timestamps are on: the system catching backdating, as it should (D76).

Patterns and counts (``COUNTS``), chosen for the demo beats:

- ``routine_approved`` 38 and ``routine_rubber_stamp`` 6: known payees, $5,000.01-$14,999.99,
  business hours, escalated by pay-over-5k-01 and all approved; the six were approved in under
  five seconds, which the learner's fatigue filter drops. Beat 5: "38 routine escalations".
- ``new_payee_after_hours`` 20: new payees outside business hours, 6 approved (3 over $10,000)
  and 14 rejected. Beat 5's tightening (the learner needs at least 20 decisions in a pattern).
- ``known_after_hours_big`` 2: known payees, over $10,000, outside business hours, approved.
  With the three above, beat 3's question matches five payments.
- ``new_payee_daytime`` 6 approved; ``small`` 70 allowed (up to $5,000, known payees);
  ``over_cap`` 2 and ``foreign_currency`` 2 denied; ``email_internal`` 10 allowed,
  ``email_external`` 5 escalated and approved; ``vendor_lookup`` 8 allowed.

Run against ``make serve`` (witnesses on): ``make seed-week`` (``ARGS="--dry-run"`` prints the
plan's counts only). Uses ``gate-1.pem`` from KEYS_DIR and the active approved policy set.
"""

import argparse
import asyncio
import heapq
import random
import sys
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.config import load_settings  # noqa: E402  # path set up above for a plain script
from gate.core import Approver, Gate  # noqa: E402
from gate.decide import load_policy_set  # noqa: E402
from gate.escalations import MemoryEscalationStore  # noqa: E402
from gate.ledger_client import HttpLedgerClient  # noqa: E402
from gate.normalizer import Normalizer, StaticPayees  # noqa: E402
from policy.service import restore_active  # noqa: E402

NEW_YORK = ZoneInfo("America/New_York")
AGENT = "payments-ops"
SUPERVISORS = ("j.ortiz", "m.lee")
SUPERVISOR_ROLES = frozenset({"payments-supervisor"})
KNOWN_PAYEES = ("acme-paper", "harbor-cleaning", "bayside-utilities", "keystone-it",
                "pier-logistics")  # fmt: skip  # deploy/demo/payees.json
NEW_PAYEES = ("northwind-supply", "coastal-freight", "granite-ledger-co", "tidewater-media",
              "summit-staffing", "lakeside-print", "redwood-analytics", "ironbridge-supply",
              "bluefin-catering", "maple-street-legal", "harborview-hvac", "copperline-it",
              "seabright-events", "oakmont-security", "pinecrest-media", "riverstone-labs",
              "westgate-movers", "silverline-print", "foxglove-design",
              "anchorpoint-it")  # fmt: skip  # fictional
COUNTS = Counter({
    "routine_approved": 38, "routine_rubber_stamp": 6, "new_payee_after_hours": 20,
    "known_after_hours_big": 2, "new_payee_daytime": 6, "small": 70, "over_cap": 2,
    "foreign_currency": 2, "email_internal": 10, "email_external": 5, "vendor_lookup": 8,
})  # fmt: skip
# new_payee_after_hours: (dollars, verdict); exactly three approvals are over $10,000.
AFTER_HOURS_NEW = (
    ("48000.00", "approve"), ("12500.00", "approve"), ("23750.00", "approve"),
    ("2400.00", "approve"), ("6800.00", "approve"), ("950.00", "approve"),
    ("31000.00", "reject"), ("8800.00", "reject"), ("15200.00", "reject"), ("4100.00", "reject"),
    ("57000.00", "reject"), ("2950.00", "reject"), ("19900.00", "reject"), ("7300.00", "reject"),
    ("11400.00", "reject"), ("3650.00", "reject"), ("27800.00", "reject"), ("1875.00", "reject"),
    ("9420.00", "reject"), ("16300.00", "reject"),
)  # fmt: skip
SEED = 20261028


@dataclass(frozen=True)
class Verdict:
    after: timedelta  # time from decision to verdict
    approver: str
    verdict: str


@dataclass(frozen=True)
class Event:
    at: datetime  # UTC
    pattern: str
    tool: str
    arguments: dict[str, Any] = field(hash=False, compare=True)
    verdict: Verdict | None = None


class SimClock:
    """The simulated clock: set to each event's time, never backwards, 1 ms per reading."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def advance_to(self, moment: datetime) -> None:
        self.current = max(self.current, moment)

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=1)
        return value


@dataclass(frozen=True)
class Window:
    start: datetime
    stop: datetime  # events with a verdict stop an hour early, so the verdict is in the week


def _moment(rng: random.Random, w: Window, hours: range, weekend: bool) -> datetime:
    """A local time on one of the window's days, with its hour in ``hours``, as UTC."""
    first = w.start.astimezone(NEW_YORK).date()
    days = (w.stop.astimezone(NEW_YORK).date() - first).days + 1
    while True:
        day = first + timedelta(days=rng.randrange(days))
        local = datetime(day.year, day.month, day.day, rng.choice(hours), rng.randrange(60),
                         rng.randrange(60), tzinfo=NEW_YORK)  # fmt: skip
        if (local.isoweekday() > 5) == weekend and w.start <= local.astimezone(UTC) < w.stop:
            return local.astimezone(UTC)


def _business(rng: random.Random, w: Window) -> datetime:
    return _moment(rng, w, range(9, 17), weekend=False)


def _after_hours(rng: random.Random, w: Window) -> datetime:
    if rng.random() < 0.3:
        return _moment(rng, w, range(10, 18), weekend=True)
    return _moment(rng, w, range(18, 23), weekend=False)


def _dollars(rng: random.Random, low: int, high: int) -> str:
    cents = rng.randrange(low * 100, high * 100)
    return f"{cents // 100}.{cents % 100:02d}"


def _pay(payee: str, amount: str, currency: str = "USD") -> dict[str, Any]:
    return {"payee_id": payee, "amount": amount, "currency": currency}


def _approval(rng: random.Random, low: float, high: float, verdict: str = "approve") -> Verdict:
    seconds = rng.uniform(low, high)
    return Verdict(timedelta(seconds=round(seconds, 3)), rng.choice(SUPERVISORS), verdict)


def plan_week(end: datetime) -> list[Event]:
    """Every event of the week before ``end``, sorted by time. Same ``end``, same plan."""
    rng = random.Random(SEED)  # noqa: S311  # demo data, reproducible on purpose; not crypto
    start = end - timedelta(days=7)
    held, free = Window(start, end - timedelta(hours=1)), Window(start, end)
    events: list[Event] = []

    def add(pattern: str, at: datetime, tool: str, arguments: dict[str, Any],
            verdict: Verdict | None = None) -> None:  # fmt: skip
        events.append(Event(at, pattern, tool, arguments, verdict))

    def routine(pattern: str, low: float, high: float) -> None:
        payment = _pay(rng.choice(KNOWN_PAYEES), _dollars(rng, 5001, 15000))
        add(pattern, _business(rng, held), "create_payment", payment, _approval(rng, low, high))

    for _ in range(COUNTS["routine_approved"]):
        routine("routine_approved", 45, 900)
    for _ in range(COUNTS["routine_rubber_stamp"]):
        routine("routine_rubber_stamp", 1, 4.5)
    for payee, (amount, verdict) in zip(NEW_PAYEES, AFTER_HOURS_NEW, strict=True):
        add("new_payee_after_hours", _after_hours(rng, held), "create_payment",
            _pay(payee, amount), _approval(rng, 120, 1800, verdict))  # fmt: skip
    for amount in ("18400.00", "26500.00"):
        add("known_after_hours_big", _after_hours(rng, held), "create_payment",
            _pay(rng.choice(KNOWN_PAYEES), amount), _approval(rng, 300, 1500))  # fmt: skip
    for payee in NEW_PAYEES[: COUNTS["new_payee_daytime"]]:
        add("new_payee_daytime", _business(rng, held), "create_payment",
            _pay(payee + "-2", _dollars(rng, 300, 4000)), _approval(rng, 60, 1200))  # fmt: skip
    for i in range(COUNTS["small"]):
        at = _after_hours(rng, free) if i % 7 == 0 else _business(rng, free)
        add("small", at, "create_payment", _pay(rng.choice(KNOWN_PAYEES), _dollars(rng, 40, 5000)))
    for amount in ("300000.00", "410000.00"):
        payment = _pay(rng.choice(KNOWN_PAYEES), amount)
        add("over_cap", _business(rng, free), "create_payment", payment)
    for amount in ("1200.00", "3400.00"):
        payment = _pay(rng.choice(KNOWN_PAYEES), amount, "EUR")
        add("foreign_currency", _business(rng, free), "create_payment", payment)
    for i in range(COUNTS["email_internal"]):
        add("email_internal", _business(rng, free), "send_email",
            {"to": ["treasury@harborfederal.example"], "subject": f"Payment run {i + 1}",
             "body": "Daily payment run summary."})  # fmt: skip
    for i in range(COUNTS["email_external"]):
        add("email_external", _business(rng, held), "send_email",
            {"to": [f"ap@{rng.choice(KNOWN_PAYEES)}.example"], "subject": f"Remittance {i + 1}",
             "body": "Remittance advice attached."}, _approval(rng, 60, 900))  # fmt: skip
    for _ in range(COUNTS["vendor_lookup"]):
        query = {"query": rng.choice(KNOWN_PAYEES)}
        add("vendor_lookup", _business(rng, free), "lookup_vendor", query)
    return sorted(events, key=lambda e: e.at)


async def play(gate: Gate, clock: SimClock, events: list[Event]) -> dict[str, int]:
    """Decide every event at its time and give each verdict at its time, in time order."""
    queue: list[tuple[datetime, int, str, Verdict]] = []
    decisions = verdicts = 0
    for number, event in enumerate(events):
        while queue and queue[0][0] <= event.at:
            verdicts += await _give(gate, clock, heapq.heappop(queue))
        clock.advance_to(event.at)
        decided = await gate.decide(AGENT, event.tool, event.arguments)
        decisions += 1
        if event.verdict is not None and decided.escalation is not None:
            heapq.heappush(queue, (event.at + event.verdict.after, number,
                                   decided.escalation.id, event.verdict))  # fmt: skip
    while queue:
        verdicts += await _give(gate, clock, heapq.heappop(queue))
    return {"decisions": decisions, "verdicts": verdicts}


async def _give(gate: Gate, clock: SimClock, item: tuple[datetime, int, str, Verdict]) -> int:
    at, _, escalation_id, verdict = item
    clock.advance_to(at)
    await gate.verdict(escalation_id, Approver(verdict.approver, SUPERVISOR_ROLES), verdict.verdict)
    return 1


@asynccontextmanager
async def open_seed_gate(clock: SimClock) -> AsyncIterator[Gate]:
    """Gate as pl-gate builds it (keys, payees, active approved policy), on ``clock``."""
    # Imported here: the plan and play() run in unit tests without the server modules.
    from gate.server import GateSettings, load_gate_keys
    from ledger.pg_store import PostgresStore

    settings, gate_settings = load_settings(), GateSettings()
    signer, log_keys, _ = load_gate_keys(settings.keys_dir, gate_settings.producer_id)
    normalizer = Normalizer(
        StaticPayees.from_file(gate_settings.payees_file),
        internal_domains=[d.strip() for d in gate_settings.internal_domains.split(",")],
        clock=clock, timezone=NEW_YORK,
    )  # fmt: skip
    history = await PostgresStore.open(settings.database_url, max_size=2)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            gate = Gate(
                producer_id=gate_settings.producer_id, signer=signer, stream=settings.ledger_stream,
                ledger=HttpLedgerClient(gate_settings.ledger_url, http),
                log_keys=log_keys, normalizer=normalizer,
                policies=load_policy_set(gate_settings.policy_file),
                escalations=MemoryEscalationStore(), clock=clock,
                approval_timeout=timedelta(hours=1),
            )  # fmt: skip
            await restore_active(gate, history)
            yield gate
    finally:
        await history.close()


async def run(end: datetime, dry_run: bool) -> int:
    plan = plan_week(end)
    print(f"plan: {len(plan)} decisions, {sum(1 for e in plan if e.verdict)} with verdicts")
    for pattern, count in sorted(Counter(e.pattern for e in plan).items()):
        print(f"  {pattern:24} {count}")
    if dry_run:
        return 0
    clock = SimClock(end - timedelta(days=7))
    async with open_seed_gate(clock) as gate:
        print(f"policy set {gate.policies.digest[:19]}… (approval {gate.policy_record_id})")
        summary = await play(gate, clock, plan)
    print(f"appended {summary['decisions']} decisions and {summary['verdicts']} verdicts")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan's counts only")
    args = parser.parse_args(argv)
    end = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    return asyncio.run(run(end, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
