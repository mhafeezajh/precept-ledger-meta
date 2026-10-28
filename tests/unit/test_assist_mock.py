"""MODEL_PROFILE=mock (assist/mock.py): every feature runs offline at no cost, through the same
validation and checks as a real model, and every record it feeds names ``mock/scripted``."""

import csv
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from assist import mock
from assist.auditor import Auditor, RecordQuery
from assist.brief import Brief, BriefWriter, check_brief
from assist.evidence import HttpLedgerReader
from assist.llm import LlmClient, ModelConfig, model_config
from assist.mock import MOCK_MODEL, scripted_completion
from common.errors import ModelOutputError
from tests.gate_helpers import AGENT, SMALL_PAYMENT
from tests.helpers import STREAM
from tests.unit.test_assist_auditor import NOW, QUERY, QUESTION
from tests.unit.test_assist_brief import context_for, seeded
from tests.unit.test_assist_evidence import settings
from tests.unit.test_claims_checker import TRUST, World, world

__all__ = ["world"]  # fixture


def mock_llm(tmp_path: Path) -> LlmClient:
    log = tmp_path / "model_calls.csv"
    config = model_config(settings(tmp_path, model_profile="mock", model_call_log=log))
    assert config is not None
    return LlmClient(config)


def test_the_mock_profile_needs_no_key_and_ignores_one(tmp_path: Path) -> None:
    for key in (None, SecretStr("test-only-key-7781")):
        config = model_config(settings(tmp_path, model_profile="mock", meta_api_key=key))
        assert isinstance(config, ModelConfig)
        assert config.profile == "mock"
        assert config.route("spark").model == config.route("wonder").model == MOCK_MODEL
        assert config.route("spark").api_key is None  # a key is never sent anywhere


async def test_a_mock_brief_passes_the_same_checks_and_names_the_mock(tmp_path: Path) -> None:
    r, decision, escalation = await seeded()
    body = await BriefWriter(mock_llm(tmp_path), r.ledger.store).write(escalation, decision)
    assert body["model"] == MOCK_MODEL
    context = await context_for(r, decision, escalation)
    brief = Brief.model_validate({k: body[k] for k in ("headline", "unusual", "approver_checks")})
    assert check_brief(brief, context) == []
    assert "$48,000.00" in brief.headline
    assert any("no earlier payments" in p.text for p in brief.unusual)
    with (tmp_path / "model_calls.csv").open() as file:
        (row,) = list(csv.DictReader(file))
    assert (row["profile"], row["model"], row["outcome"]) == ("mock", MOCK_MODEL, "ok")
    assert (row["input_tokens"], row["output_tokens"]) == ("0", "0")


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (QUESTION, QUERY),
        (
            "Which emails were denied today?",
            {**QUERY, "action": "comm.external", "decision": "deny", "verdict": "any",
             "min_amount_cents": None, "outside_business_hours": "any", "since_days": 1},
        ),
        (
            "Payments under $500 approved by m.lee to acme-paper in the last 30 days",
            {**QUERY, "min_amount_cents": None, "max_amount_cents": 49_999,
             "outside_business_hours": "any", "approver": "m.lee", "payee_id": "acme-paper",
             "since_days": 30},
        ),
    ],
)  # fmt: skip
async def test_the_mock_turns_plain_questions_into_queries(
    question: str, expected: dict[str, Any], tmp_path: Path
) -> None:
    query = await mock_llm(tmp_path).complete_structured(
        "spark", RecordQuery,
        [{"role": "system", "content": "x"},
         {"role": "user", "content": f"Today is Wednesday 2026-10-28.\nQuestion: {question}"}],
        prompt_version="auditor_query/1",
    )  # fmt: skip
    assert query.model_dump() == expected


async def test_a_mock_audit_is_proven_like_a_real_one(world: World, tmp_path: Path) -> None:
    reader = HttpLedgerReader("http://test", world.http, STREAM)
    tz = ZoneInfo("America/New_York")
    auditor = Auditor(mock_llm(tmp_path), reader, TRUST, None, clock=NOW, timezone=tz)
    result = await auditor.ask(QUESTION)
    (claim,) = result.results
    assert claim.accepted, claim.reasons
    assert "j.ortiz" in claim.claim.text
    assert "$48,000.00" in claim.claim.text
    assert result.summary_problems == ()
    assert result.answer_text() == result.summary
    faulty = await auditor.ask(QUESTION, inject_fault=True)
    assert not faulty.results[0].accepted


async def test_a_schema_the_mock_has_no_script_for_is_refused(tmp_path: Path) -> None:
    class Unknown(Brief):
        pass

    with pytest.raises(ModelOutputError) as caught:
        await mock_llm(tmp_path).complete_structured(
            "spark", Unknown, [{"role": "user", "content": "x"}], prompt_version="x/1"
        )
    assert caught.value.code == "model.refused"


async def test_the_scripted_completion_looks_like_litellm() -> None:
    fmt = {"type": "json_schema", "json_schema": {"name": "RecordQuery"}}
    messages = [{"role": "user", "content": "Question: every payment"}]
    response = await scripted_completion(model=MOCK_MODEL, messages=messages, response_format=fmt)
    assert json.loads(response.choices[0].message.content)["action"] == "value.transfer"
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (0, 0)


async def test_a_mock_brief_for_a_known_payee_cites_its_history(tmp_path: Path) -> None:
    r, _, _ = await seeded()
    again = await r.gate.decide(AGENT, "create_payment", {**SMALL_PAYMENT, "amount": "7000.00"})
    assert again.escalation is not None
    body = await BriefWriter(mock_llm(tmp_path), r.ledger.store).write(
        again.escalation, again.record
    )
    assert any("4 earlier decisions and 1 earlier approvals" in p["text"] for p in body["unusual"])


async def test_mock_answers_cover_every_kind_of_row(world: World, tmp_path: Path) -> None:
    reader = HttpLedgerReader("http://test", world.http, STREAM)
    tz = ZoneInfo("America/New_York")
    auditor = Auditor(mock_llm(tmp_path), reader, TRUST, None, clock=NOW, timezone=tz)
    every = await auditor.ask("Show every payment")
    assert [r.accepted for r in every.results] == [True, True]
    texts = [r.claim.text for r in every.results]
    assert "A payment of $120.00 to acme-paper outside business hours was allowed." in texts, texts
    none = await auditor.ask("Show every payment over $1,000,000")
    assert (none.results, none.answer_text()) == ((), "No records matched the query.")
    row = {"decision_record_id": "d", "verdict_record_id": "v", "amount_cents": None,
           "tool": "send_email", "agent": "payments-ops", "decision": "escalate",
           "outside_business_hours": False, "verdict": "expire", "approver": None}  # fmt: skip
    claim = mock._claim(row)
    assert claim["text"] == "A send_email call by payments-ops was left to expire."
    assert {a["field"] for a in claim["assertions"]} == {
        "body.decision", "body.tool", "body.agent", "body.verdict"
    }  # fmt: skip
