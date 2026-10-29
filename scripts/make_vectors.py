"""Generate tests/vectors/ledger-v1.json, checkpoint-v1.json and cosignature-v1.json from
fixed inputs and the TEST ONLY keys.

Run with ``make vectors``. Output is deterministic (Ed25519 signatures are deterministic), so a
diff after running it means the format changed, which needs a DECISIONS.md entry (skill §4).
"""

import base64
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.chain import GENESIS, link  # noqa: E402  # path set up above for a plain script
from ledger.checkpoint import Checkpoint, NoteKey, sign_checkpoint  # noqa: E402
from ledger.cosignature import (  # noqa: E402
    CosignerKey,
    add_cosignatures,
    cosign,
    cosignature_message,
)
from ledger.format import b64encode, canonical_bytes  # noqa: E402
from ledger.receipts import issue_receipt  # noqa: E402
from ledger.signing import Signer, sign_submission  # noqa: E402
from merkle.proofs import root_hash  # noqa: E402

OUT = ROOT / "tests" / "vectors" / "ledger-v1.json"
CHECKPOINT_OUT = ROOT / "tests" / "vectors" / "checkpoint-v1.json"
COSIGNATURE_OUT = ROOT / "tests" / "vectors" / "cosignature-v1.json"
DELEGATION_OUT = ROOT / "tests" / "vectors" / "delegation-v1.json"
COSIGN_TIME = 1793300000  # 2026-10-28T19:33:20Z, fixed so the vectors are deterministic
STREAM = "harbor-federal/decisions"
ORIGIN = f"precept-ledger.local/{STREAM}"

CANONICAL_CASES: list[Any] = [
    {"b": 1, "a": "€", "c": [True, None, -7]},
    {"z": {"y": 2, "x": 1}, "a": []},
    {"text": 'line\nbreak "quoted" \u0007 é 😀'},
    [9007199254740991, -9007199254740991, 0],
]

RECORDS = [
    {
        "record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
        "ts": "2026-10-29T21:14:03.112Z",
        "type": "gate.decision",
        "body": {
            "agent": "payments-ops",
            "action": "value.transfer",
            "decision": "escalate",
            "resource": {"amount": 48000, "payee": "Northwind Supply", "payee_new": True},
            "policy_ids": ["pay-new-payee-01"],
        },
        "received_at": "2026-10-29T21:14:03.140Z",
    },
    {
        "record_id": "01JB6X9D0A1B2C3D4E5F6G7H8J",
        "ts": "2026-10-29T21:16:41.508Z",
        "type": "gate.approval",
        "body": {
            "decision_record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
            "approver": "j.ortiz",
            "verdict": "approved",
        },
        "received_at": "2026-10-29T21:16:41.530Z",
    },
    {
        "record_id": "01JB6X9F7K8M9N0P1Q2R3S4T5V",
        "ts": "2026-10-29T21:16:42.001Z",
        "type": "gate.result",
        "body": {
            "decision_record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
            "tool": "create_payment",
            "status": "ok",
            "note": "Café Österreich — façade",
        },
        "received_at": "2026-10-29T21:16:42.020Z",
    },
]


def seed(name: str) -> Signer:
    seeds = json.loads((ROOT / "tests" / "keys" / "test-keys.json").read_text())["seeds"]
    return Signer.from_seed(bytes.fromhex(seeds[name]))


def build() -> dict[str, Any]:
    gate, log = seed("gate-1"), seed("log")
    head, records = GENESIS, []
    for spec in RECORDS:
        sub = sign_submission(
            gate,
            producer_id="gate-1",
            stream=STREAM,
            record_id=spec["record_id"],
            ts=spec["ts"],
            record_type=spec["type"],
            body=spec["body"],
        )
        item = link(head, sub)
        receipt = issue_receipt(log, item, spec["received_at"])
        records.append(
            {
                "signing_payload_hex": sub.signing_bytes().hex(),
                "producer_sig": sub.producer.sig,
                "record_canonical": item.record.canonical().decode("utf-8"),
                "leaf_hash": b64encode(item.leaf_hash),
                "chain_hash": b64encode(item.chain_hash),
                "receipt_canonical": canonical_bytes(receipt.to_json()).decode("utf-8"),
                "receipt_signing_payload_hex": receipt.signing_bytes().hex(),
            }
        )
        head = item.head
    return {
        "description": "Precept Ledger record format v1 vectors. Keys are TEST ONLY (tests/keys/).",
        "keys": {
            name: {"public_key": b64encode(s.public_key_raw), "key_id": s.key_id}
            for name, s in (("gate-1", gate), ("log", log))
        },
        "canonical": [
            {
                "input_json": json.dumps(case, ensure_ascii=False),
                "canonical": canonical_bytes(case).decode("utf-8"),
            }
            for case in CANONICAL_CASES
        ],
        "stream": STREAM,
        "records": records,
    }


def build_checkpoint() -> dict[str, Any]:
    """A checkpoint over the three records in ledger-v1.json, signed by the TEST ONLY log key."""
    leaves = [base64.b64decode(r["leaf_hash"]) for r in build()["records"]]
    checkpoint = Checkpoint(ORIGIN, len(leaves), root_hash(leaves))
    log = seed("log")
    return {
        "description": "C2SP checkpoint over the ledger-v1 records. Keys are TEST ONLY.",
        "checkpoint": {
            "origin": ORIGIN,
            "size": checkpoint.size,
            "root_hash": b64encode(checkpoint.root),
            "body": checkpoint.body(),
            "verifier_key": NoteKey(ORIGIN, log.public_key_raw).verifier(),
            "note": sign_checkpoint(checkpoint, log),
        },
    }


def build_cosignature() -> dict[str, Any]:
    """Two witnesses cosign the checkpoint in checkpoint-v1.json. Keys are TEST ONLY."""
    note = build_checkpoint()["checkpoint"]["note"]
    leaves = [base64.b64decode(r["leaf_hash"]) for r in build()["records"]]
    checkpoint = Checkpoint(ORIGIN, len(leaves), root_hash(leaves))
    witnesses = []
    for offset, name in enumerate(("witness-1", "witness-2")):
        signer = seed(name)
        timestamp = COSIGN_TIME + offset
        witnesses.append(
            {
                "name": name,
                "verifier_key": CosignerKey(name, signer.public_key_raw).verifier(),
                "timestamp": timestamp,
                "message": cosignature_message(checkpoint, timestamp).decode(),
                "line": cosign(checkpoint, signer, name, timestamp),
            }
        )
    return {
        "description": "C2SP cosignature/v1 lines over checkpoint-v1. Keys are TEST ONLY.",
        "checkpoint_note": note,
        "witnesses": witnesses,
        "cosigned_note": add_cosignatures(note, [w["line"] for w in witnesses]),
    }


def build_delegation() -> dict[str, Any]:
    """A delegation credential (D88): its signed bytes, hash, envelope and a revocation."""
    from gate.delegation import (
        Revocation,
        credential_hash,
        credential_payload,
        parse_credential,
        sign_credential,
        sign_revocation,
    )

    principal = seed("principal-1")
    credential = parse_credential({
        "credential_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0", "principal_id": "treasury.harbor-federal",
        "agent_id": "payments-ops", "action_classes": ["data.read", "value.transfer"],
        "max_amount": "5000.00", "allowed_payees": ["acme-paper", "keystone-it"],
        "not_before": "2026-10-29T20:14:03.112Z", "expires_at": "2026-11-28T21:14:03.112Z",
    })  # fmt: skip
    digest = credential_hash(credential)
    revocation = Revocation(credential.credential_id, digest, credential.principal_id,
                            "2026-10-30T09:00:00.000Z")  # fmt: skip
    return {
        "warning": "TEST ONLY key (tests/keys/test-keys.json)",
        "signer": "principal-1",
        "credential": credential.to_json(),
        "signing_payload_hex": credential_payload(credential).hex(),
        "credential_hash": digest,
        "signed": sign_credential(principal, credential).to_json(),
        "revocation": sign_revocation(principal, revocation).to_json(),
    }


def write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    write(OUT, build())
    write(CHECKPOINT_OUT, build_checkpoint())
    write(COSIGNATURE_OUT, build_cosignature())
    write(DELEGATION_OUT, build_delegation())
