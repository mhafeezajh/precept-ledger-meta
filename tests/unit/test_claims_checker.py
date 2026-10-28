"""Claim checker (claims/checker.py): every claim about the ledger is proven or rejected (R5).

The fixture runs the real chain: Gate appends decisions and verdicts to the ledger API, three
witnesses cosign the checkpoint, and evidence comes from the proof endpoint.
"""

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from claims.checker import (
    Assertion,
    Claim,
    ClaimResult,
    Evidence,
    Trust,
    check_claims,
    check_summary,
    resolve,
    unbacked_names,
)
from gate.ledger_client import HttpLedgerClient
from tests.bundles import ORIGIN, trusted_keys_json
from tests.gate_helpers import AGENT, DEMO_PAYMENT, SMALL_PAYMENT, SUPERVISOR, rig
from tests.unit.test_api_witnessed import open_client, publish, services
from tests.unit.test_witnessing import KEYS, witnesses
from verify.keys import TrustedKeys, WitnessPolicy

TRUST = Trust(TrustedKeys.from_json(trusted_keys_json()), ORIGIN, WitnessPolicy(KEYS, 2))


@dataclass
class World:
    http: httpx.AsyncClient
    svc: Any
    decision_id: str  # the $48,000 escalated payment
    verdict_id: str  # j.ortiz's approval
    small_id: str  # the $120 allowed payment

    async def evidence(self, *record_ids: str) -> dict[str, Evidence]:
        out = {}
        for record_id in record_ids:
            record = (await self.http.get(f"/ledger/v1/records/{record_id}")).json()["record"]
            proof = (await self.http.get(f"/ledger/v1/records/{record_id}/proof")).json()
            out[record_id] = Evidence.from_api(record, proof)
        return out


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    svc = services(tmp_path, witnesses())  # type: ignore[arg-type]
    async for http in open_client(svc, None):
        r = rig()
        r.gate._ledger = HttpLedgerClient("http://test", http)  # Gate writes to the real API
        small = await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
        demo = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
        assert demo.escalation is not None
        verdict = await r.gate.verdict(demo.escalation.id, SUPERVISOR, "approve")
        await publish(svc)
        yield World(http, svc, demo.record.record_id, verdict.record_id, small.record.record_id)


def demo_claim(w: World, **changes: Any) -> Claim:
    claim = Claim(
        text="A payment of $48,000.00 to northwind-supply was approved by j.ortiz.",
        record_ids=(w.decision_id, w.verdict_id),
        assertions=(
            Assertion(w.decision_id, "body.resource.amount_cents", "eq", 4_800_000),
            Assertion(w.decision_id, "body.resource.payee_id", "eq", "northwind-supply"),
            Assertion(w.verdict_id, "body.verdict", "eq", "approve"),
            Assertion(w.verdict_id, "body.approver", "eq", "j.ortiz"),
            Assertion(w.verdict_id, "body.decision_record_id", "eq", w.decision_id),
        ),
    )
    return replace(claim, **changes)


async def test_a_true_claim_with_proofs_is_accepted(world: World) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id)
    (result,) = check_claims([demo_claim(world)], evidence, TRUST)
    assert result.accepted, result.reasons
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("assertion", "reason"),
    [
        (("verdict", "body.approver", "eq", "m.lee"), "body.approver eq 'm.lee'"),
        (("decision", "body.resource.amount_cents", "eq", 4_900_000), "amount_cents eq 4900000"),
        (("decision", "body.resource.amount_cents", "gt", 4_800_000), "gt 4800000"),
        (("decision", "body.resource.payee_new", "eq", 1), "eq 1"),  # true is not 1
        (("decision", "body.resource.memo", "eq", "x"), "field not found"),
        (("decision", "body.resource.amount_cents", "gt", "100"), "cannot compare"),
    ],
)
async def test_a_wrong_assertion_rejects_the_claim(
    world: World, assertion: tuple, reason: str
) -> None:
    which, field, op, value = assertion
    record_id = world.verdict_id if which == "verdict" else world.decision_id
    claim = demo_claim(world, assertions=(Assertion(record_id, field, op, value),))
    claim = replace(claim, text="A payment was approved.")
    (result,) = check_claims(
        [claim], await world.evidence(world.decision_id, world.verdict_id), TRUST
    )
    assert not result.accepted
    assert any(reason in r for r in result.reasons), result.reasons


async def test_comparisons_work_on_integers(world: World) -> None:
    claim = demo_claim(
        world,
        text="The payment was over $10,000.",
        assertions=(Assertion(world.decision_id, "body.resource.amount_cents", "gt", 1_000_000),),
    )
    (result,) = check_claims(
        [claim], await world.evidence(world.decision_id, world.verdict_id), TRUST
    )
    assert result.accepted, result.reasons


async def test_an_unknown_record_id_rejects_the_claim(world: World) -> None:
    ghost = "01JB6X7Q2M9ZK3T8V4N5R6S7W0"
    claim = demo_claim(world, record_ids=(world.decision_id, ghost))
    (result,) = check_claims([claim], await world.evidence(world.decision_id), TRUST)
    assert any("no evidence for record" in r for r in result.reasons)


async def test_an_assertion_about_an_uncited_record_is_rejected(world: World) -> None:
    claim = demo_claim(world, record_ids=(world.decision_id,))
    (result,) = check_claims(
        [claim], await world.evidence(world.decision_id, world.verdict_id), TRUST
    )
    assert any("not cited" in r for r in result.reasons)


async def test_a_number_in_the_text_must_be_asserted(world: World) -> None:
    claim = demo_claim(world, text="A payment of $84,000.00 was approved by j.ortiz.")
    (result,) = check_claims(
        [claim], await world.evidence(world.decision_id, world.verdict_id), TRUST
    )
    assert any("84000" in r for r in result.reasons), result.reasons


async def test_a_claim_needs_records_and_assertions(world: World) -> None:
    evidence = await world.evidence(world.decision_id)
    empty = Claim(text="Nothing.", record_ids=(), assertions=())
    (result,) = check_claims([empty], evidence, TRUST)
    assert not result.accepted
    assert "cites no record" in result.reasons[0]


def flip_b64(value: str) -> str:
    raw = bytearray(base64.b64decode(value))
    raw[0] ^= 1
    return base64.b64encode(bytes(raw)).decode()


TAMPERS = {
    "edited field": lambda e: replace(
        e, record={**e.record, "body": {**e.record["body"], "approver": "m.lee"}}
    ),
    "wrong proof hash": lambda e: replace(e, hashes=(flip_b64(e.hashes[0]), *e.hashes[1:])),
    "wrong leaf index": lambda e: replace(e, leaf_index=e.leaf_index + 1),
    "forged checkpoint": lambda e: replace(
        e, checkpoint=e.checkpoint.replace("\n\n", "\nx\n\n", 1)
    ),
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
async def test_evidence_that_does_not_prove_the_record_rejects_the_claim(
    world: World, tamper: str
) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id)
    evidence[world.verdict_id] = TAMPERS[tamper](evidence[world.verdict_id])
    (result,) = check_claims([demo_claim(world)], evidence, TRUST)
    assert not result.accepted
    assert any(world.verdict_id in r for r in result.reasons), result.reasons


async def test_a_checkpoint_below_the_witness_quorum_is_not_trusted(world: World) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id)
    unwitnessed = Trust(TRUST.keys, ORIGIN, WitnessPolicy(KEYS, 3))  # all three, but...
    stripped = {
        k: replace(
            v, checkpoint="\n".join(x for x in v.checkpoint.split("\n") if "witness-3" not in x)
        )
        for k, v in evidence.items()
    }
    (result,) = check_claims([demo_claim(world)], stripped, unwitnessed)
    assert any("not final" in r for r in result.reasons), result.reasons


async def test_records_from_an_untrusted_producer_are_rejected(world: World) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id)
    no_gate = Trust(
        TrustedKeys(producers={}, log_keys=TRUST.keys.log_keys), ORIGIN, TRUST.witnesses
    )
    (result,) = check_claims([demo_claim(world)], evidence, no_gate)
    assert any("producer" in r for r in result.reasons), result.reasons


async def test_a_record_not_yet_checkpointed_cannot_be_proven(world: World) -> None:
    evidence = await world.evidence(world.decision_id)
    late = replace(evidence[world.decision_id], tree_size=evidence[world.decision_id].leaf_index)
    claim = demo_claim(
        world,
        record_ids=(world.decision_id,),
        assertions=demo_claim(world).assertions[:2],
        text="A payment of $48,000.00 to northwind-supply.",
    )
    (result,) = check_claims([claim], {world.decision_id: late}, TRUST)
    assert not result.accepted


async def test_each_claim_is_judged_on_its_own(world: World) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id, world.small_id)
    by_lee = Assertion(world.verdict_id, "body.approver", "eq", "m.lee")
    wrong = demo_claim(world, assertions=(by_lee,), text="Approved by m.lee.")
    small = Claim(
        text="A $120.00 payment was allowed without approval.",
        record_ids=(world.small_id,),
        assertions=(
            Assertion(world.small_id, "body.resource.amount_cents", "eq", 12_000),
            Assertion(world.small_id, "body.decision", "eq", "allow"),
        ),
    )
    results = check_claims([demo_claim(world), wrong, small], evidence, TRUST)
    assert [r.accepted for r in results] == [True, False, True]


def test_field_paths_resolve_only_inside_the_record() -> None:
    record = {"body": {"resource": {"amount_cents": 5}, "ids": [1]}, "ts": "t"}
    assert resolve(record, "body.resource.amount_cents") == (True, 5)
    assert resolve(record, "ts") == (True, "t")
    assert resolve(record, "body.missing") == (False, None)
    assert resolve(record, "body.ids.0") == (False, None)  # no list indexing
    assert resolve(record, "__class__") == (False, None)


async def test_a_name_or_id_in_the_text_must_be_asserted(world: World) -> None:
    evidence = await world.evidence(world.decision_id, world.verdict_id)
    invented = demo_claim(world, text="$48,000.00 to northwind-supply was approved by m.lee.")
    unasserted = demo_claim(
        world,
        text="$48,000.00 was paid to northwind-supply by payments-agent-7.",
        assertions=demo_claim(world).assertions[:2],
    )
    results = check_claims([invented, unasserted], evidence, TRUST)
    assert [r.accepted for r in results] == [False, False]
    assert any("m.lee" in r and "names" in r for r in results[0].reasons), results[0].reasons
    assert any("payments-agent-7" in r for r in results[1].reasons), results[1].reasons


def test_plain_words_are_not_names() -> None:
    assert unbacked_names("Approved after hours, outside business hours.", ()) == []
    assertion = Assertion("x", "body.approver", "eq", "j.ortiz")
    assert unbacked_names("Approved by j.ortiz.", (assertion,)) == []
    assert unbacked_names("Approved by J.Ortiz and k.wu.", (assertion,)) == ["k.wu"]


def test_the_summary_may_use_only_what_accepted_claims_establish() -> None:
    amount = Assertion("x", "body.resource.amount_cents", "eq", 4_800_000)
    approver = Assertion("y", "body.approver", "eq", "j.ortiz")
    claim = Claim("t", ("x", "y"), (amount, approver))
    held = ClaimResult(claim, True)
    assert check_summary("One payment: $48,000.00, approved by j.ortiz.", [held]) == []
    assert check_summary("2 payments matched.", [held], numbers={2}) == []
    problems = check_summary("$84,000.00 approved by m.lee.", [held])
    assert any("84000" in p for p in problems)
    assert any("m.lee" in p for p in problems)
    rejected = ClaimResult(claim, False, ("wrong",))
    assert check_summary("$48,000.00 approved by j.ortiz.", [rejected]) != []
