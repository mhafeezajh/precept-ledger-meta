"""Anchor checks for pl-verify: RFC 3161 tokens and OpenTimestamps proofs in a bundle (D48).

Every anchor must belong to a checkpoint that matched the records, and commit to that
checkpoint's ``anchor_digest``. With trusted TSA roots (``--tsa-roots``):

- each RFC 3161 token must verify (``bad_timestamp`` otherwise);
- some checkpoint must carry a trusted timestamp when there are records (``not_timestamped``);
- back-dating: for each record, the earliest trusted timestamp of a checkpoint that covers
  it must come no more than ``max_lag_seconds`` after the record's own ``ts``
  (``late_timestamp``), and not before it, beyond ``skew_seconds`` (``record_after_timestamp``).
  An operator who rebuilds history later, even with every witness, can sign old ``ts`` values
  but cannot get a TSA to sign an old time.

OpenTimestamps proofs are read from the digest. A Bitcoin attestation names a block height
and the merkle root it claims; checking that needs a Bitcoin node, so it is reported, not
decided, offline.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from cryptography import x509

from anchor.ots import read_timestamp
from anchor.rfc3161 import anchor_digest, verify_token
from common.errors import AnchorError
from ledger.checkpoint import Checkpoint
from verify.bundle import BundleAnchor
from verify.findings import Finding


@dataclass(frozen=True)
class AnchorPolicy:
    roots: tuple[x509.Certificate, ...]
    max_lag_seconds: int = 3600
    skew_seconds: int = 300


@dataclass(frozen=True)
class Timestamped:
    size: int
    gen_time: datetime
    tsa: str


@dataclass(frozen=True)
class BitcoinClaim:
    size: int
    status: str  # pending | bitcoin | unknown
    height: int | None
    commitment: bytes  # for bitcoin: the block merkle root the proof claims
    calendar: str | None


@dataclass(frozen=True)
class AnchorReport:
    checked: bool  # TSA roots were given
    tokens: int
    timestamps: tuple[Timestamped, ...]
    bitcoin: tuple[BitcoinClaim, ...]
    findings: tuple[Finding, ...]

    @property
    def latest(self) -> Timestamped | None:
        return max(self.timestamps, key=lambda t: (t.size, t.gen_time), default=None)


def _read(
    anchor: BundleAnchor, checkpoint: Checkpoint, policy: AnchorPolicy | None
) -> tuple[list[Timestamped], list[BitcoinClaim]]:
    digest = anchor_digest(checkpoint)
    if anchor.kind == "rfc3161":
        if policy is None:
            return [], []
        info = verify_token(anchor.data, digest, policy.roots)
        return [Timestamped(anchor.size, info.gen_time, info.tsa)], []
    claims = [
        BitcoinClaim(anchor.size, a.kind, a.height, a.commitment, a.calendar)
        for a in read_timestamp(anchor.data, digest)
    ]
    return [], claims


def _time_findings(
    stamps: Sequence[Timestamped],
    record_times: Sequence[datetime | None],
    record_ids: Sequence[str | None],
    policy: AnchorPolicy,
) -> list[Finding]:
    findings: list[Finding] = []
    # covering[s-1]: earliest trusted time of any timestamped checkpoint of size >= s
    covering: list[datetime | None] = [None] * len(record_times)
    for stamp in stamps:
        for index in range(min(stamp.size, len(record_times))):
            current = covering[index]
            covering[index] = stamp.gen_time if current is None else min(current, stamp.gen_time)
    late = early = None
    late_count = early_count = 0
    for index, (claimed, proven) in enumerate(zip(record_times, covering, strict=True)):
        if claimed is None or proven is None:
            continue
        lag = (proven - claimed).total_seconds()
        if lag > policy.max_lag_seconds:
            late_count += 1
            late = late or (index, lag)
        elif -lag > policy.skew_seconds:
            early_count += 1
            early = early or (index, lag)
    if late is not None:
        detail = (f"first proven {int(late[1])} s after its ts (limit {policy.max_lag_seconds} s);"
                  f" {late_count} records")  # fmt: skip
        findings.append(Finding("late_timestamp", detail, late[0] + 1, record_ids[late[0]]))
    if early is not None:
        detail = f"ts is {int(-early[1])} s after a timestamp covering it; {early_count} records"
        findings.append(
            Finding("record_after_timestamp", detail, early[0] + 1, record_ids[early[0]])
        )
    return findings


def check_anchors(
    anchors: Sequence[BundleAnchor],
    checkpoints: Mapping[int, Checkpoint],
    record_times: Sequence[datetime | None],
    record_ids: Sequence[str | None],
    policy: AnchorPolicy | None,
) -> AnchorReport:
    """``checkpoints``: the bundle's checkpoints that matched its records, by size."""
    findings: list[Finding] = []
    stamps: list[Timestamped] = []
    claims: list[BitcoinClaim] = []
    for anchor in anchors:
        checkpoint = checkpoints.get(anchor.size)
        if checkpoint is None:
            detail = f"{anchor.kind} anchor for size {anchor.size}, which no valid checkpoint has"
            findings.append(Finding("anchor_without_checkpoint", detail))
            continue
        try:
            found_stamps, found_claims = _read(anchor, checkpoint, policy)
        except AnchorError as exc:
            check = "bad_timestamp" if anchor.kind == "rfc3161" else "bad_ots"
            findings.append(Finding(check, f"checkpoint {anchor.size}: {exc}"))
            continue
        stamps += found_stamps
        claims += found_claims
    if policy is not None:
        if any(t is not None for t in record_times) and not stamps:
            findings.append(
                Finding("not_timestamped", "no checkpoint carries a trusted RFC 3161 timestamp")
            )
        findings += _time_findings(stamps, record_times, record_ids, policy)
    return AnchorReport(
        checked=policy is not None,
        tokens=sum(a.kind == "rfc3161" for a in anchors),
        timestamps=tuple(stamps),
        bitcoin=tuple(claims),
        findings=tuple(findings),
    )
