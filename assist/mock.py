"""A scripted stand-in for the model (``MODEL_PROFILE=mock``): development and demos at no cost.

It is not a model and is never presented as one. Its route is ``mock/scripted``, so every
record it feeds (``assist.brief``, ``assist.qa``) and every call-log row names it. It answers
from the same messages a model receives, deterministically, and ``assist.llm`` puts its answers
through the same schema validation and checks, so a feature that works on the mock works on the
same code path as on Muse Spark. Switch to live with ``MODEL_PROFILE=cloud`` (D72).
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

MOCK_MODEL = "mock/scripted"
MAX_CLAIMS = 12  # assist.auditor.AuditorAnswer allows 12
_AMOUNT = r"\$([\d,]+(?:\.\d{2})?)"


def _user_text(messages: Sequence[Mapping[str, str]]) -> str:
    return next((m["content"] for m in messages if m["role"] == "user"), "")


def _after(text: str, marker: str) -> str:
    return text.split(marker, 1)[1] if marker in text else ""


def _dollars(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _cents(amount: str) -> int:
    whole, _, part = amount.replace(",", "").partition(".")
    return int(whole) * 100 + int((part or "0").ljust(2, "0"))


def _brief(user: str) -> dict[str, Any]:
    data = json.loads(_after(user, "Context:\n"))
    decision, facts = data["decision"], data["facts"]
    payee, amount = facts.get("payee_id", "the payee"), facts.get("amount", "the amount")
    roles = " and ".join(decision["approver_roles"])
    cite = [decision["record_id"]]
    points = []
    history = data.get("payee_history") or {}
    if history.get("earlier_decisions", 0) == 0:
        points.append({"text": f"{payee} has no earlier payments or approvals in the ledger.",
                       "record_ids": cite})  # fmt: skip
    else:
        text = (f"{payee} has {history['earlier_decisions']} earlier decisions and"
                f" {history['earlier_approvals']} earlier approvals.")  # fmt: skip
        points.append({"text": text, "record_ids": cite + history["record_ids"][:2]})
    recent = data.get("agent_payments_last_30_days") or {}
    if recent.get("largest_record_id"):
        text = (
            f"The amount is {amount}; the agent's largest cleared payment in 30 days was"
            f" {recent['largest_amount']} and the median {recent['median_amount']}."
        )
        points.append({"text": text, "record_ids": [*cite, recent["largest_record_id"]]})
    if data.get("after_business_hours"):
        points.append({"text": "The request came outside business hours.", "record_ids": cite})
    policies = ", ".join(decision["policies_matched"])
    points.append({"text": f"It matched {policies}.", "record_ids": cite})
    return {
        "headline": f"{decision['agent']} wants to pay {amount} to {payee}; it is held for"
        f" {roles} review.",
        "unusual": points[:4],
        "approver_checks": [
            "Confirm the payee's bank details by phone, using a number already on file.",
            "Check the invoice and the business reason with the requesting team.",
        ],
    }


def _choice(text: str, options: Sequence[tuple[str, str]], default: str) -> str:
    return next((value for pattern, value in options if re.search(pattern, text)), default)


def _since_days(q: str) -> int | None:
    if days := re.search(r"last (\d{1,4}) days", q):
        return int(days.group(1))
    spans = {"today": 1, "this week": 7, "past week": 7, "this month": 30}
    return next((days for phrase, days in spans.items() if phrase in q), None)


def _query(user: str) -> dict[str, Any]:
    q = _after(user, "Question:").lower()
    over = re.search(rf"(?:over|above|more than|exceeding)\s+{_AMOUNT}", q)
    least = re.search(rf"at least\s+{_AMOUNT}", q)
    under = re.search(rf"(?:under|below|less than)\s+{_AMOUNT}", q)
    approver = re.search(r"approved by ([a-z][a-z0-9]*\.[a-z0-9]+)", q)
    payee = re.search(r"\bto ([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b", q)
    return {
        "action": _choice(q, [(r"email", "comm.external"), (r"pay|transfer", "value.transfer"),
                              (r"vendor|lookup|read", "data.read")], "any"),
        "decision": _choice(q, [(r"denied|blocked", "deny"), (r"held|escalat", "escalate"),
                                (r"allowed", "allow")], "any"),
        "verdict": _choice(q, [(r"approved", "approve"), (r"rejected", "reject"),
                               (r"expired", "expire"), (r"pending|waiting", "none")], "any"),
        "min_amount_cents": _cents(over.group(1)) + 1 if over
        else _cents(least.group(1)) if least else None,
        "max_amount_cents": _cents(under.group(1)) - 1 if under else None,
        "outside_business_hours": _choice(
            q, [(r"outside business hours|after[ -]hours", "yes"),
                (r"during business hours", "no")], "any"),
        "approver": approver.group(1) if approver else None,
        "payee_id": payee.group(1) if payee else None,
        "since_days": _since_days(q),
    }  # fmt: skip


_PAST = {"allow": "allowed", "deny": "denied", "escalate": "held for approval",
         "approve": "approved", "reject": "rejected", "expire": "left to expire"}  # fmt: skip


def _claim(row: dict[str, Any]) -> dict[str, Any]:
    decision_id, verdict_id = row["decision_record_id"], row["verdict_record_id"]
    check: list[dict[str, Any]] = []  # amount first: the demo fault changes the first one
    if row["amount_cents"] is not None:
        subject = f"A payment of {_dollars(row['amount_cents'])} to {row['payee_id']}"
        check += [
            {"record_id": decision_id, "field": "body.resource.amount_cents", "op": "eq",
             "value": row["amount_cents"]},
            {"record_id": decision_id, "field": "body.resource.payee_id", "op": "eq",
             "value": row["payee_id"]},
        ]  # fmt: skip
    else:
        subject = f"A {row['tool']} call by {row['agent']}"
        check += [{"record_id": decision_id, "field": f"body.{k}", "op": "eq", "value": row[k]}
                  for k in ("tool", "agent")]  # fmt: skip
    check.append({"record_id": decision_id, "field": "body.decision", "op": "eq",
                  "value": row["decision"]})  # fmt: skip
    if row["outside_business_hours"]:
        subject += " outside business hours"
    if verdict_id is None:
        return {"text": f"{subject} was {_PAST[row['decision']]}.", "record_ids": [decision_id],
                "assertions": check}  # fmt: skip
    check.append({"record_id": verdict_id, "field": "body.verdict", "op": "eq",
                  "value": row["verdict"]})  # fmt: skip
    by = ""
    if row["approver"]:
        by = f" by {row['approver']}"
        check.append({"record_id": verdict_id, "field": "body.approver", "op": "eq",
                      "value": row["approver"]})  # fmt: skip
    return {"text": f"{subject} was {_PAST[row['verdict']]}{by}.",
            "record_ids": [decision_id, verdict_id], "assertions": check[:8]}  # fmt: skip


def _answer(user: str) -> dict[str, Any]:
    rows = json.loads(_after(user, "Rows:\n") or "[]")
    if not rows:
        return {"summary": "No records matched the query.", "claims": []}
    claims = [_claim(row) for row in rows[:MAX_CLAIMS]]
    return {"summary": f"Matching decisions: {len(rows)}. Each one is listed below.",
            "claims": claims}  # fmt: skip


_SCRIPTS: dict[str, Callable[[str], dict[str, Any]]] = {
    "Brief": _brief,
    "RecordQuery": _query,
    "AuditorAnswer": _answer,
}


async def scripted_completion(
    *,
    model: str,
    messages: Sequence[Mapping[str, str]],
    response_format: dict[str, Any],
    **_: Any,
) -> SimpleNamespace:
    """Shaped like a LiteLLM response, so ``LlmClient`` treats it exactly like one."""
    name = response_format["json_schema"]["name"]
    script = _SCRIPTS.get(name)
    if script is None:  # LlmClient turns this into model.refused (R4)
        raise LookupError(f"the mock has no script for {name}")
    content = json.dumps(script(_user_text(messages)))
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)
