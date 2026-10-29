"""Any single-byte change to a signed delegation credential's canonical JSON fails verification
(skill §4: a property test for every new signed format). TEST ONLY keys."""

from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from common.clock import format_ts
from common.errors import FormatError, GateError
from gate.delegation import (
    parse_credential,
    parse_signed_credential,
    sign_credential,
    verify_signed_credential,
)
from ledger.format import canonical_bytes, parse_canonical
from ledger.signing import KeyRing
from tests.helpers import T0, signer_for

PRINCIPAL = signer_for("principal-1")
PRINCIPALS = KeyRing([("treasury.harbor-federal", PRINCIPAL.public_key_raw)])
ENVELOPE = sign_credential(PRINCIPAL, parse_credential({
    "credential_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0", "principal_id": "treasury.harbor-federal",
    "agent_id": "payments-ops", "action_classes": ["value.transfer"], "max_amount": "5000.00",
    "allowed_payees": ["acme-paper"], "not_before": format_ts(T0),
    "expires_at": format_ts(T0 + timedelta(days=1)),
})).to_json()  # fmt: skip
WIRE = canonical_bytes(ENVELOPE)


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0, max_value=len(WIRE) - 1), st.integers(min_value=1, max_value=255))
def test_any_single_byte_edit_is_refused(index: int, delta: int) -> None:
    edited = bytearray(WIRE)
    edited[index] = (edited[index] + delta) % 256
    try:
        value = parse_canonical(bytes(edited))
        verify_signed_credential(parse_signed_credential(value), PRINCIPALS)
    except (FormatError, GateError, UnicodeDecodeError):
        return  # refused: unreadable, malformed, or the signature fails
    pytest.fail(f"an edit at byte {index} still verified")


def test_the_unedited_envelope_verifies() -> None:
    verify_signed_credential(parse_signed_credential(parse_canonical(WIRE)), PRINCIPALS)
