"""Storage interface for the ledger, and an in-memory store for tests and offline demos.

A store owns exactly one hard job: running ``build`` against the current head and saving its
result atomically, with no other append to the same stream in between (D22).
"""

import asyncio
import copy
from collections.abc import Callable
from typing import Any, Protocol

from common.errors import LedgerError
from ledger.chain import GENESIS, ChainedRecord, ChainHead
from ledger.format import Receipt, Record

# Given the stream head, return the record to append and its receipt. Must be pure.
type AppendBuilder = Callable[[ChainHead], tuple[ChainedRecord, Receipt]]


class Store(Protocol):
    async def append(self, stream: str, build: AppendBuilder) -> Receipt:
        """Atomically append ``build(head)``. Raises ``StoreError`` if the write did not happen."""
        ...

    async def get(self, record_id: str) -> tuple[Record, Receipt] | None: ...

    async def read_raw(self, stream: str) -> list[dict[str, Any]]:
        """Records as stored, in seq order, as plain JSON objects.

        Untyped on purpose: this is the input to verification, which must see edited rows
        as they are rather than fail while loading them.
        """
        ...


def check_built(head: ChainHead, stream: str, item: ChainedRecord, receipt: Receipt) -> None:
    """Defensive check every store runs on a builder's output before writing it."""
    record = item.record
    if record.seq != head.seq + 1 or receipt.seq != record.seq:
        raise LedgerError(
            f"builder produced seq {record.seq} after {head.seq}", code="store.bad_seq"
        )
    if record.submission.stream != stream or receipt.stream != stream:
        raise LedgerError("builder produced a record for another stream", code="store.bad_stream")


class MemoryStore:
    """Process-local store. Not durable; for tests and the offline demo only."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._streams: dict[str, list[ChainedRecord]] = {}
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._receipts: dict[str, Receipt] = {}

    async def append(self, stream: str, build: AppendBuilder) -> Receipt:
        async with self._lock:
            history = self._streams.setdefault(stream, [])
            head = history[-1].head if history else GENESIS
            item, receipt = build(head)
            check_built(head, stream, item, receipt)
            if item.record.submission.record_id in self._receipts:
                raise LedgerError("record_id already in the ledger", code="ledger.duplicate_record")
            history.append(item)
            self._rows.setdefault(stream, []).append(item.record.to_json())
            self._receipts[receipt.record_id] = receipt
            return receipt

    async def get(self, record_id: str) -> tuple[Record, Receipt] | None:
        receipt = self._receipts.get(record_id)
        if receipt is None:
            return None
        row = self._rows[receipt.stream][receipt.seq - 1]
        return Record.from_json(copy.deepcopy(row)), receipt

    async def read_raw(self, stream: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._rows.get(stream, []))

    def history_for_tests(self, stream: str) -> list[ChainedRecord]:
        return list(self._streams.get(stream, []))

    def tamper_for_tests(
        self, stream: str, seq: int, edit: Callable[[dict[str, Any]], Any]
    ) -> None:
        """Edit a stored row in place, like a database admin would. Tests only."""
        edit(self._rows[stream][seq - 1])
