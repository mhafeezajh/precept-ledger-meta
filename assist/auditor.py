"""Auditor Q&A with proof-checked answers (demo beat 3, ARCHITECTURE flow C).

1. The stream's export is verified up to its latest final checkpoint (``claims.log``). If it
   does not verify, nothing is asked and nothing is answered.
2. Muse Spark turns the question into a ``RecordQuery``: filters only, no answers.
3. Code runs the query over the proven records (``claims.query``): decisions joined with their
   verdicts. This is the model's only view of the ledger, and it is read-only.
4. Muse Spark answers with claims: text, cited record ids, and field assertions.
5. ``claims.checker`` checks every claim against the proven records, and the summary against
   the accepted claims. Only what holds reaches the answer text; the rest is shown with reasons.
6. The question, the query, the proven log size and root, the matching row ids, every claim
   and its result are appended as ``assist.qa``, signed by ``assist-1``.

``inject_fault`` (demo only) changes one assertion of the first claim before checking, and
the result says so, to show a wrong claim being rejected on demand.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from assist.brief import load_prompt
from assist.evidence import LedgerReader
from assist.llm import LlmClient
from claims.checker import (
    Assertion,
    Claim,
    ClaimResult,
    Trust,
    check_claims_in_log,
    check_summary,
)
from claims.log import ProvenLog, prove_log
from claims.query import MAX_ROWS, Query, run_query
from common.clock import Clock, format_ts
from common.errors import VerificationError
from common.ids import new_ulid
from gate.ledger_client import LedgerClient
from gate.records import Receipted, verify_receipted
from ledger.signing import KeyRing, Signer, sign_submission

PROMPTS = Path(__file__).parent / "prompts"
# Live, an audit took 38-41 s at medium and 18-19 s at minimal, every claim proven either way;
# the claim checker still checks each claim (docs/MODELS.md).
AUDIT_EFFORT = "minimal"
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

    def to_query(self) -> Query:
        return Query(**self.model_dump())


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
    log: ProvenLog
    matched: tuple[str, ...]  # decision ids of every matching row, newest first
    rows: int  # how many of them the model saw (at most MAX_ROWS)
    summary: str
    summary_problems: tuple[str, ...]
    results: tuple[ClaimResult, ...]
    injected: int | None  # index of the claim changed by inject_fault
    record: Receipted | None

    @property
    def accepted(self) -> list[ClaimResult]:
        return [r for r in self.results if r.accepted]

    def answer_text(self) -> str:
        """The model's summary only if it and every claim held; otherwise the accepted claims."""
        if all(r.accepted for r in self.results) and not self.summary_problems:
            return self.summary
        accepted = [r.claim.text for r in self.accepted]
        held = f"{len(accepted)} of {len(self.results)} claims were proven."
        return " ".join([held, *accepted])

    def coverage(self) -> str:
        """What the code-run query covered: the basis for any "every" in the answer (D65)."""
        size, matched = self.log.size, len(self.matched)
        text = (
            f"The query ran in code over all {size} records under the final checkpoint"
            f" (size {size}, root {self.log.root_b64[:12]}…) and matched {matched}."
        )
        if self.rows < matched:
            text += f" The model saw the newest {self.rows}."
        if self.log.newer:
            text += f" {self.log.newer} newer records are not final yet and were not searched."
        return text


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
        log = prove_log(await self._ledger.export(), self._trust)
        if isinstance(log, str):  # R4: no answer about a log that does not verify
            raise VerificationError(log, code="audit.log_unproven")
        now = self._clock.now()
        today = now.astimezone(self._timezone).strftime("%A %Y-%m-%d")
        query = await self._llm.complete_structured(
            "spark", RecordQuery,
            [{"role": "system", "content": self._query_prompt},
             {"role": "user", "content": f"Today is {today}.\nQuestion: {question}"}],
            prompt_version=self.query_version, reasoning_effort=AUDIT_EFFORT,
        )  # fmt: skip
        matched = run_query(query.to_query(), log.records, now)
        rows = matched[:MAX_ROWS]
        answer = await self._llm.complete_structured(
            "spark", AuditorAnswer,
            [{"role": "system", "content": self._answer_prompt},
             {"role": "user", "content": f"Question: {question}\nRows:\n{_rows_json(rows)}"}],
            prompt_version=self.answer_version, check=lambda a: _check_ids(a, rows),
            reasoning_effort=AUDIT_EFFORT,
        )  # fmt: skip
        claims = [to_claim(c) for c in answer.claims]
        injected = None
        if inject_fault and claims:
            claims[0], injected = inject_wrong_assertion(claims[0]), 0
        results = check_claims_in_log(claims, log)
        counts = (len(matched), len(rows), log.size)
        result = AuditResult(
            question, query, log, tuple(r["decision_record_id"] for r in matched), len(rows),
            answer.summary, tuple(check_summary(answer.summary, results, numbers=counts)),
            tuple(results), injected, None,
        )  # fmt: skip
        if self._recorder is not None:
            result = replace(result, record=await self._recorder.record(self._qa_body(result)))
        return result

    def _qa_body(self, result: AuditResult) -> dict[str, Any]:
        return {
            "question": result.question,
            "query": result.query.model_dump(),
            "log": {"size": result.log.size, "root": result.log.root_b64},
            "matched": list(result.matched),
            "rows": result.rows,
            "coverage": result.coverage(),
            "model": self._llm.config.route("spark").model,
            "prompt_versions": [self.query_version, self.answer_version],
            "answer": result.answer_text(),
            "summary_problems": list(result.summary_problems),
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
