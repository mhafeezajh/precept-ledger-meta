"""C2SP tlog-cosignature (cosignature/v1, Ed25519): message bytes, key ids, lines, opening."""

import base64
import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from common.errors import FormatError, SignatureError
from ledger.checkpoint import Checkpoint, NoteKey, open_checkpoint, open_note, sign_checkpoint
from ledger.cosignature import (
    COSIGNATURE_ALG,
    CosignerKey,
    add_cosignatures,
    cosign,
    cosignature_message,
    format_add_checkpoint,
    open_cosignatures,
    parse_add_checkpoint,
)
from tests.helpers import signer_for

LOG, W1, W2 = signer_for("log"), signer_for("witness-1"), signer_for("witness-2")
ORIGIN = "precept-ledger.local/harbor-federal/decisions"
CHECKPOINT = Checkpoint(ORIGIN, 3, bytes(range(32)))
NOTE = sign_checkpoint(CHECKPOINT, LOG)
K1, K2 = CosignerKey("witness-1", W1.public_key_raw), CosignerKey("witness-2", W2.public_key_raw)
T = 1_793_300_000


def test_message_matches_the_spec_example() -> None:
    # The example from c2sp.org/tlog-cosignature, byte for byte.
    root = base64.b64decode("CsUYapGGPo4dkMgIAUqom/Xajj7h2fB2MPA3j2jxq2I=")
    message = cosignature_message(
        Checkpoint("example.com/behind-the-sofa", 20852163, root), 1679315147
    )
    assert message == (
        b"cosignature/v1\n"
        b"time 1679315147\n"
        b"example.com/behind-the-sofa\n"
        b"20852163\n"
        b"CsUYapGGPo4dkMgIAUqom/Xajj7h2fB2MPA3j2jxq2I=\n"
    )


def test_message_includes_extension_lines() -> None:
    checkpoint = Checkpoint(ORIGIN, 3, bytes(32), ("ext one",))
    assert cosignature_message(checkpoint, 5).endswith(b"\next one\n")


def test_key_hash_uses_signature_type_4() -> None:
    material = b"witness-1\n\x04" + W1.public_key_raw
    assert K1.key_hash == hashlib.sha256(material).digest()[:4]
    assert COSIGNATURE_ALG == 0x04
    # The same key under the plain Ed25519 type has a different key hash.
    assert NoteKey("witness-1", W1.public_key_raw).key_hash != K1.key_hash


def test_verifier_round_trip_and_rejections() -> None:
    text = K1.verifier()
    name, hex_hash, encoded = text.split("+", 2)
    assert (name, hex_hash) == ("witness-1", K1.key_hash.hex())
    assert base64.b64decode(encoded) == b"\x04" + W1.public_key_raw
    assert CosignerKey.from_verifier(text) == K1
    with pytest.raises(FormatError):  # a plain Ed25519 note key is not a cosigner key
        CosignerKey.from_verifier(NoteKey("witness-1", W1.public_key_raw).verifier())
    with pytest.raises(FormatError):
        CosignerKey.from_verifier(f"witness-1+00000000+{encoded}")
    with pytest.raises(FormatError):
        CosignerKey.from_verifier("witness-1")
    with pytest.raises(FormatError):
        CosignerKey("bad name", W1.public_key_raw)
    with pytest.raises(FormatError):
        CosignerKey("witness-1", b"short")


def test_line_layout_is_key_hash_timestamp_signature() -> None:
    line = cosign(CHECKPOINT, W1, "witness-1", T)
    assert line.startswith("— witness-1 ")
    assert line.endswith("\n")
    raw = base64.b64decode(line.split(" ")[2])
    assert len(raw) == 4 + 8 + 64
    assert raw[:4] == K1.key_hash
    assert int.from_bytes(raw[4:12], "big") == T
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(W1.public_key_raw).verify(
        raw[12:], cosignature_message(CHECKPOINT, T)
    )


def test_open_cosignatures_returns_verified_names_and_times() -> None:
    note = add_cosignatures(
        NOTE, [cosign(CHECKPOINT, W1, "witness-1", T), cosign(CHECKPOINT, W2, "witness-2", T + 1)]
    )
    assert open_cosignatures(note, [K1, K2]) == {"witness-1": T, "witness-2": T + 1}
    assert open_cosignatures(note, [K1]) == {"witness-1": T}
    assert open_cosignatures(note, []) == {}
    # The log signature still verifies: cosignature lines are ignored as unknown keys.
    assert open_checkpoint(note, NoteKey(ORIGIN, LOG.public_key_raw)) == CHECKPOINT


def test_bad_cosignature_from_a_trusted_key_rejects() -> None:
    other = Checkpoint(ORIGIN, 3, bytes(32))
    note = add_cosignatures(NOTE, [cosign(other, W1, "witness-1", T)])
    with pytest.raises(SignatureError) as exc:
        open_cosignatures(note, [K1])
    assert exc.value.code == "cosignature.bad_signature"


def test_changed_timestamp_rejects() -> None:
    line = cosign(CHECKPOINT, W1, "witness-1", T)
    raw = bytearray(base64.b64decode(line.split(" ")[2]))
    raw[11] ^= 1
    forged = f"— witness-1 {base64.b64encode(bytes(raw)).decode()}\n"
    with pytest.raises(SignatureError):
        open_cosignatures(add_cosignatures(NOTE, [forged]), [K1])


def test_wrong_length_cosignature_is_malformed() -> None:
    raw = K1.key_hash + bytes(8 + 63)
    bad = f"— witness-1 {base64.b64encode(raw).decode()}\n"
    with pytest.raises(FormatError) as exc:
        open_cosignatures(add_cosignatures(NOTE, [bad]), [K1])
    assert exc.value.code == "cosignature.malformed"


def test_two_cosignatures_from_one_witness_reject() -> None:
    lines = [cosign(CHECKPOINT, W1, "witness-1", T), cosign(CHECKPOINT, W1, "witness-1", T + 1)]
    with pytest.raises(FormatError) as exc:
        open_cosignatures(add_cosignatures(NOTE, lines), [K1])
    assert exc.value.code == "cosignature.duplicate"


def test_cosignature_cannot_pass_as_a_note_signature() -> None:
    # Domain separation: the raw Ed25519 signature over the cosignature message, relabelled
    # with the plain-Ed25519 key hash, must not verify as a signature over the note text.
    raw = base64.b64decode(cosign(CHECKPOINT, W1, ORIGIN, T).split(" ")[2])
    relabelled = NoteKey(ORIGIN, W1.public_key_raw).key_hash + raw[12:]
    note = CHECKPOINT.body() + f"\n— {ORIGIN} {base64.b64encode(relabelled).decode()}\n"
    with pytest.raises(SignatureError):
        open_note(note, [NoteKey(ORIGIN, W1.public_key_raw)])


def test_add_cosignatures_is_idempotent_and_checks_lines() -> None:
    line = cosign(CHECKPOINT, W1, "witness-1", T)
    once = add_cosignatures(NOTE, [line])
    assert add_cosignatures(once, [line]) == once
    assert once == NOTE + line
    with pytest.raises(FormatError):
        add_cosignatures(NOTE, ["not a signature line\n"])


def test_timestamp_must_fit_in_64_bits() -> None:
    with pytest.raises(FormatError):
        cosign(CHECKPOINT, W1, "witness-1", -1)
    with pytest.raises(FormatError):
        cosign(CHECKPOINT, W1, "witness-1", 2**64)


@given(
    size=st.integers(0, 2**40),
    root=st.binary(min_size=32, max_size=32),
    timestamp=st.integers(0, 2**64 - 1),
    flip=st.integers(0, 75),
)
def test_round_trip_and_any_byte_flip_fails(
    size: int, root: bytes, timestamp: int, flip: int
) -> None:
    checkpoint = Checkpoint(ORIGIN, size, root)
    note = sign_checkpoint(checkpoint, LOG)
    line = cosign(checkpoint, W1, "witness-1", timestamp)
    assert open_cosignatures(add_cosignatures(note, [line]), [K1]) == {"witness-1": timestamp}
    raw = bytearray(base64.b64decode(line.split(" ")[2]))
    raw[flip] ^= 0x01
    forged = f"— witness-1 {base64.b64encode(bytes(raw)).decode()}\n"
    tampered = add_cosignatures(note, [forged])
    if flip < 4:  # key hash changed: no longer this witness's line, so it is ignored
        assert open_cosignatures(tampered, [K1]) == {}
    else:
        with pytest.raises(SignatureError):
            open_cosignatures(tampered, [K1])


# --- add-checkpoint request body (C2SP tlog-witness) ----------------------------------------


def test_add_checkpoint_body_round_trip() -> None:
    proof = [bytes([1]) * 32, bytes([2]) * 32]
    body = format_add_checkpoint(2, proof, NOTE)
    old = base64.b64encode(bytes([1]) * 32).decode()
    assert body.startswith(f"old 2\n{old}\n".encode())
    assert body.endswith(b"\n\n" + NOTE.encode())
    assert parse_add_checkpoint(body) == (2, proof, NOTE)
    assert parse_add_checkpoint(format_add_checkpoint(0, [], NOTE)) == (0, [], NOTE)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"old 2\n",
        b"old 02\n\n" + NOTE.encode(),
        b"old -1\n\n" + NOTE.encode(),
        b"new 2\n\n" + NOTE.encode(),
        b"old 2\nnot-base64!\n\n" + NOTE.encode(),
        b"old 2\n" + base64.b64encode(bytes(31)) + b"\n\n" + NOTE.encode(),
        b"old 2\n" + (base64.b64encode(bytes(32)) + b"\n") * 64 + b"\n" + NOTE.encode(),
        b"old 2\n\n\xff",
        b"old 2\n\n" + b"x" * 80_000,
    ],
    ids=[
        "empty",
        "no-note",
        "leading-zero",
        "negative",
        "wrong-keyword",
        "bad-base64",
        "short-hash",
        "too-many-hashes",
        "not-utf8",
        "too-large",
    ],
)
def test_malformed_add_checkpoint_bodies_are_rejected(body: bytes) -> None:
    with pytest.raises(FormatError) as exc:
        parse_add_checkpoint(body)
    assert exc.value.code == "witness.bad_request"
