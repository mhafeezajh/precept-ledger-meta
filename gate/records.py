"""Gate's ledger records and the checks anyone holding one can run.

Three record types, all signed by Gate's producer key (layer 2):

- ``gate.decision``: what Gate decided for one tool call, the facts Cedar saw, the policy
  ids and policy-set digest, and the digest of the exact arguments;
- ``gate.verdict``: a person's approve or reject for an escalated decision, or ``expire``;
- ``gate.result``: that the tool ran, and the digest of what it returned.

A ``Receipted`` record is only usable once ``verify_receipted`` passes: Gate's signature,
the log's receipt signature, and a receipt that names this exact record (R3, R5).
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from common.errors import FormatError, GateError, SignatureError
from ledger.format import Receipt, Record, Submission, canonical_bytes, check_ijson, leaf_hash
from ledger.receipts import verify_receipt
from ledger.signing import KeyRing

DECISION = "gate.decision"
VERDICT = "gate.verdict"
RESULT = "gate.result"
RESULT_DIGEST_CONTEXT = b"PL-result-v1\x00"  # skill §4: its own context


@dataclass(frozen=True)
class Receipted:
    """A submission and the ledger's receipt for it."""

    submission: Submission
    receipt: Receipt

    @property
    def record_id(self) -> str:
        return self.submission.record_id

    @property
    def body(self) -> dict[str, Any]:
        return self.submission.body

    def to_json(self) -> dict[str, Any]:
        return {"record": self.submission.to_json(), "receipt": self.receipt.to_json()}

    @classmethod
    def from_json(cls, value: object) -> "Receipted":
        if not isinstance(value, dict) or set(value) != {"record", "receipt"}:
            raise GateError("expected {record, receipt}", code="gate.bad_record")
        try:
            return cls(Submission.from_json(value["record"]), Receipt.from_json(value["receipt"]))
        except FormatError as exc:
            raise GateError(
                f"malformed record or receipt ({exc.code})", code="gate.bad_record"
            ) from exc

    @classmethod
    def from_record(cls, record: Record, receipt: Receipt) -> "Receipted":
        """From a stored record: its leaf hash must be the one the log signed."""
        if not hmac.compare_digest(leaf_hash(record.canonical()), receipt.leaf_hash_bytes()):
            raise GateError("stored record does not match its receipt", code="gate.bad_record")
        return cls(record.submission, receipt)


def verify_receipted(
    item: Receipted, *, producer_id: str, producers: KeyRing, log_keys: KeyRing, record_type: str
) -> None:
    """Raise ``GateError(gate.bad_record)`` unless both signatures hold and match each other."""
    sub, receipt = item.submission, item.receipt
    if sub.producer.id != producer_id or sub.type != record_type:
        raise GateError(f"expected a {record_type} from {producer_id}", code="gate.bad_record")
    if (receipt.stream, receipt.record_id) != (sub.stream, sub.record_id):
        raise GateError("receipt is for another record", code="gate.bad_record")
    try:
        producers.verify_submission(sub)
        verify_receipt(receipt, log_keys)
    except SignatureError as exc:
        raise GateError(f"signature check failed ({exc.code})", code="gate.bad_record") from exc


def result_digest(result: object) -> str | None:
    """``sha256:<hex>`` of a JSON-safe tool result; ``None`` when it is not I-JSON."""
    try:
        check_ijson(result, "$.result")
        data = canonical_bytes(result)
    except FormatError:
        return None
    return "sha256:" + hashlib.sha256(RESULT_DIGEST_CONTEXT + data).hexdigest()
