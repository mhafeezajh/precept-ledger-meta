"""Collects witness cosignatures for each new checkpoint (ARCHITECTURE flow D, steps 2-3).

After the checkpointer signs, every witness that has not cosigned this size yet is sent the
checkpoint with a consistency proof from the size it last cosigned (C2SP tlog-witness). A
witness that answers ``409`` gets one retry from the size it reports. Every returned line is
verified under that witness's trusted key before it is stored: a witness's word is never
taken on trust (R5). A checkpoint is final once ``quorum`` distinct witnesses cosigned it
(D5: 2 of 3). Refusals, timeouts and bugs in a client all count as "no cosignature"; none of
them makes a checkpoint final (R4).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from common.errors import LedgerError, PreceptError, WitnessError
from common.logging import log_event
from ledger.checkpointer import Checkpointer, SignedCheckpoint
from ledger.cosignature import CosignerKey, add_cosignatures, open_cosignatures
from merkle.log import LogTree

logger = logging.getLogger("pl.ledger.witnessing")

MAX_RESPONSE_LINES = 16


class WitnessClient(Protocol):
    """One witness, local or remote. ``witness.core.Witness`` fits, and so does the HTTP client."""

    name: str

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        """The witness's signature line(s); ``WitnessError`` if it refuses or is unreachable."""
        ...


class CosignatureStore(Protocol):
    """Cosignature lines of one stream's checkpoints, insert-only, one per witness per size."""

    async def add(self, size: int, witness: str, line: str) -> None:
        """Keep the first line stored for ``(size, witness)``; later ones are ignored."""
        ...

    async def lines(self, size: int) -> dict[str, str]: ...


@dataclass
class MemoryCosignatureStore:
    rows: dict[int, dict[str, str]] = field(default_factory=dict)

    async def add(self, size: int, witness: str, line: str) -> None:
        self.rows.setdefault(size, {}).setdefault(witness, line)

    async def lines(self, size: int) -> dict[str, str]:
        return dict(self.rows.get(size, {}))


@dataclass(frozen=True)
class WitnessSet:
    """The witnesses this ledger trusts for a stream, the quorum, and where lines are kept."""

    keys: tuple[CosignerKey, ...]
    quorum: int
    store: CosignatureStore

    def __post_init__(self) -> None:
        names = [key.name for key in self.keys]
        if len(set(names)) != len(names):
            raise WitnessError("two witness keys share a name", code="witness.config")
        if not 1 <= self.quorum <= len(self.keys):
            raise WitnessError(
                f"quorum {self.quorum} needs 1..{len(self.keys)} witnesses", code="witness.config"
            )

    def key_for(self, name: str) -> CosignerKey:
        for key in self.keys:
            if key.name == name:
                return key
        raise WitnessError(f"no trusted key for witness {name!r}", code="witness.config")

    async def cosigned(self, note: str, size: int) -> tuple[str, dict[str, int]]:
        """The note with its stored cosignatures, and ``{witness: time}`` of those that verify.

        A stored line that no longer verifies means the store was edited: raise, never serve.
        """
        stored = await self.store.lines(size)
        full = add_cosignatures(note, [stored[name] for name in sorted(stored)])
        try:
            return full, open_cosignatures(full, self.keys)
        except LedgerError as exc:
            raise LedgerError(
                f"stored cosignature for size {size} does not verify",
                code="cosignature.unverifiable",
            ) from exc

    def is_final(self, cosigners: dict[str, int]) -> bool:
        return len(cosigners) >= self.quorum


@dataclass(frozen=True)
class WitnessResult:
    size: int
    cosigners: tuple[str, ...]
    refused: dict[str, str]  # witness -> error code
    quorum: int

    @property
    def final(self) -> bool:
        return len(self.cosigners) >= self.quorum


def _own_line(response: str, key: CosignerKey, note: str) -> str:
    """The one line in a witness's response that is its valid cosignature of ``note``."""
    for line in response.splitlines(keepends=True)[:MAX_RESPONSE_LINES]:
        if not line.startswith(f"— {key.name} "):
            continue
        try:
            if key.name in open_cosignatures(add_cosignatures(note, [line]), [key]):
                return line
        except LedgerError:
            continue
    raise WitnessError(f"{key.name} returned no valid cosignature", code="witness.bad_cosignature")


class Witnessing:
    def __init__(
        self,
        tree: LogTree,
        witnesses: WitnessSet,
        clients: Sequence[WitnessClient],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._tree, self._set, self._timeout = tree, witnesses, timeout_seconds
        self._clients = list(clients)
        self._keys = {client.name: witnesses.key_for(client.name) for client in self._clients}
        self._known: dict[str, int] = {}  # size each witness last cosigned, as far as we know

    @property
    def witnesses(self) -> WitnessSet:
        return self._set

    async def _proof(self, name: str, old: int, size: int) -> list[bytes]:
        if old > size:
            # The witness cosigned a bigger tree than the one we are signing now: this log
            # was rolled back. Nothing honest can answer that; say so loudly.
            log_event(logger, logging.CRITICAL, "witness is ahead of the log", witness=name,
                      witness_size=old, size=size)  # fmt: skip
            raise WitnessError(
                f"{name} already cosigned size {old}, beyond {size}", code="witness.ahead"
            )
        if old in (0, size):
            return []
        return list((await self._tree.prove_consistency(old, size)).hashes)

    async def _ask(self, client: WitnessClient, signed: SignedCheckpoint) -> str:
        size, old = signed.checkpoint.size, self._known.get(client.name, 0)
        for attempt in (1, 2):
            proof = await self._proof(client.name, old, size)
            try:
                response = await asyncio.wait_for(
                    client.add_checkpoint(old, proof, signed.note), self._timeout
                )
            except WitnessError as exc:
                retry = exc.code == "witness.conflict" and exc.latest_size is not None
                if retry and attempt == 1 and exc.latest_size is not None:
                    old = exc.latest_size  # the witness told us where it is (409)
                    continue
                raise
            line = _own_line(response, self._keys[client.name], signed.note)
            self._known[client.name] = size
            return line
        raise WitnessError(f"{client.name} kept conflicting", code="witness.conflict")

    async def collect(self, signed: SignedCheckpoint) -> WitnessResult:
        size = signed.checkpoint.size
        have = await self._set.store.lines(size)
        pending = [client for client in self._clients if client.name not in have]
        outcomes = await asyncio.gather(
            *(self._ask(client, signed) for client in pending), return_exceptions=True
        )
        refused: dict[str, str] = {}
        for client, outcome in zip(pending, outcomes, strict=True):
            if isinstance(outcome, str):
                await self._set.store.add(size, client.name, outcome)
                continue
            if isinstance(outcome, PreceptError):
                refused[client.name] = outcome.code
            elif isinstance(outcome, TimeoutError):
                refused[client.name] = "witness.timeout"
            elif isinstance(outcome, Exception):
                refused[client.name] = "witness.error"  # a client bug never counts as a yes
            else:
                raise outcome  # cancellation and other BaseExceptions propagate
            log_event(logger, logging.WARNING, "witness did not cosign", witness=client.name,
                      size=size, code=refused[client.name])  # fmt: skip
        _, cosigners = await self._set.cosigned(signed.note, size)
        return WitnessResult(size, tuple(sorted(cosigners)), refused, self._set.quorum)


class WitnessedPublisher:
    """A checkpointer whose checkpoints are then witnessed; fits ``CheckpointLoop``.

    The signed checkpoint is stored either way. Without a quorum, ``publish`` raises
    ``witness.no_quorum`` so the loop logs it and health shows it.
    """

    def __init__(self, checkpointer: Checkpointer, witnessing: Witnessing) -> None:
        self._checkpointer, self._witnessing = checkpointer, witnessing

    async def publish(self) -> SignedCheckpoint:
        signed = await self._checkpointer.publish()
        result = await self._witnessing.collect(signed)
        if not result.final:
            raise WitnessError(
                f"checkpoint {result.size} has {len(result.cosigners)} of {result.quorum}"
                f" cosignatures (refused: {result.refused})",
                code="witness.no_quorum",
            )
        return signed
