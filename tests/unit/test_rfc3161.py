"""RFC 3161 tokens: requests, responses, and every check verify_token makes, against a test
TSA and against real tokens from two public TSAs (tests/vectors/rfc3161-published.json)."""

import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from asn1crypto import tsp
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from anchor.rfc3161 import (
    anchor_digest,
    load_roots,
    timestamp_request,
    token_from_response,
    verify_token,
)
from common.errors import AnchorError
from ledger.checkpoint import Checkpoint, parse_checkpoint
from tests.tsa import T0, FakeTSA

DIGEST = hashlib.sha256(b"checkpoint body\n").digest()
PUBLISHED = json.loads(
    (Path(__file__).parents[1] / "vectors" / "rfc3161-published.json").read_text("utf-8")
)


@pytest.fixture(scope="module")
def tsa() -> FakeTSA:
    return FakeTSA()


def code_of(token: bytes, tsa: FakeTSA, digest: bytes = DIGEST, reason: str | None = None) -> str:
    with pytest.raises(AnchorError, match=reason) as exc:
        verify_token(token, digest, load_roots(tsa.root_pem))
    return exc.value.code


def test_anchor_digest_is_sha256_of_the_checkpoint_body() -> None:
    checkpoint = Checkpoint("o.example/s", 3, bytes(32))
    assert anchor_digest(checkpoint) == hashlib.sha256(checkpoint.body().encode()).digest()


def test_request_round_trip() -> None:
    request = tsp.TimeStampReq.load(timestamp_request(DIGEST, 99))
    assert request["message_imprint"]["hashed_message"].native == DIGEST
    assert request["message_imprint"]["hash_algorithm"]["algorithm"].native == "sha256"
    assert (request["nonce"].native, request["cert_req"].native) == (99, True)
    with pytest.raises(AnchorError):
        timestamp_request(b"short", 1)


@pytest.mark.parametrize(
    "key",
    [
        ec.generate_private_key(ec.SECP256R1()),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ed25519.Ed25519PrivateKey.generate(),
    ],
    ids=["ecdsa", "rsa", "ed25519"],
)
def test_valid_tokens_verify_for_each_key_type(key: object) -> None:
    tsa = FakeTSA(key=key)  # type: ignore[arg-type]
    info = verify_token(tsa.token(DIGEST), DIGEST, load_roots(tsa.root_pem))
    assert info.gen_time == T0
    assert (info.serial, info.policy, info.tsa) == (42, "1.3.6.1.4.1.99999.1", "Test TSA")


def test_intermediate_ca_and_ess_v1(tsa: FakeTSA) -> None:
    chained = FakeTSA(intermediates=1)
    assert verify_token(chained.token(DIGEST), DIGEST, load_roots(chained.root_pem))
    assert verify_token(tsa.token(DIGEST, ess="v1"), DIGEST, load_roots(tsa.root_pem))


def test_response_checks(tsa: FakeTSA) -> None:
    good = tsa.response(DIGEST, nonce=5).dump()
    assert token_from_response(good, DIGEST, 5)
    cases = [
        (tsa.response(DIGEST, nonce=5, status="rejection").dump(), 5, "anchor.tsa_refused"),
        (good, 6, "anchor.bad_timestamp"),  # nonce mismatch
        (tsa.response(hashlib.sha256(b"x").digest(), nonce=5).dump(), 5, "anchor.wrong_digest"),
        (b"junk", 5, "anchor.bad_timestamp"),
        (b"x" * 70_000, 5, "anchor.bad_timestamp"),
    ]
    for response, nonce, code in cases:
        with pytest.raises(AnchorError) as exc:
            token_from_response(response, DIGEST, nonce)
        assert exc.value.code == code


@pytest.mark.parametrize(
    ("options", "code", "reason"),
    [
        ({"imprint_alg": "sha512"}, "anchor.bad_timestamp", "not SHA-256"),
        ({"bad_signature": True}, "anchor.bad_signature", "does not verify"),
        ({"bad_message_digest": True}, "anchor.bad_timestamp", "message-digest"),
        ({"ess": "none"}, "anchor.bad_timestamp", "no ESS"),
        ({"include_certs": False}, "anchor.bad_timestamp", "not in the token"),
        ({"content_type": "data"}, "anchor.bad_timestamp", "content-type"),
        ({"signers": 2}, "anchor.bad_timestamp", "exactly one signer"),
        ({"gen_time": T0 + timedelta(days=400)}, "anchor.bad_timestamp", "not valid at genTime"),
    ],
    ids=[
        "imprint-alg",
        "signature",
        "message-digest",
        "no-ess",
        "no-certs",
        "content-type",
        "two-signers",
        "cert-expired-at-gentime",
    ],
)
def test_broken_tokens_are_rejected(
    tsa: FakeTSA, options: dict[str, object], code: str, reason: str
) -> None:
    assert code_of(tsa.token(DIGEST, **options), tsa, reason=reason) == code


def test_wrong_digest_and_garbage(tsa: FakeTSA) -> None:
    assert (
        code_of(tsa.token(DIGEST), tsa, hashlib.sha256(b"other").digest()) == "anchor.wrong_digest"
    )
    assert code_of(b"garbage", tsa) == "anchor.bad_timestamp"
    assert code_of(b"x" * 70_000, tsa) == "anchor.bad_timestamp"


def test_ess_naming_another_certificate_is_rejected(tsa: FakeTSA) -> None:
    assert code_of(tsa.token(DIGEST, ess_cert=tsa.root), tsa, reason="another certificate") == (
        "anchor.bad_timestamp"
    )


@pytest.mark.parametrize(
    ("tsa_options", "reason"),
    [
        ({"eku": None}, "no extended key usage"),
        ({"eku": [ExtendedKeyUsageOID.CODE_SIGNING]}, "critical timeStamping"),
        ({"eku_critical": False}, "critical timeStamping"),
    ],
    ids=["no-eku", "wrong-eku", "eku-not-critical"],
)
def test_signer_must_be_a_critical_timestamping_certificate(
    tsa_options: dict[str, object], reason: str
) -> None:
    tsa = FakeTSA(**tsa_options)  # type: ignore[arg-type]
    assert code_of(tsa.token(DIGEST), tsa, reason=reason) == "anchor.bad_timestamp"


def test_untrusted_root_is_rejected(tsa: FakeTSA) -> None:
    other = FakeTSA()
    with pytest.raises(AnchorError) as exc:
        verify_token(tsa.token(DIGEST), DIGEST, load_roots(other.root_pem))
    assert exc.value.code == "anchor.untrusted"
    with pytest.raises(AnchorError):
        verify_token(tsa.token(DIGEST), DIGEST, [])


def test_load_roots_rejects_garbage() -> None:
    with pytest.raises(AnchorError) as exc:
        load_roots(b"not pem")
    assert exc.value.code == "anchor.bad_roots"


# --- real tokens from public TSAs, verified offline -----------------------------------------


@pytest.mark.parametrize("entry", PUBLISHED["tokens"], ids=lambda e: e["tsa"])
def test_published_tokens_verify_offline(entry: dict[str, str]) -> None:
    checkpoint = parse_checkpoint(PUBLISHED["checkpoint_body"])
    digest = anchor_digest(checkpoint)
    assert digest.hex() == PUBLISHED["digest_hex"]
    token = base64.b64decode(entry["token_b64"])
    info = verify_token(token, digest, load_roots(entry["root_pem"].encode()))
    assert info.gen_time.isoformat() == entry["gen_time"]
    with pytest.raises(AnchorError):
        verify_token(
            token, hashlib.sha256(b"rewritten").digest(), load_roots(entry["root_pem"].encode())
        )
    others = [e for e in PUBLISHED["tokens"] if e is not entry]
    with pytest.raises(AnchorError):  # one TSA's root does not vouch for another
        verify_token(token, digest, load_roots(others[0]["root_pem"].encode()))


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"algorithm_override": "sha256_rsa"}, "unsupported for this key"),
        ({"duplicate_attribute": True}, "appears twice"),
        ({"digest_alg": "md5"}, "unsupported digest"),
    ],
    ids=["algorithm-key-mismatch", "duplicate-attribute", "md5-digest"],
)
def test_more_broken_tokens(tsa: FakeTSA, options: dict[str, object], reason: str) -> None:
    assert code_of(tsa.token(DIGEST, **options), tsa, reason=reason) == "anchor.bad_timestamp"


def test_trusting_the_tsa_certificate_itself(tsa: FakeTSA) -> None:
    assert verify_token(tsa.token(DIGEST), DIGEST, [tsa.cert])


def test_root_expired_at_gentime_is_rejected() -> None:
    tsa = FakeTSA(root_not_after=T0 - timedelta(days=1))
    assert code_of(tsa.token(DIGEST), tsa, reason="root not valid") == "anchor.bad_timestamp"


def test_chain_too_long_is_rejected() -> None:
    tsa = FakeTSA(intermediates=6)
    assert code_of(tsa.token(DIGEST), tsa, reason="too long") == "anchor.untrusted"
    deep = FakeTSA(intermediates=4)
    assert verify_token(deep.token(DIGEST), DIGEST, load_roots(deep.root_pem))


def test_not_signed_data_is_rejected(tsa: FakeTSA) -> None:
    from asn1crypto import cms

    data = cms.ContentInfo({"content_type": "data", "content": b"x"}).dump()
    assert code_of(data, tsa, reason="not CMS SignedData") == "anchor.bad_timestamp"
