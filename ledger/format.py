"""Record format v1: field validation, record and receipt types, and signing inputs.

The offline verifier may import this module (skill §2), so it stays pure: no I/O, no keys,
no network. Every hashed or signed byte string in the project is built here or in
``ledger.canonical`` (canonical JSON, encodings, hashes), whose names are re-exported below.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from common.clock import parse_ts
from common.errors import FormatError
from common.ids import is_ulid
from ledger.canonical import CHAIN_PREFIX as CHAIN_PREFIX  # re-exported: callers import format
from ledger.canonical import GENESIS_HASH as GENESIS_HASH
from ledger.canonical import HASH_SIZE as HASH_SIZE
from ledger.canonical import LEAF_PREFIX as LEAF_PREFIX
from ledger.canonical import MAX_SAFE_INT as MAX_SAFE_INT
from ledger.canonical import NODE_PREFIX as NODE_PREFIX
from ledger.canonical import b64decode as b64decode
from ledger.canonical import b64encode as b64encode
from ledger.canonical import canonical_bytes as canonical_bytes
from ledger.canonical import chain_hash as chain_hash
from ledger.canonical import check_ijson as check_ijson
from ledger.canonical import key_id_for as key_id_for
from ledger.canonical import leaf_hash as leaf_hash
from ledger.canonical import parse_canonical as parse_canonical
from ledger.canonical import parse_json_strict as parse_json_strict

RECORD_VERSION = 1
ALG_ED25519 = "ed25519"
SUPPORTED_ALGS = frozenset({ALG_ED25519})  # skill §4: unknown alg is rejected, never guessed
SIG_SIZE = 64
MAX_RECORD_BYTES = 64 * 1024

# Signing contexts (skill §4, D18); the chain and Merkle prefixes are in ledger.canonical.
RECORD_SIG_CONTEXT = b"PL-record-v1\x00"
RECEIPT_SIG_CONTEXT = b"PL-receipt-v1\x00"

_STREAM = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*$")
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_PRODUCER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KEY_ID = re.compile(r"^ed25519:[0-9a-f]{64}$")


# --- field helpers --------------------------------------------------------------------------


def _object(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FormatError(f"{what} must be a JSON object", code="format.bad_type")
    return value


def _exact_keys(data: Mapping[str, Any], required: frozenset[str], what: str) -> None:
    unknown = sorted(set(data) - required)
    if unknown:
        raise FormatError(f"{what}: unknown field(s) {unknown}", code="format.unknown_field")
    missing = sorted(required - set(data))
    if missing:
        raise FormatError(f"{what}: missing field(s) {missing}", code="format.missing_field")


def _str(
    value: object, field: str, pattern: re.Pattern[str] | None = None, max_len: int = 128
) -> str:
    if not isinstance(value, str):
        raise FormatError(f"{field} must be a string", code="format.bad_type")
    if len(value) > max_len or (pattern is not None and not pattern.fullmatch(value)):
        raise FormatError(f"{field} has an invalid value {value!r}", code="format.bad_value")
    return value


def _int(value: object, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormatError(f"{field} must be an integer", code="format.bad_type")
    if not minimum <= value <= MAX_SAFE_INT:
        raise FormatError(f"{field} out of range: {value}", code="format.bad_value")
    return value


def _version(value: object) -> None:
    if _int(value, "v", 0) != RECORD_VERSION:
        raise FormatError(f"unsupported version {value}", code="format.bad_version")


def _alg(value: object, field: str) -> None:
    if _str(value, field) not in SUPPORTED_ALGS:
        raise FormatError(
            f"{field}: unsupported algorithm {value!r}", code="format.unsupported_alg"
        )


def _ulid(value: object, field: str) -> None:
    if not is_ulid(_str(value, field)):
        raise FormatError(f"{field} is not a ULID", code="format.bad_value")


def _ts(value: object, field: str) -> None:
    parse_ts(_str(value, field))


# --- record types ---------------------------------------------------------------------------

_PRODUCER_KEYS = frozenset({"id", "alg", "key_id", "sig"})
_SUBMISSION_KEYS = frozenset({"v", "stream", "record_id", "ts", "type", "producer", "body"})
_RECORD_KEYS = _SUBMISSION_KEYS | {"seq", "prev_hash"}
_RECEIPT_KEYS = frozenset(
    {
        "v",
        "alg",
        "stream",
        "record_id",
        "seq",
        "leaf_hash",
        "chain_hash",
        "received_at",
        "key_id",
        "sig",
    }
)


@dataclass(frozen=True)
class Producer:
    id: str
    key_id: str
    sig: str
    alg: str = ALG_ED25519

    def __post_init__(self) -> None:
        _str(self.id, "producer.id", _PRODUCER_ID)
        _alg(self.alg, "producer.alg")
        _str(self.key_id, "producer.key_id", _KEY_ID)
        b64decode(self.sig, size=SIG_SIZE, field="producer.sig")

    def sig_bytes(self) -> bytes:
        return b64decode(self.sig, size=SIG_SIZE, field="producer.sig")

    @classmethod
    def from_json(cls, value: object) -> "Producer":
        data = _object(value, "producer")
        _exact_keys(data, _PRODUCER_KEYS, "producer")
        return cls(id=data["id"], alg=data["alg"], key_id=data["key_id"], sig=data["sig"])

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "alg": self.alg, "key_id": self.key_id, "sig": self.sig}


def record_signing_payload(unsigned: Mapping[str, Any]) -> bytes:
    """Bytes a producer signs: context ‖ JCS(submission without ``producer.sig``) (D9, D18)."""
    return RECORD_SIG_CONTEXT + canonical_bytes(dict(unsigned))


@dataclass(frozen=True)
class Submission:
    """A record as signed by its producer, before the ledger assigns ``seq`` and ``prev_hash``.

    ``body`` is treated as immutable; nothing in the project mutates it after construction.
    """

    stream: str
    record_id: str
    ts: str
    type: str
    producer: Producer
    body: dict[str, Any]
    v: int = RECORD_VERSION

    def __post_init__(self) -> None:
        _version(self.v)
        _str(self.stream, "stream", _STREAM)
        _ulid(self.record_id, "record_id")
        _ts(self.ts, "ts")
        _str(self.type, "type", _TYPE, max_len=64)
        if not isinstance(self.body, dict):
            raise FormatError("body must be a JSON object", code="format.bad_type")
        check_ijson(self.body, "$.body")

    @classmethod
    def from_json(cls, value: object) -> "Submission":
        data = _object(value, "submission")
        _exact_keys(data, _SUBMISSION_KEYS, "submission")
        return cls._from_fields(data)

    @classmethod
    def _from_fields(cls, data: Mapping[str, Any]) -> "Submission":
        return cls(
            v=data["v"],
            stream=data["stream"],
            record_id=data["record_id"],
            ts=data["ts"],
            type=data["type"],
            producer=Producer.from_json(data["producer"]),
            body=data["body"],
        )

    def unsigned_json(self) -> dict[str, Any]:
        producer = self.producer.to_json()
        del producer["sig"]
        return {**self._common_json(), "producer": producer}

    def to_json(self) -> dict[str, Any]:
        return {**self._common_json(), "producer": self.producer.to_json()}

    def _common_json(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "stream": self.stream,
            "record_id": self.record_id,
            "ts": self.ts,
            "type": self.type,
            "body": self.body,
        }

    def signing_bytes(self) -> bytes:
        return record_signing_payload(self.unsigned_json())


@dataclass(frozen=True)
class Record:
    """A submission placed in its stream: the exact object the chain and leaf hashes cover."""

    submission: Submission
    seq: int
    prev_hash: str

    def __post_init__(self) -> None:
        _int(self.seq, "seq", 1)  # D16: the first record is seq 1
        b64decode(self.prev_hash, size=HASH_SIZE, field="prev_hash")
        if len(self.canonical()) > MAX_RECORD_BYTES:
            raise FormatError(f"record exceeds {MAX_RECORD_BYTES} bytes", code="format.too_large")

    @classmethod
    def from_json(cls, value: object) -> "Record":
        data = _object(value, "record")
        _exact_keys(data, _RECORD_KEYS, "record")
        return cls(
            submission=Submission._from_fields(data), seq=data["seq"], prev_hash=data["prev_hash"]
        )

    def to_json(self) -> dict[str, Any]:
        return {**self.submission.to_json(), "seq": self.seq, "prev_hash": self.prev_hash}

    def canonical(self) -> bytes:
        return canonical_bytes(self.to_json())

    def prev_hash_bytes(self) -> bytes:
        return b64decode(self.prev_hash, size=HASH_SIZE, field="prev_hash")


def receipt_signing_payload(unsigned: Mapping[str, Any]) -> bytes:
    """Bytes the log key signs: context ‖ JCS(receipt without ``sig``) (D18)."""
    return RECEIPT_SIG_CONTEXT + canonical_bytes(dict(unsigned))


@dataclass(frozen=True)
class Receipt:
    """The ledger's signed statement that it accepted a record at ``seq`` with these hashes."""

    stream: str
    record_id: str
    seq: int
    leaf_hash: str
    chain_hash: str
    received_at: str
    key_id: str
    sig: str
    v: int = RECORD_VERSION
    alg: str = ALG_ED25519

    def __post_init__(self) -> None:
        _version(self.v)
        _alg(self.alg, "alg")
        _str(self.stream, "stream", _STREAM)
        _ulid(self.record_id, "record_id")
        _int(self.seq, "seq", 1)
        b64decode(self.leaf_hash, size=HASH_SIZE, field="leaf_hash")
        b64decode(self.chain_hash, size=HASH_SIZE, field="chain_hash")
        _ts(self.received_at, "received_at")
        _str(self.key_id, "key_id", _KEY_ID)
        b64decode(self.sig, size=SIG_SIZE, field="sig")

    @classmethod
    def from_json(cls, value: object) -> "Receipt":
        data = _object(value, "receipt")
        _exact_keys(data, _RECEIPT_KEYS, "receipt")
        return cls(**data)

    def unsigned_json(self) -> dict[str, Any]:
        data = self.to_json()
        del data["sig"]
        return data

    def to_json(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "alg": self.alg,
            "stream": self.stream,
            "record_id": self.record_id,
            "seq": self.seq,
            "leaf_hash": self.leaf_hash,
            "chain_hash": self.chain_hash,
            "received_at": self.received_at,
            "key_id": self.key_id,
            "sig": self.sig,
        }

    def signing_bytes(self) -> bytes:
        return receipt_signing_payload(self.unsigned_json())

    def chain_hash_bytes(self) -> bytes:
        return b64decode(self.chain_hash, size=HASH_SIZE, field="chain_hash")

    def leaf_hash_bytes(self) -> bytes:
        return b64decode(self.leaf_hash, size=HASH_SIZE, field="leaf_hash")

    def sig_bytes(self) -> bytes:
        return b64decode(self.sig, size=SIG_SIZE, field="sig")
