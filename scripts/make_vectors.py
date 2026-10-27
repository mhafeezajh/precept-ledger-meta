"""Generate tests/vectors/ledger-v1.json from fixed inputs and the TEST ONLY keys.

Run with ``make vectors``. Output is deterministic (Ed25519 signatures are deterministic), so a
diff after running it means the format changed, which needs a DECISIONS.md entry (skill §4).
"""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.chain import GENESIS, link  # noqa: E402  # path set up above for a plain script
from ledger.format import b64encode, canonical_bytes  # noqa: E402
from ledger.receipts import issue_receipt  # noqa: E402
from ledger.signing import Signer, sign_submission  # noqa: E402

OUT = ROOT / "tests" / "vectors" / "ledger-v1.json"
STREAM = "harbor-federal/decisions"

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


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
