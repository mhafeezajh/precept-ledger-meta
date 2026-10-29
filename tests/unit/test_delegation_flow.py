"""Delegation switched on end to end (D89): the dev principal's key, the credential make delegate
signs for payments-ops, the agent presenting it with every call, and what happens without it or
after it is revoked: denied before Cedar, and the bank never touched."""

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.cli import AgentSettings, load_credential, open_agent
from common.clock import format_ts
from common.config import Settings
from common.errors import ConfigError
from gate.delegation import (
    Delegations,
    Revocation,
    parse_credential,
    sign_credential,
    sign_revocation,
)
from gate.records import DECISION
from gate.server import load_principals
from ledger.signing import Signer
from scripts import delegate
from scripts.dev_keys import PRINCIPALS, ensure_keys
from tests.gate_helpers import rig
from tests.helpers import T0
from tests.unit.test_agent import build

PRINCIPAL = "treasury.harbor-federal"
PAY_ACME = "Pay Acme Paper $120.00 for invoice INV-2."


def test_dev_keys_create_the_principal_and_its_public_key(tmp_path: Path) -> None:
    ensure_keys(tmp_path / "keys")
    keys = tmp_path / "keys"
    pem = keys / "principals" / f"{PRINCIPAL}.pem"
    assert PRINCIPALS == (PRINCIPAL,)
    assert not pem.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ring = load_principals(keys / "principals.json")
    signer = Signer.from_pem_file(pem)
    assert ring.owner(signer.key_id) == PRINCIPAL
    ensure_keys(keys)  # a second run keeps the key
    assert Signer.from_pem_file(pem).key_id == signer.key_id


def test_the_credential_is_scoped_to_the_demo_vendors_and_the_cap() -> None:
    payees = delegate.vendor_ids()
    credential = delegate.build_credential(T0, "01JB6X7Q2M9ZK3T8V4N5R6S7W0", payees)
    assert credential.allowed_payees == tuple(payees)
    assert "northwind-supply" in payees
    assert len(payees) == 6
    assert credential.max_amount == "250000.00"
    assert credential.action_classes == ("comm.external", "data.read", "value.transfer")
    assert (credential.principal_id, credential.agent_id) == (PRINCIPAL, "payments-ops")


def guarded_rig(keys: Path) -> Any:
    ensure_keys(keys)
    r = rig()
    r.gate.delegations = Delegations(load_principals(keys / "principals.json"))
    return r


def credential_for(keys: Path, **changes: Any) -> dict[str, Any]:
    principal = Signer.from_pem_file(keys / "principals" / f"{PRINCIPAL}.pem")
    data = delegate.build_credential(T0, "01JB6X7Q2M9ZK3T8V4N5R6S7W0", delegate.vendor_ids())
    return sign_credential(principal, parse_credential({**data.to_json(), **changes})).to_json()


def decisions(r: Any) -> list[dict[str, Any]]:
    return [s.body for s in r.ledger.appended if s.type == DECISION]


async def test_the_agent_presents_its_credential_and_pays(tmp_path: Path) -> None:
    r = guarded_rig(tmp_path / "keys")
    envelope = credential_for(tmp_path / "keys")
    await r.gate.issue_delegation(envelope)
    agent = build(tmp_path, r)
    agent._governor.credential = envelope  # what open_agent does with the saved file
    run = await agent.run(PAY_ACME)
    assert run.stopped is None
    assert [s.outcome for s in run.steps] == ["ok", "ok", "ok"]
    assert {d["delegation"]["result"] for d in decisions(r)} == {"ok"}
    assert {d["delegation"]["principal_id"] for d in decisions(r)} == {PRINCIPAL}


async def test_without_a_credential_nothing_runs(tmp_path: Path) -> None:
    r = guarded_rig(tmp_path / "keys")
    agent = build(tmp_path, r)
    run = await agent.run(PAY_ACME)
    assert [(s.tool, s.outcome, s.code) for s in run.steps] == [
        ("lookup_vendor", "denied", "delegation.missing")
    ]
    assert (agent.bank.lookups, agent.bank.payments, agent.bank.outbox) == ([], [], [])


async def test_a_revoked_credential_stops_the_agent(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    r = guarded_rig(keys)
    envelope = credential_for(keys)
    await r.gate.issue_delegation(envelope)
    principal = Signer.from_pem_file(keys / "principals" / f"{PRINCIPAL}.pem")
    credential = parse_credential(envelope["credential"])
    from gate.delegation import credential_hash

    revocation = Revocation(credential.credential_id, credential_hash(credential), PRINCIPAL,
                            format_ts(T0))  # fmt: skip
    await r.gate.revoke_delegation(sign_revocation(principal, revocation).to_json())
    agent = build(tmp_path, r)
    agent._governor.credential = envelope
    run = await agent.run(PAY_ACME)
    assert [s.code for s in run.steps] == ["delegation.revoked"]
    assert agent.bank.payments == []


async def test_a_payment_beyond_the_credential_is_denied_even_where_policy_would_hold(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    r = guarded_rig(keys)
    narrow = credential_for(keys, max_amount="100.00")
    await r.gate.issue_delegation(narrow)
    agent = build(tmp_path, r)
    agent._governor.credential = narrow
    run = await agent.run(PAY_ACME)
    assert [(s.tool, s.code) for s in run.steps] == [
        ("lookup_vendor", None), ("create_payment", "delegation.out_of_scope"),
    ]  # fmt: skip
    assert agent.bank.payments == []


# --- pl-agent loads the saved credential ------------------------------------------------------


def test_the_saved_credential_is_loaded_or_refused(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    ensure_keys(keys)
    path = delegate.credential_file(keys)
    assert load_credential(path) is None  # none saved: the agent presents none
    delegate.save(path, credential_for(keys))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_credential(path)
    assert loaded is not None
    assert loaded["credential"]["principal_id"] == PRINCIPAL
    path.write_text('{"credential": "junk"}')
    with pytest.raises(ConfigError) as caught:
        load_credential(path)
    assert caught.value.code == "config.credential"


async def test_open_agent_hands_the_credential_to_its_governor(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    ensure_keys(keys)
    envelope = credential_for(keys)
    delegate.save(delegate.credential_file(keys), envelope)
    root = Path(__file__).resolve().parents[2]
    settings = Settings(_env_file=None, database_url="postgresql://x", keys_dir=keys,  # type: ignore[call-arg]  # values, not env
                        model_profile="mock", meta_api_key=None,
                        model_call_log=tmp_path / "calls.csv")  # fmt: skip
    agent_settings = AgentSettings(_env_file=None, vendors_file=root / "deploy/demo/vendors.json")  # type: ignore[call-arg]  # values, not env
    async with open_agent(settings, agent_settings) as agent:
        assert agent._governor.credential == envelope


# --- make delegate: issue, revoke and status, against Gate in process --------------------------


async def test_make_delegate_issues_revokes_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "keys"
    r = guarded_rig(keys)
    calls: list[tuple[str, Any]] = []

    def post(path: str, body: dict[str, Any], token: str) -> httpx.Response:
        calls.append((path, body))
        return httpx.Response(201, json={"record": {"type": "x", "record_id": "01Z"}})

    def get(url: str, **_: Any) -> httpx.Response:
        found = r.gate.delegations.find(url.rsplit("/", 1)[1])
        state = "revoked" if found and found[1] else "active"
        return httpx.Response(200, json={"state": state})

    monkeypatch.setattr(delegate, "_post", post)
    monkeypatch.setattr(delegate.httpx, "get", get)
    assert delegate.status(keys, "t") == 1  # nothing saved yet
    assert delegate.issue(keys, "test-only-token") == 0
    issued = calls[-1][1]
    await r.gate.issue_delegation(issued)  # what Gate does with the POST
    assert delegate.status(keys, "t") == 0
    assert delegate.revoke(keys, "t") == 0
    await r.gate.revoke_delegation(calls[-1][1])
    assert delegate.status(keys, "t") == 0
    out = capsys.readouterr().out
    assert f"{PRINCIPAL} delegated to payments-ops" in out
    assert ": revoked, until" in out
    saved = json.loads(delegate.credential_file(keys).read_text())
    assert saved == issued
    not_before = datetime.fromisoformat(saved["credential"]["not_before"].replace("Z", "+00:00"))
    assert not_before <= datetime.now(UTC)
    refusal = httpx.Response(400, json={"error": {"code": "delegation.bad_signature"}})
    monkeypatch.setattr(delegate, "_post", lambda *a: refusal)
    assert delegate.issue(keys, "t") == 1
    assert delegate.revoke(keys, "t") == 1
