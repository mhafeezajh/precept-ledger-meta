"""Proven log and code-run queries (claims/log.py, claims/query.py): "every payment" is a
statement about the whole log, so the whole export up to a final checkpoint is verified before
a query runs over it, and claims are checked against those proven records."""

import dataclasses
from datetime import timedelta

import pytest

from claims.checker import Assertion, Claim, Trust, check_claims_in_log
from claims.log import ProvenLog, prove_log
from claims.query import DECISION, MAX_ROWS, VERDICT, Query, build_rows, run_query
from gate import records as gate_records
from gate.ledger_client import HttpLedgerClient
from tests.bundles import ORIGIN
from tests.gate_helpers import AGENT, SMALL_PAYMENT, rig
from tests.helpers import STREAM, T0
from tests.unit.test_claims_checker import TRUST, World, demo_claim, world
from verify.bundle import Bundle, read_bundle
from verify.keys import TrustedKeys

__all__ = ["world"]  # fixture
NOW = T0 + timedelta(hours=1)
EVERY = Query(
    action="any", decision="any", verdict="any", min_amount_cents=None, max_amount_cents=None,
    outside_business_hours="any", approver=None, payee_id=None, since_days=None,
)  # fmt: skip


async def export(w: World) -> Bundle:
    response = await w.http.get("/ledger/v1/export", params={"stream": STREAM})
    return read_bundle(response.content)


async def proven(w: World, trust: Trust = TRUST) -> ProvenLog:
    log = prove_log(await export(w), trust)
    assert isinstance(log, ProvenLog), log
    return log


async def test_the_whole_export_is_proven_up_to_the_final_checkpoint(world: World) -> None:
    log = await proven(world)
    assert log.size == len(log.records) == 3  # $120, $48,000, the verdict
    assert log.newer == 0
    assert [r["seq"] for r in log.records] == [1, 2, 3]
    assert log.get(world.verdict_id) is not None
    assert log.get("01JB6X7Q2M9ZK3T8V4N5R6S7W0") is None


async def test_records_after_the_final_checkpoint_are_left_out(world: World) -> None:
    r = rig()
    r.gate._ledger = HttpLedgerClient("http://test", world.http)
    late = await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
    log = await proven(world)
    assert (log.size, log.newer) == (3, 1)
    assert log.get(late.record.record_id) is None


def tampered(bundle: Bundle, seq: int) -> Bundle:
    line = bundle.records[seq - 1].replace(b"j.ortiz", b"m.lee0")
    records = (*bundle.records[: seq - 1], line, *bundle.records[seq:])
    return dataclasses.replace(bundle, records=records)


async def test_an_export_that_does_not_verify_proves_nothing(world: World) -> None:
    bundle = await export(world)
    assert "does not verify" in str(prove_log(tampered(bundle, 3), TRUST))
    other = dataclasses.replace(bundle, origin="elsewhere.example/" + STREAM)
    assert "origin" in str(prove_log(other, TRUST))
    no_gate = Trust(
        TrustedKeys(producers={}, log_keys=TRUST.keys.log_keys), ORIGIN, TRUST.witnesses
    )
    assert "does not verify" in str(prove_log(bundle, no_gate))


async def test_without_a_witness_quorum_nothing_is_final(world: World) -> None:
    bundle = await export(world)
    stripped = tuple(
        "\n".join(x for x in note.split("\n") if "witness-3" not in x and "witness-2" not in x)
        for note in bundle.checkpoints
    )
    assert "final" in str(prove_log(dataclasses.replace(bundle, checkpoints=stripped), TRUST))
    unwitnessed = Trust(TRUST.keys, ORIGIN, None)  # --no-witnesses: the latest signed one
    log = prove_log(dataclasses.replace(bundle, checkpoints=stripped), unwitnessed)
    assert isinstance(log, ProvenLog)
    assert log.size == 3


async def test_claims_are_checked_against_the_proven_records(world: World) -> None:
    log = await proven(world)
    small = Claim(
        "A $120.00 payment was allowed.",
        (world.small_id,),
        (Assertion(world.small_id, "body.resource.amount_cents", "eq", 12_000),),
    )
    by_lee = demo_claim(
        world,
        text="Approved by m.lee.",
        assertions=(Assertion(world.verdict_id, "body.approver", "eq", "m.lee"),),
    )
    ghost = dataclasses.replace(small, record_ids=("01JB6X7Q2M9ZK3T8V4N5R6S7W0",))
    results = check_claims_in_log([demo_claim(world), small, by_lee, ghost], log)
    assert [r.accepted for r in results] == [True, True, False, False]
    assert any("not in the proven log" in reason for reason in results[3].reasons)


async def test_the_query_runs_in_code_over_joined_rows(world: World) -> None:
    log = await proven(world)
    rows = build_rows(log.records)
    assert {r["decision_record_id"] for r in rows} == {world.decision_id, world.small_id}
    demo = next(r for r in rows if r["decision_record_id"] == world.decision_id)
    assert (demo["verdict"], demo["approver"], demo["outside_business_hours"]) == (
        "approve",
        "j.ortiz",
        True,
    )
    assert len(run_query(EVERY, log.records, NOW)) == 2
    pending = dataclasses.replace(EVERY, verdict="none")
    assert [r["decision_record_id"] for r in run_query(pending, log.records, NOW)] == [
        world.small_id
    ]
    week = dataclasses.replace(EVERY, since_days=1)
    assert run_query(week, log.records, NOW + timedelta(days=2)) == []
    big = dataclasses.replace(EVERY, min_amount_cents=1_000_001, approver="j.ortiz")
    assert [r["decision_record_id"] for r in run_query(big, log.records, NOW)] == [
        world.decision_id
    ]


def test_query_types_match_gate_and_rows_are_capped_by_the_caller() -> None:
    assert (DECISION, VERDICT) == (gate_records.DECISION, gate_records.VERDICT)
    assert MAX_ROWS == 50
    with pytest.raises(ValueError, match="unknown"):
        dataclasses.replace(EVERY, action="money.burn")
