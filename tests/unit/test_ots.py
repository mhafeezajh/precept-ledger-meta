"""OpenTimestamps proofs: parsing from our digest, attestations, upgrading, hostile input; real
calendar responses in tests/vectors/ots-published.json."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anchor.ots import (
    BITCOIN_TAG,
    PENDING_TAG,
    Attestation,
    read_timestamp,
    upgrade_timestamp,
)
from common.errors import AnchorError

DIGEST = hashlib.sha256(b"checkpoint body\n").digest()
PUBLISHED = json.loads(
    (Path(__file__).parents[1] / "vectors" / "ots-published.json").read_text("utf-8")
)


def varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def varbytes(data: bytes) -> bytes:
    return varuint(len(data)) + data


def pending(url: str) -> bytes:
    return b"\x00" + PENDING_TAG + varbytes(varbytes(url.encode()))


def bitcoin(height: int) -> bytes:
    return b"\x00" + BITCOIN_TAG + varbytes(varuint(height))


def append(data: bytes) -> bytes:
    return b"\xf0" + varbytes(data)


def prepend(data: bytes) -> bytes:
    return b"\xf1" + varbytes(data)


SHA256 = b"\x08"


def commitment_after(*steps: tuple[str, bytes]) -> bytes:
    msg = DIGEST
    for op, arg in steps:
        msg = {"append": msg + arg, "prepend": arg + msg, "sha256": hashlib.sha256(msg).digest()}[
            op
        ]
    return msg


def test_single_pending_attestation() -> None:
    proof = append(b"nonce") + SHA256 + pending("https://alice.example")
    commitment = commitment_after(("append", b"nonce"), ("sha256", b""))
    assert read_timestamp(proof, DIGEST) == [
        Attestation("pending", commitment, "https://alice.example", None)
    ]


def test_forks_and_bitcoin_attestation() -> None:
    # fork: one branch pending, one branch confirmed in block 900000
    proof = (
        b"\xff"
        + prepend(b"p")
        + SHA256
        + pending("https://bob.example")
        + SHA256
        + bitcoin(900_000)
    )
    found = read_timestamp(proof, DIGEST)
    assert [a.kind for a in found] == ["pending", "bitcoin"]
    assert found[1] == Attestation("bitcoin", hashlib.sha256(DIGEST).digest(), None, 900_000)
    assert found[0].commitment == commitment_after(("prepend", b"p"), ("sha256", b""))


def test_unknown_attestation_is_reported_not_trusted() -> None:
    proof = b"\x00" + bytes(range(8)) + varbytes(b"whatever")
    assert read_timestamp(proof, DIGEST)[0].kind == "unknown"


def test_upgrade_replaces_the_pending_branch() -> None:
    proof = append(b"n") + SHA256 + pending("https://alice.example")
    commitment = read_timestamp(proof, DIGEST)[0].commitment
    upgrade = append(b"tx") + SHA256 + bitcoin(912_345)
    upgraded = upgrade_timestamp(proof, DIGEST, commitment, upgrade)
    (attestation,) = read_timestamp(upgraded, DIGEST)
    assert (attestation.kind, attestation.height) == ("bitcoin", 912_345)
    assert attestation.commitment == hashlib.sha256(commitment + b"tx").digest()
    with pytest.raises(AnchorError):  # no pending attestation at that commitment
        upgrade_timestamp(proof, DIGEST, bytes(32), upgrade)
    with pytest.raises(AnchorError):  # the upgrade itself must parse
        upgrade_timestamp(proof, DIGEST, commitment, b"\x99")


@pytest.mark.parametrize(
    "proof",
    [
        b"",
        b"\x08",  # op with nothing after it
        b"\x99" + pending("https://x"),  # unknown op
        SHA256 + pending("https://x") + b"extra",  # trailing bytes
        b"\x00" + PENDING_TAG + varbytes(varbytes(b"bad uri \x01")),
        b"\x00" + PENDING_TAG + b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x7f",  # huge length
        append(b"x" * 5000) + pending("https://x"),  # argument too long
        SHA256 * 300 + pending("https://x"),  # too deep
        b"\xff" * 10,
        b"x" * 70_000,
        b"\x00" + PENDING_TAG + b"\x8a" + b"\xff" * 9 + b"\x01",  # varuint over 63 bits
        b"\x00" + BITCOIN_TAG + varbytes(varuint(5) + b"extra"),  # payload trailing bytes
        append(b"x" * 4000) + append(b"y" * 200) + pending("https://x"),  # message too long
    ],
    ids=[
        "empty",
        "truncated",
        "unknown-op",
        "trailing",
        "bad-uri",
        "huge-length",
        "long-arg",
        "too-deep",
        "only-forks",
        "too-large",
        "varuint-overflow",
        "payload-trailing",
        "message-too-long",
    ],
)
def test_hostile_proofs_are_rejected(proof: bytes) -> None:
    with pytest.raises(AnchorError) as exc:
        read_timestamp(proof, DIGEST)
    assert exc.value.code == "anchor.bad_ots"


def test_proof_without_any_attestation_is_rejected() -> None:
    # A proof must end in attestations; a dangling fork marker is malformed, tested above.
    with pytest.raises(AnchorError):
        read_timestamp(SHA256, DIGEST)


@given(
    st.lists(st.sampled_from(["append", "prepend", "sha256"]), max_size=20), st.binary(max_size=40)
)
def test_commitment_is_the_digest_run_through_the_ops(ops: list[str], arg: bytes) -> None:
    proof, steps = b"", []
    for op in ops:
        proof += {"append": append(arg), "prepend": prepend(arg), "sha256": SHA256}[op]
        steps.append((op, arg))
    proof += pending("https://cal.example")
    (attestation,) = read_timestamp(proof, DIGEST)
    assert attestation.commitment == commitment_after(*steps)


@pytest.mark.parametrize("entry", PUBLISHED["calendars"], ids=lambda e: e["url"])
def test_published_calendar_responses_commit_to_the_checkpoint(entry: dict[str, str]) -> None:
    digest = bytes.fromhex(PUBLISHED["digest_hex"])
    assert hashlib.sha256(PUBLISHED["checkpoint_body"].encode()).digest() == digest
    attestations = read_timestamp(base64.b64decode(entry["timestamp_b64"]), digest)
    assert attestations
    assert all(a.kind == "pending" for a in attestations)
    assert all(
        a.calendar is not None and a.calendar.endswith(".calendar.opentimestamps.org")
        for a in attestations
    )
    # The same bytes read from another digest end at different commitments: they prove
    # nothing about it.
    elsewhere = read_timestamp(base64.b64decode(entry["timestamp_b64"]), bytes(32))
    assert [a.commitment for a in elsewhere] != [a.commitment for a in attestations]


def test_digest_must_be_32_bytes_and_upgrade_start_must_be_sane() -> None:
    with pytest.raises(AnchorError):
        read_timestamp(SHA256 + pending("https://x"), b"short")
    with pytest.raises(AnchorError):
        upgrade_timestamp(SHA256 + pending("https://x"), DIGEST, b"", bitcoin(1))


@pytest.mark.parametrize(
    "entry",
    [e for e in PUBLISHED["calendars"] if "confirmed_b64" in e],
    ids=lambda e: e["url"],
)
def test_confirmed_published_proofs_reach_a_bitcoin_block(entry: dict[str, str]) -> None:
    # Present once `make ots-upgrade` has run after confirmation (O5); skipped until then.
    digest = bytes.fromhex(PUBLISHED["digest_hex"])
    kinds = {a.kind for a in read_timestamp(base64.b64decode(entry["confirmed_b64"]), digest)}
    assert "bitcoin" in kinds
