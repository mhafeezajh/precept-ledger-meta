"""Signs checkpoints of one stream's Merkle tree and writes them to write-once sinks.

Before signing, the checkpointer checks that the new tree extends its last checkpoint with a
consistency proof. That stops an honest operator's process from signing a rewritten table;
it does not stop a rogue operator with the key, which is what witnesses are for (D30).
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from common.errors import LedgerError
from ledger.checkpoint import Checkpoint, NoteKey, NoteSigner, open_checkpoint, sign_checkpoint
from merkle.log import LogTree
from merkle.proofs import verify_consistency


def origin_for(base: str, stream: str) -> str:
    """Checkpoint origin, and signed-note key name, of a stream: one tree per stream (D16)."""
    return f"{base}/{stream}"


@dataclass(frozen=True)
class SignedCheckpoint:
    checkpoint: Checkpoint
    note: str


class CheckpointSink(Protocol):
    """Somewhere checkpoints are kept for one log. Writes are write-once per tree size."""

    async def write(self, signed: SignedCheckpoint) -> None:
        """Store ``signed``. Rewriting the identical note is a no-op; a different note for a
        size already stored raises ``LedgerError checkpoint.conflict``."""
        ...

    async def latest(self) -> str | None:
        """The stored note with the largest tree size, unverified."""
        ...

    async def notes(self) -> list[str]:
        """Every stored note, smallest tree size first, unverified."""
        ...


class Checkpointer:
    def __init__(
        self, tree: LogTree, signer: NoteSigner, origin: str, sinks: Sequence[CheckpointSink]
    ) -> None:
        if not sinks:
            raise LedgerError("a checkpointer needs at least one sink", code="checkpoint.no_sink")
        self._tree, self._signer, self._origin, self._sinks = tree, signer, origin, list(sinks)
        self._key = NoteKey(origin, signer.public_key_raw)

    async def publish(self) -> SignedCheckpoint:
        """Sign the current tree head and write it to every sink; fail closed on any error."""
        previous = await self._previous()
        head = await self._tree.head()
        checkpoint = Checkpoint(self._origin, head.size, head.root)
        if previous is not None:
            await self._check_extends(previous, checkpoint)
        signed = SignedCheckpoint(checkpoint, sign_checkpoint(checkpoint, self._signer))
        # Every sink, every time: writes are idempotent, so a sink that failed last time heals.
        for sink in self._sinks:
            await sink.write(signed)
        return signed

    async def _previous(self) -> Checkpoint | None:
        note = await self._sinks[0].latest()
        # A stored checkpoint we cannot verify is never trusted as the base (R4).
        return None if note is None else open_checkpoint(note, self._key)

    async def _check_extends(self, old: Checkpoint, new: Checkpoint) -> None:
        consistent = old.size <= new.size
        if consistent and old.size < new.size:
            proof = await self._tree.prove_consistency(old.size, new.size)
            consistent = verify_consistency(old.size, new.size, old.root, new.root, proof.hashes)
        elif consistent:
            consistent = old.root == new.root
        if not consistent:
            raise LedgerError(
                f"tree at size {new.size} does not extend checkpoint {old.size}",
                code="checkpoint.inconsistent",
            )


_NOTE_FILE = re.compile(r"^[0-9]{20}\.note$")


class DirectorySink:
    """One file per tree size, created exclusively and then made read-only."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, size: int) -> Path:
        return self._dir / f"{size:020d}.note"

    async def write(self, signed: SignedCheckpoint) -> None:
        await asyncio.to_thread(self._write, signed)

    def _write(self, signed: SignedCheckpoint) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(signed.checkpoint.size)
        try:
            with path.open("x", encoding="utf-8", newline="") as file:
                file.write(signed.note)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != signed.note:
                raise LedgerError(
                    f"a different checkpoint for size {signed.checkpoint.size} exists",
                    code="checkpoint.conflict",
                ) from None
            return
        path.chmod(0o444)

    async def latest(self) -> str | None:
        return await asyncio.to_thread(self._latest)

    def _latest(self) -> str | None:
        notes = self._notes()
        return notes[-1] if notes else None

    async def notes(self) -> list[str]:
        return await asyncio.to_thread(self._notes)

    def _notes(self) -> list[str]:
        if not self._dir.exists():
            return []
        names = sorted(p.name for p in self._dir.iterdir() if _NOTE_FILE.match(p.name))
        return [(self._dir / name).read_text(encoding="utf-8") for name in names]
