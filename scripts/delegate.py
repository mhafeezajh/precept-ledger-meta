"""Delegate to the payments agent, or take the delegation back (D88, D89).

As the principal ``treasury.harbor-federal`` (its key from ``make dev-keys``), sign a
delegation credential for ``payments-ops``: every action class, payments up to the policy's
own cap ($250,000), only to the vendors in ``deploy/demo/vendors.json``, valid 30 days. The
signed credential is saved to ``KEYS_DIR/credentials/payments-ops.json``, where the agent
picks it up, and a compliance officer (``a.chen``) submits it to Gate, which records it as
``gate.delegation.issue``. ``revoke`` signs a revocation of the saved credential and submits it;
``status`` shows what Gate holds.

Run against ``make gate`` (``GATE_REQUIRE_DELEGATION=true`` to enforce it):
``make delegate`` (``ARGS=revoke`` or ``ARGS=status``).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.bank import load_vendors  # noqa: E402  # path set up above for a plain script
from common.clock import SystemClock, format_ts  # noqa: E402
from common.config import load_settings  # noqa: E402
from common.ids import new_ulid  # noqa: E402
from gate.delegation import (  # noqa: E402
    ACTION_CLASSES,
    Credential,
    Revocation,
    credential_hash,
    parse_credential,
    parse_signed_credential,
    sign_credential,
    sign_revocation,
)
from ledger.signing import Signer  # noqa: E402

GATE_URL = "http://127.0.0.1:8070"
PRINCIPAL = "treasury.harbor-federal"
AGENT = "payments-ops"
OFFICER = "a.chen"  # submits the principal's signed credential; cannot sign one
MAX_AMOUNT = "250000.00"  # clause 3.5's cap: the delegation never exceeds the policy
LIFETIME = timedelta(days=30)
VENDORS = ROOT / "deploy" / "demo" / "vendors.json"


def vendor_ids(path: Path = VENDORS) -> list[str]:
    return sorted(v.id for v in load_vendors(path))


def build_credential(now: datetime, credential_id: str, payees: list[str]) -> Credential:
    return parse_credential({
        "credential_id": credential_id, "principal_id": PRINCIPAL, "agent_id": AGENT,
        "action_classes": sorted(ACTION_CLASSES), "max_amount": MAX_AMOUNT,
        "allowed_payees": payees, "not_before": format_ts(now - timedelta(minutes=1)),
        "expires_at": format_ts(now + LIFETIME),
    })  # fmt: skip


def credential_file(keys_dir: Path, agent: str = AGENT) -> Path:
    return keys_dir / "credentials" / f"{agent}.json"


def save(path: Path, envelope: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(json.dumps(envelope, indent=2) + "\n")


def _post(path: str, body: dict[str, Any], token: str) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"}
    return httpx.post(GATE_URL + path, json=body, headers=headers, timeout=15)


def issue(keys_dir: Path, token: str) -> int:
    principal = Signer.from_pem_file(keys_dir / "principals" / f"{PRINCIPAL}.pem")
    clock = SystemClock()
    credential = build_credential(clock.now(), new_ulid(clock), vendor_ids())
    envelope = sign_credential(principal, credential).to_json()
    response = _post("/gate/v1/delegations", envelope, token)
    if response.status_code != 201:
        print(f"Gate refused the credential: {response.json().get('error')}")
        return 1
    save(credential_file(keys_dir), envelope)
    record = response.json()["record"]
    print(f"{PRINCIPAL} delegated to {AGENT}: credential {credential.credential_id}")
    print(f"  {', '.join(credential.action_classes)}; up to ${credential.max_amount}")
    print(f"  payees: {', '.join(credential.allowed_payees)}")
    print(f"  valid until {credential.expires_at}; hash {credential_hash(credential)[:19]}…")
    print(f"  recorded as {record['type']} {record['record_id']}")
    print(f"  saved for the agent: {credential_file(keys_dir)}")
    return 0


def revoke(keys_dir: Path, token: str) -> int:
    path = credential_file(keys_dir)
    if not path.exists():
        print(f"no saved credential at {path}")
        return 1
    signed = parse_signed_credential(json.loads(path.read_text()))
    principal = Signer.from_pem_file(keys_dir / "principals" / f"{PRINCIPAL}.pem")
    c = signed.credential
    revocation = Revocation(c.credential_id, signed.hash, c.principal_id,
                            format_ts(SystemClock().now()))  # fmt: skip
    response = _post("/gate/v1/delegations/revocations",
                     sign_revocation(principal, revocation).to_json(), token)  # fmt: skip
    if response.status_code != 201:
        print(f"Gate refused the revocation: {response.json().get('error')}")
        return 1
    record = response.json()["record"]
    print(
        f"{PRINCIPAL} revoked credential {c.credential_id}: {record['type']} {record['record_id']}"
    )
    print(f"  every call {AGENT} makes with it is now denied (delegation.revoked)")
    return 0


def status(keys_dir: Path, token: str) -> int:
    path = credential_file(keys_dir)
    if not path.exists():
        print(f"no saved credential at {path}")
        return 1
    c = parse_signed_credential(json.loads(path.read_text())).credential
    response = httpx.get(f"{GATE_URL}/gate/v1/delegations/{c.credential_id}",
                         headers={"authorization": f"Bearer {token}"}, timeout=15)  # fmt: skip
    if response.status_code != 200:
        print(f"Gate does not hold credential {c.credential_id}: {response.json().get('error')}")
        return 1
    print(f"credential {c.credential_id}: {response.json()['state']}, until {c.expires_at}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = {"issue": issue, "revoke": revoke, "status": status}
    parser.add_argument("command", nargs="?", default="issue", choices=sorted(commands))
    args = parser.parse_args(argv)
    keys_dir = load_settings().keys_dir
    token = (keys_dir / "tokens" / f"{OFFICER}.token").read_text().strip()
    return commands[args.command](keys_dir, token)


if __name__ == "__main__":
    sys.exit(main())
