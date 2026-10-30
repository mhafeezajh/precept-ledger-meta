"""What the web console knows: Gate and the ledger, over their HTTP APIs only.

The console holds no signing key for Gate or the log and no database role (apart from the
demo tamper button, ``web.demo``). It acts for a person by sending that person's Gate token,
read from ``KEYS_DIR/tokens``; tokens never reach the browser. Everything it shows as proven
is verified here first: the export with ``pl-verify``'s checks, a single record with its
inclusion proof against a cosigned checkpoint (``claims.checker.check_record``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from claims.checker import Evidence, Trust, check_record
from common.errors import PreceptError
from gate.auth import Callers
from ledger.format import parse_canonical
from verify.bundle import read_bundle
from verify.checks import Report, verify_bundle

MAX_EXPORT_BYTES = 64 * 1024 * 1024


class WebError(PreceptError):
    """A backend refused or could not be reached. ``status`` is what the page should return."""

    code = "web.error"

    def __init__(self, message: str, *, code: str | None = None, status: int = 502) -> None:
        super().__init__(message, code=code)
        self.message, self.status = message, status


@dataclass(frozen=True)
class Persona:
    name: str
    kind: str  # "approver" or "agent"
    roles: frozenset[str]
    token: str = field(repr=False)

    @property
    def label(self) -> str:
        role = ", ".join(sorted(self.roles)).replace("-", " ")
        return f"{self.name} ({role})" if role else self.name


def load_personas(keys_dir: Path) -> dict[str, Persona]:
    """Every Gate caller with a token file. Missing files mean that caller is not offered."""
    try:
        text = (keys_dir / "gate-callers.txt").read_text("utf-8")
        Callers.from_text(text)  # the same parser Gate uses; refuses a malformed file
    except (OSError, PreceptError) as exc:
        raise WebError(f"cannot read Gate callers: {exc}", code="config.keys", status=500) from exc
    personas = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4 or line.startswith("#"):
            continue
        kind, name, roles, _ = parts
        token_file = keys_dir / "tokens" / f"{name}.token"
        if token_file.is_file():
            role_set = frozenset(r for r in roles.split(",") if r and r != "-")
            personas[name] = Persona(name, kind, role_set, token_file.read_text("utf-8").strip())
    return personas


@dataclass(frozen=True)
class LedgerState:
    """The stream's export as verified by the console, newest record first."""

    report: Report
    records: tuple[dict[str, Any], ...]

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {r["record_id"]: r for r in self.records}


@dataclass(frozen=True)
class ProvenRecord:
    record: dict[str, Any]
    receipt: dict[str, Any]
    proof: dict[str, Any] | None
    problem: str | None  # why it is not proven, or None


def _error(response: httpx.Response) -> WebError:
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        code, message = "web.backend", f"HTTP {response.status_code}"
    return WebError(message, code=code, status=response.status_code)


class Backend:
    def __init__(
        self,
        gate: httpx.AsyncClient,
        ledger: httpx.AsyncClient,
        *,
        stream: str,
        trust: Trust,
        personas: Mapping[str, Persona],
    ) -> None:
        self._gate, self._ledger = gate, ledger
        self.stream, self.trust, self.personas = stream, trust, dict(personas)

    async def _call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        persona: Persona | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if persona is not None:
            headers["authorization"] = f"Bearer {persona.token}"
        try:
            response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            name = "Gate" if client is self._gate else "the ledger"
            raise WebError(f"{name} is not reachable", code="web.unavailable", status=503) from exc
        if response.status_code >= 400:
            raise _error(response)
        return response

    async def _json(self, client: httpx.AsyncClient, method: str, path: str,
                    persona: Persona | None = None, **kwargs: Any) -> Any:  # fmt: skip
        return (await self._call(client, method, path, persona, **kwargs)).json()

    # --- ledger -----------------------------------------------------------------------------

    async def ledger_health(self) -> dict[str, Any]:
        return dict(await self._json(self._ledger, "GET", "/ledger/v1/health"))

    async def checkpoint(self) -> dict[str, Any]:
        params = {"stream": self.stream}
        return dict(await self._json(self._ledger, "GET", "/ledger/v1/checkpoints/latest",
                                     params=params))  # fmt: skip

    async def ledger_state(self) -> LedgerState:
        response = await self._call(self._ledger, "GET", "/ledger/v1/export",
                                    params={"stream": self.stream})  # fmt: skip
        if len(response.content) > MAX_EXPORT_BYTES:
            raise WebError("the export is too large to show", code="web.export_too_large")
        try:
            bundle = read_bundle(response.content)
        except PreceptError as exc:
            raise WebError(f"the export is unreadable ({exc.code})", code="web.export") from exc
        report = verify_bundle(bundle, self.trust.keys, witnesses=self.trust.witnesses)
        records = []
        for line in bundle.records:
            value = parse_canonical(line)
            if isinstance(value, dict):
                records.append(value)
        return LedgerState(report, tuple(reversed(records)))

    async def proven_record(self, record_id: str) -> ProvenRecord:
        found = await self._json(self._ledger, "GET", f"/ledger/v1/records/{record_id}")
        try:
            proof = await self._json(self._ledger, "GET", f"/ledger/v1/records/{record_id}/proof")
        except WebError as exc:  # not yet under a checkpoint: shown, but not as proven
            return ProvenRecord(found["record"], found["receipt"], None, exc.message)
        evidence = Evidence.from_api(found["record"], proof)
        problem = check_record(record_id, evidence, self.trust)
        return ProvenRecord(found["record"], found["receipt"], proof, problem)

    # --- Gate -------------------------------------------------------------------------------

    async def gate_health(self) -> dict[str, Any]:
        return dict(await self._json(self._gate, "GET", "/gate/v1/health"))

    async def escalations(self, persona: Persona) -> list[dict[str, Any]]:
        data = await self._json(self._gate, "GET", "/gate/v1/escalations", persona)
        return list(data["escalations"])

    async def escalation(self, persona: Persona, escalation_id: str) -> dict[str, Any]:
        path = f"/gate/v1/escalations/{escalation_id}"
        return dict(await self._json(self._gate, "GET", path, persona))

    async def give_verdict(self, persona: Persona, escalation_id: str, verdict: str) -> Any:
        path = f"/gate/v1/escalations/{escalation_id}/verdict"
        return await self._json(self._gate, "POST", path, persona, json={"verdict": verdict})

    # --- policy -----------------------------------------------------------------------------

    async def active_policy(self, persona: Persona) -> dict[str, Any]:
        return dict(await self._json(self._gate, "GET", "/policy/v1/active", persona))

    async def policy_draft(self, persona: Persona, draft_id: str) -> dict[str, Any]:
        return dict(await self._json(self._gate, "GET", f"/policy/v1/drafts/{draft_id}", persona))

    async def submit_policy(
        self, persona: Persona, pdf: bytes, name: str, *, inject_fault: bool
    ) -> dict[str, Any]:
        params = {"name": name, "inject_fault": str(inject_fault).lower()}
        return dict(await self._json(self._gate, "POST", "/policy/v1/drafts", persona,
                                     params=params, content=pdf))  # fmt: skip

    async def revise_policy(
        self, persona: Persona, draft_id: str, yaml_text: str
    ) -> dict[str, Any]:
        path = f"/policy/v1/drafts/{draft_id}/revisions"
        return dict(await self._json(self._gate, "POST", path, persona,
                                     content=yaml_text.encode("utf-8")))  # fmt: skip

    async def approve_policy(self, persona: Persona, draft_id: str) -> dict[str, Any]:
        path = f"/policy/v1/drafts/{draft_id}/approve"
        return dict(await self._json(self._gate, "POST", path, persona))

    # --- learner ----------------------------------------------------------------------------

    async def run_learner(self, persona: Persona, *, inject_fault: bool) -> dict[str, Any]:
        params = {"window": "7d", "inject_fault": str(inject_fault).lower()}
        return dict(await self._json(self._gate, "POST", "/learn/v1/run", persona,
                                     params=params))  # fmt: skip

    async def proposal(self, persona: Persona, proposal_id: str) -> dict[str, Any]:
        path = f"/learn/v1/proposals/{proposal_id}"
        return dict(await self._json(self._gate, "GET", path, persona))

    async def approve_proposal(self, persona: Persona, proposal_id: str) -> dict[str, Any]:
        path = f"/learn/v1/proposals/{proposal_id}/approve"
        return dict(await self._json(self._gate, "POST", path, persona))

    # --- improver (D95) ---------------------------------------------------------------------

    async def run_improver(self, persona: Persona, *, inject_fault: bool) -> dict[str, Any]:
        params = {"inject_fault": str(inject_fault).lower()}
        return dict(await self._json(self._gate, "POST", "/improve/v1/run", persona,
                                     params=params))  # fmt: skip

    async def approve_improvement(self, persona: Persona, proposal_id: str) -> dict[str, Any]:
        path = f"/improve/v1/proposals/{proposal_id}/approve"
        return dict(await self._json(self._gate, "POST", path, persona))
