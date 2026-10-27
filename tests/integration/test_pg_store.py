"""PostgresStore against the docker compose database (make test-integration).

Needs DATABASE_URL (app role, INSERT + SELECT only) and ADMIN_DATABASE_URL (superuser, used
here only to play the database admin of demo beat 4).
"""

import asyncio
import os
from collections.abc import AsyncIterator

import psycopg
import pytest

from common.errors import LedgerError, StoreError
from ledger.chain import BreakReason
from ledger.pg_store import PostgresStore
from ledger.receipts import LOG_KEY_OWNER, verify_receipt
from ledger.service import Ledger
from ledger.signing import KeyRing
from tests.helpers import clock, make_submission, signer_for

pytestmark = pytest.mark.integration

GATE = signer_for("gate-1")
LOG = signer_for("log")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])
LOG_KEYS = KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)])


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} must be set for integration tests (see .env.example)")
    return value


@pytest.fixture
def stream() -> str:
    # A fresh stream per test: the tables are append-only, so tests never clean up.
    return f"it-{os.urandom(6).hex()}/decisions"


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    pg = await PostgresStore.open(env("DATABASE_URL"))
    try:
        yield pg
    finally:
        await pg.close()


async def admin_execute(sql: str, params: tuple[object, ...]) -> None:
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        await conn.execute(sql, params)


async def test_append_get_and_verify(store: PostgresStore, stream: str) -> None:
    ledger, clk = Ledger(store, PRODUCERS, LOG, clock()), clock()
    receipts = [
        await ledger.append(stream, make_submission(GATE, clk, stream=stream)) for _ in range(3)
    ]
    assert [r.seq for r in receipts] == [1, 2, 3]
    for receipt in receipts:
        verify_receipt(receipt, LOG_KEYS)
    record, receipt = await ledger.get(receipts[2].record_id)
    assert record.seq == 3
    assert receipt == receipts[2]
    assert (await ledger.verify_stream(stream)).ok


async def test_concurrent_appends_are_gap_free(store: PostgresStore, stream: str) -> None:
    ledger, clk = Ledger(store, PRODUCERS, LOG, clock()), clock()
    subs = [make_submission(GATE, clk, stream=stream) for _ in range(20)]
    receipts = await asyncio.gather(*(ledger.append(stream, s) for s in subs))
    assert sorted(r.seq for r in receipts) == list(range(1, 21))
    assert (await ledger.verify_stream(stream)).ok


async def test_duplicate_record_id_is_rejected(store: PostgresStore, stream: str) -> None:
    ledger = Ledger(store, PRODUCERS, LOG, clock())
    sub = make_submission(GATE, clock(), stream=stream)
    await ledger.append(stream, sub)
    with pytest.raises(LedgerError) as exc:
        await ledger.append(stream, sub)
    assert exc.value.code == "ledger.duplicate_record"


async def test_app_role_cannot_update_or_delete(store: PostgresStore, stream: str) -> None:
    ledger = Ledger(store, PRODUCERS, LOG, clock())
    await ledger.append(stream, make_submission(GATE, clock(), stream=stream))
    async with await psycopg.AsyncConnection.connect(env("DATABASE_URL")) as conn:
        for sql in (
            "UPDATE ledger_record SET type = 'x' WHERE stream = %s",
            "DELETE FROM ledger_record WHERE stream = %s",
            "DELETE FROM receipt WHERE stream = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(sql, (stream,))
            await conn.rollback()


async def test_trigger_blocks_even_the_superuser(store: PostgresStore, stream: str) -> None:
    ledger = Ledger(store, PRODUCERS, LOG, clock())
    await ledger.append(stream, make_submission(GATE, clock(), stream=stream))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await admin_execute("UPDATE ledger_record SET type = 'x' WHERE stream = %s", (stream,))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await admin_execute("DELETE FROM receipt WHERE stream = %s", (stream,))


async def test_admin_edit_with_triggers_off_is_caught_at_its_seq(
    store: PostgresStore, stream: str
) -> None:
    # Demo beat 4: an admin who bypasses the trigger edits one amount.
    ledger, clk = Ledger(store, PRODUCERS, LOG, clock()), clock()
    for amount in (1000, 48000, 3000):
        body = {"action": "value.transfer", "amount": amount}
        await ledger.append(stream, make_submission(GATE, clk, stream=stream, body=body))
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        await conn.execute("SET session_replication_role = replica")  # skips triggers
        await conn.execute(
            "UPDATE ledger_record SET body = jsonb_set(body, '{amount}', '480') "
            "WHERE stream = %s AND seq = 2",
            (stream,),
        )
    report = await ledger.verify_stream(stream)
    assert report.first_break is not None
    assert (report.first_break.seq, report.first_break.reason) == (
        2,
        BreakReason.BAD_PRODUCER_SIGNATURE,
    )


async def test_unreachable_database_fails_closed() -> None:
    store = await PostgresStore.open(
        "postgresql://nobody:nothing@127.0.0.1:1/none", timeout_seconds=1.0
    )
    try:
        ledger = Ledger(store, PRODUCERS, LOG, clock())
        sub = make_submission(GATE, clock())
        with pytest.raises(StoreError):
            await ledger.append(sub.stream, sub)
    finally:
        await store.close()
