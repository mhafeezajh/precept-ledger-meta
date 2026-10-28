"""Every check pl-verify runs on a bundle. Pure: bytes and trusted keys in, a report out.

This is a second implementation of the ledger's checks, built only on ``ledger.format``,
``ledger.checkpoint``, ``ledger.cosignature`` and the ``merkle`` core (skill §2). A
differential test runs it against ``ledger.chain`` on the same inputs (D32).

The chain walk stops at the first break so one edit is not reported as a cascade; receipts
and checkpoints are still checked against the records as given, which is what names the
edited record from more than one direction.
"""

import hmac
from dataclasses import dataclass, field

from common.errors import LedgerError, VerificationError
from ledger.checkpoint import Checkpoint, open_checkpoint
from ledger.cosignature import open_cosignatures
from ledger.format import GENESIS_HASH, Receipt, Record, chain_hash, leaf_hash, parse_canonical
from merkle.proofs import root_hash
from verify.bundle import Bundle
from verify.keys import TrustedKeys, WitnessPolicy


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str
    seq: int | None = None  # None when the problem cannot be pinned to one record
    record_id: str | None = None


@dataclass(frozen=True)
class Report:
    stream: str
    origin: str
    records: int
    chain_verified_through: int  # last seq whose chain and producer signature verified
    receipts: int
    checkpoints: int  # signed checkpoints that match the records
    latest: Checkpoint | None
    unanchored: int  # records after the latest matching checkpoint
    findings: tuple[Finding, ...]
    quorum: int | None = None  # set when witness keys were given
    final: Checkpoint | None = None  # latest matching checkpoint with a witness quorum
    final_cosigners: tuple[str, ...] = ()

    @property
    def unwitnessed(self) -> int | None:
        """Records after the latest final checkpoint, if witnesses were checked."""
        if self.quorum is None:
            return None
        return self.records - (self.final.size if self.final else 0)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def first_break(self) -> Finding | None:
        """The finding with the lowest seq; unpinned findings come last."""
        return min(self.findings, key=_order, default=None)


def _order(finding: Finding) -> tuple[bool, int]:
    return (finding.seq is None, finding.seq or 0)


@dataclass
class _Walk:
    leaves: list[bytes] = field(default_factory=list)
    chain: list[bytes] = field(default_factory=list)  # chain hashes of the verified prefix
    record_ids: list[str | None] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _record_problem(
    record: Record, seq: int, prev: bytes, stream: str, seen: set[str], keys: TrustedKeys
) -> tuple[str, str] | None:
    # Same checks, same order and same names as ledger.chain (differential test).
    sub = record.submission
    if sub.stream != stream:
        return "wrong_stream", f"record is in {sub.stream!r}"
    if record.seq != seq:
        return "seq_mismatch", f"found seq {record.seq} at position {seq}"
    if not hmac.compare_digest(record.prev_hash_bytes(), prev):
        return "prev_hash_mismatch", "prev_hash is not the chain hash of the record before"
    if sub.record_id in seen:
        return "duplicate_record_id", sub.record_id
    producer = sub.producer
    problem = keys.check_producer(
        producer.id, producer.key_id, sub.signing_bytes(), producer.sig_bytes()
    )
    return None if problem is None else ("bad_producer_signature", problem)


def _check_records(bundle: Bundle, keys: TrustedKeys) -> _Walk:
    walk, prev, seen, broken = _Walk(), GENESIS_HASH, set[str](), False
    for seq, line in enumerate(bundle.records, start=1):
        walk.leaves.append(leaf_hash(line))  # leaves cover the bytes as exported
        try:
            record = Record.from_json(parse_canonical(line))
        except LedgerError as exc:
            walk.record_ids.append(None)
            if not broken:
                walk.findings.append(Finding("malformed", str(exc), seq))
            broken = True
            continue
        record_id = record.submission.record_id
        walk.record_ids.append(record_id)
        if broken:
            continue
        problem = _record_problem(record, seq, prev, bundle.stream, seen, keys)
        if problem is not None:
            walk.findings.append(Finding(*problem, seq, record_id))
            broken = True
            continue
        seen.add(record_id)
        prev = chain_hash(prev, line)
        walk.chain.append(prev)
    return walk


def _receipt_mismatch(receipt: Receipt, stream: str, walk: _Walk) -> str | None:
    index = receipt.seq - 1
    if receipt.stream != stream or receipt.record_id != walk.record_ids[index]:
        return f"receipt is for {receipt.record_id}, the export has {walk.record_ids[index]}"
    if not hmac.compare_digest(receipt.leaf_hash_bytes(), walk.leaves[index]):
        return "leaf hash differs from the receipt"
    if index < len(walk.chain) and not hmac.compare_digest(
        receipt.chain_hash_bytes(), walk.chain[index]
    ):
        return "chain hash differs from the receipt"
    return None


def _check_receipts(bundle: Bundle, keys: TrustedKeys, walk: _Walk) -> list[Finding]:
    findings, seen = [], set[int]()
    for line in bundle.receipts:
        try:
            receipt = Receipt.from_json(parse_canonical(line))
        except LedgerError as exc:
            findings.append(Finding("malformed_receipt", str(exc)))
            continue
        seq, record_id = receipt.seq, receipt.record_id
        if seq in seen:
            findings.append(Finding("duplicate_receipt", "two receipts for one seq", seq))
            continue
        seen.add(seq)
        problem = keys.check_log(receipt.key_id, receipt.signing_bytes(), receipt.sig_bytes())
        if problem is not None:
            findings.append(Finding("bad_receipt_signature", problem, seq, record_id))
        elif seq > len(walk.leaves):
            detail = "signed receipt, but the export has no record at this seq"
            findings.append(Finding("receipt_without_record", detail, seq, record_id))
        elif (mismatch := _receipt_mismatch(receipt, bundle.stream, walk)) is not None:
            findings.append(Finding("receipt_mismatch", mismatch, seq, record_id))
    for seq in range(1, len(walk.leaves) + 1):
        if seq not in seen:
            findings.append(Finding("missing_receipt", "no receipt", seq, walk.record_ids[seq - 1]))
    return findings


def _open(note: str, keys: TrustedKeys, origin: str) -> Checkpoint | str:
    problem = "no trusted log key"
    for key in keys.log_note_keys(origin):
        try:
            return open_checkpoint(note, key)
        except LedgerError as exc:
            problem = str(exc)
    return problem


def _against_records(cp: Checkpoint, leaves: list[bytes], prefix: str) -> Finding | None:
    if cp.size > len(leaves):
        detail = f"checkpoint covers {cp.size} records, the export has {len(leaves)}"
        return Finding(f"{prefix}beyond_records", detail, len(leaves) + 1)
    if not hmac.compare_digest(root_hash(leaves[: cp.size]), cp.root):
        return Finding(f"{prefix}mismatch", f"root of records 1..{cp.size} differs")
    return None


def _check_checkpoints(
    bundle: Bundle, keys: TrustedKeys, leaves: list[bytes]
) -> tuple[list[tuple[Checkpoint, str]], list[Finding]]:
    matching, findings, by_size = [], [], dict[int, Checkpoint]()
    for note in bundle.checkpoints:
        opened = _open(note, keys, bundle.origin)
        if isinstance(opened, str):
            findings.append(Finding("bad_checkpoint", opened))
            continue
        earlier = by_size.setdefault(opened.size, opened)
        if earlier is not opened:
            if earlier.root != opened.root:  # two signed roots for one size: split view
                detail = f"two signed roots for size {opened.size}"
                findings.append(Finding("checkpoint_conflict", detail))
            continue
        problem = _against_records(opened, leaves, "checkpoint_")
        if problem is None:
            matching.append((opened, note))
        else:
            findings.append(problem)
    return matching, findings


def _check_trusted(
    note: str, keys: TrustedKeys, bundle: Bundle, leaves: list[bytes]
) -> list[Finding]:
    opened = _open(note, keys, bundle.origin)
    if isinstance(opened, str):
        raise VerificationError(
            f"trusted checkpoint does not verify: {opened}", code="verify.bad_trusted_checkpoint"
        )
    problem = _against_records(opened, leaves, "trusted_")
    if problem is None:
        return []
    rename = {"trusted_beyond_records": "truncated_since_trusted"}
    check = rename.get(problem.check, "rewritten_since_trusted")
    return [
        Finding(check, f"trusted checkpoint (size {opened.size}): {problem.detail}", problem.seq)
    ]


@dataclass(frozen=True)
class _Quorum:
    final: Checkpoint | None
    cosigners: tuple[str, ...]
    findings: list[Finding]


def _check_quorum(
    matching: list[tuple[Checkpoint, str]], policy: WitnessPolicy, records: int
) -> _Quorum:
    """Which matching checkpoints carry ``quorum`` valid cosignatures. The latest one must:
    a checkpoint the witnesses did not back is not trusted, however well it is signed (R4)."""
    final, cosigners, findings = None, tuple[str, ...](), []
    counts: dict[int, int] = {}
    for checkpoint, note in matching:
        try:
            names = open_cosignatures(note, policy.keys)
        except LedgerError as exc:
            detail = f"checkpoint {checkpoint.size}: {exc}"
            findings.append(Finding("bad_cosignature", detail))
            names = {}
        counts[checkpoint.size] = len(names)
        if len(names) >= policy.quorum and (final is None or checkpoint.size > final.size):
            final, cosigners = checkpoint, tuple(sorted(names))
    latest = max((cp for cp, _ in matching), key=lambda cp: cp.size, default=None)
    if latest is not None and counts[latest.size] < policy.quorum:
        detail = (
            f"latest checkpoint (size {latest.size}) has {counts[latest.size]} of"
            f" {policy.quorum} witness cosignatures"
        )
        findings.append(Finding("below_quorum", detail))
    elif latest is None and records:
        findings.append(Finding("below_quorum", "no checkpoint carries witness cosignatures"))
    return _Quorum(final, cosigners, findings)


def verify_bundle(
    bundle: Bundle,
    keys: TrustedKeys,
    *,
    trusted_checkpoint: str | None = None,
    witnesses: WitnessPolicy | None = None,
) -> Report:
    """Check records, receipts and checkpoints; optionally a checkpoint the auditor held and
    the witness quorum."""
    walk = _check_records(bundle, keys)
    findings = [*walk.findings, *_check_receipts(bundle, keys, walk)]
    matching, checkpoint_findings = _check_checkpoints(bundle, keys, walk.leaves)
    findings += checkpoint_findings
    if trusted_checkpoint is not None:
        findings += _check_trusted(trusted_checkpoint, keys, bundle, walk.leaves)
    quorum = None
    if witnesses is not None:
        quorum = _check_quorum(matching, witnesses, len(walk.leaves))
        findings += quorum.findings
    latest = max((cp for cp, _ in matching), key=lambda cp: cp.size, default=None)
    return Report(
        stream=bundle.stream,
        origin=bundle.origin,
        records=len(walk.leaves),
        chain_verified_through=len(walk.chain),
        receipts=len(bundle.receipts),
        checkpoints=len(matching),
        latest=latest,
        unanchored=len(walk.leaves) - (latest.size if latest else 0),
        findings=tuple(findings),
        quorum=None if witnesses is None else witnesses.quorum,
        final=None if quorum is None else quorum.final,
        final_cosigners=() if quorum is None else quorum.cosigners,
    )
