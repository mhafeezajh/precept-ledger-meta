import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from common.errors import FormatError, SignatureError
from ledger.format import Producer, b64encode
from ledger.signing import KeyRing, Signer, sign_submission
from tests.helpers import STREAM, clock, make_submission, signer_for


def signer_for_from_seed_is_deterministic() -> None:
    a = signer_for("gate-1")
    b = signer_for("gate-1")
    assert a.key_id == b.key_id
    assert a.key_id == "ed25519:" + hashlib.sha256(a.public_key_raw).hexdigest()
    assert a.sign(b"m") == b.sign(b"m")  # Ed25519 signatures are deterministic
    assert a.key_id != signer_for("gate-2").key_id


def signer_for_rejects_bad_seed_length() -> None:
    with pytest.raises(FormatError):
        Signer.from_seed(b"short")


def signer_for_repr_never_shows_private_key() -> None:
    signer = signer_for("gate-1")
    text = repr(signer) + str(signer)
    assert signer.key_id in text
    seed_hex = hashlib.sha256(b"PL TEST ONLY gate-1").hexdigest()
    assert seed_hex not in text


def test_generated_signers_differ() -> None:
    assert Signer.generate().key_id != Signer.generate().key_id


def test_pem_round_trip(tmp_path: Path) -> None:
    signer = signer_for("log")
    path = tmp_path / "log.pem"
    path.write_bytes(signer.private_pem())
    loaded = Signer.from_pem_file(path)
    assert loaded.key_id == signer.key_id


def test_pem_loader_rejects_other_key_types_and_garbage(tmp_path: Path) -> None:
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_path = tmp_path / "ec.pem"
    ec_path.write_bytes(
        ec_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(SignatureError) as exc:
        Signer.from_pem_file(ec_path)
    assert exc.value.code == "signature.bad_key"
    garbage = tmp_path / "garbage.pem"
    garbage.write_bytes(b"not a key")
    with pytest.raises(SignatureError):
        Signer.from_pem_file(garbage)
    with pytest.raises(SignatureError):
        Signer.from_pem_file(tmp_path / "missing.pem")


def test_sign_submission_verifies_with_keyring() -> None:
    signer = signer_for("gate-1")
    sub = make_submission(signer, clock())
    keys = KeyRing([("gate-1", signer.public_key_raw)])
    keys.verify_submission(sub)
    assert sub.producer.key_id == signer.key_id
    assert keys.owner(signer.key_id) == "gate-1"


def test_tampered_body_fails_verification() -> None:
    signer = signer_for("gate-1")
    sub = make_submission(signer, clock(), body={"amount": 48000})
    forged = replace(sub, body={"amount": 480})
    keys = KeyRing([("gate-1", signer.public_key_raw)])
    with pytest.raises(SignatureError) as exc:
        keys.verify_submission(forged)
    assert exc.value.code == "signature.invalid"


def test_unknown_key_is_rejected() -> None:
    sub = make_submission(signer_for("rogue"), clock())
    keys = KeyRing([("gate-1", signer_for("gate-1").public_key_raw)])
    with pytest.raises(SignatureError) as exc:
        keys.verify_submission(sub)
    assert exc.value.code == "signature.unknown_key"


def test_producer_id_must_match_key_owner() -> None:
    signer = signer_for("gate-1")
    sub = make_submission(signer, clock(), producer_id="gate-2")
    keys = KeyRing([("gate-1", signer.public_key_raw)])
    with pytest.raises(SignatureError) as exc:
        keys.verify_submission(sub)
    assert exc.value.code == "signature.producer_mismatch"


def test_signature_from_another_key_with_same_key_id_label_fails() -> None:
    # A rogue signs, then relabels the envelope with gate-1's key_id.
    gate, rogue = signer_for("gate-1"), signer_for("rogue")
    sub = make_submission(rogue, clock())
    relabeled_unsigned = replace(sub, producer=replace(sub.producer, key_id=gate.key_id))
    forged = replace(
        relabeled_unsigned,
        producer=replace(
            relabeled_unsigned.producer,
            sig=b64encode(rogue.sign(relabeled_unsigned.signing_bytes())),
        ),
    )
    with pytest.raises(SignatureError):
        KeyRing([("gate-1", gate.public_key_raw)]).verify_submission(forged)


def test_keyring_rejects_conflicting_owners_and_bad_keys() -> None:
    raw = signer_for("gate-1").public_key_raw
    with pytest.raises(SignatureError):
        KeyRing([("gate-1", raw), ("gate-2", raw)])
    with pytest.raises(FormatError):
        KeyRing([("gate-1", b"short")])
    assert len(KeyRing([("gate-1", raw), ("gate-1", raw)])) == 1


def test_keyring_verify_raw_signature() -> None:
    signer = signer_for("log")
    keys = KeyRing([("log", signer.public_key_raw)])
    keys.verify(signer.key_id, b"msg", signer.sign(b"msg"), owner="log")
    with pytest.raises(SignatureError):
        keys.verify(signer.key_id, b"other", signer.sign(b"msg"))


def test_sign_submission_rejects_invalid_fields() -> None:
    with pytest.raises(FormatError):
        sign_submission(
            signer_for("gate-1"),
            producer_id="gate-1",
            stream=STREAM,
            record_id="bad",
            ts="2026-10-29T21:14:03.112Z",
            record_type="gate.decision",
            body={},
        )


def test_producer_sig_bytes_round_trip() -> None:
    sig = signer_for("gate-1").sign(b"x")
    producer = Producer(id="gate-1", key_id=signer_for("gate-1").key_id, sig=b64encode(sig))
    assert producer.sig_bytes() == sig
