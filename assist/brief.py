"""Approval briefs: Muse Spark explains what is unusual about an escalated call (demo beat 2).

Code builds the context from the decision record and the ledger's history: the payee's past
decisions and approvals, the agent's payments in the last 30 days, and whether it is outside
business hours. The model only writes prose about that context. Its answer is rejected (one
retry, then no brief) if it cites a record ID that is not in the context, cites none for a
point, uses a number the context does not contain, or recommends a verdict. The brief never
decides: Gate's decision is already recorded, and only a person closes the escalation.
"""

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.llm import LlmClient
from claims.query import BUSINESS_HOURS
from common.clock import parse_ts
from common.ids import ULID_PATTERN
from gate.escalations import Escalation
from gate.records import DECISION, VERDICT, Receipted

PROMPT_FILE = Path(__file__).parent / "prompts" / "approval_brief.md"
HISTORY_DAYS = 30
MAX_CITED = 8  # record ids listed per history group
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ULID_IN_TEXT = re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b")
_VERDICT_WORDS = re.compile(
    r"\b(recommend\w*|should (?:be )?(?:approved?|rejected?)|safe to approve|advise)\b", re.I
)

Text = Annotated[str, StringConstraints(min_length=1, max_length=280)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BriefPoint(_Strict):
    text: Text
    record_ids: Annotated[list[str], Field(max_length=5)]


class Brief(_Strict):
    headline: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    unusual: Annotated[list[BriefPoint], Field(min_length=1, max_length=4)]
    approver_checks: Annotated[list[Text], Field(max_length=3)]


class HistorySource(Protocol):
    """A stream's records as stored (``ledger.store.Store.read_raw``)."""

    async def read_raw(self, stream: str) -> list[dict[str, Any]]: ...


def dollars(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


@dataclass(frozen=True)
class BriefContext:
    """Everything the model may say, built by code. ``as_json`` is what the model sees."""

    data: dict[str, Any]
    record_ids: frozenset[str] = field(default_factory=frozenset)

    def as_json(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True)  # model input, never hashed

    def numbers(self) -> frozenset[Decimal]:
        return frozenset(_numbers_in(_ULID_IN_TEXT.sub(" ", self.as_json())))


def _numbers_in(text: str) -> set[Decimal]:
    found = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")).normalize())
        except InvalidOperation:  # pragma: no cover  # the pattern only matches decimals
            continue
    return found


def _payments(
    history: list[dict[str, Any]], agent: str, before: str, exclude: str
) -> list[dict[str, Any]]:
    since = parse_ts(before) - timedelta(days=HISTORY_DAYS)
    return [
        r for r in history
        if r.get("type") == DECISION and r["record_id"] != exclude
        and r["body"].get("agent") == agent and r["body"].get("action") == "value.transfer"
        and since <= parse_ts(r["ts"]) <= parse_ts(before)
    ]  # fmt: skip


def build_context(
    escalation: Escalation, decision: Receipted, history: list[dict[str, Any]]
) -> BriefContext:
    body, facts = decision.body, decision.body.get("resource", {})
    payments = _payments(history, escalation.agent, decision.submission.ts, decision.record_id)
    payee_id = facts.get("payee_id")
    to_payee = [r for r in payments if r["body"]["resource"].get("payee_id") == payee_id]
    approved = {
        r["body"]["decision_record_id"]: r["record_id"]
        for r in history
        if r.get("type") == VERDICT and r["body"].get("verdict") == "approve"
    }
    payee_approvals = [approved[r["record_id"]] for r in to_payee if r["record_id"] in approved]
    hour, weekday = facts.get("local_hour"), facts.get("local_weekday")
    data: dict[str, Any] = {
        "decision": {
            "record_id": decision.record_id,
            "agent": escalation.agent,
            "tool": escalation.tool,
            "action": escalation.action,
            "policies_matched": list(body.get("policy_ids", [])),
            "approver_roles": list(escalation.approver_roles),
            "expires_at": body["escalation"]["expires_at"],
        },
        "facts": {k: v for k, v in facts.items() if k != "amount_cents"},
        "after_business_hours": hour is not None and (weekday > 5 or hour not in BUSINESS_HOURS),
    }
    if "amount_cents" in facts:
        data["facts"]["amount"] = dollars(facts["amount_cents"])
        amounts = [(r["body"]["resource"]["amount_cents"], r["record_id"]) for r in payments]
        largest = max(amounts, default=None)
        data["agent_payments_last_30_days"] = {
            "count": len(amounts),
            "median_amount": dollars(int(statistics.median(a for a, _ in amounts)))
            if amounts
            else None,
            "largest_amount": dollars(largest[0]) if largest else None,
            "largest_record_id": largest[1] if largest else None,
            "record_ids": [r for _, r in sorted(amounts, reverse=True)[:MAX_CITED]],
        }
        data["payee_history"] = {
            "earlier_decisions": len(to_payee),
            "earlier_approvals": len(payee_approvals),
            "record_ids": [r["record_id"] for r in to_payee][-MAX_CITED:]
            + payee_approvals[-MAX_CITED:],
        }
    return BriefContext(data, frozenset(_ids_in(data)))


def _ids_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if ULID_PATTERN.fullmatch(value) else set()
    if isinstance(value, dict):
        return set().union(*(_ids_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_ids_in(v) for v in value)) if value else set()
    return set()


def check_brief(brief: Brief, context: BriefContext) -> list[str]:
    """Problems that make a brief unusable; empty means it may be shown."""
    problems = []
    allowed_numbers = context.numbers()
    texts = [brief.headline, *(p.text for p in brief.unusual), *brief.approver_checks]
    for point in brief.unusual:
        if not point.record_ids:
            problems.append(f"point {point.text[:40]!r} cites no record")
        unknown = sorted(set(point.record_ids) - context.record_ids)
        if unknown:
            problems.append(f"record ids not in the context: {unknown}")
    for text in texts:
        if _ULID_IN_TEXT.search(text):
            problems.append("record ids belong in record_ids, not in the text")
        stray = sorted(str(n) for n in _numbers_in(text) - allowed_numbers)
        if stray:
            problems.append(f"numbers not in the context: {stray}")
        if _VERDICT_WORDS.search(text):
            problems.append("the brief must not recommend a verdict")
    return problems


def load_prompt(path: Path = PROMPT_FILE) -> tuple[str, str]:
    """(version, instructions) from a prompt file with a front-matter header (skill §5)."""
    text = path.read_text("utf-8")
    _, header, body = text.split("---\n", 2)
    fields = dict(line.split(": ", 1) for line in header.strip().splitlines())
    return fields["version"], body.strip()


class BriefWriter:
    """Gate's ``BriefWriter`` port, backed by Muse Spark through ``assist.llm``."""

    def __init__(
        self, llm: LlmClient, history: HistorySource, prompt_file: Path = PROMPT_FILE
    ) -> None:
        self._llm = llm
        self._history = history
        self.prompt_version, self._instructions = load_prompt(prompt_file)

    async def write(self, escalation: Escalation, decision: Receipted) -> dict[str, Any]:
        history = await self._history.read_raw(decision.submission.stream)
        context = build_context(escalation, decision, history)
        messages = [
            {"role": "system", "content": self._instructions},
            {"role": "user", "content": "Context:\n" + context.as_json()},
        ]
        brief = await self._llm.complete_structured(
            "spark", Brief, messages, prompt_version=self.prompt_version,
            check=lambda b: check_brief(b, context),
        )  # fmt: skip
        return {
            "escalation_id": escalation.id,
            "decision_record_id": decision.record_id,
            "model": self._llm.config.route("spark").model,
            "prompt_version": self.prompt_version,
            **brief.model_dump(),
            "context_record_ids": sorted(context.record_ids),
        }
