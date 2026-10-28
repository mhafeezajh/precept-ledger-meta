"""Anchors each final checkpoint outside the ledger (ARCHITECTURE layer 6, flow D step 3).

- RFC 3161: every final checkpoint is sent to each configured timestamp authority that has
  not timestamped that size yet. Tokens are verified before they are stored (R5).
- OpenTimestamps: at most once per ``ots_interval_seconds`` (an hour), the latest final
  checkpoint is submitted to each calendar, and pending proofs are upgraded once Bitcoin has
  confirmed them. Upgrades are fetched only from calendars configured here, never from a URL
  a proof names (no request goes where an outside party points it).

Rows are insert-only: an upgraded proof is a new ``confirmed`` row next to the ``pending``
one, so nothing a verifier saw is ever replaced (D47).
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cryptography import x509

from anchor.ots import read_timestamp, upgrade_timestamp
from anchor.rfc3161 import anchor_digest, verify_token
from common.clock import Clock
from common.errors import AnchorError, LedgerError, PreceptError
from common.logging import log_event
from ledger.checkpoint import Checkpoint
from ledger.checkpointer import SignedCheckpoint

logger = logging.getLogger("pl.ledger.anchoring")

RFC3161, OTS = "rfc3161", "ots"
VERIFIED, PENDING, CONFIRMED = "verified", "pending", "confirmed"


@dataclass(frozen=True)
class AnchorRow:
    size: int
    kind: str  # rfc3161 | ots
    source: str  # TSA name, or calendar URL
    status: str  # verified (rfc3161) | pending | confirmed (ots)
    token: bytes  # DER timestamp token, or serialized OpenTimestamps timestamp
    digest: bytes  # anchor_digest of that checkpoint, kept for upgrades


class AnchorStore(Protocol):
    async def add(self, row: AnchorRow) -> None:
        """Keep the first row per (size, kind, source, status); later ones are ignored."""
        ...

    async def rows(self) -> list[AnchorRow]:
        """Every row, smallest size first."""
        ...


@dataclass
class MemoryAnchorStore:
    stored: dict[tuple[int, str, str, str], AnchorRow] = field(default_factory=dict)

    async def add(self, row: AnchorRow) -> None:
        self.stored.setdefault((row.size, row.kind, row.source, row.status), row)

    async def rows(self) -> list[AnchorRow]:
        return [self.stored[key] for key in sorted(self.stored)]


class TimestampAuthority(Protocol):
    name: str

    async def timestamp(self, digest: bytes) -> bytes:
        """A token for ``digest`` that has already been verified; ``AnchorError`` if not."""
        ...


class Calendar(Protocol):
    url: str

    async def submit(self, digest: bytes) -> bytes: ...

    async def upgrade(self, commitment: bytes) -> bytes | None: ...


@dataclass(frozen=True)
class AnchorResult:
    size: int
    timestamped_by: tuple[str, ...]
    failures: dict[str, str]  # source -> error code


class Anchorer:
    def __init__(
        self,
        store: AnchorStore,
        authorities: Sequence[TimestampAuthority],
        calendars: Sequence[Calendar] = (),
        upgrade_calendars: Mapping[str, Calendar] | None = None,
        *,
        clock: Clock,
        ots_interval_seconds: float = 3600.0,
    ) -> None:
        self.store = store
        self._authorities, self._calendars = list(authorities), list(calendars)
        self._upgrade = dict(upgrade_calendars or {})
        self._clock, self._interval = clock, ots_interval_seconds
        self._last_ots: datetime | None = None

    @property
    def requires_timestamp(self) -> bool:
        return bool(self._authorities)

    async def anchor(self, signed: SignedCheckpoint) -> AnchorResult:
        size, digest = signed.checkpoint.size, anchor_digest(signed.checkpoint)
        rows = await self.store.rows()
        done = {r.source for r in rows if r.size == size and r.kind == RFC3161}
        failures: dict[str, str] = {}
        for tsa in self._authorities:
            if tsa.name in done:
                continue
            try:
                token = await tsa.timestamp(digest)
            except PreceptError as exc:
                failures[tsa.name] = exc.code
                log_event(logger, logging.WARNING, "timestamp failed", tsa=tsa.name, code=exc.code)
                continue
            await self.store.add(AnchorRow(size, RFC3161, tsa.name, VERIFIED, token, digest))
            done.add(tsa.name)
        if self._ots_due():
            failures |= await self._submit_ots(size, digest, rows)
            failures |= await self._upgrade_ots(await self.store.rows())
        return AnchorResult(size, tuple(sorted(done)), failures)

    def _ots_due(self) -> bool:
        if not self._calendars:
            return False
        now = self._clock.now()
        if self._last_ots is not None and (now - self._last_ots).total_seconds() < self._interval:
            return False
        self._last_ots = now
        return True

    async def _submit_ots(self, size: int, digest: bytes, rows: list[AnchorRow]) -> dict[str, str]:
        failures: dict[str, str] = {}
        done = {r.source for r in rows if r.size == size and r.kind == OTS}
        for calendar in self._calendars:
            if calendar.url in done:
                continue
            try:
                token = await calendar.submit(digest)
            except PreceptError as exc:
                failures[calendar.url] = exc.code
                log_event(logger, logging.WARNING, "OTS submit failed", calendar=calendar.url,
                          code=exc.code)  # fmt: skip
                continue
            await self.store.add(AnchorRow(size, OTS, calendar.url, PENDING, token, digest))
        return failures

    async def _upgrade_ots(self, rows: list[AnchorRow]) -> dict[str, str]:
        confirmed = {(r.size, r.source) for r in rows if r.kind == OTS and r.status == CONFIRMED}
        failures: dict[str, str] = {}
        for row in rows:
            if row.kind != OTS or row.status != PENDING or (row.size, row.source) in confirmed:
                continue
            try:
                upgraded = await self._upgrade_row(row)
            except PreceptError as exc:
                failures[row.source] = exc.code
                continue
            if upgraded is not None:
                await self.store.add(
                    AnchorRow(row.size, OTS, row.source, CONFIRMED, upgraded, row.digest)
                )
                log_event(logger, logging.INFO, "OTS confirmed", size=row.size,
                          calendar=row.source)  # fmt: skip
        return failures

    async def _upgrade_row(self, row: AnchorRow) -> bytes | None:
        token = row.token
        for attestation in read_timestamp(token, row.digest):
            calendar = self._upgrade.get(attestation.calendar or "")
            if attestation.kind != "pending" or calendar is None:
                continue  # only calendars configured here are ever asked
            answer = await calendar.upgrade(attestation.commitment)
            if answer is None:
                continue
            token = upgrade_timestamp(token, row.digest, attestation.commitment, answer)
        if not any(a.kind == "bitcoin" for a in read_timestamp(token, row.digest)):
            return None
        return token


@dataclass(frozen=True)
class AnchorView:
    """A stream's anchorer, plus the TSA roots its stored tokens are re-verified with."""

    anchorer: Anchorer
    tsa_roots: tuple[x509.Certificate, ...]

    async def summary(self, checkpoint: Checkpoint) -> list[dict[str, Any]]:
        """Every stored anchor of ``checkpoint``, verified again; ``anchor.unverifiable`` if
        one no longer verifies (an edited row is never served, R5)."""
        digest = anchor_digest(checkpoint)
        out: list[dict[str, Any]] = []
        for row in await self.anchorer.store.rows():
            if row.size != checkpoint.size:
                continue
            try:
                if row.kind == "rfc3161":
                    info = verify_token(row.token, digest, self.tsa_roots)
                    gen_time = info.gen_time.isoformat()
                    out.append({"kind": row.kind, "source": row.source, "status": row.status,
                                "gen_time": gen_time, "tsa": info.tsa})  # fmt: skip
                    continue
                attestations = read_timestamp(row.token, digest)
            except AnchorError as exc:
                raise LedgerError(
                    f"stored {row.kind} anchor for size {row.size} does not verify",
                    code="anchor.unverifiable",
                ) from exc
            out.append({
                "kind": row.kind, "source": row.source, "status": row.status,
                "attestations": [{"kind": a.kind, "calendar": a.calendar, "height": a.height}
                                 for a in attestations],
            })  # fmt: skip
        return out


class Publisher(Protocol):
    async def publish(self) -> SignedCheckpoint: ...


class AnchoredPublisher:
    """Publishes (and, if configured, witnesses) a checkpoint, then anchors it.

    The inner publisher raises when a checkpoint is not final, so only final checkpoints are
    anchored. Without any RFC 3161 timestamp the checkpoint stays stored and the loop
    reports ``anchor.no_timestamp``; OpenTimestamps problems are logged only.
    """

    def __init__(self, inner: Publisher, anchorer: Anchorer) -> None:
        self._inner, self._anchorer = inner, anchorer

    async def publish(self) -> SignedCheckpoint:
        signed = await self._inner.publish()
        result = await self._anchorer.anchor(signed)
        if self._anchorer.requires_timestamp and not result.timestamped_by:
            raise AnchorError(
                f"checkpoint {result.size} has no RFC 3161 timestamp ({result.failures})",
                code="anchor.no_timestamp",
            )
        return signed
