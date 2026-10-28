"""Escalations: approval requests opened by ``escalate`` decisions, closed exactly once.

The store is app state, not ledger state: what proves a verdict is the ``gate.verdict``
record. The store's job is the guard. ``close`` succeeds only from ``open``, so two approvers,
or an approver and the timeout, can never both win (ARCHITECTURE: "state guarded on open").
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from common.clock import format_ts

OPEN = "open"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
STATES = frozenset({OPEN, APPROVED, REJECTED, EXPIRED})


@dataclass(frozen=True)
class Escalation:
    id: str
    decision_record_id: str
    agent: str
    tool: str
    action: str
    approver_roles: tuple[str, ...]
    opened_at: datetime
    expires_at: datetime
    state: str = OPEN
    approver: str | None = None
    verdict_record_id: str | None = None
    decided_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "decision_record_id": self.decision_record_id,
            "agent": self.agent,
            "tool": self.tool,
            "action": self.action,
            "approver_roles": list(self.approver_roles),
            "opened_at": format_ts(self.opened_at),
            "expires_at": format_ts(self.expires_at),
            "state": self.state,
            "approver": self.approver,
            "verdict_record_id": self.verdict_record_id,
            "decided_at": None if self.decided_at is None else format_ts(self.decided_at),
        }


class EscalationStore(Protocol):
    async def open(self, escalation: Escalation) -> None: ...

    async def get(self, escalation_id: str) -> Escalation | None: ...

    async def close(
        self,
        escalation_id: str,
        state: str,
        approver: str | None,
        verdict_record_id: str | None,
        decided_at: datetime,
    ) -> bool:
        """Move an open escalation to ``state``. False if it was not open."""
        ...

    async def list_open(self) -> list[Escalation]: ...

    async def due(self, now: datetime) -> list[Escalation]:
        """Open escalations whose ``expires_at`` is at or before ``now``."""
        ...


class MemoryEscalationStore:
    def __init__(self) -> None:
        self.stored: dict[str, Escalation] = {}

    async def open(self, escalation: Escalation) -> None:
        if escalation.id in self.stored:
            raise ValueError(f"escalation {escalation.id} already exists")
        self.stored[escalation.id] = escalation

    async def get(self, escalation_id: str) -> Escalation | None:
        return self.stored.get(escalation_id)

    async def close(
        self,
        escalation_id: str,
        state: str,
        approver: str | None,
        verdict_record_id: str | None,
        decided_at: datetime,
    ) -> bool:
        current = self.stored.get(escalation_id)
        if current is None or current.state != OPEN or state not in STATES - {OPEN}:
            return False
        self.stored[escalation_id] = replace(
            current,
            state=state,
            approver=approver,
            verdict_record_id=verdict_record_id,
            decided_at=decided_at,
        )
        return True

    async def list_open(self) -> list[Escalation]:
        return sorted(
            (e for e in self.stored.values() if e.state == OPEN), key=lambda e: e.opened_at
        )

    async def due(self, now: datetime) -> list[Escalation]:
        return [e for e in await self.list_open() if e.expires_at <= now]
