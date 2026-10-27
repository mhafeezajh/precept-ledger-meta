"""Record format v1: canonical JSON, field validation, hash formulas and signing inputs.

The offline verifier may import this module (skill §2), so it stays pure: no I/O, no keys,
no network. Every hashed or signed byte string in the project is built here.
"""

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import rfc8785

from common.clock import parse_ts
from common.errors import FormatError
from common.ids import is_ulid

RECORD_VERSION = 1
ALG_ED25519 = "ed25519"
SUPPORTED_ALGS = frozenset({ALG_ED25519})  # skill §4: unknown alg is rejected, never guessed
HASH_SIZE = 32
SIG_SIZE = 64
MAX_SAFE_INT = 2**53 - 1
MAX_RECORD_BYTES = 64 * 1024

# Domain separation (skill §4, D18): no two kinds of object are ever hashed or signed alike.
GENESIS_HASH = bytes(HASH_SIZE)
CHAIN_PREFIX = b"PL-chain-v1"
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
RECORD_SIG_CONTEXT = b"PL-record-v1\x00"
RECEIPT_SIG_CONTEXT = b"PL-receipt-v1\x00"

_STREAM = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*$")
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_PRODUCER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KEY_ID = re.compile(r"^ed25519:[0-9a-f]{64}$")


# --- canonical JSON -------------------------------------------------------------------------


def check_ijson(value: object, path: str = "$") -> None:
    """Raise unless ``value`` is I-JSON as this project restricts it: no floats (D17)."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise FormatError(f"{path}: integer outside ±(2^53-1)", code="format.not_ijson")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FormatError(
                f"{path}: string is not valid Unicode", code="format.not_ijson"
            ) from exc
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_ijson(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FormatError(
                    f"{path}: object key {key!r} is not a string", code="format.not_ijson"
                )
            check_ijson(key, f"{path}.<key>")
            check_ijson(item, f"{path}.{key}")
    else:
        raise FormatError(f"{path}: {type(value).__name__} is not allowed", code="format.not_ijson")


def canonical_bytes(value: object) -> bytes:
    """RFC 8785 bytes of ``value``: the only serialization used for hashing or signing."""
    check_ijson(value)
    return rfc8785.dumps(value)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FormatError(f"duplicate key {key!r}", code="format.duplicate_key")
        result[key] = value
    return result


def _reject_float(text: str) -> None:
    raise FormatError(f"number {text} is not an integer", code="format.not_ijson")


def _parse_int(text: str) -> int:
    value = int(text)
    if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
        raise FormatError(f"integer {text} outside ±(2^53-1)", code="format.not_ijson")
    return value


def parse_json_strict(data: bytes) -> object:
    """Parse UTF-8 JSON, rejecting duplicate keys, NaN/Infinity, floats and unsafe integers."""
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_reject_float,
            parse_int=_parse_int,
            parse_constant=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError(f"not valid UTF-8 JSON: {exc}", code="format.bad_json") from exc
    check_ijson(value)
    return value


def parse_canonical(data: bytes) -> object:
    """Parse ``data`` and require it to be byte-identical to its own canonical form (D20)."""
    value = parse_json_strict(data)
    if canonical_bytes(value) != data:
        raise FormatError("bytes are not RFC 8785 canonical", code="format.not_canonical")
    return value


# --- encodings and hashes -------------------------------------------------------------------


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str, *, size: int, field: str) -> bytes:
    """Decode padded standard base64 of exactly ``size`` bytes, canonical spelling only (D19)."""
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise FormatError(f"{field}: not standard base64", code="format.bad_base64") from exc
    if len(data) != size or b64encode(data) != text:
        raise FormatError(
            f"{field}: expected canonical base64 of {size} bytes", code="format.bad_base64"
        )
    return data


def key_id_for(public_key_raw: bytes) -> str:
    if len(public_key_raw) != 32:
        raise FormatError("Ed25519 public key must be 32 bytes", code="format.bad_key")
    return "ed25519:" + hashlib.sha256(public_key_raw).hexdigest()


def chain_hash(prev_hash: bytes, record_bytes: bytes) -> bytes:
    """``h_n = SHA-256("PL-chain-v1" ‖ h_(n-1) ‖ JCS(record_n))``."""
    if len(prev_hash) != HASH_SIZE:
        raise FormatError("previous hash must be 32 bytes", code="format.bad_hash")
    return hashlib.sha256(CHAIN_PREFIX + prev_hash + record_bytes).digest()


def leaf_hash(record_bytes: bytes) -> bytes:
    """RFC 9162 leaf hash: ``SHA-256(0x00 ‖ JCS(record))``."""
    return hashlib.sha256(LEAF_PREFIX + record_bytes).digest()


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
