"""Issue and revoke delegation credentials as Gate-signed ledger records, and rebuild their
state from the ledger at startup (D88). There is no table: the ledger is the state.

- ``gate.delegation.issue``: the principal's signed credential, verified first, and its hash.
- ``gate.delegation.revoke``: the principal's signed revocation, naming the credential's hash.

``Delegations`` changes only after the record's receipt has verified, so Gate never treats a
credential as issued, or as no longer revoked, without the ledger holding the record (R3).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from common.errors import GateError
from gate.delegation import (
    ISSUE,
    REVOKE,
    Delegations,
    parse_signed_credential,
    parse_signed_revocation,
    verify_revocation,
    verify_signed_credential,
)
from gate.records import Receipted

if TYPE_CHECKING:
    from gate.core import Gate


def _guard(gate: "Gate") -> Delegations:
    if gate.delegations is None:
        raise GateError("delegation credentials are not enabled", code="delegation.disabled")
    return gate.delegations


async def issue(gate: "Gate", value: object) -> Receipted:
    delegations = _guard(gate)
    signed = parse_signed_credential(value)
    verify_signed_credential(signed, delegations.principals)
    if delegations.is_issued(signed):
        raise GateError("this credential, or its id, is already issued",
                        code="delegation.already_issued")  # fmt: skip
    c = signed.credential
    body = {
        "credential": signed.to_json(),
        "credential_hash": signed.hash,
        "credential_id": c.credential_id,
        "principal_id": c.principal_id,
        "agent_id": c.agent_id,
    }
    record = await gate.append_record(ISSUE, body)
    delegations.apply(ISSUE, record.body, record.record_id)
    return record


async def revoke(gate: "Gate", value: object) -> Receipted:
    delegations = _guard(gate)
    signed = parse_signed_revocation(value)
    verify_revocation(signed, delegations.principals)
    r = signed.revocation
    found = delegations.find(r.credential_id)
    if found is None or found[0].hash != r.credential_hash:
        raise GateError("no issued credential with this id and hash", code="delegation.not_issued")
    credential, revoked = found
    if credential.credential.principal_id != r.principal_id:
        # Only the principal who granted it may take it back.
        raise GateError("signed by another principal", code="delegation.bad_signature")
    if revoked:
        raise GateError("already revoked", code="delegation.revoked")
    body = {
        "revocation": signed.to_json(),
        "credential_hash": r.credential_hash,
        "credential_id": r.credential_id,
    }
    record = await gate.append_record(REVOKE, body)
    delegations.apply(REVOKE, record.body, record.record_id)
    return record


async def restore(gate: "Gate", records: Sequence[dict[str, Any]]) -> int:
    """Fold the stream's issue and revoke records, each re-read through the ledger API and
    checked against Gate's key and its receipt. Any record that does not verify stops startup:
    skipping a revocation would bring a revoked credential back to life (R4)."""
    delegations = _guard(gate)
    count = 0
    for raw in records:
        kind = raw.get("type")
        if kind not in (ISSUE, REVOKE):
            continue
        record = await gate.read_record(str(raw.get("record_id")), str(kind))
        delegations.apply(str(kind), record.body, record.record_id)
        count += 1
    return count
