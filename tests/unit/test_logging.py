"""JSON logs with a trace id; never request bodies (skill §7)."""

import json
import logging

import pytest

from common.logging import JsonFormatter, accept_trace_id, log_event, new_trace_id, trace_id_var


def test_trace_ids() -> None:
    made = new_trace_id()
    assert len(made) == 32
    assert int(made, 16) >= 0
    assert accept_trace_id("ab" * 16) == "ab" * 16
    for bad in (None, "", "AB" * 16, "ab" * 17, "zz" * 16, "../../etc"):
        assert len(accept_trace_id(bad)) == 32
        assert accept_trace_id(bad) != bad


def test_json_formatter_includes_trace_id_and_fields() -> None:
    token = trace_id_var.set("cd" * 16)
    try:
        record = logging.LogRecord("pl", logging.INFO, __file__, 1, "request", None, None)
        record.fields = {"status": 201, "path": "/ledger/v1/streams/{stream}/records"}
        line = json.loads(JsonFormatter().format(record))
    finally:
        trace_id_var.reset(token)
    assert line["trace_id"] == "cd" * 16
    assert line["msg"] == "request"
    assert line["level"] == "INFO"
    assert line["status"] == 201
    assert line["ts"].endswith("Z")


def test_log_event_passes_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("pl.test")
    with caplog.at_level(logging.INFO, logger="pl.test"):
        log_event(logger, logging.INFO, "hello", answer=42)
    (record,) = caplog.records
    assert getattr(record, "fields", None) == {"answer": 42}


def test_configure_logging_installs_one_json_handler() -> None:
    from common.logging import configure_logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("WARNING")
        configure_logging("WARNING")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
