"""Auditor Q&A (assist/auditor.py), demo beat 3, over the real ledger API with witnesses and
real proofs. Only the two model answers are faked."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from assist.auditor import QA, Auditor, LedgerRecorder, inject_wrong_assertion
from assist.evidence import HttpLedgerReader
from claims.checker import Assertion, Claim
from common.clock import FixedClock
from common.errors import ModelOutputError, VerificationError
from gate.ledger_client import HttpLedgerClient
from ledger.receipts import LOG_KEY_OWNER
from ledger.signing import KeyRing
from tests.gate_helpers import AGENT, LOG, SMALL_PAYMENT, LocalLedger, rig
from tests.helpers import STREAM, T0, signer_for
from tests.unit.test_assist_brief import llm
from tests.unit.test_assist_llm import FakeCompletion
from tests.unit.test_claims_checker import TRUST, World, world
from verify.keys import TrustedKeys

__all__ = ["world"]  # fixture
QUESTION = (
    "Show every payment over $10,000 approved outside business hours this week,"
    " and who approved it."
)
NOW = FixedClock(T0 + timedelta(hours=1))
QUERY = {
    "action": "value.transfer", "decision": "any", "verdict": "approve",
    "min_amount_cents": 1_000_001, "max_amount_cents": None, "outside_business_hours": "yes",
    "approver": None, "payee_id": None, "since_days": 7,
}  # fmt: skip


def check(record_id: str, field: str, value: object) -> dict[str, object]:
    return {"record_id": record_id, "field": field, "op": "eq", "value": value}


def answer(
    w: World,
    approver: str = "j.ortiz",
    ids: list[str] | None = None,
    summary: str = "One payment matches: $48,000.00, approved by j.ortiz.",
) -> str:
    claim = {
        "text": f"$48,000.00 to northwind-supply was approved by {approver} after hours.",
        "record_ids": ids or [w.decision_id, w.verdict_id],
        "assertions": [
            check(w.decision_id, "body.resource.amount_cents", 4_800_000),
            check(w.decision_id, "body.resource.payee_id", "northwind-supply"),
            check(w.verdict_id, "body.verdict", "approve"),
            check(w.verdict_id, "body.approver", approver),
        ],
    }
    return json.dumps({"summary": summary, "claims": [claim]})


def auditor(w: World, fake: FakeCompletion, tmp_path: Path, recorder: Any = None) -> Auditor:
    reader = HttpLedgerReader("http://test", w.http, STREAM)
    return Auditor(
        llm(tmp_path, fake),
        reader,
        TRUST,
        recorder,
        clock=NOW,
        timezone=ZoneInfo("America/New_York"),
    )


async def test_beat_3_a_true_answer_is_proven_claim_by_claim(world: World, tmp_path: Path) -> None:
    fake = FakeCompletion(json.dumps(QUERY), answer(world))
    result = await auditor(world, fake, tmp_path).ask(QUESTION)
    assert (result.rows, result.matched) == (1, (world.decision_id,))
    assert result.coverage().startswith(
        "The query ran in code over all 3 records under the final checkpoint (size 3, root "
    )
    assert result.coverage().endswith("and matched 1.")
    (claim,) = result.results
    assert claim.accepted, claim.reasons
    assert result.answer_text() == "One payment matches: $48,000.00, approved by j.ortiz."
    rows_sent = fake.calls[1]["messages"][1]["content"]
    assert world.decision_id in rows_sent  # only the query's rows
    assert world.small_id not in rows_sent


async def test_a_claim_the_model_got_wrong_is_rejected_and_left_out(
    world: World, tmp_path: Path
) -> None:
    fake = FakeCompletion(json.dumps(QUERY), answer(world, approver="m.lee"))
    result = await auditor(world, fake, tmp_path).ask(QUESTION)
    (claim,) = result.results
    assert not claim.accepted
    assert any(
        "body.approver eq 'm.lee' is false (record has 'j.ortiz')" in r for r in claim.reasons
    )
    assert result.answer_text() == "0 of 1 claims were proven."


async def test_an_injected_fault_is_caught_and_labelled(world: World, tmp_path: Path) -> None:
    fake = FakeCompletion(json.dumps(QUERY), answer(world))
    result = await auditor(world, fake, tmp_path).ask(QUESTION, inject_fault=True)
    assert result.injected == 0
    assert not result.results[0].accepted
    assert any("4900000" in r for r in result.results[0].reasons)


async def test_ids_outside_the_rows_get_one_retry(world: World, tmp_path: Path) -> None:
    outside = answer(world, ids=[world.decision_id, world.verdict_id, world.small_id])
    fake = FakeCompletion(json.dumps(QUERY), outside, answer(world))
    result = await auditor(world, fake, tmp_path).ask(QUESTION)
    assert result.results[0].accepted
    assert "not in the rows" in fake.calls[2]["messages"][-1]["content"]
    with pytest.raises(ModelOutputError):
        await auditor(world, FakeCompletion(json.dumps(QUERY), outside, outside), tmp_path).ask(
            QUESTION
        )


async def test_a_log_that_does_not_verify_gets_no_answer(world: World, tmp_path: Path) -> None:
    fake = FakeCompletion(json.dumps(QUERY), answer(world))
    a = auditor(world, fake, tmp_path)
    a._trust = replace(TRUST, keys=TrustedKeys(producers={}, log_keys=TRUST.keys.log_keys))
    with pytest.raises(VerificationError) as caught:
        await a.ask(QUESTION)
    assert caught.value.code == "audit.log_unproven"
    assert fake.calls == []  # the model is never asked


async def test_a_summary_that_goes_beyond_the_claims_is_not_shown(
    world: World, tmp_path: Path
) -> None:
    beyond = answer(world, summary="One payment, $48,000.00, approved by m.lee.")
    result = await auditor(world, FakeCompletion(json.dumps(QUERY), beyond), tmp_path).ask(QUESTION)
    assert result.results[0].accepted
    assert any("m.lee" in p for p in result.summary_problems)
    assert result.answer_text().startswith("1 of 1 claims were proven. $48,000.00")


async def test_the_audit_is_itself_recorded(world: World, tmp_path: Path) -> None:
    assist = signer_for("assist-1")
    ledger = LocalLedger(FixedClock(T0))
    ledger.ledger._producers = KeyRing([("assist-1", assist.public_key_raw)])
    recorder = LedgerRecorder(
        signer=assist, producer_id="assist-1", stream=STREAM, ledger=ledger,
        log_keys=KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)]), clock=NOW,
    )  # fmt: skip
    fake = FakeCompletion(json.dumps(QUERY), answer(world))
    result = await auditor(world, fake, tmp_path, recorder).ask(QUESTION, inject_fault=True)
    assert result.record is not None
    assert result.record.submission.type == QA
    body = result.record.body
    assert body["question"] == QUESTION
    assert body["query"]["min_amount_cents"] == 1_000_001
    assert body["log"]["size"] == 3
    assert body["matched"] == [world.decision_id]
    assert body["coverage"] == result.coverage()
    assert body["claims"][0]["accepted"] is False
    assert body["claims"][0]["injected"] is True
    assert body["prompt_versions"] == ["auditor_query/1", "auditor_answer/2"]


def test_injection_changes_values_of_every_type() -> None:
    claim = Claim("t", ("a",), (Assertion("a", "f", "eq", "j.ortiz"),))
    assert inject_wrong_assertion(claim).assertions[0].value == "j.ortiz-x"
    for value, changed in ((5, 100_005), (True, False), (None, True)):
        claim = Claim("t", ("a",), (Assertion("a", "f", "eq", value),))
        assert inject_wrong_assertion(claim).assertions[0].value == changed


@pytest.mark.live
async def test_a_real_audit_answer_from_muse_spark(world: World, tmp_path: Path) -> None:
    """Calls the Meta Model API (Standard mode). Needs META_API_KEY; run with -m live."""
    import os

    from assist.llm import LlmClient, ModelConfig, Route

    key = os.environ.get("META_API_KEY")
    if not key:
        pytest.skip("META_API_KEY is not set")
    route = Route("meta/muse-spark-1.3", "https://api.meta.ai/v1", key)
    config = ModelConfig("cloud", {"cloud": {"spark": route}}, 60, tmp_path / "calls.csv")
    reader = HttpLedgerReader("http://test", world.http, STREAM)
    tz = ZoneInfo("America/New_York")
    result = await Auditor(LlmClient(config), reader, TRUST, None, clock=NOW, timezone=tz).ask(
        QUESTION
    )
    for claim in result.results:
        print(claim.accepted, claim.claim.text, claim.reasons)
    assert result.results


async def test_coverage_says_what_the_query_did_not_search(world: World, tmp_path: Path) -> None:
    r = rig()
    r.gate._ledger = HttpLedgerClient("http://test", world.http)
    await r.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)  # after the final checkpoint
    fake = FakeCompletion(json.dumps(QUERY), answer(world))
    result = await auditor(world, fake, tmp_path).ask(QUESTION)
    assert result.coverage().endswith(
        "matched 1. 1 newer records are not final yet and were not searched."
    )
    assert (
        replace(result, rows=0)
        .coverage()
        .endswith(
            "matched 1. The model saw the newest 0. 1 newer records are not final yet and were not"
            " searched."
        )
    )
