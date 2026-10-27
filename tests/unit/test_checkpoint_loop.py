"""Background checkpointing: every stream each tick; one failing stream never blocks another."""

import asyncio
import logging
from pathlib import Path

import pytest

from common.errors import LedgerError
from ledger.checkpoint_loop import CheckpointLoop
from ledger.checkpointer import Checkpointer, DirectorySink, SignedCheckpoint
from merkle.hashing import leaf_hash
from merkle.log import LogTree
from merkle.store import MemoryTreeStore
from tests.bundles import LOG, ORIGIN


def checkpointer(directory: Path, leaves: int) -> Checkpointer:
    tree = MemoryTreeStore()
    for i in range(leaves):
        tree.append(leaf_hash(bytes([i])))
    return Checkpointer(LogTree(tree), LOG, ORIGIN, [DirectorySink(directory)])


class Refusing:
    async def publish(self) -> SignedCheckpoint:
        raise LedgerError("tree does not extend", code="checkpoint.inconsistent")


class Crashing:
    async def publish(self) -> SignedCheckpoint:
        raise RuntimeError("bug")


async def test_run_once_publishes_each_stream_and_isolates_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    loop = CheckpointLoop({"a/s": Refusing(), "b/s": checkpointer(tmp_path, 3)}, 10)
    with caplog.at_level(logging.ERROR):
        results = await loop.run_once()
    assert isinstance(results["a/s"], LedgerError)
    signed = results["b/s"]
    assert isinstance(signed, SignedCheckpoint)
    assert signed.checkpoint.size == 3
    logged = [getattr(r, "fields", {}) for r in caplog.records]
    assert {"stream": "a/s", "code": "checkpoint.inconsistent"} in logged
    assert loop.status() == "running"
    assert loop.last_errors() == {"a/s": "checkpoint.inconsistent"}


async def test_run_forever_ticks_until_cancelled(tmp_path: Path) -> None:
    loop = CheckpointLoop({"b/s": checkpointer(tmp_path, 1)}, 0.01)
    task = asyncio.create_task(loop.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.ticks >= 2


async def test_unexpected_error_stops_the_loop_visibly() -> None:
    loop = CheckpointLoop({"a/s": Crashing()}, 0.01)
    with pytest.raises(RuntimeError):
        await loop.run_forever()
    assert loop.status() == "stopped"


def test_needs_a_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval"):
        CheckpointLoop({}, 0)
