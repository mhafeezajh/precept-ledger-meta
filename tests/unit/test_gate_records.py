"""Record checks (gate/records.py): what the decorator and Gate accept as a receipted record."""

from dataclasses import replace

import pytest

from common.errors import GateError
from gate.records import DECISION, VERDICT, Receipted, verify_receipted
from tests.bundles import GATE
from tests.gate_helpers import AGENT, GATE_KEYS, LOG_KEYS, SMALL_PAYMENT, rig
from tests.helpers import clock, make_submission


async def decision() -> Receipted:
    return (await rig().gate.decide(AGENT, "create_payment", SMALL_PAYMENT)).record


def check(item: Receipted, record_type: str = DECISION, producer_id: str = "gate-1") -> None:
    verify_receipted(
        item,
        producer_id=producer_id,
        producers=GATE_KEYS,
        log_keys=LOG_KEYS,
        record_type=record_type,
    )


async def test_a_genuine_record_round_trips_and_verifies() -> None:
    item = await decision()
    again = Receipted.from_json(item.to_json())
    assert again == item
    check(again)


async def test_type_producer_and_receipt_must_all_match() -> None:
    item = await decision()
    with pytest.raises(GateError):
        check(item, record_type=VERDICT)
    with pytest.raises(GateError):
        check(item, producer_id="gate-2")
    other = make_submission(GATE, clock(), record_type="gate.decision")
    with pytest.raises(GateError):
        check(replace(item, submission=other))  # receipt names another record


async def test_a_receipt_with_a_broken_signature_is_refused() -> None:
    item = await decision()
    sig = item.receipt.sig
    broken = replace(item.receipt, sig=("B" if sig[0] == "A" else "A") + sig[1:])
    with pytest.raises(GateError) as caught:
        check(replace(item, receipt=broken))
    assert caught.value.code == "gate.bad_record"


@pytest.mark.parametrize("value", [[], {"record": {}}, {"record": {}, "receipt": {}}])
def test_malformed_json_is_refused(value: object) -> None:
    with pytest.raises(GateError) as caught:
        Receipted.from_json(value)
    assert caught.value.code == "gate.bad_record"


async def test_a_stored_record_must_match_its_receipt() -> None:
    r = rig()
    first = (await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)).record
    second = (await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)).record
    record, _ = await r.ledger.get(first.record_id)
    with pytest.raises(GateError) as caught:
        Receipted.from_record(record, second.receipt)
    assert caught.value.code == "gate.bad_record"
