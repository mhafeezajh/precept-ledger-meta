"""pl-agent wiring (agent/cli.py): keys from KEYS_DIR, the active model profile, progress lines.

Settings are built with ``_env_file=None`` so the developer's .env never leaks in.
"""

import json
from base64 import b64encode
from pathlib import Path
from typing import Any

import pytest

from agent import cli
from agent.cli import AgentSettings, Printer, open_agent
from agent.steps import AgentStep, StepRecord
from common.config import Settings
from common.errors import ConfigError
from tests.gate_helpers import GATE, LOG

ROOT = Path(__file__).resolve().parents[2]


def settings(tmp_path: Path, **values: Any) -> Settings:
    values = {"model_profile": "mock", "meta_api_key": None,
              "model_call_log": tmp_path / "calls.csv", **values}  # fmt: skip
    return Settings(_env_file=None, database_url="postgresql://x", keys_dir=tmp_path, **values)  # type: ignore[call-arg]  # values, not env


def keys_dir(tmp_path: Path) -> Path:
    trusted = {"v": 1, "producers": {"gate-1": [b64encode(GATE.public_key_raw).decode()]},
               "log": [b64encode(LOG.public_key_raw).decode()]}  # fmt: skip
    (tmp_path / "trusted.json").write_text(json.dumps(trusted))
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "payments-ops.token").write_text("test-only-token\n")
    return tmp_path


def agent_settings(**values: Any) -> AgentSettings:
    return AgentSettings(_env_file=None, vendors_file=ROOT / "deploy/demo/vendors.json", **values)  # type: ignore[call-arg]  # values, not env


async def test_open_agent_wires_the_mock_planner_and_the_demo_bank(tmp_path: Path) -> None:
    keys_dir(tmp_path)
    async with open_agent(settings(tmp_path), agent_settings()) as agent:
        assert agent.bank.payments == []
        assert agent._planner.model == "mock/scripted"  # type: ignore[attr-defined]  # a peek in a test


async def test_open_agent_refuses_without_keys_or_a_model(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        async with open_agent(settings(tmp_path), agent_settings()):
            pass
    assert caught.value.code == "config.keys"
    keys_dir(tmp_path)
    with pytest.raises(ConfigError) as caught:
        async with open_agent(settings(tmp_path, model_profile="cloud"), agent_settings()):
            pass
    assert caught.value.code == "config.model_profile"


async def test_with_no_gate_listening_the_cli_reports_and_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keys_dir(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings(tmp_path))
    monkeypatch.setattr(cli, "AgentSettings", lambda: agent_settings(gate_url="http://127.0.0.1:9"))
    assert await cli.run("Pay Acme Paper $120.00 for invoice INV-1.") == 0
    out = capsys.readouterr().out
    assert "1. lookup_vendor {'query': 'Acme Paper'}" in out
    assert "denied (Gate not reached): gate.unreachable" in out
    assert "reply: I did not pay anyone" in out
    assert "test-only-token" not in out


def test_the_printer_shows_holds_approvals_and_codes(capsys: pytest.CaptureFixture[str]) -> None:
    printer = Printer()
    step = AgentStep(
        note="Pay them.",
        tool="create_payment",
        lookup_vendor=None,
        create_payment={"payee_id": "a-b", "amount": "1.00", "currency": "USD", "memo": ""},
        send_email=None,
        reply="",
    )  # type: ignore[arg-type]  # validated by pydantic
    printer.started(2, step)
    printer.held(2, "01DEC")
    printer.finished(StepRecord(2, "", "create_payment", {}, "ok", {"payment_id": "pay-1"}, None,
                                "escalate", "01DEC", "01VER"))  # fmt: skip
    out = capsys.readouterr().out
    assert "why: Pay them." in out
    assert "held by Gate: decision 01DEC; waiting for an approver" in out
    assert "ok (Gate escalate, approved in 01VER): {'payment_id': 'pay-1'}" in out
