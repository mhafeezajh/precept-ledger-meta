"""Gate over Postgres (make test-integration): the escalation table, its guard, and a full
decide -> approve -> result run with the real ledger store underneath."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from common.clock import FixedClock
from common.errors import PreceptError
from gate.core import Gate
from gate.escalations import APPROVED, EXPIRED, OPEN
from gate.normalizer import Normalizer, StaticPayees
from gate.pg_escalations import PostgresEscalationStore
from ledger.format import Receipt, Record, Submission
from ledger.pg_store import PostgresStore
from ledger.service import Ledger
from tests.gate_helpers import (
    AGENT,
    DEMO_PAYMENT,
    GATE,
    GATE_KEYS,
    LOG,
    LOG_KEYS,
    POLICIES,
    SUPERVISOR,
)
from tests.helpers import T0
from tests.integration.test_pg_store import env

pytestmark = pytest.mark.integration


class PgLedger:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    async def append(self, submission: Submission) -> Receipt:
        return await self.ledger.append(submission.stream, submission)

    async def get(self, record_id: str) -> tuple[Record, Receipt]:
        return await self.ledger.get(record_id)


@pytest.fixture
async def escalations(stream: str) -> AsyncIterator[PostgresEscalationStore]:
    store = await PostgresEscalationStore.open_pool(env("DATABASE_URL"), stream)
    try:
        yield store
    finally:
        await store.close_pool()


@pytest.fixture
async def gate(escalations: PostgresEscalationStore, stream: str) -> AsyncIterator[Gate]:
    clock = FixedClock(T0, timedelta(milliseconds=1))
    pg = await PostgresStore.open(env("DATABASE_URL"))
    normalizer = Normalizer(
        StaticPayees({"acme-paper"}), internal_domains=(), clock=clock,
        timezone=ZoneInfo("America/New_York"),
    )  # fmt: skip
    try:
        yield Gate(
            producer_id="gate-1", signer=GATE, stream=stream,
            ledger=PgLedger(Ledger(pg, GATE_KEYS, LOG, clock)), log_keys=LOG_KEYS,
            normalizer=normalizer, policies=POLICIES, escalations=escalations, clock=clock,
            approval_timeout=timedelta(minutes=10),
        )  # fmt: skip
    finally:
        await pg.close()


@pytest.fixture
def stream() -> str:
    return f"it-{os.urandom(6).hex()}/decisions"


async def test_decide_approve_and_record_a_result(
    gate: Gate, escalations: PostgresEscalationStore
) -> None:
    decision = await gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    stored = await escalations.get(decision.escalation.id)
    assert stored == decision.escalation
    assert decision.escalation in await escalations.list_open()
    verdict = await gate.verdict(decision.escalation.id, SUPERVISOR, "approve")
    closed = await gate.status(decision.escalation.id)
    assert (closed.state, closed.verdict_record_id) == (APPROVED, verdict.record_id)
    assert await gate.verdict_record(closed) == verdict
    result = await gate.record_result(
        AGENT, decision.record.record_id, ok=True, digest=None, error=None
    )
    assert result.body["verdict_record_id"] == verdict.record_id


async def test_concurrent_closers_in_separate_processes_cannot_both_win(
    gate: Gate, escalations: PostgresEscalationStore
) -> None:
    # Two Gate processes share the table; the UPDATE ... WHERE state = 'open' picks one.
    decision = await gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    now = T0 + timedelta(seconds=1)
    verdict_id = "01JB6X7Q2M9ZK3T8V4N5R6S7W0"
    closers = [
        escalations.close(decision.escalation.id, APPROVED, "j.ortiz", verdict_id, now)
        for _ in range(5)
    ]
    results = await asyncio.gather(*closers)
    assert sorted(results) == [False, False, False, False, True]


async def test_each_stream_sees_only_its_own_escalations(
    gate: Gate, escalations: PostgresEscalationStore
) -> None:
    decision = await gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    other = await PostgresEscalationStore.open_pool(env("DATABASE_URL"), "it-other/decisions")
    try:
        assert decision.escalation not in await other.list_open()
        assert await other.get(decision.escalation.id) is None
        assert decision.escalation not in await other.due(decision.escalation.expires_at)
        when = decision.escalation.expires_at
        assert not await other.close(decision.escalation.id, EXPIRED, None, None, when)
    finally:
        await other.close_pool()
    assert (await escalations.get(decision.escalation.id)) == decision.escalation


async def test_expiry_is_found_by_the_due_query(
    gate: Gate, escalations: PostgresEscalationStore
) -> None:
    decision = await gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    assert decision.escalation not in await escalations.due(decision.escalation.opened_at)
    assert decision.escalation in await escalations.due(decision.escalation.expires_at)
    gate._clock.current = decision.escalation.expires_at  # type: ignore[attr-defined]
    assert (await gate.status(decision.escalation.id)).state == EXPIRED


async def test_the_guard_trigger_forbids_reopening_editing_and_deleting(gate: Gate) -> None:
    decision = await gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.escalation is not None
    escalation_id = decision.escalation.id
    async with await psycopg.AsyncConnection.connect(
        env("ADMIN_DATABASE_URL"), autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="only the outcome"):
            await conn.execute(
                "UPDATE escalation SET expires_at = expires_at + interval '1 day' WHERE id = %s",
                (escalation_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
            await conn.execute("DELETE FROM escalation WHERE id = %s", (escalation_id,))
    await gate.verdict(escalation_id, SUPERVISOR, "reject")
    async with await psycopg.AsyncConnection.connect(
        env("ADMIN_DATABASE_URL"), autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="already rejected"):
            await conn.execute(
                "UPDATE escalation SET state = %s WHERE id = %s", (OPEN, escalation_id)
            )
    async with await psycopg.AsyncConnection.connect(env("DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("TRUNCATE escalation")


async def test_an_unreachable_database_is_a_store_error() -> None:
    store = await PostgresEscalationStore.open_pool("postgresql://nobody@127.0.0.1:1/none", "s")
    try:
        with pytest.raises(PreceptError) as caught:
            await store.list_open()
        assert caught.value.code == "gate.store_unavailable"
    finally:
        await store.close_pool()
