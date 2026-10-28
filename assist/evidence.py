"""What the auditor reads from the ledger API: the stream's export bundle.

``GET /ledger/v1/export`` is read with the verifier's hostile-input bundle reader. Nothing here
is trusted: ``claims.log`` verifies the whole bundle before anything is asked or answered.
"""

from typing import Protocol

import httpx

from common.errors import ModelOutputError, PreceptError
from verify.bundle import Bundle, read_bundle

MAX_EXPORT_BYTES = 64 * 1024 * 1024


class LedgerReader(Protocol):
    async def export(self) -> Bundle: ...


class HttpLedgerReader:
    def __init__(self, base_url: str, http: httpx.AsyncClient, stream: str) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._stream = stream

    async def export(self) -> Bundle:
        try:
            response = await self._http.get(
                f"{self._base}/ledger/v1/export", params={"stream": self._stream}
            )
        except httpx.HTTPError as exc:
            raise ModelOutputError("ledger unreachable", code="audit.ledger_unavailable") from exc
        if response.status_code != 200 or len(response.content) > MAX_EXPORT_BYTES:
            raise ModelOutputError("export unavailable", code="audit.ledger_unavailable")
        try:
            return read_bundle(response.content)
        except PreceptError as exc:
            raise ModelOutputError(
                f"export unreadable ({exc.code})", code="audit.ledger_unavailable"
            ) from exc
