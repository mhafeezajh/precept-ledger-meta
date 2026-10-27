"""Shared test helpers. Keys come from tests/keys/test-keys.json and are TEST ONLY."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common.clock import FixedClock, format_ts
from common.ids import new_ulid
from ledger.format import Receipt, Submission
from ledger.signing import Signer, sign_submission

if TYPE_CHECKING:
    from ledger.chain import ChainedRecord

KEYS_FILE = Path(__file__).parent / "keys" / "test-keys.json"
STREAM = "harbor-federal/decisions"
T0 = datetime(2026, 10, 29, 21, 14, 3, 112000, tzinfo=UTC)


def signer_for(name: str) -> Signer:
    seeds = json.loads(KEYS_FILE.read_text())["seeds"]
    return Signer.from_seed(bytes.fromhex(seeds[name]))


def clock(step_ms: int = 1) -> FixedClock:
    return FixedClock(T0, timedelta(milliseconds=step_ms))


def make_submission(
    signer: Signer,
    clk: FixedClock,
    *,
    body: dict[str, Any] | None = None,
    stream: str = STREAM,
    producer_id: str = "gate-1",
    record_type: str = "gate.decision",
) -> Submission:
    return sign_submission(
        signer,
        producer_id=producer_id,
        stream=stream,
        record_id=new_ulid(clk),
        ts=format_ts(clk.now()),
        record_type=record_type,
        body=body if body is not None else {"action": "value.transfer", "amount": 48000},
    )


def build_chain(count: int, signer: Signer, clk: FixedClock) -> list["ChainedRecord"]:
    """``count`` linked records signed by ``signer``, amounts 1000, 2000, ..."""
    from ledger.chain import GENESIS, link

    chained = []
    head = GENESIS
    for index in range(count):
        item = link(head, make_submission(signer, clk, body={"amount": 1000 * (index + 1)}))
        chained.append(item)
        head = item.head
    return chained


def issue_all(chained: list["ChainedRecord"], log: Signer, clk: FixedClock) -> list[Receipt]:
    from ledger.receipts import issue_receipt

    return [issue_receipt(log, item, format_ts(clk.now())) for item in chained]
