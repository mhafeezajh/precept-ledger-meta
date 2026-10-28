"""Postgres 16 store (D3, D21, D22). Schema and triggers: ``ledger/schema.sql``.

Every driver error becomes ``StoreError`` (or ``LedgerError`` for a duplicate), so callers
fail closed on one exception type and never see a half-written append.
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from common.errors import LedgerError, StoreError
from ledger.chain import GENESIS, ChainedRecord, ChainHead
from ledger.format import HASH_SIZE, Receipt, Record, b64decode, b64encode
from ledger.pg_tree import (
    PostgresAnchorStore,
    PostgresCheckpointSink,
    PostgresCosignatureStore,
    PostgresTreeStore,
)
from ledger.store import AppendBuilder, check_built

_RECORD_COLUMNS = (
    "v, stream, seq, record_id, ts, type, producer_id, producer_alg, producer_key_id,"
    " producer_sig, body, prev_hash"
)
_RECEIPT_COLUMNS = "v, alg, stream, record_id, seq, leaf_hash, chain_hash, received_at, key_id, sig"

# S608 flags f-strings in SQL; only the constant column lists above are interpolated here,
# and every value is a bound parameter.
_INSERT_RECORD = (
    f"INSERT INTO ledger_record ({_RECORD_COLUMNS}, chain_hash, leaf_hash)"  # noqa: S608
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_INSERT_RECEIPT = (
    f"INSERT INTO receipt ({_RECEIPT_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"  # noqa: S608
)
_SELECT_RECORD = f"SELECT {_RECORD_COLUMNS} FROM ledger_record WHERE record_id = %s"  # noqa: S608
_SELECT_RECEIPT = f"SELECT {_RECEIPT_COLUMNS} FROM receipt WHERE record_id = %s"  # noqa: S608
_SELECT_STREAM = f"SELECT {_RECORD_COLUMNS} FROM ledger_record WHERE stream = %s ORDER BY seq"  # noqa: S608
_SELECT_STREAM_RECEIPTS = f"SELECT {_RECEIPT_COLUMNS} FROM receipt WHERE stream = %s ORDER BY seq"  # noqa: S608


def _row_to_json(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the record JSON from columns exactly as they are now (D21)."""
    producer = {
        "id": row["producer_id"],
        "alg": row["producer_alg"],
        "key_id": row["producer_key_id"],
        "sig": row["producer_sig"],
    }
    keys = ("v", "stream", "seq", "record_id", "ts", "type", "body", "prev_hash")
    return {**{k: row[k] for k in keys}, "producer": producer}


def _record_params(item: ChainedRecord) -> tuple[object, ...]:
    record, sub = item.record, item.record.submission
    producer = sub.producer
    return (
        sub.v, sub.stream, record.seq, sub.record_id, sub.ts, sub.type, producer.id,
        producer.alg, producer.key_id, producer.sig, Jsonb(sub.body), record.prev_hash,
        b64encode(item.chain_hash), b64encode(item.leaf_hash),
    )  # fmt: skip


def _receipt_params(receipt: Receipt) -> tuple[object, ...]:
    data = receipt.to_json()
    return tuple(data[name.strip()] for name in _RECEIPT_COLUMNS.split(","))


class PostgresStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(
        cls, url: str, *, max_size: int = 8, timeout_seconds: float = 5.0
    ) -> "PostgresStore":
        pool = AsyncConnectionPool(
            url, min_size=0, max_size=max_size, timeout=timeout_seconds, open=False
        )
        await pool.open(wait=False)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    def tree(self, stream: str) -> PostgresTreeStore:
        return PostgresTreeStore(self._pool, stream)

    def checkpoint_sink(self, stream: str) -> PostgresCheckpointSink:
        return PostgresCheckpointSink(self._pool, stream)

    def cosignatures(self, stream: str) -> PostgresCosignatureStore:
        return PostgresCosignatureStore(self._pool, stream)

    def anchors(self, stream: str) -> PostgresAnchorStore:
        return PostgresAnchorStore(self._pool, stream)

    async def append(self, stream: str, build: AppendBuilder) -> Receipt:
        try:
            async with self._pool.connection() as conn, conn.transaction():
                # D22: one append per stream at a time; released at commit or rollback.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (stream,)
                )
                head = await self._head(conn, stream)
                item, receipt = build(head)
                check_built(head, stream, item, receipt)
                await conn.execute(_INSERT_RECORD, _record_params(item))
                await conn.execute(_INSERT_RECEIPT, _receipt_params(receipt))
            return receipt
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "ledger_record_record_id_key":
                raise LedgerError(
                    "record_id already in the ledger", code="ledger.duplicate_record"
                ) from exc
            # Any other unique violation means the lock or seq logic is broken: fail closed.
            raise StoreError(f"append conflicted on {exc.diag.constraint_name}") from exc
        except (psycopg.Error, PoolTimeout) as exc:
            raise StoreError(f"append failed: {type(exc).__name__}") from exc

    async def _head(self, conn: psycopg.AsyncConnection[Any], stream: str) -> ChainHead:
        cur = await conn.execute(
            "SELECT seq, chain_hash FROM ledger_record WHERE stream = %s ORDER BY seq DESC LIMIT 1",
            (stream,),
        )
        row = await cur.fetchone()
        if row is None:
            return GENESIS
        return ChainHead(row[0], b64decode(row[1], size=HASH_SIZE, field="chain_hash"))

    async def get(self, record_id: str) -> tuple[Record, Receipt] | None:
        record_row = await self._fetch(_SELECT_RECORD, (record_id,))
        receipt_row = await self._fetch(_SELECT_RECEIPT, (record_id,))
        if not record_row or not receipt_row:
            return None
        return Record.from_json(_row_to_json(record_row[0])), Receipt.from_json(receipt_row[0])

    async def read_raw(self, stream: str) -> list[dict[str, Any]]:
        return [_row_to_json(row) for row in await self._fetch(_SELECT_STREAM, (stream,))]

    async def read_receipts(self, stream: str) -> list[dict[str, Any]]:
        return await self._fetch(_SELECT_STREAM_RECEIPTS, (stream,))

    async def _fetch(self, sql: str, params: tuple[object, ...]) -> list[dict[str, Any]]:
        try:
            async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()
        except (psycopg.Error, PoolTimeout) as exc:
            raise StoreError(f"read failed: {type(exc).__name__}") from exc
