"""HTTP clients for a timestamp authority (RFC 3161) and an OpenTimestamps calendar.

Everything they receive is untrusted. A TSA token is fully verified (``verify_token``)
before it is returned; a calendar's answer must parse as a timestamp of our digest that ends
in a pending attestation. ``verify/`` never imports this module.
"""

import secrets

import httpx
from cryptography import x509

from anchor.ots import read_timestamp
from anchor.rfc3161 import timestamp_request, token_from_response, verify_token
from common.errors import AnchorError

MAX_RESPONSE_BYTES = 64 * 1024


def _check_size(response: httpx.Response, source: str) -> None:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise AnchorError(f"{source} sent too much", code="anchor.bad_response")


class HttpTimestampAuthority:
    def __init__(
        self, name: str, url: str, http: httpx.AsyncClient, roots: list[x509.Certificate]
    ) -> None:
        self.name, self._url, self._http, self._roots = name, url, http, roots

    async def timestamp(self, digest: bytes) -> bytes:
        nonce = secrets.randbits(63)
        try:
            response = await self._http.post(
                self._url,
                content=timestamp_request(digest, nonce),
                headers={"content-type": "application/timestamp-query"},
            )
        except httpx.HTTPError as exc:
            raise AnchorError(
                f"TSA {self.name} unreachable ({type(exc).__name__})", code="anchor.unreachable"
            ) from exc
        _check_size(response, self.name)
        if response.status_code != 200:
            raise AnchorError(
                f"TSA {self.name} answered {response.status_code}",
                code=f"anchor.http_{response.status_code}",
            )
        token = token_from_response(response.content, digest, nonce)
        verify_token(token, digest, self._roots)  # never store a token we cannot verify (R5)
        return token


class HttpCalendar:
    def __init__(self, url: str, http: httpx.AsyncClient) -> None:
        self.url, self._http = url.rstrip("/"), http

    async def _call(self, method: str, path: str, content: bytes | None = None) -> httpx.Response:
        try:
            response = await self._http.request(
                method,
                self.url + path,
                content=content,
                headers={"accept": "application/vnd.opentimestamps.v1"},
            )
        except httpx.HTTPError as exc:
            raise AnchorError(
                f"calendar {self.url} unreachable ({type(exc).__name__})",
                code="anchor.unreachable",
            ) from exc
        _check_size(response, self.url)
        return response

    async def submit(self, digest: bytes) -> bytes:
        response = await self._call("POST", "/digest", digest)
        if response.status_code != 200:
            raise AnchorError(
                f"calendar answered {response.status_code}",
                code=f"anchor.http_{response.status_code}",
            )
        if not any(a.kind == "pending" for a in read_timestamp(response.content, digest)):
            raise AnchorError("calendar returned no pending attestation", code="anchor.bad_ots")
        return response.content

    async def upgrade(self, commitment: bytes) -> bytes | None:
        """The calendar's timestamp from ``commitment`` once confirmed; ``None`` if pending."""
        response = await self._call("GET", f"/timestamp/{commitment.hex()}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AnchorError(
                f"calendar answered {response.status_code}",
                code=f"anchor.http_{response.status_code}",
            )
        return response.content
