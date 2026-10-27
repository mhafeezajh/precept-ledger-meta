"""Canonical JSON (RFC 8785), strict I-JSON parsing, encodings and the chain and leaf hashes.

Split out of ``ledger.format`` to keep both under the size limit (skill §3); ``ledger.format``
re-exports every name here, so callers keep importing from it. Pure: no I/O, no keys.
"""

import base64
import binascii
import hashlib
import json
from typing import Any, cast

import rfc8785

from common.errors import FormatError
from merkle.hashing import LEAF_PREFIX as LEAF_PREFIX  # re-exported for callers
from merkle.hashing import NODE_PREFIX as NODE_PREFIX
from merkle.hashing import leaf_hash as merkle_leaf_hash

HASH_SIZE = 32
MAX_SAFE_INT = 2**53 - 1

# Domain separation (skill §4, D18): no two kinds of object are ever hashed or signed alike.
# LEAF_PREFIX and NODE_PREFIX come from merkle.hashing (D25) and are re-exported here.
GENESIS_HASH = bytes(HASH_SIZE)
CHAIN_PREFIX = b"PL-chain-v1"


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
    return rfc8785.dumps(cast(Any, value))  # check_ijson just proved value is a JSON value


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
    """RFC 9162 leaf hash of a record: ``SHA-256(0x00 ‖ JCS(record))``, defined in ``merkle``."""
    return merkle_leaf_hash(record_bytes)
