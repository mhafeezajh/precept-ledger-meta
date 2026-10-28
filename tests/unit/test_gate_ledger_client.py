"""Gate's HTTP ledger client against the real ledger API, and every way the ledger can fail."""

from pathlib import Path

import httpx
import pytest

from common.errors import GateError
from gate.ledger_client import HttpLedgerClient
from tests.bundles import GATE
from tests.helpers import clock, make_submission
from tests.unit.test_api import client_for, memory_services


async def test_append_and_read_back_through_the_ledger_api(tmp_path: Path) -> None:
    async with client_for(memory_services(tmp_path)) as http:
        ledger = HttpLedgerClient("http://test", http)
        submission = make_submission(GATE, clock())
        receipt = await ledger.append(submission)
        record, again = await ledger.get(submission.record_id)
    assert (receipt.record_id, receipt.seq) == (submission.record_id, 1)
    assert record.submission == submission
    assert again == receipt


async def test_a_record_the_ledger_refuses_is_a_gate_error(tmp_path: Path) -> None:
    async with client_for(memory_services(tmp_path)) as http:
        ledger = HttpLedgerClient("http://test", http)
        submission = make_submission(GATE, clock())
        await ledger.append(submission)
        with pytest.raises(GateError) as caught:
            await ledger.append(submission)  # duplicate record id
        assert caught.value.code == "gate.ledger_refused"
        with pytest.raises(GateError) as missing:
            await ledger.get("01JB6X7Q2M9ZK3T8V4N5R6S7W0")
        assert missing.value.code == "gate.record_not_found"


def answering(status: int, content: bytes = b"{}") -> httpx.AsyncClient:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    return httpx.AsyncClient(transport=transport)


def unreachable() -> httpx.AsyncClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(fail))


@pytest.mark.parametrize(
    ("http", "code"),
    [
        (unreachable(), "gate.ledger_unavailable"),
        (answering(503, b'{"error": {"code": "store.unavailable"}}'), "gate.ledger_unavailable"),
        (answering(201, b'{"not": "a receipt"}'), "gate.bad_receipt"),
        (answering(201, b"x" * 20_000), "gate.bad_receipt"),
        (answering(400, b"not json"), "gate.ledger_refused"),
    ],
)
async def test_append_failures_never_look_like_receipts(http: httpx.AsyncClient, code: str) -> None:
    with pytest.raises(GateError) as caught:
        await HttpLedgerClient("http://ledger", http).append(make_submission(GATE, clock()))
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("http", "record_id", "code"),
    [
        (answering(200), "../health", "gate.bad_request"),
        (answering(500), "01JB6X7Q2M9ZK3T8V4N5R6S7W0", "gate.ledger_unavailable"),
        (answering(200, b'{"record": {}}'), "01JB6X7Q2M9ZK3T8V4N5R6S7W0", "gate.bad_receipt"),
        (answering(200, b"[1]"), "01JB6X7Q2M9ZK3T8V4N5R6S7W0", "gate.bad_receipt"),
    ],
)
async def test_reads_that_cannot_be_trusted_are_errors(
    http: httpx.AsyncClient, record_id: str, code: str
) -> None:
    with pytest.raises(GateError) as caught:
        await HttpLedgerClient("http://ledger", http).get(record_id)
    assert caught.value.code == code
