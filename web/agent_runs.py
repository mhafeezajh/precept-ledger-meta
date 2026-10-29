"""Agent runs started from the console, kept in memory while the process lives.

Each run is the real payments agent (``agent.cli.open_agent``: its own Gate token, the active
model profile, every call through ``@governed``). An observer records each step as it happens;
the page polls the run and shows a held payment as waiting for an approver.
"""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.graph import PaymentsAgent, StepRecord
from agent.steps import AgentStep
from common.clock import SystemClock
from common.ids import new_ulid
from common.logging import log_event

logger = logging.getLogger("pl.web.agent")
MAX_RUNS = 20
AgentFactory = Callable[[], AbstractAsyncContextManager[PaymentsAgent]]


@dataclass
class Event:
    n: int
    tool: str
    arguments: dict[str, Any]
    note: str
    state: str = "running"  # running, held, then the step's outcome
    decision: str | None = None
    decision_record_id: str | None = None
    verdict_record_id: str | None = None
    code: str | None = None
    result: Any = None


@dataclass
class Run:
    id: str
    task: str
    started_at: datetime
    events: list[Event] = field(default_factory=list)
    reply: str | None = None
    stopped: str | None = None
    error: str | None = None
    finished: bool = False


class _Observer:
    def __init__(self, run: Run) -> None:
        self._run = run

    def started(self, n: int, step: AgentStep) -> None:
        self._run.events.append(Event(n, step.tool, step.arguments(), step.note))

    def held(self, n: int, decision_record_id: str) -> None:
        event = self._run.events[-1]
        event.state, event.decision, event.decision_record_id = (
            "held",
            "escalate",
            decision_record_id,
        )

    def finished(self, record: StepRecord) -> None:
        event = self._run.events[-1]
        event.state, event.code, event.result = record.outcome, record.code, record.result
        event.decision = record.decision
        event.decision_record_id = record.decision_record_id
        event.verdict_record_id = record.verdict_record_id


class AgentRuns:
    def __init__(self, factory: AgentFactory) -> None:
        self._factory = factory
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, task: str) -> Run:
        clock = SystemClock()
        run = Run(new_ulid(clock).lower(), task, clock.now())
        self._runs[run.id] = run
        while len(self._runs) > MAX_RUNS:
            self._runs.popitem(last=False)
        job = asyncio.create_task(self._go(run))
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)
        return run

    async def _go(self, run: Run) -> None:
        try:
            async with self._factory() as agent:
                result = await agent.run(run.task, observer=_Observer(run))
            run.reply, run.stopped = result.reply, result.stopped
        except Exception as exc:  # noqa: BLE001  # shown on the page; the agent ran nothing more
            run.error = getattr(exc, "code", type(exc).__name__)
            log_event(logger, logging.WARNING, "agent run failed", run=run.id, code=run.error)
        finally:
            run.finished = True

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def recent(self) -> list[Run]:
        return list(reversed(self._runs.values()))

    async def wait(self) -> None:
        """Wait for runs still going (tests, shutdown)."""
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks)
