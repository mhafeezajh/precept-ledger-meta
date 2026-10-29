"""Gate test wiring: a real ledger in memory, a Gate, and an endpoint that goes through JSON.

Keys are TEST ONLY (tests/keys/test-keys.json).
"""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common.clock import FixedClock
from common.errors import GateError
from gate.core import Approver, Gate
from gate.decide import SCHEMA_FILE, PolicySet
from gate.escalations import MemoryEscalationStore
from gate.governed import EscalationStatus, Governor
from gate.normalizer import Normalizer, StaticPayees
from gate.records import Receipted
from ledger.format import Receipt, Record, Submission
from ledger.service import Ledger
from ledger.signing import KeyRing, Signer
from ledger.store import MemoryStore
from tests.helpers import STREAM, T0, signer_for

ROOT = Path(__file__).resolve().parents[1]
POLICIES = PolicySet(
    (ROOT / "deploy/policies/harbor-federal.cedar").read_text(), SCHEMA_FILE.read_text()
)
GATE, LOG = signer_for("gate-1"), signer_for("log")
GATE_KEYS = KeyRing([("gate-1", GATE.public_key_raw)])
LOG_KEYS = KeyRing([("log", LOG.public_key_raw)])
AGENT = "payments-ops"
SUPERVISOR = Approver("j.ortiz", frozenset({"payments-supervisor"}))
CLERK = Approver("a.chen", frozenset({"payments-clerk"}))
TIMEOUT = timedelta(minutes=10)
DEMO_PAYMENT = {"payee_id": "northwind-supply", "amount": "48000.00", "currency": "USD"}
SMALL_PAYMENT = {"payee_id": "acme-paper", "amount": "120.00", "currency": "USD"}


class LocalLedger:
    """The real ``Ledger`` over a memory store, behind Gate's ``LedgerClient`` port."""

    def __init__(self, clock: FixedClock, log_signer: Signer = LOG) -> None:
        self.store = MemoryStore()
        self.ledger = Ledger(self.store, GATE_KEYS, log_signer, clock)
        self.down = False
        self.appended: list[Submission] = []

    async def append(self, submission: Submission) -> Receipt:
        if self.down:
            raise GateError("ledger unreachable", code="gate.ledger_unavailable")
        receipt = await self.ledger.append(submission.stream, submission)
        self.appended.append(submission)
        return receipt

    async def get(self, record_id: str) -> tuple[Record, Receipt]:
        if self.down:
            raise GateError("ledger unreachable", code="gate.ledger_unavailable")
        return await self.ledger.get(record_id)

    def types(self) -> list[str]:
        return [s.type for s in self.appended]


@dataclass
class Rig:
    clock: FixedClock
    ledger: LocalLedger
    gate: Gate
    store: MemoryEscalationStore
    payees: StaticPayees


def rig(*, log_signer: Signer = LOG, timeout: timedelta = TIMEOUT) -> Rig:
    clock = FixedClock(T0, timedelta(milliseconds=1))
    ledger = LocalLedger(clock, log_signer)
    store = MemoryEscalationStore()
    payees = StaticPayees({"acme-paper"})
    normalizer = Normalizer(
        payees, internal_domains={"harborfederal.example"}, clock=clock,
        timezone=ZoneInfo("America/New_York"),
    )  # fmt: skip
    gate = Gate(
        producer_id="gate-1", signer=GATE, stream=STREAM, ledger=ledger, log_keys=LOG_KEYS,
        normalizer=normalizer, policies=POLICIES, escalations=store, clock=clock,
        approval_timeout=timeout,
    )  # fmt: skip
    return Rig(clock, ledger, gate, store, payees)


class LocalEndpoint:
    """``GateEndpoint`` over a ``Gate`` in this process, through JSON like the HTTP client."""

    def __init__(self, gate: Gate, agent: str = AGENT) -> None:
        self.gate = gate
        self.agent = agent

    async def decide(
        self, tool: str, arguments: dict[str, Any], *, credential: dict[str, Any] | None = None
    ) -> Receipted:
        decision = await self.gate.decide(self.agent, tool, arguments, credential=credential)
        return Receipted.from_json(decision.record.to_json())

    async def wait(self, escalation_id: str, timeout_seconds: float) -> EscalationStatus:
        escalation = await self.gate.wait(escalation_id, timeout_seconds)
        verdict = await self.gate.verdict_record(escalation)
        return EscalationStatus(escalation.state, verdict)

    async def record_result(
        self, decision_record_id: str, *, ok: bool, digest: str | None, error: str | None
    ) -> Receipted:
        return await self.gate.record_result(
            self.agent, decision_record_id, ok=ok, digest=digest, error=error
        )


def governor(endpoint: Any, clock: FixedClock | None = None) -> Governor:
    return Governor(endpoint, agent=AGENT, gate_keys=GATE_KEYS, log_keys=LOG_KEYS, clock=clock)
