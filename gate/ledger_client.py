"""Gate's view of the ledger: append a signed submission, read a record back.

Everything the ledger returns is untrusted here; ``gate.core`` verifies each receipt before
anything depends on it. Every failure is a ``GateError``, so a caller has one exception type
to turn into a deny (R3, R4).
"""

import json
from typing import Protocol

import httpx

from common.errors import FormatError, GateError
from common.ids import is_ulid
from ledger.format import Receipt, Record, Submission, canonical_bytes, parse_json_strict

MAX_RECEIPT_BYTES = 16 * 1024
MAX_RECORD_RESPONSE_BYTES = 160 * 1024  # a 64 KiB record plus its receipt, as JSON


class LedgerClient(Protocol):
    async def append(self, submission: Submission) -> Receipt: ...

    async def get(self, record_id: str) -> tuple[Record, Receipt]: ...


def _ledger_code(response: httpx.Response) -> str:
    try:
        code = json.loads(response.content)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    return code if isinstance(code, str) and len(code) <= 64 else f"http_{response.status_code}"


class HttpLedgerClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http

    async def _send(self, request: httpx.Request, limit: int) -> httpx.Response:
        try:
            response = await self._http.send(request)
        except httpx.HTTPError as exc:
            raise GateError("ledger unreachable", code="gate.ledger_unavailable") from exc
        if len(response.content) > limit:
            raise GateError("ledger response too large", code="gate.bad_receipt")
        return response

    async def append(self, submission: Submission) -> Receipt:
        url = f"{self._base}/ledger/v1/streams/{submission.stream}/records"
        request = self._http.build_request(
            "POST",
            url,
            content=canonical_bytes(submission.to_json()),
            headers={"content-type": "application/json"},
        )
        response = await self._send(request, MAX_RECEIPT_BYTES)
        if response.status_code != 201:
            # The ledger refused the record: never retried blindly, never treated as accepted.
            code = (
                "gate.ledger_unavailable" if response.status_code >= 500 else "gate.ledger_refused"
            )
            raise GateError(f"ledger refused the record ({_ledger_code(response)})", code=code)
        try:
            return Receipt.from_json(parse_json_strict(response.content))
        except FormatError as exc:
            raise GateError(f"malformed receipt ({exc.code})", code="gate.bad_receipt") from exc

    async def get(self, record_id: str) -> tuple[Record, Receipt]:
        if not is_ulid(record_id):  # it becomes part of the URL path
            raise GateError("record id must be a ULID", code="gate.bad_request")
        request = self._http.build_request("GET", f"{self._base}/ledger/v1/records/{record_id}")
        response = await self._send(request, MAX_RECORD_RESPONSE_BYTES)
        if response.status_code == 404:
            raise GateError(f"no record {record_id}", code="gate.record_not_found")
        if response.status_code != 200:
            raise GateError(
                f"ledger read failed ({_ledger_code(response)})", code="gate.ledger_unavailable"
            )
        try:
            data = parse_json_strict(response.content)
            if not isinstance(data, dict) or set(data) != {"record", "receipt"}:
                raise FormatError("expected {record, receipt}", code="format.bad_type")
            return Record.from_json(data["record"]), Receipt.from_json(data["receipt"])
        except FormatError as exc:
            raise GateError(f"malformed record ({exc.code})", code="gate.bad_receipt") from exc
