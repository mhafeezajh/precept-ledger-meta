"""Postgres ``EscalationStore``. Schema and guard trigger: ``gate/schema.sql``.

``close`` is one ``UPDATE ... WHERE state = 'open'``: the database, not a lock in this
process, decides which closer wins. Driver errors become ``GateError(gate.store_unavailable)``
so Gate fails closed on one exception type.
"""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from common.errors import GateError
from gate.escalations import OPEN, Escalation

_COLUMNS = (
    "id, decision_record_id, agent, tool, action, approver_roles, opened_at, expires_at,"
    " state, approver, verdict_record_id, decided_at"
)
# S608: only the constant column list is interpolated; every value is a bound parameter.
_SELECT = f"SELECT {_COLUMNS} FROM escalation"  # noqa: S608


def _row(row: dict[str, Any]) -> Escalation:
    return Escalation(**{**row, "approver_roles": tuple(row["approver_roles"])})


class PostgresEscalationStore:
    """One stream's escalations. A Gate never sees, or expires into its own stream, another's."""

    def __init__(self, pool: AsyncConnectionPool, stream: str) -> None:
        self._pool = pool
        self._stream = stream

    @classmethod
    async def open_pool(
        cls, url: str, stream: str, *, max_size: int = 4
    ) -> "PostgresEscalationStore":
        pool = AsyncConnectionPool(url, min_size=0, max_size=max_size, timeout=5.0, open=False)
        await pool.open(wait=False)
        return cls(pool, stream)

    async def close_pool(self) -> None:
        await self._pool.close()

    async def _execute(self, sql: str, params: tuple[object, ...]) -> list[dict[str, Any]]:
        try:
            async with self._pool.connection() as conn:
                cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
                return await cur.fetchall() if cur.description else [{"rowcount": cur.rowcount}]
        except (psycopg.Error, PoolTimeout) as exc:
            raise GateError(
                f"escalation store: {type(exc).__name__}", code="gate.store_unavailable"
            ) from exc

    async def open(self, escalation: Escalation) -> None:
        e = escalation
        values = (
            self._stream, e.id, e.decision_record_id, e.agent, e.tool, e.action,
            list(e.approver_roles), e.opened_at, e.expires_at, OPEN, None, None, None,
        )  # fmt: skip
        placeholders = ", ".join(["%s"] * len(values))
        await self._execute(
            f"INSERT INTO escalation (stream, {_COLUMNS}) VALUES ({placeholders})",  # noqa: S608
            values,
        )

    async def get(self, escalation_id: str) -> Escalation | None:
        rows = await self._execute(
            f"{_SELECT} WHERE stream = %s AND id = %s", (self._stream, escalation_id)
        )
        return _row(rows[0]) if rows else None

    async def close(
        self,
        escalation_id: str,
        state: str,
        approver: str | None,
        verdict_record_id: str | None,
        decided_at: datetime,
    ) -> bool:
        rows = await self._execute(
            "UPDATE escalation SET state = %s, approver = %s, verdict_record_id = %s,"
            " decided_at = %s WHERE stream = %s AND id = %s AND state = 'open'",
            (state, approver, verdict_record_id, decided_at, self._stream, escalation_id),
        )
        return bool(rows[0]["rowcount"] == 1)

    async def list_open(self) -> list[Escalation]:
        rows = await self._execute(
            f"{_SELECT} WHERE stream = %s AND state = 'open' ORDER BY opened_at", (self._stream,)
        )
        return [_row(r) for r in rows]

    async def due(self, now: datetime) -> list[Escalation]:
        sql = (
            f"{_SELECT} WHERE stream = %s AND state = 'open' AND expires_at <= %s"
            " ORDER BY expires_at"
        )
        return [_row(r) for r in await self._execute(sql, (self._stream, now))]
