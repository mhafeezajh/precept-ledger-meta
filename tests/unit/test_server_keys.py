"""Key loading for pl-ledger and the dev key script: fail closed, never inside the repo."""

import json
import stat
from pathlib import Path

import pytest

from common.errors import ConfigError
from ledger.server import load_keys, load_witness_set, parse_witnesses
from ledger.witnessing import MemoryCosignatureStore
from scripts.dev_keys import ROOT, ensure_keys
from scripts.dev_keys import main as dev_keys_main
from verify.keys import WitnessPolicy


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


def test_dev_keys_create_witness_keys_and_public_files(tmp_path: Path) -> None:
    from ledger.checkpoint import NoteKey
    from witness.keys import load_log_keys, load_signer

    origin = "precept-ledger.local/harbor-federal/decisions"
    signers = ensure_keys(tmp_path, origin)
    for n in (1, 2, 3):
        path = tmp_path / "witnesses" / f"witness-{n}.pem"
        assert load_signer(path).public_key_raw == signers[f"witness-{n}"].public_key_raw
    assert load_log_keys(tmp_path / "log-verifiers.txt") == [
        NoteKey(origin, signers["log"].public_key_raw)
    ]
    policy = WitnessPolicy.from_text((tmp_path / "witnesses.txt").read_bytes(), 2)
    assert [k.name for k in policy.keys] == ["witness-1", "witness-2", "witness-3"]
    for name in ("witnesses.txt", "log-verifiers.txt", "trusted.json"):
        assert "PRIVATE" not in (tmp_path / name).read_text()


def test_parse_witnesses() -> None:
    assert parse_witnesses("") == []
    assert parse_witnesses("witness-1=http://127.0.0.1:8091, witness-2=https://w2.example") == [
        ("witness-1", "http://127.0.0.1:8091"),
        ("witness-2", "https://w2.example"),
    ]
    for bad in ("witness-1", "=http://x", "w=ftp://x", "w=http://a,w=http://b"):
        with pytest.raises(ConfigError):
            parse_witnesses(bad)


def test_load_witness_set_checks_names_and_quorum(tmp_path: Path) -> None:
    ensure_keys(tmp_path, "o.example/s")
    store = MemoryCosignatureStore()
    witness_set = load_witness_set(tmp_path, ["witness-1", "witness-3"], 2, store)
    assert witness_set.quorum == 2
    with pytest.raises(ConfigError):
        load_witness_set(tmp_path, ["witness-9"], 2, store)
    with pytest.raises(ConfigError):
        load_witness_set(tmp_path, ["witness-1"], 4, store)
    with pytest.raises(ConfigError):
        load_witness_set(tmp_path / "missing", ["witness-1"], 2, store)


def test_anchor_view_from_settings(tmp_path: Path) -> None:
    import httpx

    from common.config import Settings
    from ledger.anchoring import MemoryAnchorStore
    from ledger.server import anchor_view, parse_urls

    def settings(**values: object) -> Settings:
        return Settings(database_url="postgresql://x", keys_dir=tmp_path, **values)  # type: ignore[arg-type]

    http, store = httpx.AsyncClient(), MemoryAnchorStore()
    assert anchor_view(settings(), store, http) is None
    view = anchor_view(
        settings(tsas="digicert=http://timestamp.digicert.com",
                 ots_calendars="https://a.pool.opentimestamps.org"),
        store,
        http,
    )  # fmt: skip
    assert view is not None
    assert view.anchorer.requires_timestamp
    assert len(view.tsa_roots) == 2  # deploy/tsa/roots.pem
    with pytest.raises(ConfigError):
        anchor_view(
            settings(tsas="digicert=http://x", tsa_roots_file=tmp_path / "none"), store, http
        )
    (tmp_path / "empty.pem").write_text("")
    with pytest.raises(ConfigError):
        anchor_view(settings(tsas="d=http://x", tsa_roots_file=tmp_path / "empty.pem"), store, http)
    with pytest.raises(ConfigError):
        parse_urls("http://plain.example", "OTS_CALENDARS")
    assert parse_urls(" https://a.example/ ,", "X") == ["https://a.example"]


def test_dev_keys_give_gate_its_key_and_callers(tmp_path: Path) -> None:
    from gate.auth import APPROVER
    from gate.server import load_gate_keys

    signers = ensure_keys(tmp_path)
    signer, log_keys, callers = load_gate_keys(tmp_path, "gate-1")
    assert signer.key_id == signers["gate-1"].key_id
    assert log_keys.owner(signers["log"].key_id) == "log"
    token_file = tmp_path / "tokens" / "j.ortiz.token"
    assert not token_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    caller = callers.authenticate(f"Bearer {token_file.read_text().strip()}", kind=APPROVER)
    assert caller.roles == frozenset({"payments-supervisor"})
    before = (tmp_path / "gate-callers.txt").read_text()
    ensure_keys(tmp_path)
    assert (tmp_path / "gate-callers.txt").read_text() == before  # tokens never change


def test_dev_keys_cli_never_prints_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dev_keys_main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for token_file in (tmp_path / "tokens").iterdir():
        assert token_file.read_text().strip() not in out


def test_gate_refuses_a_key_the_trusted_file_does_not_list(tmp_path: Path) -> None:
    from gate.server import load_gate_keys

    ensure_keys(tmp_path)
    trusted = json.loads((tmp_path / "trusted.json").read_text())
    trusted["producers"]["gate-1"] = trusted["log"]
    (tmp_path / "trusted.json").write_text(json.dumps(trusted))
    with pytest.raises(ConfigError, match="not listed"):
        load_gate_keys(tmp_path, "gate-1")
    with pytest.raises(ConfigError):
        load_gate_keys(tmp_path / "missing", "gate-1")
