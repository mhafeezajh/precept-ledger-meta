"""Delegation credentials (gate/delegation.py and Gate.decide): a principal's signed, scoped
grant to an agent, checked before Cedar. Every failure is a recorded deny and the tool never
runs. Issue and revoke are ledger records, and revocation is derived from the ledger alone.
Keys are TEST ONLY (tests/keys/test-keys.json)."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from common.clock import format_ts
from common.errors import GateError
from gate.api import create_gate_app
from gate.auth import Callers, token_hash
from gate.core import Gate
from gate.delegation import (
    ISSUE,
    REVOKE,
    Credential,
    Delegations,
    Revocation,
    check_scope,
    credential_hash,
    credential_payload,
    parse_credential,
    parse_signed_credential,
    parse_signed_revocation,
    sign_credential,
    sign_revocation,
    verify_signed_credential,
)
from gate.governed import GateDenied
from gate.records import DECISION
from ledger.format import b64decode, b64encode
from ledger.signing import KeyRing
from tests.gate_helpers import AGENT, GATE, LOG_KEYS, LocalEndpoint, governor, rig
from tests.helpers import STREAM, T0, signer_for

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "tests" / "vectors" / "delegation-v1.json"
PRINCIPAL, OTHER = signer_for("principal-1"), signer_for("principal-2")
PRINCIPAL_ID, OTHER_ID = "treasury.harbor-federal", "ops.harbor-federal"
PRINCIPALS = KeyRing([(PRINCIPAL_ID, PRINCIPAL.public_key_raw), (OTHER_ID, OTHER.public_key_raw)])
CREDENTIAL_ID = "01JB6X7Q2M9ZK3T8V4N5R6S7W0"
PAY = {"payee_id": "acme-paper", "amount": "120.00", "currency": "USD"}


def grant(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "credential_id": CREDENTIAL_ID,
        "principal_id": PRINCIPAL_ID,
        "agent_id": AGENT,
        "action_classes": ["data.read", "value.transfer"],
        "max_amount": "5000.00",
        "allowed_payees": ["acme-paper", "keystone-it"],
        "not_before": format_ts(T0 - timedelta(hours=1)),
        "expires_at": format_ts(T0 + timedelta(days=30)),
    }
    return {**data, **changes}


def signed(signer: Any = PRINCIPAL, **changes: Any) -> dict[str, Any]:
    return sign_credential(signer, parse_credential(grant(**changes))).to_json()


# --- the credential format -------------------------------------------------------------------


def test_the_credential_parses_strictly() -> None:
    credential = parse_credential(grant())
    assert credential.max_amount_cents == 500_000
    assert credential.to_json() == grant()
    bad = [
        {**grant(), "extra": 1},
        {k: v for k, v in grant().items() if k != "max_amount"},
        grant(max_amount=5000),  # a number, not an exact decimal string
        grant(max_amount="5000.5"),
        grant(action_classes=["value.transfer", "value.transfer"]),
        grant(action_classes=["value.teleport"]),
        grant(action_classes=[]),
        grant(allowed_payees=["acme paper"]),
        grant(agent_id="Payments Ops"),
        grant(credential_id="not-a-ulid"),
        grant(not_before="2026-10-29"),
        grant(expires_at=format_ts(T0 - timedelta(hours=2))),  # before not_before
        grant(expires_at=format_ts(T0 + timedelta(days=400))),  # longer than 90 days
    ]
    for data in bad:
        with pytest.raises(GateError) as caught:
            parse_credential(data)
        assert caught.value.code == "delegation.malformed", data


def test_vectors_fix_the_signed_bytes_hash_and_signature() -> None:
    vector = json.loads(VECTORS.read_text())
    credential = parse_credential(vector["credential"])
    assert credential_payload(credential).hex() == vector["signing_payload_hex"]
    assert credential_hash(credential) == vector["credential_hash"]
    envelope = sign_credential(signer_for(vector["signer"]), credential).to_json()
    assert envelope == vector["signed"]  # Ed25519 is deterministic
    revocation = parse_signed_revocation(vector["revocation"])
    assert revocation.revocation.credential_hash == vector["credential_hash"]


def test_a_signature_must_be_the_named_principals() -> None:
    good = parse_signed_credential(signed())
    verify_signed_credential(good, PRINCIPALS)
    cases = [
        signed(OTHER),  # signed by another principal's key
        {**signed(), "alg": "rsa"},
        {**signed(), "sig": b64encode(bytes(64))},
    ]
    for envelope in cases:
        with pytest.raises(GateError) as caught:
            verify_signed_credential(parse_signed_credential(envelope), PRINCIPALS)
        assert caught.value.code == "delegation.bad_signature"
    tampered = signed()
    tampered["credential"]["max_amount"] = "500000.00"  # widened after signing
    with pytest.raises(GateError) as widened:
        verify_signed_credential(parse_signed_credential(tampered), PRINCIPALS)
    assert widened.value.code == "delegation.bad_signature"
    with pytest.raises(GateError) as unknown:
        verify_signed_credential(good, KeyRing([(OTHER_ID, OTHER.public_key_raw)]))
    assert unknown.value.code == "delegation.bad_signature"


@pytest.mark.parametrize(
    ("changes", "agent", "action", "facts", "at", "code"),
    [
        (
            {},
            AGENT,
            "value.transfer",
            {"amount_cents": 500_000, "payee_id": "acme-paper", "currency": "USD"},
            T0,
            None,
        ),
        ({}, "other-agent", "value.transfer", {}, T0, "delegation.wrong_agent"),
        ({}, AGENT, "comm.external", {}, T0, "delegation.out_of_scope"),
        (
            {},
            AGENT,
            "value.transfer",
            {"amount_cents": 500_001, "payee_id": "acme-paper", "currency": "USD"},
            T0,
            "delegation.out_of_scope",
        ),
        (
            {},
            AGENT,
            "value.transfer",
            {"amount_cents": 100, "payee_id": "northwind-supply", "currency": "USD"},
            T0,
            "delegation.out_of_scope",
        ),
        (
            {},
            AGENT,
            "value.transfer",
            {"amount_cents": 100, "payee_id": "acme-paper", "currency": "EUR"},
            T0,
            "delegation.out_of_scope",
        ),
        ({}, AGENT, "value.transfer", {}, T0, "delegation.out_of_scope"),  # no amount: not provable
        (
            {},
            AGENT,
            "data.read",
            {"data_class": "vendor"},
            T0 - timedelta(hours=2),
            "delegation.not_yet_valid",
        ),
        ({}, AGENT, "data.read", {}, T0 + timedelta(days=30), "delegation.expired"),
    ],
)
def test_scope_is_checked_field_by_field(
    changes: dict[str, Any], agent: str, action: str, facts: dict[str, Any], at: Any, code: Any
) -> None:
    credential = parse_credential(grant(**changes))
    assert check_scope(credential, agent=agent, action=action, facts=facts, now=at) == code


# --- Gate: every failure is a recorded deny, before Cedar, and the tool never runs ------------


def guarded() -> Any:
    r = rig()
    r.gate.delegations = Delegations(PRINCIPALS)
    return r


async def issue(r: Any, envelope: dict[str, Any] | None = None) -> dict[str, Any]:
    envelope = envelope or signed()
    await r.gate.issue_delegation(envelope)
    return envelope


async def attempt(r: Any, credential: dict[str, Any] | None, arguments: dict[str, Any] = PAY,
                  tool: str = "create_payment") -> tuple[Any, list[str]]:  # fmt: skip
    """Run one call through the governor; return the error (or result) and what the tool saw."""
    ran: list[str] = []

    async def effect() -> str:
        ran.append(tool)
        return "paid"

    gov = governor(LocalEndpoint(r.gate), r.clock)
    gov.credential = credential
    try:
        return await gov.run(tool, arguments, effect), ran
    except GateDenied as exc:
        return exc, ran


def last_decision(r: Any) -> dict[str, Any]:
    return next(s.body for s in reversed(r.ledger.appended) if s.type == DECISION)


async def test_each_failure_denies_before_cedar_and_the_tool_never_runs() -> None:
    r = guarded()
    good = await issue(r)
    other = await issue(r, signed(credential_id="01JB6X7Q2M9ZK3T8V4N5R6S7W1"))
    await r.gate.revoke_delegation(sign_revocation(PRINCIPAL, Revocation(
        "01JB6X7Q2M9ZK3T8V4N5R6S7W1", credential_hash(parse_credential(other["credential"])),
        PRINCIPAL_ID, format_ts(T0))).to_json())  # fmt: skip
    widened = json.loads(json.dumps(good))
    widened["credential"]["max_amount"] = "900000.00"
    never_issued = signed(credential_id="01JB6X7Q2M9ZK3T8V4N5R6S7W2")
    expired = await issue(r, signed(credential_id="01JB6X7Q2M9ZK3T8V4N5R6S7W3",
                                    not_before=format_ts(T0 - timedelta(days=3)),
                                    expires_at=format_ts(T0 - timedelta(days=1))))  # fmt: skip
    big = {**PAY, "amount": "5000.01"}
    elsewhere = {**PAY, "payee_id": "northwind-supply"}
    email = {"to": ["ap@acme-paper.example"], "subject": "s", "body": "b"}
    cases = [
        (None, PAY, "create_payment", "delegation.missing"),
        ({"credential": "junk"}, PAY, "create_payment", "delegation.malformed"),
        (widened, PAY, "create_payment", "delegation.bad_signature"),
        (never_issued, PAY, "create_payment", "delegation.not_issued"),
        (expired, PAY, "create_payment", "delegation.expired"),
        (other, PAY, "create_payment", "delegation.revoked"),
        (good, big, "create_payment", "delegation.out_of_scope"),
        (good, elsewhere, "create_payment", "delegation.out_of_scope"),
        (good, email, "send_email", "delegation.out_of_scope"),
    ]
    for credential, arguments, tool, code in cases:
        result, ran = await attempt(r, credential, arguments, tool)
        assert isinstance(result, GateDenied), code
        assert result.code == code
        assert ran == []  # never called
        body = last_decision(r)
        assert (body["decision"], body["reason"], body["policy_ids"]) == ("deny", code, [])
        assert body["args_digest"].startswith("sha256:")
        assert body["policy_set"] == r.gate.policies.digest
    # Signed by another agent's principal for another agent: the agent must match too.
    for_other = await issue(
        r, signed(credential_id="01JB6X7Q2M9ZK3T8V4N5R6S7W4", agent_id="treasury-bot")
    )
    result, ran = await attempt(r, for_other)
    assert (result.code, ran) == ("delegation.wrong_agent", [])


async def test_a_valid_credential_reaches_cedar_and_the_record_binds_it() -> None:
    r = guarded()
    good = await issue(r)
    result, ran = await attempt(r, good)
    assert (result, ran) == ("paid", ["create_payment"])
    body = last_decision(r)
    assert body["decision"] == "allow"
    assert body["policy_ids"] == ["pay-known-payee-01"]
    expected = credential_hash(parse_credential(good["credential"]))
    assert body["delegation"] == {
        "credential_id": CREDENTIAL_ID, "credential_hash": expected,
        "principal_id": PRINCIPAL_ID, "result": "ok",
    }  # fmt: skip
    assert body["resource"]["amount_cents"] == 12_000  # the typed facts Cedar saw
    assert body["policy_set"] == r.gate.policies.digest
    # Inside the delegation, Cedar still decides: a new payee is still held for approval.
    new_payee = {**PAY, "payee_id": "keystone-it", "amount": "4000.00"}
    held = await r.gate.decide(AGENT, "create_payment", new_payee, credential=good)
    assert held.outcome == "escalate"


async def test_without_a_delegation_guard_gate_is_unchanged() -> None:
    r = rig()
    decided = await r.gate.decide(AGENT, "create_payment", PAY)
    assert decided.outcome == "allow"
    assert decided.record.body["delegation"] is None


# --- issue and revoke: ledger records, and revocation derived from the ledger -----------------


async def test_issue_and_revoke_are_ledger_records_and_survive_a_restart() -> None:
    r = guarded()
    good = await issue(r)
    with pytest.raises(GateError) as twice:
        await r.gate.issue_delegation(good)
    assert twice.value.code == "delegation.already_issued"
    with pytest.raises(GateError) as forged:
        await r.gate.issue_delegation(signed(OTHER, credential_id="01JB6X7Q2M9ZK3T8V4N5R6S7W5"))
    assert forged.value.code == "delegation.bad_signature"
    digest = credential_hash(parse_credential(good["credential"]))
    by_other = sign_revocation(OTHER, Revocation(CREDENTIAL_ID, digest, OTHER_ID, format_ts(T0)))
    with pytest.raises(GateError) as not_theirs:
        await r.gate.revoke_delegation(by_other.to_json())
    assert not_theirs.value.code == "delegation.bad_signature"
    unknown = sign_revocation(PRINCIPAL, Revocation(CREDENTIAL_ID, "sha256:" + "0" * 64,
                                                    PRINCIPAL_ID, format_ts(T0)))  # fmt: skip
    with pytest.raises(GateError) as missing:
        await r.gate.revoke_delegation(unknown.to_json())
    assert missing.value.code == "delegation.not_issued"
    revocation = sign_revocation(PRINCIPAL, Revocation(CREDENTIAL_ID, digest, PRINCIPAL_ID,
                                                       format_ts(T0)))  # fmt: skip
    record = await r.gate.revoke_delegation(revocation.to_json())
    assert record.submission.type == REVOKE
    assert record.body["credential_hash"] == digest
    with pytest.raises(GateError) as again:
        await r.gate.revoke_delegation(revocation.to_json())
    assert again.value.code == "delegation.revoked"
    types = [s.type for s in r.ledger.appended]
    assert types.count(ISSUE) == 1
    assert types.count(REVOKE) == 1

    # A new Gate on the same ledger knows only what the ledger says: still revoked.
    fresh = rig()
    fresh.gate._ledger = r.gate._ledger
    fresh.gate.delegations = Delegations(PRINCIPALS)
    restored = await fresh.gate.restore_delegations(await r.ledger.store.read_raw(STREAM))
    assert restored == 2
    result, ran = await attempt(fresh, good)
    assert (result.code, ran) == ("delegation.revoked", [])


async def test_a_restore_refuses_a_record_gate_did_not_sign() -> None:
    r = guarded()
    await issue(r)
    records = await r.ledger.store.read_raw(STREAM)
    forged = [{**records[0], "record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7ZZ"}]
    fresh = guarded()
    fresh.gate._ledger = r.gate._ledger
    with pytest.raises(GateError):
        await fresh.gate.restore_delegations(forged)


# --- the HTTP API ------------------------------------------------------------------------------

AGENT_TOKEN, OFFICER_TOKEN = (
    "test-only-delegation-agent",
    "test-only-delegation-officer",
)  # TEST ONLY
CALLERS = Callers.from_text(
    f"agent {AGENT} - {token_hash(AGENT_TOKEN)}\n"
    f"approver a.chen compliance-officer {token_hash(OFFICER_TOKEN)}\n"
)


@asynccontextmanager
async def api() -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    r = guarded()

    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield r.gate, CALLERS

    app = create_gate_app(open_gate, sweep_interval=None)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gate") as http,
    ):
        yield r, http


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_the_api_issues_revokes_and_decides_with_a_credential() -> None:
    async with api() as (r, http):
        good = signed()
        issued = await http.post("/gate/v1/delegations", json=good, headers=auth(OFFICER_TOKEN))
        assert issued.status_code == 201, issued.text
        assert issued.json()["record"]["type"] == ISSUE
        by_agent = await http.post("/gate/v1/delegations", json=good, headers=auth(AGENT_TOKEN))
        assert by_agent.status_code == 403  # an agent cannot issue its own authority
        status = await http.get(
            f"/gate/v1/delegations/{CREDENTIAL_ID}", headers=auth(OFFICER_TOKEN)
        )
        assert status.json()["state"] == "active"
        body = {"tool": "create_payment", "arguments": PAY, "credential": good}
        decided = await http.post("/gate/v1/decide", json=body, headers=auth(AGENT_TOKEN))
        assert decided.json()["decision"] == "allow"
        missing = await http.post("/gate/v1/decide", json={"tool": "create_payment",
                                                           "arguments": PAY},
                                  headers=auth(AGENT_TOKEN))  # fmt: skip
        assert (missing.json()["decision"], missing.json()["reason"]) == (
            "deny",
            "delegation.missing",
        )
        digest = credential_hash(parse_credential(good["credential"]))
        revocation = sign_revocation(PRINCIPAL, Revocation(CREDENTIAL_ID, digest, PRINCIPAL_ID,
                                                           format_ts(T0))).to_json()  # fmt: skip
        revoked = await http.post("/gate/v1/delegations/revocations", json=revocation,
                                  headers=auth(OFFICER_TOKEN))  # fmt: skip
        assert revoked.status_code == 201
        status = await http.get(
            f"/gate/v1/delegations/{CREDENTIAL_ID}", headers=auth(OFFICER_TOKEN)
        )
        assert status.json()["state"] == "revoked"
        after = await http.post("/gate/v1/decide", json=body, headers=auth(AGENT_TOKEN))
        assert after.json()["reason"] == "delegation.revoked"
        junk = await http.post("/gate/v1/delegations", json={"credential": 1},
                               headers=auth(OFFICER_TOKEN))  # fmt: skip
        assert (junk.status_code, junk.json()["error"]["code"]) == (400, "delegation.malformed")
        unknown = await http.get("/gate/v1/delegations/01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                                 headers=auth(OFFICER_TOKEN))  # fmt: skip
        assert unknown.status_code == 404
        assert r.gate.delegations is not None


def test_envelopes_round_trip_and_reject_extra_fields() -> None:
    envelope = signed()
    assert parse_signed_credential(envelope).to_json() == envelope
    with pytest.raises(GateError):
        parse_signed_credential({**envelope, "note": "x"})
    sig = b64decode(envelope["sig"], size=64, field="sig")
    assert len(sig) == 64
    assert isinstance(parse_signed_credential(envelope).credential, Credential)
    assert GATE.key_id != PRINCIPAL.key_id
    assert len(LOG_KEYS) == 1


def test_principal_keys_load_from_their_own_file(tmp_path: Path) -> None:
    from common.errors import ConfigError
    from gate.server import load_principals

    path = tmp_path / "principals.json"
    path.write_text(json.dumps({"v": 1, "principals": {
        PRINCIPAL_ID: [b64encode(PRINCIPAL.public_key_raw)]}}))  # fmt: skip
    ring = load_principals(path)
    assert ring.owner(PRINCIPAL.key_id) == PRINCIPAL_ID
    for bad in ({"v": 2, "principals": {}}, {"v": 1, "principals": {PRINCIPAL_ID: ["!!"]}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ConfigError):
            load_principals(path)
    with pytest.raises(ConfigError):
        load_principals(tmp_path / "missing.json")
