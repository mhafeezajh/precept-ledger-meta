import asyncio
from dataclasses import replace

import pytest

from common.errors import FormatError, LedgerError, SignatureError, StoreError
from ledger.chain import BreakReason, ChainedRecord, ChainHead, link
from ledger.format import Receipt, canonical_bytes
from ledger.receipts import LOG_KEY_OWNER, check_receipts, issue_receipt, verify_receipt
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import AppendBuilder, MemoryStore
from tests.helpers import STREAM, clock, make_submission, signer_for

GATE = signer_for("gate-1")
LOG = signer_for("log")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])
LOG_KEYS = KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)])


def new_ledger(store: MemoryStore | None = None) -> Ledger:
    return Ledger(store or MemoryStore(), PRODUCERS, LOG, clock())


async def test_append_returns_verifiable_receipts_in_order() -> None:
    ledger, clk = new_ledger(), clock()
    receipts = [await ledger.append(STREAM, make_submission(GATE, clk)) for _ in range(3)]
    assert [r.seq for r in receipts] == [1, 2, 3]
    for receipt in receipts:
        verify_receipt(receipt, LOG_KEYS)
    record, stored_receipt = await ledger.get(receipts[1].record_id)
    assert record.seq == 2
    assert stored_receipt == receipts[1]
    report = await ledger.verify_stream(STREAM)
    assert report.ok
    assert report.checked == 3


async def test_append_json_parses_strictly_at_the_boundary() -> None:
    ledger = new_ledger()
    sub = make_submission(GATE, clock())
    receipt = await ledger.append_json(STREAM, canonical_bytes(sub.to_json()))
    assert receipt.seq == 1
    duplicate_keys = b'{"v":1,"v":1}'
    with pytest.raises(FormatError):
        await ledger.append_json(STREAM, duplicate_keys)
    with pytest.raises(FormatError):
        await ledger.append_json(STREAM, b"x" * (70 * 1024))


async def test_bad_producer_signature_is_rejected_and_nothing_is_written() -> None:
    store = MemoryStore()
    ledger = new_ledger(store)
    sub = make_submission(GATE, clock(), body={"amount": 48000})
    with pytest.raises(SignatureError):
        await ledger.append(STREAM, replace(sub, body={"amount": 1}))
    with pytest.raises(SignatureError):
        await ledger.append(STREAM, make_submission(signer_for("rogue"), clock()))
    assert await store.read_raw(STREAM) == []


async def test_stream_mismatch_is_rejected() -> None:
    sub = make_submission(GATE, clock(), stream="other-bank/decisions")
    with pytest.raises(LedgerError) as exc:
        await new_ledger().append(STREAM, sub)
    assert exc.value.code == "ledger.stream_mismatch"


async def test_duplicate_record_id_is_rejected() -> None:
    ledger = new_ledger()
    sub = make_submission(GATE, clock())
    await ledger.append(STREAM, sub)
    with pytest.raises(LedgerError) as exc:
        await ledger.append(STREAM, sub)
    assert exc.value.code == "ledger.duplicate_record"


async def test_unknown_record_is_not_found() -> None:
    with pytest.raises(LedgerError) as exc:
        await new_ledger().get("01JB6X7Q2M9ZK3T8V4N5R6S7W0")
    assert exc.value.code == "ledger.not_found"


async def test_concurrent_appends_are_gap_free() -> None:
    ledger, clk = new_ledger(), clock()
    subs = [make_submission(GATE, clk) for _ in range(25)]
    receipts = await asyncio.gather(*(ledger.append(STREAM, s) for s in subs))
    assert sorted(r.seq for r in receipts) == list(range(1, 26))
    assert (await ledger.verify_stream(STREAM)).ok


async def test_streams_are_independent() -> None:
    ledger, clk = new_ledger(), clock()
    await ledger.append(STREAM, make_submission(GATE, clk))
    other = "harbor-federal/policy"
    receipt = await ledger.append(other, make_submission(GATE, clk, stream=other))
    assert receipt.seq == 1


async def test_tampered_store_is_caught_by_verify_stream() -> None:
    store = MemoryStore()
    ledger, clk = new_ledger(store), clock()
    for _ in range(3):
        await ledger.append(STREAM, make_submission(GATE, clk))
    store.tamper_for_tests(STREAM, 2, lambda data: data["body"].update(amount=1))
    report = await ledger.verify_stream(STREAM)
    assert report.first_break is not None
    assert (report.first_break.seq, report.first_break.reason) == (
        2,
        BreakReason.BAD_PRODUCER_SIGNATURE,
    )


async def test_verify_stream_reports_malformed_rows() -> None:
    store = MemoryStore()
    ledger = new_ledger(store)
    await ledger.append(STREAM, make_submission(GATE, clock()))
    store.tamper_for_tests(STREAM, 1, lambda data: data["body"].update(amount=1.5))
    report = await ledger.verify_stream(STREAM)
    assert report.first_break is not None
    assert report.first_break.reason is BreakReason.MALFORMED


async def test_receipts_from_the_service_match_the_stored_history() -> None:
    store = MemoryStore()
    ledger, clk = new_ledger(store), clock()
    receipts = [await ledger.append(STREAM, make_submission(GATE, clk)) for _ in range(3)]
    assert check_receipts(store.history_for_tests(STREAM), receipts, LOG_KEYS) is None


class FailingStore(MemoryStore):
    async def append(self, stream: str, build: AppendBuilder) -> Receipt:
        raise StoreError("database unreachable")


async def test_store_down_means_no_receipt() -> None:
    # Fail closed (R3, R4): the caller gets an exception, never a receipt.
    ledger = Ledger(FailingStore(), PRODUCERS, LOG, clock())
    with pytest.raises(StoreError):
        await ledger.append(STREAM, make_submission(GATE, clock()))


async def test_memory_store_rejects_builder_that_skips_a_seq() -> None:
    store, clk = MemoryStore(), clock()

    def skipping(head: ChainHead) -> tuple[ChainedRecord, Receipt]:
        item = link(ChainHead(head.seq + 1, head.chain_hash), make_submission(GATE, clk))
        return item, issue_receipt(LOG, item, "2026-10-29T21:14:03.150Z")

    with pytest.raises(LedgerError) as exc:
        await store.append(STREAM, skipping)
    assert exc.value.code == "store.bad_seq"
