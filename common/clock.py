"""Injected clocks and the one timestamp format used everywhere: RFC 3339, UTC, ms, ``Z``."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from common.errors import FormatError

_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FixedClock:
    """Test clock: returns ``current`` and moves forward by ``step`` on each call."""

    current: datetime
    step: timedelta = timedelta(0)

    def now(self) -> datetime:
        value = self.current
        self.current = self.current + self.step
        return value


def format_ts(moment: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (truncated to milliseconds)."""
    if moment.tzinfo is None:
        raise FormatError("timestamp must be timezone-aware", code="format.naive_time")
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_ts(text: str) -> datetime:
    """Parse the exact format produced by ``format_ts``; anything else is rejected."""
    if not _TS_PATTERN.fullmatch(text):
        raise FormatError(
            f"timestamp {text!r} is not RFC 3339 UTC with ms and Z", code="format.bad_time"
        )
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise FormatError(f"timestamp {text!r} is not a real date", code="format.bad_time") from exc
