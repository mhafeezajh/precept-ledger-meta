"""Auditor Q&A with proof-checked answers (demo beat 3, ARCHITECTURE flow C).

1. Muse Spark turns the question into a ``RecordQuery``: filters only, no answers.
2. Code runs the query over the stream's records (``run_query``): decisions joined with their
   verdicts. This is the model's only view of the ledger, and it is read-only.
3. Muse Spark answers with claims: text, cited record ids, and field assertions.
4. ``claims.checker`` fetches proof for every cited record and checks every assertion. Only
   accepted claims reach the answer text; rejected ones are shown with their reasons.
5. The question, the query, every claim and its result are appended as ``assist.qa``, signed
   by ``assist-1``: the audit itself is audited.

``inject_fault`` (demo only) changes one assertion of the first claim before checking, and
the result says so, to show a wrong claim being rejected on demand.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.brief import BUSINESS_HOURS, load_prompt
from assist.evidence import LedgerReader
from assist.llm import LlmClient
from claims.checker import Assertion, Claim, ClaimResult, Trust, check_claims
from common.clock import Clock, format_ts, parse_ts
from common.ids import new_ulid
from gate.ledger_client import LedgerClient
from gate.records import DECISION, VERDICT, Receipted, verify_receipted
from ledger.signing import KeyRing, Signer, sign_submission

PROMPTS = Path(__file__).parent / "prompts"
MAX_ROWS = 50
MAX_QUESTION = 500
QA = "assist.qa"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecordQuery(_Strict):
    action: Literal["value.transfer", "comm.external", "data.read", "any"]
    decision: Literal["allow", "deny", "escalate", "any"]
    verdict: Literal["approve", "reject", "expire", "none", "any"]
    min_amount_cents: Annotated[int, Field(ge=0)] | None
    max_amount_cents: Annotated[int, Field(ge=0)] | None
    outside_business_hours: Literal["yes", "no", "any"]
    approver: Annotated[str, StringConstraints(max_length=64)] | None
    payee_id: Annotated[str, StringConstraints(max_length=64)] | None
    since_days: Annotated[int, Field(ge=1, le=3660)] | None


class AssertionOut(_Strict):
    record_id: str
    field: Annotated[str, StringConstraints(max_length=80)]
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: str | int | bool | None


class ClaimOut(_Strict):
    text: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    record_ids: Annotated[list[str], Field(min_length=1, max_length=6)]
    assertions: Annotated[list[AssertionOut], Field(min_length=1, max_length=8)]


class AuditorAnswer(_Strict):
    summary: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    claims: Annotated[list[ClaimOut], Field(max_length=12)]


class QaRecorder(Protocol):
    async def record(self, body: dict[str, Any]) -> Receipted: ...


def _outside_hours(facts: dict[str, Any]) -> bool | None:
    hour, weekday = facts.get("local_hour"), facts.get("local_weekday")
    if not isinstance(hour, int) or not isinstance(weekday, int):
        return None
    return weekday > 5 or hour not in BUSINESS_HOURS


def build_rows(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per ``gate.decision``, joined with its ``gate.verdict``, newest first."""
    verdicts = {r["body"].get("decision_record_id"): r for r in records if r.get("type") == VERDICT}
    rows = []
    for r in records:
        if r.get("type") != DECISION:
            continue
        body, facts = r["body"], r["body"].get("resource") or {}
        verdict = verdicts.get(r["record_id"])
        rows.append({
            "decision_record_id": r["record_id"],
            "ts": r["ts"],
            "agent": body.get("agent"),
            "tool": body.get("tool"),
            "action": body.get("action"),
            "decision": body.get("decision"),
            "amount_cents": facts.get("amount_cents"),
            "payee_id": facts.get("payee_id"),
            "payee_new": facts.get("payee_new"),
            "local_hour": facts.get("local_hour"),
            "local_weekday": facts.get("local_weekday"),
            "outside_business_hours": _outside_hours(facts),
            "verdict_record_id": None if verdict is None else verdict["record_id"],
            "verdict": None if verdict is None else verdict["body"].get("verdict"),
            "approver": None if verdict is None else verdict["body"].get("approver"),
        })  # fmt: skip
    return sorted(rows, key=lambda row: row["ts"], reverse=True)


def _matches(row: dict[str, Any], q: RecordQuery, since: datetime | None) -> bool:
    amount = row["amount_cents"]
    wanted_hours = {"yes": True, "no": False}.get(q.outside_business_hours)
    checks = [
        q.action == "any" or row["action"] == q.action,
        q.decision == "any" or row["decision"] == q.decision,
        q.verdict == "any" or row["verdict"] == (None if q.verdict == "none" else q.verdict),
        q.min_amount_cents is None or (isinstance(amount, int) and amount >= q.min_amount_cents),
        q.max_amount_cents is None or (isinstance(amount, int) and amount <= q.max_amount_cents),
        wanted_hours is None or row["outside_business_hours"] is wanted_hours,
        q.approver is None or row["approver"] == q.approver,
        q.payee_id is None or row["payee_id"] == q.payee_id,
        since is None or parse_ts(row["ts"]) >= since,
    ]
    return all(checks)


def run_query(
    query: RecordQuery, records: Sequence[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    since = None if query.since_days is None else now - timedelta(days=query.since_days)
    return [row for row in build_rows(records) if _matches(row, query, since)][:MAX_ROWS]


def _row_ids(rows: Sequence[dict[str, Any]]) -> set[str]:
    return {i for row in rows for i in (row["decision_record_id"], row["verdict_record_id"]) if i}


def _check_ids(answer: AuditorAnswer, rows: Sequence[dict[str, Any]]) -> list[str]:
    allowed = _row_ids(rows)
    cited = {
        i for c in answer.claims for i in [*c.record_ids, *(a.record_id for a in c.assertions)]
    }
    unknown = sorted(cited - allowed)
    return [f"record ids that are not in the rows: {unknown}"] if unknown else []


def to_claim(out: ClaimOut) -> Claim:
    assertions = tuple(Assertion(a.record_id, a.field, a.op, a.value) for a in out.assertions)
    return Claim(out.text, tuple(out.record_ids), assertions)


def inject_wrong_assertion(claim: Claim) -> Claim:
    """Demo only: make the first assertion false, the way a model might misremember a fact."""
    first = claim.assertions[0]
    value = first.value
    if isinstance(value, bool) or value is None:
        changed: str | int | bool | None = not value
    elif isinstance(value, int):
        changed = value + 100_000
    else:
        changed = value + "-x"
    return replace(claim, assertions=(replace(first, value=changed), *claim.assertions[1:]))


@dataclass(frozen=True)
class AuditResult:
    question: str
    query: RecordQuery
    rows: int
    summary: str
    results: tuple[ClaimResult, ...]
    injected: int | None  # index of the claim changed by inject_fault
    record: Receipted | None

    @property
    def accepted(self) -> list[ClaimResult]:
        return [r for r in self.results if r.accepted]

    def answer_text(self) -> str:
        """The model's summary only if every claim held; otherwise only the accepted claims."""
        if all(r.accepted for r in self.results):
            return self.summary
        accepted = [r.claim.text for r in self.accepted]
        held = f"{len(accepted)} of {len(self.results)} claims were proven."
        return " ".join([held, *accepted])


class Auditor:
    def __init__(
        self,
        llm: LlmClient,
        ledger: LedgerReader,
        trust: Trust,
        recorder: QaRecorder | None,
        *,
        clock: Clock,
        timezone: ZoneInfo,
    ) -> None:
        self._llm = llm
        self._ledger = ledger
        self._trust = trust
        self._recorder = recorder
        self._clock = clock
        self._timezone = timezone
        self.query_version, self._query_prompt = load_prompt(PROMPTS / "auditor_query.md")
        self.answer_version, self._answer_prompt = load_prompt(PROMPTS / "auditor_answer.md")

    async def ask(self, question: str, *, inject_fault: bool = False) -> AuditResult:
        question = re.sub(r"\s+", " ", question).strip()[:MAX_QUESTION]
        now = self._clock.now()
        today = now.astimezone(self._timezone).strftime("%A %Y-%m-%d")
        query = await self._llm.complete_structured(
            "spark", RecordQuery,
            [{"role": "system", "content": self._query_prompt},
             {"role": "user", "content": f"Today is {today}.\nQuestion: {question}"}],
            prompt_version=self.query_version,
        )  # fmt: skip
        rows = run_query(query, await self._ledger.records(), now)
        answer = await self._llm.complete_structured(
            "spark", AuditorAnswer,
            [{"role": "system", "content": self._answer_prompt},
             {"role": "user", "content": f"Question: {question}\nRows:\n{_rows_json(rows)}"}],
            prompt_version=self.answer_version, check=lambda a: _check_ids(a, rows),
        )  # fmt: skip
        claims = [to_claim(c) for c in answer.claims]
        injected = None
        if inject_fault and claims:
            claims[0], injected = inject_wrong_assertion(claims[0]), 0
        results = await self._check(claims)
        result = AuditResult(
            question, query, len(rows), answer.summary, tuple(results), injected, None
        )
        if self._recorder is not None:
            result = replace(result, record=await self._recorder.record(self._qa_body(result)))
        return result

    async def _check(self, claims: Sequence[Claim]) -> list[ClaimResult]:
        size = await self._ledger.final_size()
        cited = sorted({i for c in claims for i in c.record_ids})
        evidence = {}
        if size is not None:
            for record_id in cited:
                found = await self._ledger.evidence(record_id, size)
                if found is not None:
                    evidence[record_id] = found
        return check_claims(claims, evidence, self._trust)

    def _qa_body(self, result: AuditResult) -> dict[str, Any]:
        return {
            "question": result.question,
            "query": result.query.model_dump(),
            "rows": result.rows,
            "model": self._llm.config.route("spark").model,
            "prompt_versions": [self.query_version, self.answer_version],
            "answer": result.answer_text(),
            "claims": [
                {
                    "text": r.claim.text,
                    "record_ids": list(r.claim.record_ids),
                    "accepted": r.accepted,
                    "reasons": list(r.reasons),
                    "injected": i == result.injected,
                }
                for i, r in enumerate(result.results)
            ],
            "asked_at": format_ts(self._clock.now()),
        }


def _rows_json(rows: Sequence[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=1)  # model input, never hashed


class LedgerRecorder:
    """Signs ``assist.qa`` records with the assist producer key and checks each receipt."""

    def __init__(
        self,
        *,
        signer: Signer,
        producer_id: str,
        stream: str,
        ledger: LedgerClient,
        log_keys: KeyRing,
        clock: Clock,
    ) -> None:
        self._signer, self._producer_id, self._stream = signer, producer_id, stream
        self._ledger, self._log_keys, self._clock = ledger, log_keys, clock
        self._own_key = KeyRing([(producer_id, signer.public_key_raw)])

    async def record(self, body: dict[str, Any]) -> Receipted:
        submission = sign_submission(
            self._signer, producer_id=self._producer_id, stream=self._stream,
            record_id=new_ulid(self._clock), ts=format_ts(self._clock.now()),
            record_type=QA, body=body,
        )  # fmt: skip
        record = Receipted(submission, await self._ledger.append(submission))
        verify_receipted(
            record, producer_id=self._producer_id, producers=self._own_key,
            log_keys=self._log_keys, record_type=QA,
        )  # fmt: skip
        return record
