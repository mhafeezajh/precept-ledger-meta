"""``@governed``: the agent-side wrapper that lets a tool run only after Gate's record exists.

The wrapper does not take Gate's word for it. Before the tool runs it checks, itself, that:

- the ``gate.decision`` record is signed by Gate and carries a receipt signed by the log;
- the record is for this agent, this tool, and these exact arguments (argument digest);
- the decision is ``allow``, or ``escalate`` followed by an approving ``gate.verdict`` record
  that is also signed, receipted and linked to this decision.

Anything else, including Gate or the ledger being unreachable, raises ``GateDenied`` and the
tool never runs (R3, R4). After the tool runs, a ``gate.result`` is recorded; if that fails
the result is still returned, because the effect already happened, and the failure is
logged ``CRITICAL`` rather than hidden by re-running or swallowing the tool's result.
"""

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from common.clock import Clock, SystemClock, parse_ts
from common.errors import GateError, PreceptError
from common.logging import log_event
from gate.normalizer import args_digest
from gate.records import DECISION, VERDICT, Receipted, result_digest, verify_receipted
from ledger.signing import KeyRing

logger = logging.getLogger("pl.gate.governed")

T = TypeVar("T")
EXPIRY_GRACE_SECONDS = 5.0  # Gate closes expired escalations lazily; allow it to answer
MAX_WAIT_SECONDS = 25.0  # one long poll


class GateDenied(GateError):
    """The tool did not run. ``code`` says why; ``decision`` is the record, when there is one."""

    code = "gate.denied"

    def __init__(self, message: str, *, code: str, decision: Receipted | None = None) -> None:
        super().__init__(message, code=code)
        self.decision = decision


@dataclass(frozen=True)
class EscalationStatus:
    state: str
    verdict: Receipted | None


class GateEndpoint(Protocol):
    """Gate as the agent sees it. ``gate.client.HttpGateClient`` is the real one."""

    async def decide(self, tool: str, arguments: dict[str, Any]) -> Receipted: ...

    async def wait(self, escalation_id: str, timeout_seconds: float) -> EscalationStatus: ...

    async def record_result(
        self, decision_record_id: str, *, ok: bool, digest: str | None, error: str | None
    ) -> Receipted: ...


class Governor:
    def __init__(
        self,
        endpoint: GateEndpoint,
        *,
        agent: str,
        gate_keys: KeyRing,
        log_keys: KeyRing,
        producer_id: str = "gate-1",
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._endpoint = endpoint
        self.agent = agent
        self._gate_keys = gate_keys
        self._log_keys = log_keys
        self._producer_id = producer_id
        self._clock = clock or SystemClock()
        self._monotonic = monotonic

    def tool(
        self, name: str
    ) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
        """Decorator for an async keyword-argument tool: ``@governor.tool("create_payment")``."""

        def wrap(function: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
            @functools.wraps(function)
            async def governed_call(**arguments: Any) -> T:
                return await self.run(name, arguments, lambda: function(**arguments))

            return governed_call

        return wrap

    async def run(
        self, tool: str, arguments: dict[str, Any], effect: Callable[[], Awaitable[T]]
    ) -> T:
        decision = await self._decide(tool, arguments)
        verdict = await self._clearance(decision)
        # Write before effect holds here: a verified decision (and verdict) receipt exists.
        log_event(logger, logging.INFO, "tool cleared", tool=tool, decision=decision.record_id,
                  verdict=None if verdict is None else verdict.record_id)  # fmt: skip
        try:
            result = await effect()
        except Exception as exc:
            await self._record(decision, ok=False, digest=None, error=type(exc).__name__)
            raise
        await self._record(decision, ok=True, digest=result_digest(result), error=None)
        return result

    async def _decide(self, tool: str, arguments: dict[str, Any]) -> Receipted:
        try:
            expected_digest = args_digest(arguments)
            decision = await self._endpoint.decide(tool, arguments)
        except GateDenied:
            raise
        except PreceptError as exc:
            raise GateDenied(f"no decision from Gate ({exc.code})", code=exc.code) from exc
        except Exception as exc:  # unreachable or broken Gate: the tool must not run (R4)
            raise GateDenied("no decision from Gate", code="gate.unreachable") from exc
        self._check(decision, DECISION)
        body = decision.body
        if (body.get("agent"), body.get("tool"), body.get("args_digest")) != (
            self.agent, tool, expected_digest
        ):  # fmt: skip
            raise GateDenied(
                "decision is not for this call", code="gate.mismatch", decision=decision
            )
        return decision

    async def _clearance(self, decision: Receipted) -> Receipted | None:
        outcome = decision.body.get("decision")
        if outcome == "allow":
            return None
        if outcome != "escalate":
            reason = str(decision.body.get("reason", "gate.denied"))
            raise GateDenied(f"Gate denied the call ({reason})", code=reason, decision=decision)
        escalation = decision.body["escalation"]
        status = await self._await_verdict(
            str(escalation["id"]), parse_ts(escalation["expires_at"]).timestamp()
        )
        if status.state != "approved":
            raise GateDenied(
                f"escalation {status.state}", code=f"gate.{status.state}", decision=decision
            )
        if status.verdict is None:
            raise GateDenied(
                "approved without a verdict record", code="gate.no_verdict", decision=decision
            )
        verdict = status.verdict
        self._check(verdict, VERDICT)
        body = verdict.body
        if (body.get("decision_record_id"), body.get("escalation_id"), body.get("verdict")) != (
            decision.record_id, escalation["id"], "approve"
        ):  # fmt: skip
            raise GateDenied(
                "verdict is not an approval of this call", code="gate.mismatch", decision=decision
            )
        return verdict

    async def _await_verdict(self, escalation_id: str, expires_epoch: float) -> EscalationStatus:
        # Our deadline: the signed expiry, measured on our own clock from now.
        deadline = (
            self._monotonic()
            + max(0.0, expires_epoch - self._clock.now().timestamp())
            + EXPIRY_GRACE_SECONDS
        )
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return EscalationStatus("expired", None)
            try:
                status = await self._endpoint.wait(escalation_id, min(remaining, MAX_WAIT_SECONDS))
            except Exception as exc:  # cannot learn the verdict: deny (R4)
                raise GateDenied(
                    "cannot reach Gate for the verdict", code="gate.unreachable"
                ) from exc
            if status.state != "open":
                return status

    def _check(self, record: Receipted, record_type: str) -> None:
        try:
            verify_receipted(
                record, producer_id=self._producer_id, producers=self._gate_keys,
                log_keys=self._log_keys, record_type=record_type,
            )  # fmt: skip
        except GateError as exc:
            raise GateDenied("record from Gate does not verify", code="gate.bad_record") from exc

    async def _record(
        self, decision: Receipted, *, ok: bool, digest: str | None, error: str | None
    ) -> None:
        try:
            await self._endpoint.record_result(
                decision.record_id, ok=ok, digest=digest, error=error
            )
        # The effect happened: never hide it or run it again; the failure is logged CRITICAL.
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.CRITICAL, "tool ran but its result was not recorded",
                      decision=decision.record_id, error=type(exc).__name__)  # fmt: skip
