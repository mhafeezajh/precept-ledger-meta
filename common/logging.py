"""JSON log lines with a ``trace_id`` (skill §7).

Callers log events with explicit fields through ``log_event``; nothing logs request bodies,
tool arguments, prompts, keys or PII. The trace id lives in a context variable set once per
request, so every line a request produces carries it.
"""

import json
import logging
import re
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def new_trace_id() -> str:
    return secrets.token_hex(16)


def accept_trace_id(value: str | None) -> str:
    """Keep a caller's trace id only if it is 32 lowercase hex characters; else make one."""
    return value if value is not None and _TRACE_ID.match(value) else new_trace_id()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(line, default=str)  # a log line, never hashed or signed


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
