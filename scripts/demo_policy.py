"""Demo beat 1 against a running pl-gate: policy PDF to rules, a bad rule caught, fixed, approved.

Needs ``make serve`` and ``make gate`` with ``DEMO_MODE=true`` (for the planted fault), and a
drafting model: ``MODEL_PROFILE=mock`` (scripted) or ``cloud`` (Muse Wonder). The compliance
officer is ``a.chen`` (``make dev-keys``). Each step is a ledger record signed by Gate.
Run: ``make demo-policy`` (``ARGS="--no-fault"`` drafts without the planted fault).
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.config import load_settings  # noqa: E402  # path set up above for a plain script
from policy.rules import parse_rules, to_yaml  # noqa: E402

GATE_URL = "http://127.0.0.1:8070"
PDF = ROOT / "deploy" / "demo" / "payments_policy.pdf"
OFFICER = "a.chen"


def show(draft: dict[str, Any]) -> None:
    body = draft["record"]["body"]
    print(f"  draft {draft['record']['record_id']} by {body['model'] or body['author']}")
    for rule in parse_rules(body["rules_yaml"]):
        mark = "  <- planted fault" if rule.id == body.get("injected") else ""
        print(
            f"    {rule.id:20} clause {rule.source.clause} p.{rule.source.page}  {rule.then}{mark}"
        )
    for finding in body["findings"]:
        print(f"  ! {finding['rule_id']}: {finding['check']}: {finding['detail']}")
    if not body["findings"]:
        print("  no findings")


def officer_fix(yaml_text: str, rule_id: str) -> str:
    """What the officer does in the review screen: put the clause's $5,000 back."""
    rules = []
    for rule in parse_rules(yaml_text):
        if rule.id == rule_id:
            when = tuple(
                replace(c, value=c.value / 10) if c.field == "amount" else c for c in rule.when
            )
            rule = replace(rule, when=when)
        rules.append(rule)
    return to_yaml(tuple(rules))


async def run(inject_fault: bool) -> int:
    settings = load_settings()
    token = (settings.keys_dir / "tokens" / f"{OFFICER}.token").read_text().strip()
    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=GATE_URL, headers=headers, timeout=120) as http:
        before = (await http.get("/policy/v1/active")).json()
        print(f"1. Gate's active policy set: {before['policy_set'][:19]}…")
        print(f"2. {OFFICER} uploads {PDF.name}; the drafter writes rules, code checks them:")
        params = {"name": PDF.name, "inject_fault": str(inject_fault).lower()}
        response = await http.post("/policy/v1/drafts", params=params, content=PDF.read_bytes())
        if response.status_code != 201:
            print(f"  refused: {response.json()['error']}")
            return 1
        draft = response.json()
        show(draft)
        draft_id, body = draft["record"]["record_id"], draft["record"]["body"]
        if body["findings"]:
            refused = await http.post(f"/policy/v1/drafts/{draft_id}/approve")
            print(f"3. approve as drafted: {refused.status_code} {refused.json()['error']['code']}")
            fixed = officer_fix(body["rules_yaml"], body["injected"] or "")
            response = await http.post(f"/policy/v1/drafts/{draft_id}/revisions", content=fixed)
            draft = response.json()
            print(f"4. {OFFICER} fixes the rule; the checks run again:")
            show(draft)
            draft_id = draft["record"]["record_id"]
        approved = await http.post(f"/policy/v1/drafts/{draft_id}/approve")
        if approved.status_code != 201:
            print(f"  approval refused: {approved.json()['error']}")
            return 1
        record = approved.json()["record"]
        print(f"5. approved by {record['body']['approver']}: policy.approve {record['record_id']}")
        after = (await http.get("/gate/v1/health")).json()
        print(f"6. Gate now decides with {after['policy_set'][:19]}… ({after['rules']} rules)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fault", action="store_true", help="draft without the planted fault")
    args = parser.parse_args(argv)
    return asyncio.run(run(not args.no_fault))


if __name__ == "__main__":
    sys.exit(main())
