"""The demo week (scripts/seed_week.py): a deterministic plan, played through the real Gate code
with a simulated clock, gives the records beats 3 and 5 need, in the counts the docs state."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from claims.query import Query, build_rows, outside_hours, run_query
from common.clock import parse_ts
from gate.core import Gate
from gate.escalations import MemoryEscalationStore
from gate.normalizer import Normalizer, StaticPayees
from scripts.seed_week import (
    COUNTS,
    KNOWN_PAYEES,
    NEW_YORK,
    SimClock,
    plan_week,
    play,
)
from tests.gate_helpers import GATE, LOG_KEYS, POLICIES, LocalLedger
from tests.helpers import STREAM

END = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)  # Wednesday 17:00 in New York
WEEK = Query(
    action="value.transfer", decision="any", verdict="approve", min_amount_cents=1_000_001,
    max_amount_cents=None, outside_business_hours="yes", approver=None, payee_id=None,
    since_days=7,
)  # fmt: skip


def test_the_plan_is_deterministic_and_inside_the_week() -> None:
    plan = plan_week(END)
    assert plan == plan_week(END)
    assert Counter(e.pattern for e in plan) == COUNTS
    for event in plan:
        assert END - timedelta(days=7) <= event.at < END
        if event.verdict is not None:
            assert event.at + event.verdict.after < END


def test_each_pattern_lands_where_it_should_in_bank_time() -> None:
    for event in plan_week(END):
        local = event.at.astimezone(NEW_YORK)
        facts = {"local_hour": local.hour, "local_weekday": local.isoweekday()}
        if event.pattern in ("routine_approved", "routine_rubber_stamp", "new_payee_daytime"):
            assert outside_hours(facts) is False, event
        if event.pattern in ("new_payee_after_hours", "known_after_hours_big"):
            assert outside_hours(facts) is True, event


def gate_on(clock: SimClock) -> tuple[Gate, LocalLedger]:
    ledger = LocalLedger(clock)  # type: ignore[arg-type]
    normalizer = Normalizer(
        StaticPayees(set(KNOWN_PAYEES)), internal_domains={"harborfederal.example"},
        clock=clock, timezone=ZoneInfo("America/New_York"),
    )  # fmt: skip
    gate = Gate(
        producer_id="gate-1", signer=GATE, stream=STREAM, ledger=ledger, log_keys=LOG_KEYS,
        normalizer=normalizer, policies=POLICIES, escalations=MemoryEscalationStore(),
        clock=clock, approval_timeout=timedelta(hours=1),
    )  # fmt: skip
    return gate, ledger


async def test_played_through_gate_the_week_has_what_beats_3_and_5_need() -> None:
    clock = SimClock(END - timedelta(days=7))
    gate, ledger = gate_on(clock)
    summary = await play(gate, clock, plan_week(END))
    records = await ledger.store.read_raw(STREAM)
    rows = build_rows(records)
    assert summary == {
        "decisions": len(rows), "verdicts": sum(1 for r in records if r["type"] == "gate.verdict"),
    }  # fmt: skip
    by_id = {r["record_id"]: r for r in records}

    def latency(row: dict[str, object]) -> float:
        decided = parse_ts(by_id[str(row["verdict_record_id"])]["ts"])
        return (decided - parse_ts(str(row["ts"]))).total_seconds()

    routine = [
        r for r in rows
        if r["payee_new"] is False and r["decision"] == "escalate"
        and r["outside_business_hours"] is False and r["amount_cents"] < 1_500_000
    ]  # fmt: skip
    assert len(routine) == 44
    assert {r["verdict"] for r in routine} == {"approve"}  # no past rejection to newly allow
    assert sum(1 for r in routine if latency(r) >= 5) == 38  # the learner's "38 routine"
    after_hours_new = [r for r in rows if r["payee_new"] and r["outside_business_hours"]]
    assert Counter(r["verdict"] for r in after_hours_new) == {"approve": 6, "reject": 8}
    assert len(run_query(WEEK, records, END)) == 5  # beat 3's question
    assert Counter(r["decision"] for r in rows)["deny"] == 4
    assert {r["approver"] for r in rows if r["approver"]} == {"j.ortiz", "m.lee"}
    times = [r["ts"] for r in records]
    assert times == sorted(times)  # appended in time order
