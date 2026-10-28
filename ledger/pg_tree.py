"""Postgres adapters for checkpointing: one stream's leaves as a ``TreeStore``, and the
``checkpoint`` table as a ``CheckpointSink``. Both share ``PostgresStore``'s pool.

Driver errors become ``StoreError`` so the checkpointer fails closed on one exception type.
"""

from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from common.errors import LedgerError, StoreError
from ledger.anchoring import AnchorRow
from ledger.checkpointer import SignedCheckpoint
from ledger.format import HASH_SIZE, b64decode, b64encode


async def _query(
    pool: AsyncConnectionPool, sql: str, params: tuple[object, ...]
) -> list[tuple[Any, ...]]:
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()
    except (psycopg.Error, PoolTimeout) as exc:
        raise StoreError(f"read failed: {type(exc).__name__}") from exc


class PostgresTreeStore:
    """Leaf ``i`` is ``ledger_record.leaf_hash`` at ``seq = i + 1`` (D16)."""

    def __init__(self, pool: AsyncConnectionPool, stream: str) -> None:
        self._pool, self._stream = pool, stream

    async def size(self) -> int:
        rows = await _query(
            self._pool,
            "SELECT coalesce(max(seq), 0) FROM ledger_record WHERE stream = %s",
            (self._stream,),
        )
        return int(rows[0][0])

    async def leaf_hashes(self, start: int, end: int) -> list[bytes]:
        rows = await _query(
            self._pool,
            "SELECT seq, leaf_hash FROM ledger_record"
            " WHERE stream = %s AND seq > %s AND seq <= %s ORDER BY seq",
            (self._stream, start, end),
        )
        if [row[0] for row in rows] != list(range(start + 1, start + 1 + len(rows))):
            # Triggers make gaps impossible; if one appears, never build a tree over it (R4).
            raise StoreError("gap in ledger_record seq", code="store.gap")
        return [b64decode(row[1], size=HASH_SIZE, field="leaf_hash") for row in rows]


class PostgresCheckpointSink:
    def __init__(self, pool: AsyncConnectionPool, stream: str) -> None:
        self._pool, self._stream = pool, stream

    async def write(self, signed: SignedCheckpoint) -> None:
        cp = signed.checkpoint
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO checkpoint (stream, tree_size, root_hash, note)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (stream, tree_size) DO NOTHING",
                    (self._stream, cp.size, b64encode(cp.root), signed.note),
                )
                cur = await conn.execute(
                    "SELECT note FROM checkpoint WHERE stream = %s AND tree_size = %s",
                    (self._stream, cp.size),
                )
                row = await cur.fetchone()
        except (psycopg.Error, PoolTimeout) as exc:
            raise StoreError(f"checkpoint write failed: {type(exc).__name__}") from exc
        if row is None or row[0] != signed.note:
            raise LedgerError(
                f"a different checkpoint for size {cp.size} exists", code="checkpoint.conflict"
            )

    async def latest(self) -> str | None:
        rows = await _query(
            self._pool,
            "SELECT note FROM checkpoint WHERE stream = %s ORDER BY tree_size DESC LIMIT 1",
            (self._stream,),
        )
        return str(rows[0][0]) if rows else None

    async def notes(self) -> list[str]:
        rows = await _query(
            self._pool,
            "SELECT note FROM checkpoint WHERE stream = %s ORDER BY tree_size",
            (self._stream,),
        )
        return [str(row[0]) for row in rows]


class PostgresCosignatureStore:
    """``cosignature`` rows of one stream; first line per (size, witness) wins."""

    def __init__(self, pool: AsyncConnectionPool, stream: str) -> None:
        self._pool, self._stream = pool, stream

    async def add(self, size: int, witness: str, line: str) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO cosignature (stream, tree_size, witness, line)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (self._stream, size, witness, line),
                )
        except (psycopg.Error, PoolTimeout) as exc:
            raise StoreError(f"cosignature write failed: {type(exc).__name__}") from exc

    async def lines(self, size: int) -> dict[str, str]:
        rows = await _query(
            self._pool,
            "SELECT witness, line FROM cosignature WHERE stream = %s AND tree_size = %s",
            (self._stream, size),
        )
        return {str(row[0]): str(row[1]) for row in rows}


class PostgresAnchorStore:
    """``anchor`` rows of one stream; first row per (size, kind, source, status) wins."""

    def __init__(self, pool: AsyncConnectionPool, stream: str) -> None:
        self._pool, self._stream = pool, stream

    async def add(self, row: AnchorRow) -> None:
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO anchor (stream, tree_size, kind, source, status, token, digest)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (self._stream, row.size, row.kind, row.source, row.status, row.token,
                     row.digest),
                )  # fmt: skip
        except (psycopg.Error, PoolTimeout) as exc:
            raise StoreError(f"anchor write failed: {type(exc).__name__}") from exc

    async def rows(self) -> list[AnchorRow]:
        found = await _query(
            self._pool,
            "SELECT tree_size, kind, source, status, token, digest FROM anchor"
            " WHERE stream = %s ORDER BY tree_size, kind, source, status",
            (self._stream,),
        )
        return [
            AnchorRow(int(r[0]), str(r[1]), str(r[2]), str(r[3]), bytes(r[4]), bytes(r[5]))
            for r in found
        ]
