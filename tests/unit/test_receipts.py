from dataclasses import replace

import pytest

from common.errors import SignatureError
from ledger.chain import BreakReason
from ledger.format import b64encode
from ledger.receipts import (
    LOG_KEY_OWNER,
    check_receipts,
    conflicting_receipts,
    issue_receipt,
    verify_receipt,
)
from ledger.signing import KeyRing
from tests.helpers import build_chain, clock, issue_all, signer_for

GATE = signer_for("gate-1")
LOG = signer_for("log")
LOG_KEYS = KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)])


def test_issue_receipt_fields_and_signature() -> None:
    item = build_chain(1, GATE, clock())[0]
    receipt = issue_receipt(LOG, item, "2026-10-29T21:14:03.150Z")
    assert receipt.seq == 1
    assert receipt.record_id == item.record.submission.record_id
    assert receipt.stream == item.record.submission.stream
    assert receipt.chain_hash == b64encode(item.chain_hash)
    assert receipt.leaf_hash == b64encode(item.leaf_hash)
    assert receipt.key_id == LOG.key_id
    verify_receipt(receipt, LOG_KEYS)


def test_receipt_signed_by_other_key_is_rejected() -> None:
    item = build_chain(1, GATE, clock())[0]
    receipt = issue_receipt(signer_for("rogue"), item, "2026-10-29T21:14:03.150Z")
    with pytest.raises(SignatureError):
        verify_receipt(receipt, LOG_KEYS)


def test_receipt_signed_by_a_producer_key_is_rejected() -> None:
    # A key the verifier knows, but as a producer: receipts must come from the log key.
    keys = KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw), ("gate-1", GATE.public_key_raw)])
    item = build_chain(1, GATE, clock())[0]
    receipt = issue_receipt(GATE, item, "2026-10-29T21:14:03.150Z")
    with pytest.raises(SignatureError) as exc:
        verify_receipt(receipt, keys)
    assert exc.value.code == "signature.producer_mismatch"


def test_edited_receipt_is_rejected() -> None:
    item = build_chain(1, GATE, clock())[0]
    receipt = issue_receipt(LOG, item, "2026-10-29T21:14:03.150Z")
    with pytest.raises(SignatureError):
        verify_receipt(replace(receipt, received_at="2026-10-29T21:14:04.150Z"), LOG_KEYS)


def test_check_receipts_accepts_matching_history() -> None:
    clk = clock()
    chained = build_chain(4, GATE, clk)
    assert check_receipts(chained, issue_all(chained, LOG, clk), LOG_KEYS) is None


def test_check_receipts_reports_record_missing_for_receipt() -> None:
    clk = clock()
    chained = build_chain(4, GATE, clk)
    receipts = issue_all(chained, LOG, clk)
    found = check_receipts(chained[:2], receipts, LOG_KEYS)
    assert found is not None
    assert found.seq == 3
    assert found.reason is BreakReason.MISSING_RECORD


def test_check_receipts_reports_lowest_mismatch() -> None:
    clk = clock()
    chained = build_chain(3, GATE, clk)
    other = build_chain(3, GATE, clk)
    receipts = issue_all(chained, LOG, clk)
    found = check_receipts(other, list(reversed(receipts)), LOG_KEYS)
    assert found is not None
    assert found.seq == 1
    assert found.reason is BreakReason.RECEIPT_MISMATCH


def test_check_receipts_reports_bad_receipt_signature() -> None:
    clk = clock()
    chained = build_chain(2, GATE, clk)
    receipts = issue_all(chained, signer_for("rogue"), clk)
    found = check_receipts(chained, receipts, LOG_KEYS)
    assert found is not None
    assert found.reason is BreakReason.BAD_RECEIPT_SIGNATURE


def test_conflicting_receipts_detects_two_signed_answers_for_one_seq() -> None:
    clk = clock()
    history_a = build_chain(2, GATE, clk)
    history_b = build_chain(2, GATE, clk)
    receipts = issue_all(history_a, LOG, clk) + issue_all(history_b, LOG, clk)
    pairs = conflicting_receipts(receipts)
    assert [(a.seq, b.seq) for a, b in pairs] == [(1, 1), (2, 2)]
    assert conflicting_receipts(issue_all(history_a, LOG, clk)) == []
