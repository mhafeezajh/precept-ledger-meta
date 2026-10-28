"""A proven log prefix: the whole export, verified up to its latest final checkpoint (D65).

Per-record proofs show a record is in the log; they cannot show nothing else matched. For
"every payment over $10,000", the export is checked with ``pl-verify``'s own checks (chain,
producer signatures, receipts, checkpoints against the records, witness quorum), and the query
runs only over records ``1..size`` of the latest final checkpoint. A record's place under that
root then proves its inclusion, so no per-record proof is fetched.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from claims.checker import Trust
from common.errors import VerificationError
from ledger.format import b64encode, parse_canonical
from verify.bundle import Bundle
from verify.checks import verify_bundle

# A checkpoint newer than the final one, still waiting for cosignatures, is normal while the
# log runs. It is reported as below_quorum; the prefix under the final checkpoint still holds.
_TOLERATED = {"below_quorum"}


@dataclass(frozen=True)
class ProvenLog:
    stream: str
    size: int  # the final checkpoint's size; records 1..size are proven
    root: bytes
    records: tuple[dict[str, Any], ...]
    newer: int  # records after the final checkpoint, left out
    _by_id: Mapping[str, dict[str, Any]] = field(repr=False, compare=False)

    @property
    def root_b64(self) -> str:
        return b64encode(self.root)

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self._by_id.get(record_id)


def prove_log(bundle: Bundle, trust: Trust) -> ProvenLog | str:
    """The proven prefix of ``bundle``, or why there is none. Fails closed on any finding."""
    if bundle.origin != trust.origin:
        return f"export is for origin {bundle.origin!r}, expected {trust.origin!r}"
    report = verify_bundle(bundle, trust.keys, witnesses=trust.witnesses)
    head = report.final if trust.witnesses is not None else report.latest
    serious = [f for f in report.findings if f.check not in _TOLERATED]
    if serious:
        first = serious[0]
        return f"export does not verify: {first.check}: {first.detail}"
    if head is None:
        return "no final checkpoint covers the export"
    records = tuple(_parse(line) for line in bundle.records[: head.size])
    by_id = {r["record_id"]: r for r in records}
    return ProvenLog(
        bundle.stream, head.size, head.root, records, report.records - head.size, by_id
    )


def _parse(line: bytes) -> dict[str, Any]:
    value = parse_canonical(line)
    if not isinstance(value, dict):  # cannot happen: verify_bundle parsed it as a Record
        raise VerificationError("record is not an object", code="claims.malformed")
    return value
