"""``HttpGateClient``: the agent's side of the Gate API, used by ``gate.governed.Governor``.

It only moves bytes. Everything it returns is checked by the governor before a tool runs:
records and receipts are parsed strictly here and verified there.
"""

import json
from typing import Any

import httpx

from common.errors import FormatError, GateError
from common.ids import is_ulid
from gate.governed import EscalationStatus
from gate.records import Receipted
from ledger.format import canonical_bytes, parse_json_strict

MAX_RESPONSE_BYTES = 256 * 1024
_ESCALATION_STATES = {"open", "approved", "rejected", "expired"}


class HttpGateClient:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"}
        self._http = http

    def __repr__(self) -> str:
        return f"HttpGateClient({self._base!r})"  # never the token

    async def _call(self, method: str, path: str, *, body: object = None, **params: Any) -> Any:
        headers = dict(self._headers)
        content = None
        if body is not None:
            content = canonical_bytes(body)
            headers["content-type"] = "application/json"
        try:
            response = await self._http.request(
                method, self._base + path, content=content, headers=headers, params=params or None
            )
        except httpx.HTTPError as exc:
            raise GateError("Gate unreachable", code="gate.unreachable") from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise GateError("Gate response too large", code="gate.bad_response")
        if response.status_code not in (200, 201):
            raise GateError(f"Gate answered {response.status_code}", code=_error_code(response))
        try:
            return parse_json_strict(response.content)
        except FormatError as exc:
            raise GateError("Gate response is not JSON", code="gate.bad_response") from exc

    async def decide(self, tool: str, arguments: dict[str, Any]) -> Receipted:
        data = await self._call(
            "POST", "/gate/v1/decide", body={"tool": tool, "arguments": arguments}
        )
        if not isinstance(data, dict):
            raise GateError("unexpected decision response", code="gate.bad_response")
        return Receipted.from_json({"record": data.get("record"), "receipt": data.get("receipt")})

    async def wait(self, escalation_id: str, timeout_seconds: float) -> EscalationStatus:
        if not is_ulid(escalation_id):  # it becomes part of the URL path
            raise GateError("escalation id must be a ULID", code="gate.bad_response")
        data = await self._call(
            "GET", f"/gate/v1/escalations/{escalation_id}", wait=round(timeout_seconds, 3)
        )
        try:
            state = data["escalation"]["state"]
            verdict = data["verdict"]
        except (KeyError, TypeError) as exc:
            raise GateError("unexpected escalation response", code="gate.bad_response") from exc
        if state not in _ESCALATION_STATES:
            raise GateError("unknown escalation state", code="gate.bad_response")
        return EscalationStatus(state, None if verdict is None else Receipted.from_json(verdict))

    async def record_result(
        self, decision_record_id: str, *, ok: bool, digest: str | None, error: str | None
    ) -> Receipted:
        body = {
            "decision_record_id": decision_record_id,
            "outcome": "ok" if ok else "error",
            "result_digest": digest,
            "error": error,
        }
        return Receipted.from_json(await self._call("POST", "/gate/v1/results", body=body))


def _error_code(response: httpx.Response) -> str:
    try:
        code = json.loads(response.content)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    if (
        isinstance(code, str)
        and len(code) <= 64
        and code.replace(".", "").replace("_", "").isalnum()
    ):
        return code
    return f"gate.http_{response.status_code}"
