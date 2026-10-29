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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

MOCK_MODEL = "mock/scripted"
# Drafting: the seed rules, written by hand from the demo policy, stand in for Muse Wonder.
SEED_RULES = Path(__file__).resolve().parent.parent / "deploy" / "policies" / "harbor-federal.yaml"
_CLAUSE_LINE = re.compile(r"^\[([^\]]+)\] \(page \d+\) (.*)$")
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


def _draft_conditions(conditions: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for field, test in conditions.items():
        pairs = test.items() if isinstance(test, dict) else [("eq", test)]
        out += [{"field": field, "op": op, "value": value} for op, value in pairs]
    return out


def _draft(user: str) -> dict[str, Any]:
    """Each seed rule whose clause is in the document and whose quote is in that clause."""
    clauses = {}
    for line in _after(user, "Clauses:\n").splitlines():
        if match := _CLAUSE_LINE.match(line):
            clauses[match.group(1)] = match.group(2)
    rules = []
    for seed in yaml.safe_load(SEED_RULES.read_text("utf-8")):
        source = seed["source"]
        text = clauses.get(str(source["clause"]))
        if text is None or " ".join(source["quote"].split()) not in text:
            continue
        when = {k: v for k, v in seed["when"].items() if k != "action"}
        tests = [
            {
                "facts": [
                    {"field": k, "value": v} for k, v in t.items() if k not in ("expect", "agent")
                ],
                "agent": t.get("agent"),
                "expect": t["expect"],
            }
            for t in seed["tests"]
        ]
        rules.append({
            "id": seed["id"], "clause": str(source["clause"]), "quote": source["quote"],
            "agent": seed.get("agent"), "action": seed["when"]["action"],
            "when": _draft_conditions(when), "unless": _draft_conditions(seed.get("unless") or {}),
            "then": seed["then"], "approver_role": seed.get("approver_role"), "tests": tests,
        })  # fmt: skip
    return {"rules": rules}


AP_EMAIL = "accounts-payable@harborfederal.example"  # as in assist/prompts/agent_step.md
_PAY_TASK = re.compile(rf"\bpay (.+?) {_AMOUNT}", re.IGNORECASE)


def _agent_step(user: str) -> dict[str, Any]:
    """payments-ops: look the vendor up, pay it once, tell accounts payable, then finish."""
    task = _after(user, "Task:\n").split("\n\nSteps so far:", 1)[0].strip()
    done = {s["tool"]: s for s in json.loads(_after(user, "Steps so far:\n") or "[]")}

    def step(tool: str, note: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "note": note,
            "tool": tool,
            "lookup_vendor": None,
            "create_payment": None,
            "send_email": None,
            "reply": "",
        }
        out[tool if args is not None else "reply"] = args if args is not None else note
        return out

    pay = _PAY_TASK.search(task)
    if pay is None:
        return step("finish", "I can only pay vendors; the task does not name a vendor and amount.")
    name, cents = pay.group(1).strip(), _cents(pay.group(2))
    invoice = re.search(r"\bINV-[A-Za-z0-9-]*[A-Za-z0-9]", task)
    memo = f"Invoice {invoice.group(0)}" if invoice else ""
    if "lookup_vendor" not in done:
        return step("lookup_vendor", f"Find {name} in the vendor directory.", {"query": name})
    lookup = done["lookup_vendor"]
    vendors = (lookup.get("result") or {}).get("vendors", [])
    if lookup["outcome"] != "ok" or len(vendors) != 1:
        why = lookup.get("code") or f"{len(vendors)} vendors matched {name!r}"
        return step("finish", f"I did not pay anyone: the vendor lookup failed ({why}).")
    vendor = vendors[0]
    if "create_payment" not in done:
        args = {
            "payee_id": vendor["id"],
            "amount": f"{cents // 100}.{cents % 100:02d}",
            "currency": "USD",
            "memo": memo,
        }
        return step("create_payment", f"Pay {vendor['name']} {_dollars(cents)}.", args)
    payment = done["create_payment"]
    if payment["outcome"] != "ok":
        return step(
            "finish",
            f"The payment of {_dollars(cents)} to {vendor['name']} was not"
            f" made: {payment['outcome']} ({payment.get('code')}).",
        )
    paid = payment["result"]["payment_id"]
    if "send_email" not in done:
        body = (
            f"Paid {_dollars(cents)} to {vendor['name']} ({vendor['id']}). {memo}. Payment {paid}."
        )
        args = {"to": [AP_EMAIL], "subject": f"Payment sent: {vendor['name']}", "body": body}
        return step("send_email", "Tell accounts payable.", args)
    told = (
        "and told accounts payable"
        if done["send_email"]["outcome"] == "ok"
        else ("but could not email accounts payable")
    )
    return step(
        "finish",
        f"Paid {_dollars(cents)} to {vendor['name']} (payment {paid},"
        f" {memo or 'no invoice given'}) {told}.",
    )


_RULE_PREFIX = {"value.transfer": "pay", "comm.external": "email", "data.read": "read"}
_DEFAULT_FACTS = {"amount": 100, "payee_new": False, "local_hour": 10, "local_weekday": 2}


def _holds(value: Any, op: str, wanted: Any) -> bool:
    if op in ("eq", "ne"):
        return (str(value) == str(wanted)) == (op == "eq")
    a, b = float(str(value)), float(str(wanted))
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _all_hold(facts: Mapping[str, Any], conditions: list[dict[str, Any]]) -> bool:
    return all(
        c["field"] in facts and _holds(facts[c["field"]], c["op"], c["value"]) for c in conditions
    )


def _in_scope(
    fixture: Mapping[str, Any], when: list[dict[str, Any]], unless: list[dict[str, Any]]
) -> bool:
    facts = {**_DEFAULT_FACTS, **fixture}
    return _all_hold(facts, when) and not (unless and _all_hold(facts, unless))


def _fixture(facts: Mapping[str, Any], expect: str, agent: Any = None) -> dict[str, Any]:
    pairs = [{"field": k, "value": v} for k, v in facts.items()]
    return {"facts": pairs, "agent": agent, "expect": expect}


def _learned_rule(
    active: Mapping[str, Any],
    unless: list[dict[str, Any]],
    moved: Mapping[str, Any],
    when: list[dict[str, Any]],
    scope_unless: list[dict[str, Any]],
    then: str,
) -> dict[str, Any]:
    """An active rule as a replacement: extra unless conditions, and every fixture the change
    would affect (inside the scope, expecting another outcome) moved just outside it."""
    tests = []
    for t in active["tests"]:
        facts = {k: v for k, v in t.items() if k not in ("expect", "agent")}
        if t["expect"] != then and _in_scope(facts, when, scope_unless):
            facts = {**facts, **moved}
        tests.append(_fixture(facts, t["expect"], t.get("agent")))
    return {
        "id": active["id"], "agent": active.get("agent"), "action": active["when"]["action"],
        "when": _draft_conditions({k: v for k, v in active["when"].items() if k != "action"}),
        "unless": _draft_conditions(active.get("unless") or {}) + unless,
        "then": active["then"], "approver_role": active.get("approver_role"), "tests": tests,
    }  # fmt: skip


def _learn_claim(row: Mapping[str, Any]) -> dict[str, Any]:
    decision, verdict = row["decision_record"], row["verdict_record"]
    fields = ["body.decision", "body.resource.amount_cents", "body.resource.payee_id"]
    check = [{"record_id": record["record_id"], "field": field, "op": "eq", "value": record[field]}
             for record, names in ((decision, fields), (verdict, ["body.verdict", "body.approver"]))
             for field in names if field in record]  # fmt: skip
    subject = "A request"
    if "body.resource.amount_cents" in decision:
        subject = (f"A payment of {_dollars(decision['body.resource.amount_cents'])} "
                   f"to {decision['body.resource.payee_id']}")  # fmt: skip
    text = f"{subject} was {_PAST[verdict['body.verdict']]} by {verdict.get('body.approver')}."
    return {"text": text, "record_ids": [decision["record_id"], verdict["record_id"]],
            "assertions": check}  # fmt: skip


def _learner_proposal(user: str) -> dict[str, Any]:
    """Loosen: an allow rule for the scope, and the scope as an exception to every escalate
    rule that held these requests. Tighten: a deny rule for the scope."""
    data = json.loads(_after(user, "Pattern:\n"))
    scope, counts, loosen = data["scope"], data["counts"], data["direction"] == "loosen"
    when, unless = _draft_conditions(scope["when"]), _draft_conditions(scope["unless"])
    after_hours = bool(unless)
    inside = {c["field"]: c["value"] for c in when if c["op"] in ("eq", "lte")}
    if after_hours:
        inside["local_hour"] = 20
    outside = {**inside, "local_hour": 10} if after_hours else {**inside, "local_hour": 18}
    moved = {"local_hour": 10, "local_weekday": 2} if after_hours else {"local_hour": 18}
    who = {True: "new", False: "known"}.get(scope["when"].get("payee_new"), "all")
    then = "allow" if loosen else "deny"
    hours = "after-hours" if after_hours else "business"
    new_id = f"{_RULE_PREFIX[scope['action']]}-{who}-{hours}-{then}-01"
    rules = [{
        "id": new_id, "agent": scope["agent"], "action": scope["action"], "when": when,
        "unless": unless, "then": then, "approver_role": None,
        "tests": [_fixture(inside, then), _fixture(outside, "escalate")],
    }]  # fmt: skip
    for active in data["active_rules"]:
        holds = loosen and active["then"] == "escalate" and active["id"] in data["policies_matched"]
        touched = any(
            t["expect"] != then
            and _in_scope(
                {k: v for k, v in t.items() if k not in ("expect", "agent")}, when, unless
            )
            for t in active["tests"]
        )
        if holds or touched:
            rules.append(_learned_rule(active, when if holds else [], moved, when, unless, then))
    claims = [_learn_claim(row) for row in data["rows"][:3]]
    if loosen:
        title = "Allow routine requests in this pattern without approval"
        reason = (f"All {counts['decisions']} reviewed escalations in this scope were approved"
                  f" ({counts['fast_dropped']} approvals made too quickly to be real reviews were"
                  " set aside). The change lets these requests run without approval; everything"
                  " outside the scope is still held.")  # fmt: skip
        stated = ["decisions", "approved", "fast_dropped"]
    else:
        title = "Deny requests in this pattern instead of asking a supervisor"
        reason = (f"{counts['rejected']} of {counts['decisions']} requests in this scope were"
                  " rejected by a supervisor. The change denies them outright; the same requests"
                  " in business hours are still held for approval.")  # fmt: skip
        stated = ["decisions", "rejected"]
    restated = [{"name": n, "value": counts[n]} for n in stated]
    return {"title": title, "rules": rules, "reason": reason, "counts": restated, "claims": claims}


_SCRIPTS: dict[str, Callable[[str], dict[str, Any]]] = {
    "LearnerProposal": _learner_proposal,
    "AgentStep": _agent_step,
    "Brief": _brief,
    "RecordQuery": _query,
    "AuditorAnswer": _answer,
    "DraftRules": _draft,
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
