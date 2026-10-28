"""What the auditor reads from the ledger API: the stream's records, and evidence for a record.

Records come from ``GET /ledger/v1/export``, read with the verifier's hostile-input bundle
reader. Evidence for a cited record is the record and its inclusion proof against the latest
final checkpoint. Nothing here is trusted: ``claims.checker`` proves or rejects each claim.
"""

import json
from typing import Any, Protocol

import httpx

from claims.checker import Evidence
from common.errors import ModelOutputError, PreceptError
from common.ids import is_ulid
from verify.bundle import read_bundle

MAX_EXPORT_BYTES = 64 * 1024 * 1024


class LedgerReader(Protocol):
    async def records(self) -> list[dict[str, Any]]: ...

    async def final_size(self) -> int | None: ...

    async def evidence(self, record_id: str, size: int) -> Evidence | None: ...


class HttpLedgerReader:
    def __init__(
        self, base_url: str, http: httpx.AsyncClient, stream: str, *, final: bool = True
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._stream = stream
        self._final = final  # False only without witnesses; then nothing is final

    async def _get(self, path: str, **params: Any) -> httpx.Response:
        try:
            return await self._http.get(
                self._base + path, params={"stream": self._stream, **params}
            )
        except httpx.HTTPError as exc:
            raise ModelOutputError("ledger unreachable", code="audit.ledger_unavailable") from exc

    async def records(self) -> list[dict[str, Any]]:
        response = await self._get("/ledger/v1/export")
        if response.status_code != 200 or len(response.content) > MAX_EXPORT_BYTES:
            raise ModelOutputError("export unavailable", code="audit.ledger_unavailable")
        try:
            bundle = read_bundle(response.content)
        except PreceptError as exc:
            raise ModelOutputError(
                f"export unreadable ({exc.code})", code="audit.ledger_unavailable"
            ) from exc
        return [json.loads(line) for line in bundle.records]

    async def final_size(self) -> int | None:
        response = await self._get(
            "/ledger/v1/checkpoints/latest", final="true" if self._final else "false"
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ModelOutputError("no final checkpoint", code="audit.ledger_unavailable")
        return int(response.json()["size"])

    async def evidence(self, record_id: str, size: int) -> Evidence | None:
        """The record and its proof at ``size``; None if it is unknown or not yet in it."""
        if not is_ulid(record_id):  # it becomes part of the URL path
            return None
        try:
            record = await self._http.get(f"{self._base}/ledger/v1/records/{record_id}")
            proof = await self._http.get(
                f"{self._base}/ledger/v1/records/{record_id}/proof", params={"size": size}
            )
        except httpx.HTTPError as exc:
            raise ModelOutputError("ledger unreachable", code="audit.ledger_unavailable") from exc
        if record.status_code != 200 or proof.status_code != 200:
            return None
        try:
            return Evidence.from_api(record.json()["record"], proof.json())
        except (KeyError, TypeError, ValueError):
            return None
