"""Gate: decide, record, and only then let a tool call through (ARCHITECTURE flow A).

``decide`` normalizes the call, asks Cedar, signs a ``gate.decision`` record and appends it;
it returns only after the log's receipt for that record has been verified. If the ledger is
down, refuses the record or returns a bad receipt, ``decide`` raises and the call is denied
(R3: no receipt, no effect). Escalations close exactly once: by a person with the required
role, recorded as ``gate.verdict``, or by the timeout, which is a deny.
"""

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from common.clock import Clock, format_ts
from common.errors import GateError, PreceptError
from common.ids import is_ulid, new_ulid
from common.logging import log_event
from gate.decide import Evaluation, Outcome, PolicySet
from gate.escalations import APPROVED, EXPIRED, OPEN, REJECTED, Escalation, EscalationStore
from gate.ledger_client import LedgerClient
from gate.normalizer import Normalizer, args_digest
from gate.records import DECISION, RESULT, VERDICT, Receipted, verify_receipted
from ledger.signing import KeyRing, Signer, sign_submission

logger = logging.getLogger("pl.gate")

_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")
VERDICTS = {"approve": APPROVED, "reject": REJECTED}


@dataclass(frozen=True)
class GateDecision:
    outcome: Outcome
    reason: str
    record: Receipted
    escalation: Escalation | None = None


@dataclass(frozen=True)
class Approver:
    name: str
    roles: frozenset[str]


class Gate:
    def __init__(
        self,
        *,
        producer_id: str,
        signer: Signer,
        stream: str,
        ledger: LedgerClient,
        log_keys: KeyRing,
        normalizer: Normalizer,
        policies: PolicySet,
        escalations: EscalationStore,
        clock: Clock,
        approval_timeout: timedelta,
    ) -> None:
        self.producer_id = producer_id
        self._signer = signer
        self._own_key = KeyRing([(producer_id, signer.public_key_raw)])
        self.stream = stream
        self._ledger = ledger
        self._log_keys = log_keys
        self._normalizer = normalizer
        self.policies = policies
        self.escalations = escalations
        self._clock = clock
        self._timeout = approval_timeout
        self._lock = asyncio.Lock()  # one closer at a time: verdicts and expiry
        self._closed: dict[str, asyncio.Event] = {}

    # --- decisions -----------------------------------------------------------------------

    async def decide(self, agent: str, tool: str, arguments: dict[str, Any]) -> GateDecision:
        if not _NAME.fullmatch(agent) or not _TOOL.fullmatch(tool):
            raise GateError("invalid agent or tool name", code="gate.bad_request")
        evaluation, action, facts, digest = await self._evaluate(agent, tool, arguments)
        now = self._clock.now()
        body: dict[str, Any] = {
            "agent": agent,
            "tool": tool,
            "action": action,
            "decision": evaluation.outcome.value,
            "reason": evaluation.reason,
            "resource": facts,
            "policy_ids": list(evaluation.policy_ids),
            "policy_set": self.policies.digest,
            "args_digest": digest,
        }
        escalation = None
        if evaluation.outcome is Outcome.ESCALATE:
            escalation_id, expires_at = new_ulid(self._clock), now + self._timeout
            body["escalation"] = {
                "id": escalation_id,
                "expires_at": format_ts(expires_at),
                "approver_roles": list(evaluation.approver_roles),
            }
        record = await self._append(DECISION, body)
        if evaluation.outcome is Outcome.ESCALATE:
            escalation = Escalation(
                escalation_id, record.record_id, agent, tool, str(action),
                evaluation.approver_roles, now, expires_at,
            )  # fmt: skip
            await self._open(escalation)
        log_event(
            logger, logging.INFO, "decision", record_id=record.record_id, agent=agent,
            tool=tool, outcome=evaluation.outcome.value, reason=evaluation.reason,
        )  # fmt: skip
        return GateDecision(evaluation.outcome, evaluation.reason, record, escalation)

    async def _evaluate(
        self, agent: str, tool: str, arguments: dict[str, Any]
    ) -> tuple[Evaluation, str | None, dict[str, Any], str | None]:
        """Cedar's evaluation, or a deny with the normalizer's code. Never raises GateError."""
        try:
            digest = args_digest(arguments)
            normalized = await self._normalizer.normalize(tool, arguments)
        except GateError as exc:  # recorded as a deny: refusals are evidence too
            return Evaluation.deny(exc.code), None, {}, None
        evaluation = self.policies.evaluate(agent, normalized.action, tool, normalized.facts)
        return evaluation, normalized.action, normalized.facts, digest

    async def _open(self, escalation: Escalation) -> None:
        try:
            await self.escalations.open(escalation)
        except Exception as exc:  # the decision record exists, but nobody can approve it: deny
            raise GateError("cannot open the escalation", code="gate.store_unavailable") from exc

    async def _append(self, record_type: str, body: dict[str, Any]) -> Receipted:
        submission = sign_submission(
            self._signer,
            producer_id=self.producer_id,
            stream=self.stream,
            record_id=new_ulid(self._clock),
            ts=format_ts(self._clock.now()),
            record_type=record_type,
            body=body,
        )
        try:
            receipt = await self._ledger.append(submission)
        except GateError:
            raise
        except PreceptError as exc:
            raise GateError(
                f"ledger refused the record ({exc.code})", code="gate.ledger_refused"
            ) from exc
        record = Receipted(submission, receipt)
        try:
            self._verify(record, record_type)
        except GateError as exc:
            log_event(logger, logging.CRITICAL, "bad receipt", record_id=record.record_id)
            raise GateError(
                "the ledger's receipt does not verify", code="gate.bad_receipt"
            ) from exc
        return record

    def _verify(self, record: Receipted, record_type: str) -> None:
        verify_receipted(
            record, producer_id=self.producer_id, producers=self._own_key,
            log_keys=self._log_keys, record_type=record_type,
        )  # fmt: skip

    # --- escalations ---------------------------------------------------------------------

    async def verdict(self, escalation_id: str, approver: Approver, verdict: str) -> Receipted:
        state = VERDICTS.get(verdict)
        if state is None:
            raise GateError("verdict must be approve or reject", code="gate.bad_request")
        async with self._lock:
            escalation = await self._get(escalation_id)
            now = self._clock.now()
            if escalation.state == OPEN and now >= escalation.expires_at:
                await self._expire(escalation, now)
                escalation = await self._get(escalation_id)
            if escalation.state != OPEN:
                raise GateError(f"escalation is {escalation.state}", code="gate.escalation_closed")
            if not set(escalation.approver_roles) <= approver.roles:
                raise GateError(
                    "approver lacks the required role", code="gate.approver_not_allowed"
                )
            record = await self._append(
                VERDICT, self._verdict_body(escalation, verdict, approver.name)
            )
            if not await self.escalations.close(
                escalation_id, state, approver.name, record.record_id, now
            ):
                log_event(logger, logging.CRITICAL, "verdict recorded but not applied",
                          escalation_id=escalation_id, record_id=record.record_id)  # fmt: skip
                raise GateError("escalation closed concurrently", code="gate.escalation_closed")
            self._wake(escalation_id)
        log_event(logger, logging.INFO, "verdict", escalation_id=escalation_id,
                  verdict=verdict, approver=approver.name)  # fmt: skip
        return record

    def _verdict_body(
        self, escalation: Escalation, verdict: str, approver: str | None
    ) -> dict[str, Any]:
        return {
            "decision_record_id": escalation.decision_record_id,
            "escalation_id": escalation.id,
            "verdict": verdict,
            "approver": approver,
            "agent": escalation.agent,
            "tool": escalation.tool,
            "action": escalation.action,
        }

    async def _expire(self, escalation: Escalation, now: datetime) -> None:
        """Close as expired. The record is best effort: the deny holds without it."""
        record_id = None
        try:
            record_id = (
                await self._append(VERDICT, self._verdict_body(escalation, "expire", None))
            ).record_id
        except GateError as exc:
            log_event(
                logger,
                logging.WARNING,
                "expiry not recorded",
                escalation_id=escalation.id,
                code=exc.code,
            )
        await self.escalations.close(escalation.id, EXPIRED, None, record_id, now)
        self._wake(escalation.id)

    async def _get(self, escalation_id: str) -> Escalation:
        found = await self.escalations.get(escalation_id) if is_ulid(escalation_id) else None
        if found is None:
            raise GateError("no such escalation", code="gate.escalation_not_found")
        return found

    def _wake(self, escalation_id: str) -> None:
        event = self._closed.pop(escalation_id, None)
        if event is not None:
            event.set()

    async def status(self, escalation_id: str) -> Escalation:
        """The escalation as it stands, expiring it first if its time is up."""
        escalation = await self._get(escalation_id)
        if escalation.state == OPEN and self._clock.now() >= escalation.expires_at:
            async with self._lock:
                escalation = await self._get(escalation_id)
                if escalation.state == OPEN:
                    await self._expire(escalation, self._clock.now())
                    escalation = await self._get(escalation_id)
        return escalation

    async def wait(self, escalation_id: str, timeout_seconds: float) -> Escalation:
        """Wait up to ``timeout_seconds`` for the escalation to close; return it either way."""
        event = self._closed.setdefault(
            escalation_id, asyncio.Event()
        )  # before the read: no lost wake-up
        escalation = await self.status(escalation_id)
        if escalation.state != OPEN or timeout_seconds <= 0:
            return escalation
        until_expiry = (escalation.expires_at - self._clock.now()).total_seconds()
        # On timeout it is still open, or due, and status() below expires it.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), max(0.0, min(timeout_seconds, until_expiry)))
        return await self.status(escalation_id)

    async def expire_due(self) -> int:
        count = 0
        for escalation in await self.escalations.due(self._clock.now()):
            if (await self.status(escalation.id)).state == EXPIRED:
                count += 1
        return count

    async def verdict_record(self, escalation: Escalation) -> Receipted | None:
        """The verdict record as the ledger holds it, checked against its receipt."""
        if escalation.verdict_record_id is None:
            return None
        return await self._read(escalation.verdict_record_id, VERDICT)

    async def _read(self, record_id: str, record_type: str) -> Receipted:
        try:
            record = Receipted.from_record(*await self._ledger.get(record_id))
        except GateError:
            raise
        except PreceptError as exc:
            code = (
                "gate.record_not_found"
                if exc.code == "ledger.not_found"
                else "gate.ledger_unavailable"
            )
            raise GateError(f"ledger read failed ({exc.code})", code=code) from exc
        self._verify(record, record_type)
        return record

    # --- results -------------------------------------------------------------------------

    async def record_result(
        self,
        agent: str,
        decision_record_id: str,
        *,
        ok: bool,
        digest: str | None,
        error: str | None,
    ) -> Receipted:
        """Record that a tool ran, for a decision that let it run, reported by its agent."""
        if digest is not None and not _DIGEST.fullmatch(digest):
            raise GateError("result digest must be sha256:<hex>", code="gate.bad_request")
        if error is not None and not _ERROR.fullmatch(error):
            raise GateError("error must be a short code", code="gate.bad_request")
        if not is_ulid(decision_record_id):
            raise GateError("decision record id must be a ULID", code="gate.bad_request")
        decision = await self._read(decision_record_id, DECISION)
        if decision.body.get("agent") != agent:
            raise GateError("decision belongs to another agent", code="gate.forbidden")
        verdict_id = await self._cleared_by(decision)
        body = {
            "decision_record_id": decision_record_id,
            "verdict_record_id": verdict_id,
            "agent": agent,
            "tool": decision.body["tool"],
            "outcome": "ok" if ok else "error",
            "result_digest": digest,
            "error": error,
        }
        return await self._append(RESULT, body)

    async def _cleared_by(self, decision: Receipted) -> str | None:
        """None for an allow; the approving verdict's id for an approved escalation."""
        outcome = decision.body.get("decision")
        if outcome == Outcome.ALLOW:
            return None
        if outcome == Outcome.ESCALATE:
            escalation = await self._get(str(decision.body["escalation"]["id"]))
            if escalation.state == APPROVED and escalation.verdict_record_id is not None:
                return escalation.verdict_record_id
        raise GateError("this decision did not let the tool run", code="gate.not_cleared")
