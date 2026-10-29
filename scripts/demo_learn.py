"""demo beat 6 against a running pl-gate: the ledger learns, and people decide.

Needs ``make serve`` (with witnesses) holding a seeded week (``make seed-week``), and
``make gate`` with an approved policy active (``make demo-policy``) and a model profile
(``MODEL_PROFILE=mock`` or ``cloud``). The compliance officers are ``a.chen`` and ``r.patel``
(``make dev-keys``). The learner counts patterns with SQL over the verified export, drafts a
proposal per candidate, checks its counts and claims, and replays the week under it. A
loosening then needs both officers, a tightening one; each approved proposal becomes a policy
draft that ``a.chen`` approves like any policy, and only then does Gate enforce it.
Run: ``make demo-learn`` (``ARGS="--inject-fault"`` inflates a count to show it rejected,
``ARGS="--no-approve"`` stops after the proposals; the fault needs ``DEMO_MODE=true``).
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.config import load_settings  # noqa: E402  # path set up above for a plain script
from policy.rules import parse_rules  # noqa: E402

GATE_URL = "http://127.0.0.1:8070"
OFFICERS = ("a.chen", "r.patel")
REPLAY = (
    ("escalations_removed", "escalations no longer needed"),
    ("new_denials", "requests now denied"),
    ("new_escalations", "requests now held"),
    ("rejections_newly_allowed", "past rejections now allowed"),
    ("approvals_now_denied", "past approvals now denied"),
    ("outside_scope", "changes outside the pattern"),
)


def show(index: int, proposal: dict[str, Any]) -> None:
    record = proposal["record"]
    body = record["body"]
    mark = {"review": "ready for review", "rejected": "REJECTED", "blocked": "BLOCKED"}
    print(f"\n[{index}] {body['direction'].upper()}: {body['title']}  ({mark[body['state']]})")
    print(f"    proposal {record['record_id']}, drafted by {body['model']}")
    pattern = body["pattern"]
    print(f"    pattern: {pattern['statement']}")
    counts, fatigue = pattern["counts"], pattern["fatigue"]
    print(f"    SQL: {counts['decisions']} decisions, {counts['approved']} approved,"
          f" {counts['rejected']} rejected; fatigue filter set aside"
          f" {len(fatigue['dropped_decision_record_ids'])} approvals"
          f" (under {fatigue['fast_seconds']} s or a 100% approver)")  # fmt: skip
    stated = ", ".join(f"{k} {v}" for k, v in body["stated_counts"].items())
    note = "  <- injected fault" if body["injected"] else ""
    print(f"    model restated: {stated}{note}")
    print(f"    reason: {body['reason']}")
    for claim in body["claims"]:
        print(f"    {'✅' if claim['accepted'] else '❌'} {claim['text']}")
    if body["rules_yaml"]:
        for rule in parse_rules(body["rules_yaml"]):
            print(f"    rule {rule.id:30} {rule.then:9} (source: {rule.source.clause})")
    replay = body["replay"]
    if replay is not None:
        found = [f"{replay[k]} {label}" for k, label in REPLAY if replay[k]]
        summary = "; ".join(found) or "no change"
        print(f"    replay of {replay['replayed']} past decisions: {summary}")
    for reason in body["reasons"]:
        print(f"    ! {reason}")


async def approve_policy_draft(http: httpx.AsyncClient, draft: dict[str, Any], token: str) -> None:
    draft_id = draft["record"]["record_id"]
    findings = draft["record"]["body"]["findings"]
    print(f"    policy draft {draft_id}: {len(findings)} findings")
    approved = await http.post(f"/policy/v1/drafts/{draft_id}/approve", headers=_auth(token))
    if approved.status_code != 201:
        print(f"    policy approval refused: {approved.json()['error']}")
        return
    health = (await http.get("/gate/v1/health")).json()
    print(f"    {OFFICERS[0]} approves it as policy: Gate now enforces"
          f" {health['policy_set'][:19]}… ({health['rules']} rules)")  # fmt: skip


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def review(http: httpx.AsyncClient, proposal: dict[str, Any], tokens: dict[str, str]) -> None:
    record = proposal["record"]
    body, path = record["body"], f"/learn/v1/proposals/{record['record_id']}/approve"
    print(f"\nReview [{body['direction']}]: needs {body['approvals_needed']} approver(s)")
    draft = None
    for name in OFFICERS[: body["approvals_needed"]]:
        response = await http.post(path, headers=_auth(tokens[name]))
        if response.status_code != 201:
            print(f"    {name}: refused {response.json()['error']['code']}")
            return
        approval = response.json()["approval"]["record"]["body"]
        print(f"    {name} approves ({approval['approval']} of {approval['approvals_needed']})")
        draft = response.json()["policy_draft"]
        if draft is None and body["approvals_needed"] > 1:
            again = await http.post(path, headers=_auth(tokens[name]))
            print(f"    {name} again: {again.status_code} {again.json()['error']['code']}")
    if draft is not None:
        await approve_policy_draft(http, draft, tokens[OFFICERS[0]])


async def run(inject_fault: bool, approve: bool) -> int:
    settings = load_settings()
    tokens = {
        name: (settings.keys_dir / "tokens" / f"{name}.token").read_text().strip()
        for name in OFFICERS
    }
    async with httpx.AsyncClient(base_url=GATE_URL, timeout=300) as http:
        params = {"window": "7d", "inject_fault": str(inject_fault).lower()}
        response = await http.post("/learn/v1/run", params=params, headers=_auth(tokens["a.chen"]))
        if response.status_code != 201:
            print(f"learner refused: {response.json()['error']}")
            return 1
        result = response.json()
        log = result["log"]
        root = log["root"][:12]
        print(f"The learner verified the export: {log['size']} records under the final"
              f" checkpoint (root {root}…); {log['newer']} newer not counted.")  # fmt: skip
        for skipped in result["skipped"]:
            print(f"  skipped {skipped['key']}: {skipped['reason']}")
        for failed in result["failed"]:
            print(f"  no proposal for {failed['key']}: {failed['code']}")
        proposals = result["proposals"]
        for index, proposal in enumerate(proposals, start=1):
            show(index, proposal)
        if approve:
            for proposal in proposals:
                if proposal["record"]["body"]["state"] == "review":
                    await review(http, proposal, tokens)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject-fault", action="store_true", help="inflate a restated count")
    parser.add_argument("--no-approve", action="store_true", help="stop after the proposals")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.inject_fault, not args.no_approve))


if __name__ == "__main__":
    sys.exit(main())
