"""Hash chain: link submissions into a stream and verify a stream from its first record.

Pure functions only. Verification stops at the first break and names its sequence number,
which is what demo beat 4 shows.
"""

import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from common.errors import FormatError, SignatureError
from ledger.format import (
    GENESIS_HASH,
    Record,
    Submission,
    b64encode,
    chain_hash,
    leaf_hash,
    parse_canonical,
)
from ledger.signing import KeyRing


@dataclass(frozen=True)
class ChainHead:
    seq: int
    chain_hash: bytes


GENESIS = ChainHead(seq=0, chain_hash=GENESIS_HASH)


@dataclass(frozen=True)
class ChainedRecord:
    """A record with the two hashes that cover it."""

    record: Record
    chain_hash: bytes
    leaf_hash: bytes

    @property
    def head(self) -> ChainHead:
        return ChainHead(self.record.seq, self.chain_hash)


def hash_record(record: Record) -> ChainedRecord:
    data = record.canonical()
    return ChainedRecord(record, chain_hash(record.prev_hash_bytes(), data), leaf_hash(data))


def link(head: ChainHead, submission: Submission) -> ChainedRecord:
    """Place ``submission`` right after ``head``. Does not check the producer signature."""
    record = Record(submission, seq=head.seq + 1, prev_hash=b64encode(head.chain_hash))
    return hash_record(record)


class BreakReason(StrEnum):
    MALFORMED = "malformed"
    WRONG_STREAM = "wrong_stream"
    SEQ_MISMATCH = "seq_mismatch"
    PREV_HASH_MISMATCH = "prev_hash_mismatch"
    DUPLICATE_RECORD_ID = "duplicate_record_id"
    BAD_PRODUCER_SIGNATURE = "bad_producer_signature"
    TRUNCATED = "truncated"
    HEAD_MISMATCH = "head_mismatch"
    BAD_RECEIPT_SIGNATURE = "bad_receipt_signature"
    RECEIPT_MISMATCH = "receipt_mismatch"
    MISSING_RECORD = "missing_record"


@dataclass(frozen=True)
class ChainBreak:
    seq: int
    reason: BreakReason
    detail: str


@dataclass(frozen=True)
class ChainReport:
    checked: int
    head: ChainHead
    first_break: ChainBreak | None

    @property
    def ok(self) -> bool:
        return self.first_break is None


def _load(item: Record | bytes) -> Record:
    if isinstance(item, Record):
        return item
    return Record.from_json(parse_canonical(item))  # D20: lines must be canonical


def _check_one(
    record: Record, head: ChainHead, stream: str, seen: set[str], producers: KeyRing
) -> ChainBreak | None:
    expected = head.seq + 1
    sub = record.submission
    if sub.stream != stream:
        return ChainBreak(expected, BreakReason.WRONG_STREAM, f"record is in {sub.stream!r}")
    if record.seq != expected:
        return ChainBreak(expected, BreakReason.SEQ_MISMATCH, f"found seq {record.seq}")
    if not hmac.compare_digest(record.prev_hash_bytes(), head.chain_hash):
        return ChainBreak(expected, BreakReason.PREV_HASH_MISMATCH, "prev_hash is not h_(n-1)")
    if sub.record_id in seen:
        return ChainBreak(expected, BreakReason.DUPLICATE_RECORD_ID, sub.record_id)
    try:
        producers.verify_submission(sub)
    except SignatureError as exc:
        return ChainBreak(expected, BreakReason.BAD_PRODUCER_SIGNATURE, str(exc))
    return None


def _differs_from_trusted(head: ChainHead, trusted: ChainHead | None) -> ChainBreak | None:
    if trusted is None or head.seq != trusted.seq:
        return None
    if hmac.compare_digest(head.chain_hash, trusted.chain_hash):
        return None
    return ChainBreak(
        head.seq, BreakReason.HEAD_MISMATCH, "chain hash differs from the trusted head"
    )


def verify_chain(
    items: Iterable[Record | bytes],
    producers: KeyRing,
    *,
    stream: str,
    start: ChainHead = GENESIS,
    expected_head: ChainHead | None = None,
) -> ChainReport:
    """Recompute the chain from ``start``; report the first break, if any.

    ``items`` are records or canonical JSON lines from an export. ``expected_head`` is a head
    the caller already trusts (from a receipt or checkpoint); a chain that ends before it is
    truncated, and one whose hash at that seq differs has been rewritten.
    """
    head, seen, checked = start, set[str](), 0
    for item in items:
        try:
            record = _load(item)
        except FormatError as exc:
            return ChainReport(
                checked, head, ChainBreak(head.seq + 1, BreakReason.MALFORMED, str(exc))
            )
        broken = _check_one(record, head, stream, seen, producers)
        if broken is not None:
            return ChainReport(checked, head, broken)
        seen.add(record.submission.record_id)
        head = hash_record(record).head
        checked += 1
        rewritten = _differs_from_trusted(head, expected_head)
        if rewritten is not None:
            return ChainReport(checked, head, rewritten)
    if expected_head is not None and head.seq < expected_head.seq:
        detail = f"chain ends at seq {head.seq}, trusted head is at {expected_head.seq}"
        return ChainReport(checked, head, ChainBreak(head.seq + 1, BreakReason.TRUNCATED, detail))
    return ChainReport(checked, head, None)
