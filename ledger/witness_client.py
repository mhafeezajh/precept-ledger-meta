"""Talks to a remote witness over HTTP (C2SP tlog-witness). Everything it returns is untrusted:
``ledger.witnessing`` verifies the cosignature line before using it.
"""

import json
import re
from collections.abc import Sequence

import httpx

from common.errors import WitnessError
from ledger.cosignature import format_add_checkpoint

MAX_RESPONSE_BYTES = 16 * 1024
_SIZE = re.compile(r"^(0|[1-9][0-9]*)\n$")


def _error_code(response: httpx.Response) -> str:
    try:
        code = json.loads(response.content)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    # Only witness codes are passed on; anything else becomes a plain HTTP status code.
    if isinstance(code, str) and re.fullmatch(r"witness\.[a-z_]{1,40}", code):
        return code
    return f"witness.http_{response.status_code}"


class HttpWitnessClient:
    def __init__(self, name: str, base_url: str, http: httpx.AsyncClient) -> None:
        self.name = name
        self._url = base_url.rstrip("/") + "/witness/v1/cosign"
        self._http = http

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        try:
            response = await self._http.post(
                self._url,
                content=format_add_checkpoint(old_size, proof, note),
                headers={"content-type": "text/plain; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise WitnessError(
                f"{self.name} unreachable ({type(exc).__name__})", code="witness.unreachable"
            ) from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise WitnessError(f"{self.name} sent too much", code="witness.bad_response")
        if response.status_code == 200:
            return response.text
        if response.status_code == 409:
            size = _SIZE.match(response.text)
            if size is None or response.headers.get("content-type") != "text/x.tlog.size":
                raise WitnessError(f"{self.name}: malformed 409", code="witness.bad_response")
            raise WitnessError(
                f"{self.name} last cosigned size {size[1]}",
                code="witness.conflict",
                latest_size=int(size[1]),
            )
        code = _error_code(response)
        raise WitnessError(f"{self.name} refused: {code}", code=code)
