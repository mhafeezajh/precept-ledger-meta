"""Check tests/vectors/ledger-v1.json two ways: against the code, and against hashlib and
cryptography directly, so the vectors are not only self-consistent."""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ledger.chain import verify_chain
from ledger.format import canonical_bytes
from ledger.signing import KeyRing
from scripts.make_vectors import build

VECTORS = json.loads((Path(__file__).parents[1] / "vectors" / "ledger-v1.json").read_text("utf-8"))


def test_vectors_file_matches_generator() -> None:
    assert build() == VECTORS


def test_canonical_vectors() -> None:
    for case in VECTORS["canonical"]:
        assert canonical_bytes(json.loads(case["input_json"])).decode() == case["canonical"]


def test_record_vectors_independently() -> None:
    keys = VECTORS["keys"]
    gate = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys["gate-1"]["public_key"]))
    log = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys["log"]["public_key"]))
    for name in ("gate-1", "log"):
        raw = base64.b64decode(keys[name]["public_key"])
        assert keys[name]["key_id"] == "ed25519:" + hashlib.sha256(raw).hexdigest()
    prev = bytes(32)
    for vec in VECTORS["records"]:
        payload = bytes.fromhex(vec["signing_payload_hex"])
        assert payload.startswith(b"PL-record-v1\x00")
        gate.verify(base64.b64decode(vec["producer_sig"]), payload)
        record = vec["record_canonical"].encode()
        assert json.loads(record)["prev_hash"] == base64.b64encode(prev).decode()
        assert (
            vec["leaf_hash"] == base64.b64encode(hashlib.sha256(b"\x00" + record).digest()).decode()
        )
        chain = hashlib.sha256(b"PL-chain-v1" + prev + record).digest()
        assert vec["chain_hash"] == base64.b64encode(chain).decode()
        receipt = json.loads(vec["receipt_canonical"])
        receipt_payload = bytes.fromhex(vec["receipt_signing_payload_hex"])
        assert receipt_payload.startswith(b"PL-receipt-v1\x00")
        log.verify(base64.b64decode(receipt["sig"]), receipt_payload)
        assert receipt["chain_hash"] == vec["chain_hash"]
        prev = chain


def test_record_vectors_pass_chain_verification() -> None:
    raw = base64.b64decode(VECTORS["keys"]["gate-1"]["public_key"])
    lines = [v["record_canonical"].encode() for v in VECTORS["records"]]
    report = verify_chain(lines, KeyRing([("gate-1", raw)]), stream=VECTORS["stream"])
    assert report.ok
    assert report.checked == 3


CHECKPOINT_VECTORS = json.loads(
    (Path(__file__).parents[1] / "vectors" / "checkpoint-v1.json").read_text("utf-8")
)


def test_checkpoint_vectors_match_generator() -> None:
    from scripts.make_vectors import build_checkpoint

    assert build_checkpoint() == CHECKPOINT_VECTORS


def test_checkpoint_vector_independently() -> None:
    # The root commits to the three ledger-v1 records; the signature is plain Ed25519 over the
    # body, with a 4-byte key hash in front (C2SP signed-note).
    from merkle.proofs import root_hash

    vec = CHECKPOINT_VECTORS["checkpoint"]
    leaves = [base64.b64decode(r["leaf_hash"]) for r in VECTORS["records"]]
    assert base64.b64decode(vec["root_hash"]) == root_hash(leaves)
    assert vec["body"] == f"{vec['origin']}\n{len(leaves)}\n{vec['root_hash']}\n"
    name, key_hash, key_b64 = vec["verifier_key"].split("+", 2)
    key = base64.b64decode(key_b64)
    assert key[0] == 1
    assert key[1:] == base64.b64decode(VECTORS["keys"]["log"]["public_key"])
    assert hashlib.sha256(name.encode() + b"\n" + key).digest()[:4].hex() == key_hash
    text, sig_line = vec["note"].split("\n\n")
    dash, signer_name, sig_b64 = sig_line.rstrip("\n").split(" ")
    assert (dash, signer_name) == ("—", name)
    raw = base64.b64decode(sig_b64)
    assert raw[:4].hex() == key_hash
    Ed25519PublicKey.from_public_bytes(key[1:]).verify(raw[4:], (text + "\n").encode())
