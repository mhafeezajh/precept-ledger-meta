from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from common.clock import FixedClock, SystemClock, format_ts, parse_ts
from common.config import load_settings
from common.errors import FormatError, LedgerError, PreceptError
from common.ids import is_ulid, new_ulid


def test_format_ts_is_utc_with_milliseconds() -> None:
    moment = datetime(2026, 10, 29, 16, 14, 3, 112999, tzinfo=timezone(timedelta(hours=-5)))
    assert format_ts(moment) == "2026-10-29T21:14:03.112Z"


def test_format_ts_rejects_naive_datetimes() -> None:
    with pytest.raises(FormatError):
        format_ts(datetime(2026, 10, 29))  # naive on purpose


def test_parse_ts_round_trip_and_rejections() -> None:
    assert format_ts(parse_ts("2026-10-29T21:14:03.112Z")) == "2026-10-29T21:14:03.112Z"
    for bad in ("2026-10-29T21:14:03Z", "2026-10-29 21:14:03.112Z", "2026-02-30T00:00:00.000Z"):
        with pytest.raises(FormatError):
            parse_ts(bad)


def test_clocks() -> None:
    assert SystemClock().now().tzinfo is UTC
    clk = FixedClock(datetime(2026, 10, 27, tzinfo=UTC), timedelta(seconds=1))
    assert clk.now() < clk.now()


def test_ulids_are_valid_ordered_by_time_and_unique() -> None:
    clk = FixedClock(datetime(2026, 10, 27, tzinfo=UTC), timedelta(milliseconds=1))
    ids = [new_ulid(clk) for _ in range(50)]
    assert all(is_ulid(i) for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == 50
    assert not is_ulid("01JB6X7Q2M9ZK3T8V4N5R6S7WU")  # U is not Crockford
    assert not is_ulid("81JB6X7Q2M9ZK3T8V4N5R6S7W0")  # exceeds 128 bits


def test_ulid_rejects_times_before_the_epoch() -> None:
    with pytest.raises(ValueError, match="ULID range"):
        new_ulid(FixedClock(datetime(1969, 12, 31, tzinfo=UTC)))


def test_errors_carry_codes() -> None:
    err = LedgerError("boom", code="ledger.custom")
    assert err.code == "ledger.custom"
    assert str(err) == "[ledger.custom] boom"
    assert isinstance(err, PreceptError)
    assert FormatError("x").code == "format.invalid"


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("DATABASE_URL", "postgresql://pl_app@localhost/precept_ledger")
    monkeypatch.setenv("KEYS_DIR", str(tmp_path))
    settings = load_settings()
    assert settings.keys_dir == tmp_path
    assert settings.demo_mode is False
    assert settings.ledger_stream == "harbor-federal/decisions"


def test_settings_fail_without_required_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KEYS_DIR", raising=False)
    with pytest.raises(ValueError, match="database_url"):
        load_settings()
