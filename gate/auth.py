"""Who is calling Gate: agents and approvers, each with a bearer token.

The callers file (``KEYS_DIR/gate-callers.txt``) holds one caller per line::

    agent payments-ops - <sha256 hex of the token>
    approver j.ortiz payments-supervisor <sha256 hex of the token>

Gate stores only token hashes. An agent's identity comes from its token, never from the
request body, so an agent cannot claim another agent's policies. Agents and approvers are
separate kinds, so no agent token can approve an escalation (P6).
"""

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass

from common.errors import ConfigError, GateError

AGENT = "agent"
APPROVER = "approver"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ROLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Caller:
    kind: str
    name: str
    roles: frozenset[str] = frozenset()


def token_hash(token: str) -> str:
    # Tokens are 256-bit random values (scripts/dev_keys.py), so a plain hash is enough.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bad(line_no: int, message: str) -> ConfigError:
    return ConfigError(f"callers file line {line_no}: {message}", code="config.gate_callers")


def _parse_line(line_no: int, line: str) -> tuple[Caller, str]:
    parts = line.split()
    if len(parts) != 4:
        raise _bad(line_no, "expected: kind name roles token-hash")
    kind, name, roles_text, digest = parts
    if kind not in (AGENT, APPROVER) or not _NAME.fullmatch(name):
        raise _bad(line_no, "kind must be agent or approver, with a lowercase name")
    roles = frozenset() if roles_text == "-" else frozenset(roles_text.split(","))
    if not all(_ROLE.fullmatch(role) for role in roles) or (kind == AGENT and roles):
        raise _bad(line_no, "roles are comma-separated, and only approvers have roles")
    if not _HASH.fullmatch(digest):
        raise _bad(line_no, "token hash must be 64 lowercase hex characters")
    return Caller(kind, name, roles), digest


class Callers:
    def __init__(self, entries: Iterable[tuple[Caller, str]]) -> None:
        self._entries = list(entries)
        names = [caller.name for caller, _ in self._entries]
        hashes = [digest for _, digest in self._entries]
        if len(set(names)) != len(names) or len(set(hashes)) != len(hashes):
            raise ConfigError("caller names and tokens must be unique", code="config.gate_callers")

    @classmethod
    def from_text(cls, text: str) -> "Callers":
        entries = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if line and not line.startswith("#"):
                entries.append(_parse_line(line_no, line))
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def authenticate(self, authorization: str | None, *, kind: str | None) -> Caller:
        """The caller for an ``Authorization: Bearer <token>`` header; ``kind`` None is any."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise GateError("bearer token required", code="gate.unauthorized")
        presented = token_hash(token.strip())
        found = None
        for caller, digest in self._entries:  # compare against every entry, no early exit
            if hmac.compare_digest(presented, digest):
                found = caller
        if found is None:
            raise GateError("unknown token", code="gate.unauthorized")
        if kind is not None and found.kind != kind:
            raise GateError(f"this endpoint is for {kind}s", code="gate.forbidden")
        return found
