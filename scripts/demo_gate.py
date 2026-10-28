"""Demo beat 2 against running servers: Gate holds a $48,000 payment until a supervisor approves.

Needs ``pl-ledger`` (``make serve``) and ``pl-gate`` (``make gate``) running, and the dev
tokens in ``KEYS_DIR/tokens/``. The payment tool here is a stand-in that prints; the LangGraph
agent replaces this script in the agent module. Run: ``make demo-gate`` (add ``--manual`` to
approve yourself with curl instead of as j.ortiz automatically).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.clock import SystemClock, format_ts  # noqa: E402  # path set up above for a plain script
from gate.client import HttpGateClient  # noqa: E402
from gate.governed import GateDenied, Governor  # noqa: E402
from ledger.receipts import LOG_KEY_OWNER  # noqa: E402
from ledger.signing import KeyRing  # noqa: E402
from verify.keys import TrustedKeys  # noqa: E402

GATE_URL = os.environ.get("GATE_URL", "http://127.0.0.1:8070")


def token(keys_dir: Path, name: str) -> str:
    return (keys_dir / "tokens" / f"{name}.token").read_text().strip()


async def create_payment(**arguments: Any) -> dict[str, str]:
    amount, payee = f"{arguments['amount']} {arguments['currency']}", arguments["payee_id"]
    print(f"  >> payment tool ran: {amount} to {payee}")
    return {"payment_id": "pay-demo-0001", "status": "sent"}


async def approve_ours(http: httpx.AsyncClient, keys_dir: Path, since: str) -> None:
    """Approve, as j.ortiz, the payment escalation this run opened (none older)."""
    headers = {"authorization": f"Bearer {token(keys_dir, 'j.ortiz')}"}
    while True:
        queue = (await http.get(f"{GATE_URL}/gate/v1/escalations", headers=headers)).json()
        ours = [e for e in queue["escalations"] if e["opened_at"] >= since]
        if ours:
            break
        await asyncio.sleep(0.2)
    escalation = ours[0]
    print(f"  approval queue: escalation {escalation['id']}, needs {escalation['approver_roles']}")
    await asyncio.sleep(1.0)  # the payment is visibly held before anyone approves
    response = await http.post(
        f"{GATE_URL}/gate/v1/escalations/{escalation['id']}/verdict",
        headers=headers,
        json={"verdict": "approve"},
    )
    response.raise_for_status()
    print(f"  j.ortiz approved: verdict record {response.json()['record']['record_id']}")


async def run(keys_dir: Path, manual: bool) -> int:
    trusted = TrustedKeys.from_json((keys_dir / "trusted.json").read_bytes())
    log_keys = KeyRing((LOG_KEY_OWNER, raw) for raw in trusted.log_keys)
    async with httpx.AsyncClient(timeout=30) as http:
        client = HttpGateClient(GATE_URL, token(keys_dir, "payments-ops"), http)
        governor = Governor(
            client,
            agent="payments-ops",
            gate_keys=KeyRing(trusted.producers.values()),
            log_keys=log_keys,
        )
        pay = governor.tool("create_payment")(create_payment)
        print("1. $120 to a known payee (acme-paper):")
        await pay(payee_id="acme-paper", amount="120.00", currency="USD")
        print("2. $48,000 to a new payee (northwind-supply):")
        since = format_ts(SystemClock().now())
        approver = None if manual else asyncio.create_task(approve_ours(http, keys_dir, since))
        if manual:
            print("  held; approve with POST /gate/v1/escalations/<id>/verdict as a supervisor")
        await pay(payee_id="northwind-supply", amount="48000.00", currency="USD")
        if approver is not None:
            await approver
        print("3. $300,000 (over the cap):")
        try:
            await pay(payee_id="acme-paper", amount="300000.00", currency="USD")
        except GateDenied as exc:
            print(f"  denied before the tool ran: {exc.code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", default=os.environ.get("KEYS_DIR"))
    parser.add_argument("--manual", action="store_true")
    args = parser.parse_args(argv)
    if not args.keys_dir:
        parser.error("set KEYS_DIR or pass --keys-dir")
    return asyncio.run(run(Path(args.keys_dir), args.manual))


if __name__ == "__main__":
    sys.exit(main())
