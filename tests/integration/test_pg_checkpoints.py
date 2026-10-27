"""Checkpoints over Postgres (make test-integration): tree adapter, checkpoint table, refusals."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

from common.errors import LedgerError, StoreError
from ledger.checkpoint import Checkpoint, NoteKey, open_checkpoint, sign_checkpoint
from ledger.checkpointer import Checkpointer, DirectorySink, SignedCheckpoint, origin_for
from ledger.format import b64encode
from ledger.pg_store import PostgresStore
from ledger.service import Ledger
from ledger.signing import KeyRing
from merkle.hashing import leaf_hash
from merkle.log import LogTree
from merkle.proofs import root_hash, verify_inclusion
from tests.helpers import clock, make_submission, signer_for
from tests.integration.test_pg_store import env

pytestmark = pytest.mark.integration

GATE, LOG = signer_for("gate-1"), signer_for("log")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])


@pytest.fixture
def stream() -> str:
    return f"it-{os.urandom(6).hex()}/decisions"


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    pg = await PostgresStore.open(env("DATABASE_URL"))
    try:
        yield pg
    finally:
        await pg.close()


def checkpointer(store: PostgresStore, stream: str, directory: Path) -> Checkpointer:
    origin = origin_for("precept-ledger.local", stream)
    sinks = [store.checkpoint_sink(stream), DirectorySink(directory)]
    return Checkpointer(LogTree(store.tree(stream)), LOG, origin, sinks)


async def append(store: PostgresStore, stream: str, count: int) -> list[bytes]:
    ledger, clk = Ledger(store, PRODUCERS, LOG, clock()), clock()
    receipts = [
        await ledger.append(stream, make_submission(GATE, clk, stream=stream)) for _ in range(count)
    ]
    return [r.leaf_hash_bytes() for r in receipts]


async def test_checkpoint_commits_to_the_receipted_leaves(
    store: PostgresStore, stream: str, tmp_path: Path
) -> None:
    leaves = await append(store, stream, 4)
    signed = await checkpointer(store, stream, tmp_path).publish()
    assert (signed.checkpoint.size, signed.checkpoint.root) == (4, root_hash(leaves))
    tree = LogTree(store.tree(stream))
    proof = await tree.prove_inclusion(2, 4)
    assert verify_inclusion(leaves[2], 2, 4, proof.hashes, signed.checkpoint.root)
    stored = await store.checkpoint_sink(stream).latest()
    key = NoteKey(signed.checkpoint.origin, LOG.public_key_raw)
    assert stored == signed.note
    assert open_checkpoint(stored, key) == signed.checkpoint
    assert (tmp_path / "00000000000000000004.note").read_text() == signed.note


async def test_growth_and_republish(store: PostgresStore, stream: str, tmp_path: Path) -> None:
    cp = checkpointer(store, stream, tmp_path)
    await append(store, stream, 2)
    first = await cp.publish()
    assert await cp.publish() == first
    await append(store, stream, 3)
    assert (await cp.publish()).checkpoint.size == 5


async def test_conflicting_checkpoint_for_a_size_is_refused(
    store: PostgresStore, stream: str
) -> None:
    sink = store.checkpoint_sink(stream)
    origin = origin_for("precept-ledger.local", stream)
    for root in (leaf_hash(b"a"), leaf_hash(b"b")):
        cp = Checkpoint(origin, 1, root)
        signed = SignedCheckpoint(cp, sign_checkpoint(cp, LOG))
        if root == leaf_hash(b"a"):
            await sink.write(signed)
            await sink.write(signed)  # identical rewrite is fine
        else:
            with pytest.raises(LedgerError) as err:
                await sink.write(signed)
            assert err.value.code == "checkpoint.conflict"


async def test_checkpoint_table_is_append_only(
    store: PostgresStore, stream: str, tmp_path: Path
) -> None:
    await append(store, stream, 1)
    await checkpointer(store, stream, tmp_path).publish()
    async with await psycopg.AsyncConnection.connect(env("DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM checkpoint WHERE stream = %s", (stream,))
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute("UPDATE checkpoint SET note = '' WHERE stream = %s", (stream,))


async def test_admin_rewrite_of_a_leaf_is_refused_at_the_next_checkpoint(
    store: PostgresStore, stream: str, tmp_path: Path
) -> None:
    # An admin bypasses the triggers and rewrites leaf 2 to match an edited record. The next
    # checkpoint would not extend the signed one, so the checkpointer refuses to sign it.
    await append(store, stream, 3)
    cp = checkpointer(store, stream, tmp_path)
    await cp.publish()
    await append(store, stream, 1)
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        await conn.execute("SET session_replication_role = replica")  # skips triggers
        await conn.execute(
            "UPDATE ledger_record SET leaf_hash = %s WHERE stream = %s AND seq = 2",
            (b64encode(leaf_hash(b"forged")), stream),
        )
    with pytest.raises(LedgerError) as err:
        await cp.publish()
    assert err.value.code == "checkpoint.inconsistent"


async def test_gap_from_a_deleted_row_fails_closed(
    store: PostgresStore, stream: str, tmp_path: Path
) -> None:
    await append(store, stream, 3)
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        await conn.execute("SET session_replication_role = replica")  # skips triggers
        await conn.execute("DELETE FROM receipt WHERE stream = %s AND seq = 2", (stream,))
        await conn.execute("DELETE FROM ledger_record WHERE stream = %s AND seq = 2", (stream,))
    with pytest.raises(StoreError) as err:
        await checkpointer(store, stream, tmp_path).publish()
    assert err.value.code == "store.gap"


async def test_unreachable_database_fails_closed(tmp_path: Path) -> None:
    pg = await PostgresStore.open(
        "postgresql://nobody:nothing@127.0.0.1:1/none", timeout_seconds=1.0
    )
    try:
        with pytest.raises(StoreError):
            await checkpointer(pg, "s/decisions", tmp_path).publish()
        with pytest.raises(StoreError):
            await pg.checkpoint_sink("s/decisions").write(
                SignedCheckpoint(Checkpoint("o", 0, leaf_hash(b"")), "n")
            )
    finally:
        await pg.close()
