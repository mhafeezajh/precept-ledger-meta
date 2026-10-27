"""Key loading for pl-ledger and the dev key script: fail closed, never inside the repo."""

import json
import stat
from pathlib import Path

import pytest

from common.errors import ConfigError
from ledger.server import load_keys
from scripts.dev_keys import ROOT, ensure_keys
from scripts.dev_keys import main as dev_keys_main


def test_dev_keys_then_load(tmp_path: Path) -> None:
    signers = ensure_keys(tmp_path / "keys")
    log, producers = load_keys(tmp_path / "keys")
    assert log.key_id == signers["log"].key_id
    assert producers.owner(signers["gate-1"].key_id) == "gate-1"
    mode = (tmp_path / "keys" / "log.pem").stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_dev_keys_never_overwrite(tmp_path: Path) -> None:
    first = ensure_keys(tmp_path)
    second = ensure_keys(tmp_path)
    assert {n: s.key_id for n, s in first.items()} == {n: s.key_id for n, s in second.items()}


def test_dev_keys_refuse_the_repository() -> None:
    with pytest.raises(SystemExit, match="refusing"):
        ensure_keys(ROOT / "keys")


def test_dev_keys_cli_prints_only_key_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dev_keys_main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ed25519:" in out
    assert "PRIVATE" not in out


def test_missing_keys_fail(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_keys(tmp_path)


def test_log_key_not_in_trusted_file_fails(tmp_path: Path) -> None:
    ensure_keys(tmp_path)
    trusted = json.loads((tmp_path / "trusted.json").read_text())
    trusted["log"] = trusted["producers"]["gate-1"]
    (tmp_path / "trusted.json").write_text(json.dumps(trusted))
    with pytest.raises(ConfigError, match="not a log key"):
        load_keys(tmp_path)
