"""The witness: cosigns a checkpoint only if it extends the last one it cosigned.

Checks follow C2SP tlog-witness, in order: the note is a checkpoint of a known log, signed by
that log's key; ``old`` is not beyond the new size and equals the size this witness last
cosigned; the consistency proof from there verifies. Only then is the new checkpoint saved
and cosigned. The check and the save run under one lock, so two requests cannot both extend
the same old size (no rollback race).

A refusal after a valid log signature is evidence that the operator signed a history that
does not extend the one it showed before: it is kept and logged as an alert (flow D step 4).
"""

import asyncio
import hmac
import logging
from collections.abc import Iterable, Sequence

from common.clock import Clock
from common.errors import FormatError, LedgerError, SignatureError, WitnessError
from common.logging import log_event
from ledger.checkpoint import Checkpoint, NoteKey, NoteSigner, open_checkpoint, peek_checkpoint
from ledger.cosignature import CosignerKey, add_cosignatures, cosign, open_cosignatures
from merkle.hashing import EMPTY_ROOT
from merkle.proofs import verify_consistency
from witness.state import Cosigned, WitnessState

logger = logging.getLogger("pl.witness")


def _consistency_problem(
    latest: Cosigned | None, checkpoint: Checkpoint, proof: Sequence[bytes]
) -> str | None:
    if checkpoint.size == 0 and not hmac.compare_digest(checkpoint.root, EMPTY_ROOT):
        return "an empty tree must have the empty root"
    if latest is None:  # old size is 0: every tree extends the empty tree
        return "old size 0 takes no proof" if proof else None
    if latest.size == checkpoint.size:
        same = not proof and hmac.compare_digest(latest.root, checkpoint.root)
        return None if same else f"a different root for size {checkpoint.size}"
    if not verify_consistency(latest.size, checkpoint.size, latest.root, checkpoint.root, proof):
        return f"no valid consistency proof from size {latest.size} to {checkpoint.size}"
    return None


class Witness:
    def __init__(
        self,
        name: str,
        signer: NoteSigner,
        logs: Iterable[NoteKey],
        state: WitnessState,
        clock: Clock,
    ) -> None:
        self.name = name
        self.key = CosignerKey(name, signer.public_key_raw)
        self._signer, self._state, self._clock = signer, state, clock
        self._logs = {key.name: key for key in logs}  # origin -> the log's note key
        if not self._logs:
            raise WitnessError("a witness needs at least one log key", code="witness.no_logs")
        self._lock = asyncio.Lock()

    @property
    def origins(self) -> list[str]:
        return sorted(self._logs)

    async def latest(self, origin: str) -> Cosigned | None:
        """The last checkpoint cosigned for ``origin``, re-verified: bad state fails closed."""
        saved = await self._state.latest(origin)
        if saved is None:
            return None
        key = self._logs.get(origin)
        try:
            checkpoint = None if key is None else open_checkpoint(saved.note, key)
            note = add_cosignatures(saved.note, [saved.cosignature])
            times = open_cosignatures(note, [self.key])
        except LedgerError as exc:
            raise WitnessError("saved state does not verify", code="witness.state_invalid") from exc
        expected = Checkpoint(origin, saved.size, saved.root)
        if checkpoint is None or checkpoint != expected or times != {self.name: saved.timestamp}:
            raise WitnessError("saved state does not match itself", code="witness.state_invalid")
        return saved

    async def latest_note(self, origin: str) -> str | None:
        saved = await self.latest(origin)
        return None if saved is None else add_cosignatures(saved.note, [saved.cosignature])

    def _open(self, note: str) -> Checkpoint:
        try:
            origin = peek_checkpoint(note).origin
        except FormatError as exc:
            raise WitnessError("not a checkpoint note", code="witness.bad_request") from exc
        key = self._logs.get(origin)
        if key is None:
            raise WitnessError(f"unknown log {origin!r}", code="witness.unknown_log")
        try:
            return open_checkpoint(note, key)
        except SignatureError as exc:
            raise WitnessError(
                "checkpoint is not signed by the log key", code="witness.bad_signature"
            ) from exc
        except FormatError as exc:
            raise WitnessError("malformed checkpoint", code="witness.bad_request") from exc

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        """Check, save, then return this witness's cosignature line. ``WitnessError`` if not."""
        checkpoint = self._open(note)
        if old_size > checkpoint.size:
            raise WitnessError(
                f"old size {old_size} is beyond the checkpoint size {checkpoint.size}",
                code="witness.bad_old_size",
            )
        async with self._lock:
            latest = await self.latest(checkpoint.origin)
            current = latest.size if latest is not None else 0
            if old_size != current:
                raise WitnessError(
                    f"this witness last cosigned size {current}, not {old_size}",
                    code="witness.conflict",
                    latest_size=current,
                )
            problem = _consistency_problem(latest, checkpoint, proof)
            if problem is not None:
                await self._refuse(checkpoint, note, problem)
            timestamp = int(self._clock.now().timestamp())
            if latest is not None:
                timestamp = max(timestamp, latest.timestamp)  # cosignature times never go back
            line = cosign(checkpoint, self._signer, self.name, timestamp)
            await self._state.save(
                Cosigned(checkpoint.origin, checkpoint.size, checkpoint.root, note, line, timestamp)
            )
        log_event(logger, logging.INFO, "cosigned", origin=checkpoint.origin, size=checkpoint.size)
        return line

    async def _refuse(self, checkpoint: Checkpoint, note: str, problem: str) -> None:
        code = "witness.inconsistent"
        await self._state.record_refusal(checkpoint.origin, note, code)
        # A log-signed checkpoint that does not extend what we cosigned: alert (flow D).
        log_event(
            logger,
            logging.CRITICAL,
            "witness alert: inconsistent checkpoint",
            origin=checkpoint.origin,
            size=checkpoint.size,
            code=code,
        )
        raise WitnessError(problem, code=code)
