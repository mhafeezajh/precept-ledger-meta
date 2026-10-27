"""Tamper suite (skill §6): each attack is rejected with the right reason and sequence number."""

import json

from ledger.chain import GENESIS, BreakReason, ChainedRecord, link, verify_chain
from ledger.format import Record, canonical_bytes
from ledger.receipts import LOG_KEY_OWNER, check_receipts, conflicting_receipts
from ledger.signing import KeyRing
from tests.helpers import STREAM, build_chain, clock, issue_all, make_submission, signer_for

GATE = signer_for("gate-1")
LOG = signer_for("log")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])
LOG_KEYS = KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)])


def lines_of(chained: list[ChainedRecord]) -> list[bytes]:
    return [c.record.canonical() for c in chained]


def rechain(records: list[Record]) -> list[ChainedRecord]:
    """What a rogue operator does: keep the producer-signed parts, recompute seq and hashes."""
    head, out = GENESIS, []
    for record in records:
        item = link(head, record.submission)
        out.append(item)
        head = item.head
    return out


def first_break(lines: list[bytes]) -> tuple[int, BreakReason]:
    report = verify_chain(lines, PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    return report.first_break.seq, report.first_break.reason


def test_edit_body_is_caught_at_its_seq() -> None:
    lines = lines_of(build_chain(5, GATE, clock()))
    data = Record.from_json(json.loads(lines[2])).to_json()
    data["body"]["amount"] = 480
    lines[2] = canonical_bytes(data)
    assert first_break(lines) == (3, BreakReason.BAD_PRODUCER_SIGNATURE)


def test_delete_is_caught_at_its_seq() -> None:
    lines = lines_of(build_chain(5, GATE, clock()))
    del lines[2]
    assert first_break(lines) == (3, BreakReason.SEQ_MISMATCH)


def test_insert_forged_record_is_caught() -> None:
    clk = clock()
    chained = build_chain(4, GATE, clk)
    forged = link(chained[1].head, make_submission(signer_for("rogue"), clk))
    lines = [*lines_of(chained[:2]), forged.record.canonical(), *lines_of(chained[2:])]
    assert first_break(lines) == (3, BreakReason.BAD_PRODUCER_SIGNATURE)


def test_insert_replayed_record_is_caught() -> None:
    chained = build_chain(4, GATE, clock())
    replay = link(chained[2].head, chained[0].record.submission)
    lines = [*lines_of(chained[:3]), replay.record.canonical(), *lines_of(chained[3:])]
    assert first_break(lines) == (4, BreakReason.DUPLICATE_RECORD_ID)


def test_reorder_is_caught_at_first_moved_seq() -> None:
    lines = lines_of(build_chain(5, GATE, clock()))
    lines[1], lines[2] = lines[2], lines[1]
    assert first_break(lines) == (2, BreakReason.SEQ_MISMATCH)


def test_prev_hash_edit_is_caught() -> None:
    chained = build_chain(3, GATE, clock())
    wrong = Record(chained[2].record.submission, 3, chained[0].record.prev_hash)
    lines = [*lines_of(chained[:2]), wrong.canonical()]
    assert first_break(lines) == (3, BreakReason.PREV_HASH_MISMATCH)


def test_truncate_is_caught_against_the_last_receipt_head() -> None:
    chained = build_chain(5, GATE, clock())
    report = verify_chain(
        [c.record for c in chained[:3]], PRODUCERS, stream=STREAM, expected_head=chained[-1].head
    )
    assert report.first_break is not None
    assert (report.first_break.seq, report.first_break.reason) == (4, BreakReason.TRUNCATED)


def test_truncate_is_caught_by_held_receipts() -> None:
    clk = clock()
    chained = build_chain(5, GATE, clk)
    found = check_receipts(chained[:3], issue_all(chained, LOG, clk), LOG_KEYS)
    assert found is not None
    assert (found.seq, found.reason) == (4, BreakReason.MISSING_RECORD)


def test_rechained_delete_passes_the_chain_but_fails_held_receipts() -> None:
    # D9 consequence: producer signatures exclude seq/prev_hash, so a consistent re-chain
    # verifies on its own. The receipts producers hold are what catch it.
    clk = clock()
    chained = build_chain(5, GATE, clk)
    receipts = issue_all(chained, LOG, clk)
    rewritten = rechain([c.record for c in chained if c.record.seq != 3])
    assert verify_chain([c.record for c in rewritten], PRODUCERS, stream=STREAM).ok
    found = check_receipts(rewritten, receipts, LOG_KEYS)
    assert found is not None
    assert (found.seq, found.reason) == (3, BreakReason.RECEIPT_MISMATCH)


def test_fork_is_caught_by_held_receipts() -> None:
    clk = clock()
    shared = build_chain(2, GATE, clk)
    branch_a = [*shared, link(shared[-1].head, make_submission(GATE, clk, body={"amount": 1}))]
    branch_b = [*shared, link(shared[-1].head, make_submission(GATE, clk, body={"amount": 2}))]
    found = check_receipts(branch_b, issue_all(branch_a, LOG, clk), LOG_KEYS)
    assert found is not None
    assert (found.seq, found.reason) == (3, BreakReason.RECEIPT_MISMATCH)


def test_rogue_resign_with_log_key_leaves_conflicting_receipts() -> None:
    # The operator holds the log key, rewrites history and issues fresh receipts for it.
    # Each set verifies alone; together they are signed proof of misbehavior.
    clk = clock()
    chained = build_chain(4, GATE, clk)
    original = issue_all(chained, LOG, clk)
    records = [c.record for c in chained]
    rewritten = rechain([records[0], records[2], records[1], records[3]])
    fresh = issue_all(rewritten, LOG, clk)
    assert check_receipts(rewritten, fresh, LOG_KEYS) is None
    pairs = conflicting_receipts(original + fresh)
    # seq 4 conflicts too: its chain hash depends on everything before it.
    assert [a.seq for a, _ in pairs] == [2, 3, 4]


def test_rogue_resign_with_another_key_is_rejected() -> None:
    clk = clock()
    chained = build_chain(3, GATE, clk)
    found = check_receipts(chained, issue_all(chained, signer_for("rogue"), clk), LOG_KEYS)
    assert found is not None
    assert (found.seq, found.reason) == (1, BreakReason.BAD_RECEIPT_SIGNATURE)
