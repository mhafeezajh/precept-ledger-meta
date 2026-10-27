"""Publishes a checkpoint for every stream on a fixed interval (ARCHITECTURE flow D).

A ``PreceptError`` in one stream (store down, inconsistent tree) is logged with its code and
that stream simply gets no checkpoint this tick; the other streams are unaffected. Any other
exception is a bug: the loop logs it, stops, and reports ``stopped`` so health checks fail
instead of the loop dying quietly (R4).
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Protocol

from common.errors import PreceptError
from common.logging import log_event
from ledger.checkpointer import SignedCheckpoint

logger = logging.getLogger("pl.ledger.checkpoints")


class Publisher(Protocol):
    async def publish(self) -> SignedCheckpoint: ...


class CheckpointLoop:
    def __init__(self, publishers: Mapping[str, Publisher], interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("checkpoint interval must be positive")
        self._publishers = dict(publishers)
        self._interval = interval_seconds
        self._errors: dict[str, str] = {}
        self._stopped = False
        self.ticks = 0

    async def run_once(self) -> dict[str, SignedCheckpoint | PreceptError]:
        results: dict[str, SignedCheckpoint | PreceptError] = {}
        for stream, publisher in self._publishers.items():
            try:
                results[stream] = await publisher.publish()
                self._errors.pop(stream, None)
            except PreceptError as exc:
                results[stream] = exc
                self._errors[stream] = exc.code
                log_event(logger, logging.ERROR, "checkpoint failed", stream=stream, code=exc.code)
        self.ticks += 1
        return results

    async def run_forever(self) -> None:
        try:
            while True:
                await self.run_once()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stopped = True
            log_event(logger, logging.CRITICAL, "checkpoint loop stopped", error=type(exc).__name__)
            raise

    def status(self) -> str:
        return "stopped" if self._stopped else "running"

    def last_errors(self) -> dict[str, str]:
        return dict(self._errors)
