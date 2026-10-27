import base64
import hashlib
from typing import Any

import pytest

from common.errors import FormatError
from ledger import format as fmt

SIG = base64.b64encode(bytes(64)).decode()
HASH = base64.b64encode(bytes(32)).decode()
KEY_ID = "ed25519:" + "ab" * 32


def submission_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "v": 1,
        "stream": "harbor-federal/decisions",
        "record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
        "ts": "2026-10-29T21:14:03.112Z",
        "type": "gate.decision",
        "producer": {"id": "gate-1", "alg": "ed25519", "key_id": KEY_ID, "sig": SIG},
        "body": {"decision": "escalate", "resource": {"amount": 48000, "payee_new": True}},
    }
    data.update(overrides)
    return data


def record_json(**overrides: Any) -> dict[str, Any]:
    data = submission_json()
    data.update({"seq": 1, "prev_hash": HASH})
    data.update(overrides)
    return data


def receipt_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "v": 1,
        "alg": "ed25519",
        "stream": "harbor-federal/decisions",
        "record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
        "seq": 1,
        "leaf_hash": HASH,
        "chain_hash": HASH,
        "received_at": "2026-10-29T21:14:03.150Z",
        "key_id": KEY_ID,
        "sig": SIG,
    }
    data.update(overrides)
    return data


# --- canonical JSON -------------------------------------------------------------------------


def test_canonical_bytes_sorts_keys_and_encodes_utf8() -> None:
    assert fmt.canonical_bytes({"b": 1, "a": "€", "c": [True, None]}) == (
        '{"a":"€","b":1,"c":[true,null]}'.encode()
    )


def test_canonical_bytes_is_independent_of_insertion_order() -> None:
    assert fmt.canonical_bytes({"x": {"b": 2, "a": 1}}) == fmt.canonical_bytes(
        {"x": {"a": 1, "b": 2}}
    )


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        float("nan"),
        float("inf"),
        2**53,
        -(2**53),
        {1: "non-string key"},
        ("tuple",),
        b"bytes",
        "\ud800",  # lone surrogate is not I-JSON
        {"nested": [{"deep": 0.1}]},
    ],
)
def test_canonical_bytes_rejects_non_ijson(value: object) -> None:
    with pytest.raises(FormatError):
        fmt.canonical_bytes(value)


def test_canonical_bytes_accepts_safe_integer_bounds() -> None:
    assert fmt.canonical_bytes([2**53 - 1, -(2**53 - 1)]) == b"[9007199254740991,-9007199254740991]"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1.0}',
        b'{"a":1e3}',
        b'{"a":"\xff"}',
        b'{"a":',
        b'{"a":9007199254740992}',
    ],
)
def test_parse_json_strict_rejects(raw: bytes) -> None:
    with pytest.raises(FormatError):
        fmt.parse_json_strict(raw)


def test_parse_json_strict_accepts_valid_json() -> None:
    assert fmt.parse_json_strict(b' {"b": [1, "x"], "a": null} ') == {"a": None, "b": [1, "x"]}


@pytest.mark.parametrize("raw", [b'{"b":1, "a":2}', b'{"b":1,"a":2}', b'{"a":"\\u0041"}'])
def test_parse_canonical_rejects_non_canonical_bytes(raw: bytes) -> None:
    with pytest.raises(FormatError) as exc:
        fmt.parse_canonical(raw)
    assert exc.value.code in {"format.not_canonical", "format.bad_json"}


def test_parse_canonical_accepts_canonical_bytes() -> None:
    assert fmt.parse_canonical(b'{"a":2,"b":1}') == {"a": 2, "b": 1}


# --- base64, key ids, hashes ----------------------------------------------------------------


def test_b64_round_trip() -> None:
    data = bytes(range(32))
    assert fmt.b64decode(fmt.b64encode(data), size=32, field="h") == data


@pytest.mark.parametrize(
    "text",
    [
        "AAAA",  # wrong length
        "not base64!" * 4,
        base64.b64encode(bytes(32)).decode().rstrip("="),  # missing padding
        base64.b64encode(bytes(32)).decode()[:-2] + "B=",  # same bytes, non-canonical bits
        base64.urlsafe_b64encode(b"\xfb" * 32).decode(),  # url-safe alphabet
    ],
)
def test_b64decode_rejects_bad_or_non_canonical_text(text: str) -> None:
    with pytest.raises(FormatError):
        fmt.b64decode(text, size=32, field="h")


def test_key_id_is_sha256_of_raw_public_key() -> None:
    raw = bytes(range(32))
    assert fmt.key_id_for(raw) == "ed25519:" + hashlib.sha256(raw).hexdigest()
    with pytest.raises(FormatError):
        fmt.key_id_for(b"short")


def test_chain_and_leaf_hash_formulas() -> None:
    prev = bytes(range(32))
    body = b'{"a":1}'
    assert fmt.chain_hash(prev, body) == hashlib.sha256(b"PL-chain-v1" + prev + body).digest()
    assert fmt.leaf_hash(body) == hashlib.sha256(b"\x00" + body).digest()
    assert fmt.chain_hash(prev, body) != fmt.leaf_hash(body)
    with pytest.raises(FormatError):
        fmt.chain_hash(b"short", body)


def test_domain_prefixes_are_distinct() -> None:
    prefixes = [
        fmt.CHAIN_PREFIX,
        fmt.LEAF_PREFIX,
        fmt.NODE_PREFIX,
        fmt.RECORD_SIG_CONTEXT,
        fmt.RECEIPT_SIG_CONTEXT,
    ]
    assert len(set(prefixes)) == len(prefixes)


# --- submissions and records ----------------------------------------------------------------


def test_submission_round_trip() -> None:
    sub = fmt.Submission.from_json(submission_json())
    assert sub.to_json() == submission_json()
    assert sub.producer.id == "gate-1"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"v": 2}, "format.bad_version"),
        ({"v": True}, "format.bad_type"),
        ({"stream": "Harbor Federal"}, "format.bad_value"),
        ({"stream": "a" * 129}, "format.bad_value"),
        ({"record_id": "not-a-ulid"}, "format.bad_value"),
        ({"ts": "2026-10-29T21:14:03Z"}, "format.bad_time"),
        ({"type": "Decision"}, "format.bad_value"),
        ({"body": []}, "format.bad_type"),
        ({"body": {"amount": 1.5}}, "format.not_ijson"),
        ({"extra": 1}, "format.unknown_field"),
        ({"seq": 1}, "format.unknown_field"),
    ],
)
def test_submission_rejects_bad_fields(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(FormatError) as exc:
        fmt.Submission.from_json(submission_json(**overrides))
    assert exc.value.code == code


def test_submission_rejects_missing_field() -> None:
    data = submission_json()
    del data["body"]
    with pytest.raises(FormatError) as exc:
        fmt.Submission.from_json(data)
    assert exc.value.code == "format.missing_field"


@pytest.mark.parametrize(
    ("producer", "code"),
    [
        ({"id": "gate-1", "alg": "rsa", "key_id": KEY_ID, "sig": SIG}, "format.unsupported_alg"),
        (
            {"id": "gate-1", "alg": "ed25519", "key_id": "ed25519:zz", "sig": SIG},
            "format.bad_value",
        ),
        ({"id": "gate-1", "alg": "ed25519", "key_id": KEY_ID, "sig": HASH}, "format.bad_base64"),
        ({"id": "Gate 1", "alg": "ed25519", "key_id": KEY_ID, "sig": SIG}, "format.bad_value"),
        ({"id": "gate-1", "alg": "ed25519", "key_id": KEY_ID}, "format.missing_field"),
        (
            {"id": "g", "alg": "ed25519", "key_id": KEY_ID, "sig": SIG, "x": 1},
            "format.unknown_field",
        ),
    ],
)
def test_submission_rejects_bad_producer(producer: dict[str, Any], code: str) -> None:
    with pytest.raises(FormatError) as exc:
        fmt.Submission.from_json(submission_json(producer=producer))
    assert exc.value.code == code


def test_from_json_rejects_non_object() -> None:
    with pytest.raises(FormatError):
        fmt.Submission.from_json([])
    with pytest.raises(FormatError):
        fmt.Record.from_json("x")
    with pytest.raises(FormatError):
        fmt.Receipt.from_json(None)


def test_signing_bytes_exclude_sig_seq_and_prev_hash() -> None:
    record = fmt.Record.from_json(record_json())
    payload = record.submission.signing_bytes()
    assert payload.startswith(fmt.RECORD_SIG_CONTEXT)
    unsigned = fmt.parse_canonical(payload[len(fmt.RECORD_SIG_CONTEXT) :])
    assert isinstance(unsigned, dict)
    assert "seq" not in unsigned
    assert "prev_hash" not in unsigned
    assert "sig" not in unsigned["producer"]
    assert unsigned["producer"]["key_id"] == KEY_ID


def test_record_round_trip_and_canonical_bytes() -> None:
    record = fmt.Record.from_json(record_json())
    assert record.to_json() == record_json()
    assert record.canonical() == fmt.canonical_bytes(record_json())
    assert record.seq == 1
    assert record.prev_hash_bytes() == bytes(32)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"seq": 0}, "format.bad_value"),
        ({"seq": True}, "format.bad_type"),
        ({"seq": "1"}, "format.bad_type"),
        ({"prev_hash": "AAAA"}, "format.bad_base64"),
        ({"extra": 1}, "format.unknown_field"),
    ],
)
def test_record_rejects_bad_fields(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(FormatError) as exc:
        fmt.Record.from_json(record_json(**overrides))
    assert exc.value.code == code


def test_record_rejects_oversize_body() -> None:
    with pytest.raises(FormatError) as exc:
        fmt.Record.from_json(record_json(body={"blob": "x" * fmt.MAX_RECORD_BYTES}))
    assert exc.value.code == "format.too_large"


# --- receipts -------------------------------------------------------------------------------


def test_receipt_round_trip_and_signing_bytes() -> None:
    receipt = fmt.Receipt.from_json(receipt_json())
    assert receipt.to_json() == receipt_json()
    payload = receipt.signing_bytes()
    assert payload.startswith(fmt.RECEIPT_SIG_CONTEXT)
    assert b'"sig"' not in payload
    assert receipt.chain_hash_bytes() == bytes(32)
    assert receipt.leaf_hash_bytes() == bytes(32)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"alg": "ed448"}, "format.unsupported_alg"),
        ({"v": 2}, "format.bad_version"),
        ({"seq": -1}, "format.bad_value"),
        ({"received_at": "yesterday"}, "format.bad_time"),
        ({"chain_hash": SIG}, "format.bad_base64"),
        ({"extra": 1}, "format.unknown_field"),
    ],
)
def test_receipt_rejects_bad_fields(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(FormatError) as exc:
        fmt.Receipt.from_json(receipt_json(**overrides))
    assert exc.value.code == code
