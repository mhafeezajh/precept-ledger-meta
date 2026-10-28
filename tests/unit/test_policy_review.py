"""Policy steps 2 and 3 (policy/pdf.py, draft.py, service.py, api.py), demo beat 1 end to end:
the policy PDF is drafted into rules (by the mock here), a planted bad rule is caught, the
officer fixes and approves it, and Gate decides with the approved set, restored at startup."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pypdf
import pytest

from assist.llm import LlmClient, ModelConfig, Route
from assist.mock import MOCK_MODEL, scripted_completion
from common.errors import GateError, ModelOutputError, PolicyError
from gate.api import create_gate_app
from gate.auth import Callers, token_hash
from gate.core import Gate
from policy.api import install_policy_routes
from policy.draft import Drafter, inject_wrong_threshold
from policy.pdf import MAX_PDF_BYTES, read_pdf
from policy.rules import FIELDS, parse_rules, to_yaml
from policy.service import APPROVE, DRAFT, Officer, PolicyService, restore_active
from scripts.make_policy_pdf import build_pdf
from tests.gate_helpers import AGENT, DEMO_PAYMENT, Rig, rig
from tests.unit.test_assist_llm import FakeCompletion

ROOT = Path(__file__).resolve().parents[2]
PDF = (ROOT / "deploy" / "demo" / "payments_policy.pdf").read_bytes()
TEXT = (ROOT / "deploy" / "demo" / "payments_policy.txt").read_text()
PROMPT = (ROOT / "assist" / "prompts" / "policy_draft.md").read_text()
NAME = "payments_policy.pdf"
OFFICER = Officer("a.chen", frozenset({"compliance-officer"}))
CLERK = Officer("j.ortiz", frozenset({"payments-supervisor"}))
OFFICER_TOKEN = "test-only-approver-token-officer"  # TEST ONLY
SUPERVISOR_TOKEN = "test-only-approver-token-supervisor"  # TEST ONLY
CALLERS = Callers.from_text(
    f"approver a.chen compliance-officer {token_hash(OFFICER_TOKEN)}\n"
    f"approver j.ortiz payments-supervisor {token_hash(SUPERVISOR_TOKEN)}\n"
)


def drafter(tmp_path: Path, completion: Any = scripted_completion) -> Drafter:
    route = Route(MOCK_MODEL, None, None)
    config = ModelConfig("mock", {"mock": {"wonder": route}}, 5, tmp_path / "calls.csv")
    return Drafter(LlmClient(config, completion=completion))


# --- the PDF -----------------------------------------------------------------------------


def test_the_demo_pdf_is_the_policy_text_and_is_rebuilt_byte_for_byte() -> None:
    assert build_pdf(TEXT) == PDF  # scripts/make_policy_pdf.py after any text change
    doc = read_pdf(PDF, NAME)
    assert (doc.pages, len(doc.clauses)) == (2, 11)
    assert [c.id for c in doc.clauses][:3] == ["1.1", "1.2", "2.1"]
    for clause in doc.clauses:
        assert doc.text[clause.start : clause.end] == clause.text
    email = doc.clause("4.2")
    assert email is not None
    assert email.page == 2
    start, end = doc.locate("3.5", "No single payment   may exceed $250,000.") or (0, 0)
    assert doc.text[start:end] == "No single payment may exceed $250,000."
    assert doc.locate("3.5", "Any payment over $5,000") is None  # that is clause 3.2
    assert doc.locate("9.9", "x") is None


def _encrypted() -> bytes:
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(__import__("io").BytesIO(PDF)))
    writer.encrypt("test-only-password")
    out = __import__("io").BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not a pdf", "policy.pdf_unreadable"),
        (PDF[: len(PDF) // 2], "policy.pdf_unreadable"),
        (b"%PDF-1.4" + b" " * MAX_PDF_BYTES, "policy.pdf_too_large"),
        (build_pdf("A policy with no numbered clauses.\n"), "policy.pdf_no_clauses"),
        (build_pdf("1.1 First.\n1.1 Again.\n"), "policy.pdf_duplicate_clause"),
    ],
)
def test_a_hostile_or_unusable_pdf_is_refused(data: bytes, code: str) -> None:
    with pytest.raises(PolicyError) as caught:
        read_pdf(data, NAME)
    assert caught.value.code == code


def test_an_encrypted_pdf_is_refused() -> None:
    with pytest.raises(PolicyError) as caught:
        read_pdf(_encrypted(), NAME)
    assert caught.value.code == "policy.pdf_encrypted"


# --- drafting ----------------------------------------------------------------------------


async def test_the_draft_cites_clauses_with_spans_computed_by_code(tmp_path: Path) -> None:
    doc = read_pdf(PDF, NAME)
    draft = await drafter(tmp_path).draft(doc)
    assert [r.id for r in draft.rules] == [r.id for r in parse_rules(_seed())]
    assert draft.report.ok, draft.report.findings
    assert (draft.model, draft.prompt_version, draft.injected) == (
        MOCK_MODEL,
        "policy_draft/1",
        None,
    )
    for rule in draft.rules:
        assert rule.source.span is not None
        assert doc.text[rule.source.span[0] : rule.source.span[1]] == rule.source.quote
        assert rule.source.page == (2 if rule.source.clause.startswith("4.") else 1)


def _seed() -> str:
    return (ROOT / "deploy" / "policies" / "harbor-federal.yaml").read_text()


async def test_the_planted_bad_rule_is_caught_by_its_clause(tmp_path: Path) -> None:
    draft = await drafter(tmp_path).draft(read_pdf(PDF, NAME), inject_fault=True)
    assert draft.injected == "pay-known-payee-01"
    assert not draft.report.ok
    numbers = [f for f in draft.report.findings if f.check == "clause_number"]
    assert [(f.rule_id, "50000" in f.detail) for f in numbers] == [("pay-known-payee-01", True)]
    assert {f.rule_id for f in draft.report.findings} == {"pay-known-payee-01"}


def test_injection_without_an_amount_changes_nothing() -> None:
    email_only = parse_rules(_seed())[-2:]
    assert inject_wrong_threshold(email_only) == (email_only, None)


async def test_a_quote_that_is_not_in_its_clause_gets_one_retry(tmp_path: Path) -> None:
    good = (await scripted_completion(
        model=MOCK_MODEL,
        messages=[{"role": "user", "content": _clauses_message()}],
        response_format={"json_schema": {"name": "DraftRules"}},
    )).choices[0].message.content  # fmt: skip
    bad = json.loads(good)
    bad["rules"][0]["quote"] = "Agents may pay anyone without approval."
    fake = FakeCompletion(json.dumps(bad), good)
    draft = await drafter(tmp_path, fake).draft(read_pdf(PDF, NAME))
    assert draft.report.ok
    assert "not in clause '2.1'" in fake.calls[1]["messages"][-1]["content"]
    with pytest.raises(ModelOutputError):
        await drafter(tmp_path, FakeCompletion(json.dumps(bad), json.dumps(bad))).draft(
            read_pdf(PDF, NAME)
        )


def _clauses_message() -> str:
    doc = read_pdf(PDF, NAME)
    lines = "\n".join(f"[{c.id}] (page {c.page}) {c.text}" for c in doc.clauses)
    return f"Document: {NAME}\nClauses:\n{lines}"


def test_the_prompt_names_every_field_and_is_versioned() -> None:
    assert "version: policy_draft/1" in PROMPT
    for fields in FIELDS.values():
        for name in fields:
            assert name in PROMPT, name


# --- review, approval, activation --------------------------------------------------------


async def service(tmp_path: Path) -> tuple[Rig, PolicyService]:
    r = rig()
    return r, PolicyService(r.gate, drafter(tmp_path))


async def test_beat_1_caught_fixed_approved_and_enforced(tmp_path: Path) -> None:
    r, svc = await service(tmp_path)
    draft = await svc.submit(PDF, NAME, OFFICER, inject_fault=True)
    assert draft.submission.type == DRAFT
    assert draft.body["injected"] == "pay-known-payee-01"
    assert draft.body["policy_set"] is not None
    assert draft.body["findings"]
    with pytest.raises(PolicyError) as caught:
        await svc.approve(draft.record_id, OFFICER)
    assert caught.value.code == "policy.not_clean"
    fixed = draft.body["rules_yaml"].replace("lte: 50000", "lte: 5000")
    revision = await svc.revise(draft.record_id, fixed, OFFICER)
    assert (revision.body["revises"], revision.body["findings"]) == (draft.record_id, [])
    assert (revision.body["author"], revision.body["model"]) == ("a.chen", None)
    approval = await svc.approve(revision.record_id, OFFICER)
    assert approval.submission.type == APPROVE
    assert approval.body["approver"] == "a.chen"
    assert r.gate.policies.digest == approval.body["policy_set"] == revision.body["policy_set"]
    assert r.gate.policy_record_id == approval.record_id
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    assert decision.record.body["policy_set"] == approval.body["policy_set"]
    assert decision.record.body["decision"] == "escalate"


async def test_only_a_compliance_officer_drafts_revises_or_approves(tmp_path: Path) -> None:
    r, svc = await service(tmp_path)
    draft = await svc.submit(PDF, NAME, OFFICER)
    for call in (
        svc.submit(PDF, NAME, CLERK),
        svc.revise(draft.record_id, draft.body["rules_yaml"], CLERK),
        svc.approve(draft.record_id, CLERK),
    ):
        with pytest.raises(PolicyError) as caught:
            await call
        assert caught.value.code == "policy.forbidden"
    assert r.gate.policy_record_id is None


async def test_approval_rechecks_and_never_trusts_stored_findings(tmp_path: Path) -> None:
    r, svc = await service(tmp_path)
    clean = await svc.submit(PDF, NAME, OFFICER)
    decision = await r.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
    with pytest.raises(GateError):  # a decision record is not a draft
        await svc.approve(decision.record.record_id, OFFICER)
    assert (await svc.approve(clean.record_id, OFFICER)).body["rules"][0] == "vendor-lookup-01"


async def test_without_a_drafter_nothing_is_drafted(tmp_path: Path) -> None:
    with pytest.raises(PolicyError) as caught:
        await PolicyService(rig().gate, None).submit(PDF, NAME, OFFICER)
    assert caught.value.code == "policy.no_drafter"


async def test_gate_restores_the_latest_approval_at_startup(tmp_path: Path) -> None:
    r, svc = await service(tmp_path)
    assert await restore_active(r.gate, r.ledger.store) is None
    draft = await svc.submit(PDF, NAME, OFFICER)
    first = await svc.approve(draft.record_id, OFFICER)
    trimmed = to_yaml(parse_rules(draft.body["rules_yaml"])[:-2])  # no email rules
    second = await svc.approve(
        (await svc.revise(draft.record_id, trimmed, OFFICER)).record_id, OFFICER
    )
    restarted = rig()
    restarted.gate._ledger = r.gate._ledger  # same ledger, fresh Gate with the seed file
    assert restarted.gate.policy_record_id is None
    assert await restore_active(restarted.gate, r.ledger.store) == second.record_id
    assert restarted.gate.policies.digest == second.body["policy_set"] != first.body["policy_set"]


# --- the HTTP API ------------------------------------------------------------------------


@dataclass
class Api:
    rig: Rig
    http: httpx.AsyncClient

    async def call(
        self, method: str, path: str, token: str = OFFICER_TOKEN, **kwargs: Any
    ) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"}
        return await self.http.request(method, path, headers=headers, **kwargs)


@asynccontextmanager
async def api(tmp_path: Path, *, demo_mode: bool = True) -> AsyncIterator[Api]:
    r = rig()

    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield r.gate, CALLERS

    app = create_gate_app(
        open_gate, sweep_interval=None,
        extend=lambda a: install_policy_routes(a, drafter(tmp_path), demo_mode=demo_mode),
    )  # fmt: skip
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gate") as http,
    ):
        yield Api(r, http)


async def test_beat_1_over_http(tmp_path: Path) -> None:
    async with api(tmp_path) as s:
        params = {"name": NAME, "inject_fault": "true"}
        draft = await s.call("POST", "/policy/v1/drafts", params=params, content=PDF)
        assert draft.status_code == 201, draft.text
        draft_id = draft.json()["record"]["record_id"]
        body = draft.json()["record"]["body"]
        refused = await s.call("POST", f"/policy/v1/drafts/{draft_id}/approve")
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "policy.not_clean")
        fixed = body["rules_yaml"].replace("lte: 50000", "lte: 5000")
        revised = await s.call("POST", f"/policy/v1/drafts/{draft_id}/revisions", content=fixed)
        assert revised.status_code == 201
        revision_id = revised.json()["record"]["record_id"]
        shown = await s.call("GET", f"/policy/v1/drafts/{revision_id}")
        assert shown.json()["record"]["body"]["findings"] == []
        approved = await s.call("POST", f"/policy/v1/drafts/{revision_id}/approve")
        assert approved.status_code == 201
        active = (await s.call("GET", "/policy/v1/active")).json()
        assert active["policy_record"] == approved.json()["record"]["record_id"]
        health = (await s.http.get("/gate/v1/health")).json()
        assert (health["policy_record"], health["rules"]) == (active["policy_record"], 8)


async def test_the_api_refuses_what_it_should(tmp_path: Path) -> None:
    async with api(tmp_path, demo_mode=False) as s:
        injected = await s.call(
            "POST", "/policy/v1/drafts", params={"name": NAME, "inject_fault": "true"}, content=PDF
        )
        assert (injected.status_code, injected.json()["error"]["code"]) == (403, "policy.forbidden")
        supervisor = await s.call(
            "POST", "/policy/v1/drafts", SUPERVISOR_TOKEN, params={"name": NAME}, content=PDF
        )
        assert supervisor.status_code == 403
        junk = await s.call("POST", "/policy/v1/drafts", params={"name": NAME}, content=b"junk")
        assert (junk.status_code, junk.json()["error"]["code"]) == (400, "policy.pdf_unreadable")
        draft = await s.call("POST", "/policy/v1/drafts", params={"name": NAME}, content=PDF)
        draft_id = draft.json()["record"]["record_id"]
        bad_yaml = await s.call(
            "POST", f"/policy/v1/drafts/{draft_id}/revisions", content="a: [unclosed"
        )
        assert (bad_yaml.status_code, bad_yaml.json()["error"]["code"]) == (400, "policy.yaml")
        not_utf8 = await s.call(
            "POST", f"/policy/v1/drafts/{draft_id}/revisions", content=b"\xff\xfe"
        )
        assert not_utf8.status_code == 400
        anonymous = await s.http.get(f"/policy/v1/drafts/{draft_id}")
        assert anonymous.status_code == 401
