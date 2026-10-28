"""Claim checker: a model's claim about the ledger is shown as true only if it is proven (R5).

Deterministic and offline, like the verifier (skill §2: only ``ledger.format``, the C2SP
checkpoint and cosignature code, ``merkle`` and ``verify.keys``). For every record a claim
cites, the evidence must show, all checked here:

1. the record is well formed, is the one cited, and its producer's trusted key signed it;
2. a checkpoint signed by a trusted log key, cosigned by the witness quorum (final);
3. a Merkle inclusion proof from the record's leaf hash to that checkpoint's root.

Then every assertion is checked against the record's fields, and every number and every
name-like token (``j.ortiz``, ``northwind-supply``) in the claim's text must be one an
assertion establishes. A claim with any failure is rejected, with reasons.

``check_claims_in_log`` does the same against a ``claims.log.ProvenLog``, where the whole
export was verified and a record's presence under the final root is its proof.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from common.errors import PreceptError
from ledger.checkpoint import open_checkpoint
from ledger.cosignature import open_cosignatures
from ledger.format import HASH_SIZE, Record, b64decode, leaf_hash
from merkle.proofs import verify_inclusion
from verify.keys import TrustedKeys, WitnessPolicy

if TYPE_CHECKING:
    from claims.log import ProvenLog

OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_FIELD = re.compile(r"^(body|producer|type|ts|record_id|seq|stream)(\.[a-z_][a-z0-9_]{0,63}){0,6}$")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ULID = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")
# Ids and names as the ledger writes them: letters and digits joined by . _ or - (j.ortiz,
# northwind-supply, create_payment). A word like this in a claim must be asserted (D66).
_NAME = re.compile(r"(?<![\w.-])[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+(?![\w-]|\.\w)", re.IGNORECASE)
Value = str | int | bool | None


@dataclass(frozen=True)
class Assertion:
    record_id: str
    field: str  # a dotted path into the record JSON, e.g. body.resource.amount_cents
    op: str
    value: Value

    def describe(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


@dataclass(frozen=True)
class Claim:
    text: str
    record_ids: tuple[str, ...]
    assertions: tuple[Assertion, ...]


@dataclass(frozen=True)
class Evidence:
    """A record as served, and its inclusion proof against a cosigned checkpoint note."""

    record: dict[str, Any]
    leaf_index: int
    tree_size: int
    hashes: tuple[str, ...]
    checkpoint: str

    @classmethod
    def from_api(cls, record: dict[str, Any], proof: dict[str, Any]) -> "Evidence":
        """From ``GET /ledger/v1/records/{id}`` (its ``record``) and ``.../proof``."""
        return cls(
            record=record,
            leaf_index=int(proof["leaf_index"]),
            tree_size=int(proof["tree_size"]),
            hashes=tuple(proof["hashes"]),
            checkpoint=str(proof["checkpoint"]),
        )


@dataclass(frozen=True)
class Trust:
    keys: TrustedKeys  # producer and log keys, from the auditor's keys file
    origin: str  # the log's checkpoint origin
    witnesses: WitnessPolicy | None  # None: checkpoints need no cosignatures (not final)


@dataclass(frozen=True)
class ClaimResult:
    claim: Claim
    accepted: bool
    reasons: tuple[str, ...] = field(default=())


def resolve(record: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """``(True, value)`` for a dotted path through JSON objects, else ``(False, None)``."""
    if not _FIELD.fullmatch(path):
        return False, None
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _same_type(a: object, b: object) -> bool:
    # JSON true is not 1 and 1 is not "1": bool is checked before int on purpose.
    kinds = (bool, int, str, type(None))
    return any(isinstance(a, k) and isinstance(b, k) for k in kinds) and (
        isinstance(a, bool) == isinstance(b, bool)
    )


def _compare(actual: Any, op: str, expected: Value) -> bool | None:
    """The comparison's truth, or None when the two values cannot be compared."""
    if op in ("eq", "ne"):
        if isinstance(actual, (dict, list)):
            return None
        equal = _same_type(actual, expected) and actual == expected
        return equal if op == "eq" else not equal
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (actual, expected))
    if not ints:
        return None
    a, b = int(actual), int(expected)  # type: ignore[arg-type]  # both checked to be int
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def check_record(record_id: str, evidence: Evidence | None, trust: Trust) -> str | None:
    """Why the evidence does not prove ``record_id`` is in a final checkpoint, or None."""
    if evidence is None:
        return f"no evidence for record {record_id}"
    try:
        record = Record.from_json(evidence.record)
        sub = record.submission
        if sub.record_id != record_id:
            return f"{record_id}: evidence is for {sub.record_id}"
        why = trust.keys.check_producer(
            sub.producer.id, sub.producer.key_id, sub.signing_bytes(), sub.producer.sig_bytes()
        )
        if why is not None:
            return f"{record_id}: producer signature: {why}"
        checkpoint = None
        for key in trust.keys.log_note_keys(trust.origin):
            try:
                checkpoint = open_checkpoint(evidence.checkpoint, key)
                break
            except PreceptError:
                continue
        if checkpoint is None:
            return f"{record_id}: checkpoint does not verify under a trusted log key"
        if trust.witnesses is not None:
            cosigners = open_cosignatures(evidence.checkpoint, trust.witnesses.keys)
            have, need = len(cosigners), trust.witnesses.quorum
            if have < need:
                return f"{record_id}: checkpoint not final ({have} of {need} cosignatures)"
        if checkpoint.size != evidence.tree_size or evidence.leaf_index != record.seq - 1:
            return f"{record_id}: proof is not for this record and checkpoint"
        hashes = [b64decode(h, size=HASH_SIZE, field="proof") for h in evidence.hashes]
    except PreceptError as exc:
        return f"{record_id}: malformed evidence ({exc.code})"
    if not verify_inclusion(
        leaf_hash(record.canonical()),
        evidence.leaf_index,
        evidence.tree_size,
        hashes,
        checkpoint.root,
    ):
        return f"{record_id}: inclusion proof does not reach the checkpoint root"
    return None


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(t.replace(",", "")) for t in _NUMBER.findall(_ULID.sub(" ", text))}


def _names(text: str) -> set[str]:
    return {t.lower() for t in _NAME.findall(text)}


def _backed_numbers(assertions: Sequence[Assertion]) -> set[Decimal]:
    """Numbers the assertions establish: values, cents as dollars, digits inside strings."""
    backed: set[Decimal] = set()
    for a in assertions:
        if isinstance(a.value, int) and not isinstance(a.value, bool):
            backed.add(Decimal(a.value))
            if a.field.endswith("_cents"):
                backed.add(Decimal(a.value) / 100)
        elif isinstance(a.value, str):
            backed |= _numbers(a.value)
    return backed


def _backed_names(assertions: Iterable[Assertion]) -> set[str]:
    values = [a.value for a in assertions if isinstance(a.value, str)]
    return {v.lower() for v in values} | {n for v in values for n in _names(v)}


def unbacked_names(text: str, assertions: Iterable[Assertion]) -> list[str]:
    """Name-like tokens in ``text`` that no assertion's string value establishes."""
    return sorted(_names(text) - _backed_names(assertions))


def _text_problems(
    text: str, assertions: Sequence[Assertion], numbers: Iterable[int] = ()
) -> list[str]:
    problems = []
    unbacked = sorted(_numbers(text) - _backed_numbers(assertions) - {Decimal(n) for n in numbers})
    if unbacked:
        problems.append(
            f"numbers in the text that no assertion establishes: {[str(n) for n in unbacked]}"
        )
    if names := unbacked_names(text, assertions):
        problems.append(f"names in the text that no assertion establishes: {names}")
    return problems


def _check_assertion(
    a: Assertion, claim: Claim, record_of: Callable[[str], Mapping[str, Any]]
) -> str | None:
    if a.record_id not in claim.record_ids:
        return f"{a.record_id}: asserted on but not cited by the claim"
    if a.op not in OPS:
        return f"unknown operator {a.op!r}"
    found, actual = resolve(record_of(a.record_id), a.field)
    if not found:
        return f"{a.record_id}: field not found: {a.field}"
    outcome = _compare(actual, a.op, a.value)
    if outcome is None:
        return f"{a.record_id}: cannot compare {a.describe()} with {actual!r}"
    return None if outcome else f"{a.record_id}: {a.describe()} is false (record has {actual!r})"


def _judge(
    claim: Claim,
    unproven: Callable[[str], str | None],
    record_of: Callable[[str], Mapping[str, Any]],
) -> ClaimResult:
    if not claim.record_ids or not claim.assertions:
        return ClaimResult(claim, False, ("claim cites no record or makes no assertion",))
    reasons = [r for record_id in claim.record_ids if (r := unproven(record_id))]
    if not reasons:  # fields are only read from records whose proofs held
        reasons += [r for a in claim.assertions if (r := _check_assertion(a, claim, record_of))]
    reasons += _text_problems(claim.text, claim.assertions)
    return ClaimResult(claim, not reasons, tuple(reasons))


def check_claims(
    claims: Sequence[Claim], evidence: Mapping[str, Evidence], trust: Trust
) -> list[ClaimResult]:
    """One result per claim, in order. Each record's proof is checked once."""
    cache: dict[str, str | None] = {}

    def unproven(record_id: str) -> str | None:
        if record_id not in cache:
            cache[record_id] = check_record(record_id, evidence.get(record_id), trust)
        return cache[record_id]

    return [_judge(c, unproven, lambda i: evidence[i].record) for c in claims]


def check_claims_in_log(claims: Sequence[Claim], log: "ProvenLog") -> list[ClaimResult]:
    """One result per claim, against records proven by verifying the whole export."""

    def unproven(record_id: str) -> str | None:
        if log.get(record_id) is None:
            return f"{record_id}: not in the proven log (records 1..{log.size})"
        return None

    def record_of(record_id: str) -> Mapping[str, Any]:
        return log.get(record_id) or {}

    return [_judge(c, unproven, record_of) for c in claims]


def check_summary(
    summary: str, results: Sequence[ClaimResult], *, numbers: Iterable[int] = ()
) -> list[str]:
    """Problems with a summary: it may state only numbers and names that accepted claims
    assert, plus ``numbers`` code computed (a row count, a log size)."""
    held = [a for r in results if r.accepted for a in r.claim.assertions]
    return _text_problems(summary, held, numbers)
