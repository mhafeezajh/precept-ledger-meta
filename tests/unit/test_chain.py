import hashlib
from dataclasses import replace

import pytest

from common.errors import FormatError
from ledger.chain import GENESIS, BreakReason, ChainHead, hash_record, link, verify_chain
from ledger.format import GENESIS_HASH, Record, b64encode
from ledger.signing import KeyRing
from tests.helpers import STREAM, build_chain, clock, make_submission, signer_for

GATE = signer_for("gate-1")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])


def test_genesis_head() -> None:
    assert ChainHead(seq=0, chain_hash=GENESIS_HASH) == GENESIS


def test_link_from_genesis_follows_the_formula() -> None:
    item = link(GENESIS, make_submission(GATE, clock()))
    assert item.record.seq == 1
    assert item.record.prev_hash == b64encode(bytes(32))
    data = item.record.canonical()
    assert item.chain_hash == hashlib.sha256(b"PL-chain-v1" + bytes(32) + data).digest()
    assert item.leaf_hash == hashlib.sha256(b"\x00" + data).digest()
    assert item.head == ChainHead(1, item.chain_hash)


def test_hash_record_matches_link() -> None:
    item = link(GENESIS, make_submission(GATE, clock()))
    assert hash_record(item.record) == item


def test_valid_chain_verifies_from_records_and_from_lines() -> None:
    chained = build_chain(5, GATE, clock())
    records = [c.record for c in chained]
    report = verify_chain(records, PRODUCERS, stream=STREAM)
    assert report.ok
    assert report.checked == 5
    assert report.head == chained[-1].head
    assert report.first_break is None
    lines = [r.canonical() for r in records]
    assert verify_chain(lines, PRODUCERS, stream=STREAM).head == chained[-1].head


def test_empty_chain_is_ok() -> None:
    report = verify_chain([], PRODUCERS, stream=STREAM)
    assert report.ok
    assert report.head == GENESIS


def test_suffix_verifies_from_a_known_head() -> None:
    chained = build_chain(5, GATE, clock())
    report = verify_chain(
        [c.record for c in chained[2:]], PRODUCERS, stream=STREAM, start=chained[1].head
    )
    assert report.ok
    assert report.checked == 3


def test_wrong_stream_is_reported() -> None:
    chained = build_chain(2, GATE, clock())
    report = verify_chain([c.record for c in chained], PRODUCERS, stream="other-bank/decisions")
    assert report.first_break is not None
    assert report.first_break.seq == 1
    assert report.first_break.reason is BreakReason.WRONG_STREAM


def test_unknown_producer_key_is_reported() -> None:
    chained = build_chain(2, signer_for("rogue"), clock())
    report = verify_chain([c.record for c in chained], PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    assert report.first_break.reason is BreakReason.BAD_PRODUCER_SIGNATURE
    assert "unknown_key" in report.first_break.detail


def test_malformed_line_is_reported_at_its_position() -> None:
    lines = [c.record.canonical() for c in build_chain(3, GATE, clock())]
    lines[1] = b'{"not":"a record"}'
    report = verify_chain(lines, PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    assert report.first_break.seq == 2
    assert report.first_break.reason is BreakReason.MALFORMED
    assert report.checked == 1


def test_non_canonical_line_is_malformed() -> None:
    lines = [c.record.canonical() for c in build_chain(2, GATE, clock())]
    lines[0] = lines[0] + b" "
    report = verify_chain(lines, PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    assert report.first_break.reason is BreakReason.MALFORMED
    assert report.first_break.seq == 1


def test_expected_head_hash_mismatch() -> None:
    chained = build_chain(3, GATE, clock())
    other = ChainHead(2, bytes(32))
    report = verify_chain(
        [c.record for c in chained], PRODUCERS, stream=STREAM, expected_head=other
    )
    assert report.first_break is not None
    assert report.first_break.reason is BreakReason.HEAD_MISMATCH
    assert report.first_break.seq == 2


def test_expected_head_matches_inside_a_longer_chain() -> None:
    chained = build_chain(4, GATE, clock())
    report = verify_chain(
        [c.record for c in chained], PRODUCERS, stream=STREAM, expected_head=chained[1].head
    )
    assert report.ok


def test_link_rejects_head_past_safe_range() -> None:
    sub = make_submission(GATE, clock())
    with pytest.raises(FormatError):
        link(ChainHead(2**53 - 1, bytes(32)), sub)


def test_record_with_bad_prev_hash_size_cannot_be_built() -> None:
    record = link(GENESIS, make_submission(GATE, clock())).record
    with pytest.raises(FormatError):
        replace(record, prev_hash="AAAA")


def test_record_type_is_kept() -> None:
    sub = make_submission(GATE, clock(), record_type="policy.approve")
    assert Record(sub, 1, b64encode(bytes(32))).submission.type == "policy.approve"
