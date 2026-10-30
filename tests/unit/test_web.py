"""The console (web/), against the real thing: the ledger API with three witnesses, a real
pl-gate app (Gate, policy and learner routes) writing to it, and the console in front of both,
all in process. Pages are fetched as a browser would, with the session cookie and CSRF token."""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from agent.graph import PaymentsAgent
from assist.auditor import Auditor
from assist.evidence import HttpLedgerReader
from assist.llm import LlmClient, ModelConfig, Route
from assist.mock import MOCK_MODEL
from common.clock import FixedClock
from gate.api import create_gate_app
from gate.auth import Callers, token_hash
from gate.core import Gate
from gate.ledger_client import HttpLedgerClient
from learn.api import ExportView, LearnSettings, install_learn_routes
from learn.draft import LearnDrafter
from ledger.checkpoint import Checkpoint
from policy.api import install_policy_routes
from policy.draft import Drafter
from tests.gate_helpers import AGENT, DEMO_PAYMENT, SMALL_PAYMENT, Rig, rig
from tests.helpers import STREAM, T0
from tests.unit.test_agent import build
from tests.unit.test_api_witnessed import open_client, publish, services
from tests.unit.test_claims_checker import TRUST
from tests.unit.test_witnessing import witnesses
from verify.checks import Report
from verify.findings import Finding
from web import demo, views
from web.agent_runs import AgentRuns
from web.app import Console, WebSettings, create_web_app
from web.backend import Backend, Persona, WebError, load_personas
from web.nav import current, navigation
from web.security import COOKIE, CSP, Sessions

TOKENS = {  # TEST ONLY
    "j.ortiz": ("approver", "payments-supervisor", "test-only-web-ortiz"),
    "a.chen": ("approver", "compliance-officer", "test-only-web-chen"),
    "r.patel": ("approver", "compliance-officer", "test-only-web-patel"),
    "payments-ops": ("agent", "-", "test-only-web-agent"),
}
CALLERS = Callers.from_text("".join(
    f"{kind} {name} {roles} {token_hash(token)}\n" for name, (kind, roles, token) in TOKENS.items()
))  # fmt: skip
PERSONAS = {
    name: Persona(name, kind, frozenset(r for r in roles.split(",") if r != "-"), token)
    for name, (kind, roles, token) in TOKENS.items()
}
ZONE = ZoneInfo("America/New_York")
_CSRF = re.compile(r'"X-CSRF-Token": "([0-9a-f]{64})"')


def mock_config(tmp_path: Path) -> ModelConfig:
    route = Route(MOCK_MODEL, None, None)
    return ModelConfig("mock", {"mock": {"spark": route, "wonder": route}}, 5, tmp_path / "c.csv")


@dataclass
class Stack:
    rig: Rig
    svc: Any
    ledger: httpx.AsyncClient
    console: Console
    web: httpx.AsyncClient
    csrf: str = ""

    async def start(self) -> None:
        page = await self.web.get("/ledger")
        found = _CSRF.search(page.text)
        assert found is not None, page.text[:500]
        self.csrf = found.group(1)

    async def post(self, path: str, *, htmx: bool = True, **kwargs: Any) -> httpx.Response:
        headers = {"x-csrf-token": self.csrf, **({"hx-request": "true"} if htmx else {})}
        return await self.web.post(path, headers=headers, **kwargs)

    async def checkpoint(self) -> None:
        await publish(self.svc)


async def _fake_rogue() -> demo.RogueRun:
    return demo.RogueRun(0, ("1. Honest ledger", "5. pl-verify rogue.tar with witness keys"))


@asynccontextmanager
async def stack(tmp_path: Path, *, demo_mode: bool = True) -> AsyncIterator[Stack]:
    svc = services(tmp_path, witnesses())  # type: ignore[arg-type]
    async for ledger in open_client(svc, None):
        async with _on_ledger(tmp_path, svc, ledger, demo_mode) as s:
            yield s


@asynccontextmanager
async def _on_ledger(
    tmp_path: Path, svc: Any, ledger: httpx.AsyncClient, demo_mode: bool
) -> AsyncIterator[Stack]:
    """Gate (writing to ``ledger``), its policy and learner routes, and the console."""
    r = rig()
    r.gate._ledger = HttpLedgerClient("http://test", ledger)
    config = mock_config(tmp_path)
    view = ExportView("http://test", STREAM, TRUST, 5, http=ledger)

    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield r.gate, CALLERS

    def extend(app: Any) -> None:
        install_policy_routes(app, Drafter(LlmClient(config)), demo_mode=True)
        install_learn_routes(app, LearnDrafter(LlmClient(config)), view, LearnSettings(),
                             demo_mode=True)  # fmt: skip

    gate_app = create_gate_app(open_gate, sweep_interval=None, extend=extend)
    gate_transport = httpx.ASGITransport(app=gate_app, raise_app_exceptions=False)

    @asynccontextmanager
    async def agent() -> AsyncIterator[PaymentsAgent]:
        yield build(tmp_path, r)

    async def ask(question: str, inject_fault: bool) -> Any:
        reader = HttpLedgerReader("http://test", ledger, STREAM)
        clock = FixedClock(datetime.now(UTC))
        auditor = Auditor(LlmClient(config), reader, TRUST, None, clock=clock, timezone=ZONE)
        return await auditor.ask(question, inject_fault=inject_fault)

    async with (
        gate_app.router.lifespan_context(gate_app),
        httpx.AsyncClient(transport=gate_transport, base_url="http://gate") as gate_http,
    ):
        backend = Backend(gate_http, ledger, stream=STREAM, trust=TRUST, personas=PERSONAS)
        approvers = {n: p for n, p in PERSONAS.items() if p.kind == "approver"}
        sessions = Sessions(b"test-only-session-secret", approvers, "j.ortiz")
        extras = {"agent_runs": AgentRuns(agent), "audit": ask, "rogue": _fake_rogue}
        made = Console(backend, sessions, ZONE, demo_mode, None, WebSettings(), extras)
        app = create_web_app(made)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://web") as web:
            s = Stack(r, svc, ledger, made, web)
            await s.start()
            yield s


async def switch(s: Stack, persona: str) -> None:
    response = await s.post("/session/persona", htmx=False, data={"persona": persona, "back": "/"})
    assert response.status_code == 303
    await s.start()  # a new session has a new CSRF token


# --- views, navigation, sessions ---------------------------------------------------------


def _record(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "body": body, "record_id": "01M62RCYKH5H9HMG1J0P714D4Z", "seq": 3}


def test_views_describe_records_from_their_own_fields() -> None:
    decision = _record("gate.decision", {
        "agent": "payments-ops", "tool": "create_payment", "decision": "escalate",
        "resource": {"amount_cents": 4_800_000, "payee_id": "northwind-supply", "payee_new": True},
    })  # fmt: skip
    assert views.summary(decision) == (
        "payments-ops called create_payment: $48,000.00 to northwind-supply (new payee)"
    )
    assert (views.outcome(decision), views.tone("escalate"), views.tone("x")) == (
        "escalate", "escalate", "neutral",
    )  # fmt: skip
    expired = _record("gate.verdict", {"tool": "t", "agent": "a", "verdict": "expire"})
    assert views.summary(expired) == "t by a: expire by nobody, before the deadline"
    assert (
        views.outcome(_record("policy.approve", {"rules": ["a"], "approver": "a.chen"}))
        == "approve"
    )
    second = _record("learn.approve", {"approval": 1, "approvals_needed": 2, "approver": "a.chen"})
    assert views.summary(second) == "Approval 1 of 2 by a.chen"
    assert views.summary(_record("other.kind", {})) == "other.kind"
    assert views.dollars(True) == ""
    assert views.dollars(5) == "$0.05"
    assert views.local_time("2026-10-28T21:04:00.000Z", ZONE) == "Wed 28 Oct, 17:04"


def _report(**changes: Any) -> Report:
    head = Checkpoint("o", 10, bytes(32))
    base: dict[str, Any] = {
        "stream": STREAM, "origin": "o", "records": 12, "chain_verified_through": 12,
        "receipts": 12, "checkpoints": 1, "latest": head, "unanchored": 2, "findings": (),
        "quorum": 2, "final": head, "final_cosigners": ("witness-1", "witness-2"),
    }  # fmt: skip
    return Report(**{**base, **changes})


def test_the_seal_says_proven_pending_or_broken() -> None:
    proven = views.seal(_report())
    assert (proven.state, proven.title) == ("proven", "Proven through record 10")
    assert "Cosigned by witness-1, witness-2" in proven.detail
    assert "2 newer records" in proven.detail
    waiting = views.seal(_report(final=None, findings=(Finding("below_quorum", "1 of 2"),)))
    assert (waiting.state, waiting.title) == ("pending", "Waiting for witnesses")
    assert views.seal(_report(latest=None, final=None)).title == "Not yet checkpointed"
    broken = views.seal(_report(findings=(Finding("bad_producer_signature", "x", 7, "id"),)))
    assert (broken.state, broken.seq, broken.title) == (
        "broken",
        7,
        "Verification failed at record 7",
    )


def test_navigation_hides_attacks_outside_demo_mode() -> None:
    assert [g for g, _ in navigation(demo_mode=False)] == ["Start", "Payments", "Rules", "Proof"]
    assert "Demo" in [g for g, _ in navigation(demo_mode=True)]
    assert current("/learn/proposals/x").path == "/learn"  # type: ignore[union-attr]
    assert current("/").path == "/"
    assert current("/nowhere") is None


def test_sessions_are_signed_and_bound_to_a_known_persona() -> None:
    sessions = Sessions(b"k", {"j.ortiz": PERSONAS["j.ortiz"]}, "j.ortiz")
    cookie = sessions.issue("j.ortiz")
    session = sessions.read(cookie)
    assert session is not None
    assert session.persona.name == "j.ortiz"
    assert len(session.csrf) == 64
    _, nonce, mac = cookie.split("|")
    assert sessions.read(f"a.chen|{nonce}|{mac}") is None  # renamed: the MAC no longer fits
    assert sessions.read("junk") is None
    assert sessions.read(None) is None
    assert Sessions(b"other", sessions.personas, "j.ortiz").read(cookie) is None
    with pytest.raises(WebError):
        sessions.issue("k.wu")
    with pytest.raises(WebError):
        Sessions(b"k", {}, "j.ortiz")
    assert repr(PERSONAS["j.ortiz"]).count("test-only") == 0  # tokens never in a repr


def test_personas_come_from_the_gate_callers_with_a_token(tmp_path: Path) -> None:
    (tmp_path / "tokens").mkdir()
    lines = "".join(f"{kind} {name} {roles} {token_hash(token)}\n"
                    for name, (kind, roles, token) in TOKENS.items())  # fmt: skip
    (tmp_path / "gate-callers.txt").write_text("# kind name roles hash\n" + lines)
    (tmp_path / "tokens" / "a.chen.token").write_text("test-only-web-chen\n")
    found = load_personas(tmp_path)
    assert list(found) == ["a.chen"]
    assert found["a.chen"].roles == {"compliance-officer"}
    assert found["a.chen"].label == "a.chen (compliance officer)"
    with pytest.raises(WebError):
        load_personas(tmp_path / "missing")


# --- pages over the real stack ---------------------------------------------------------------


async def test_every_page_is_safe_by_default(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        page = await s.web.get("/")
        assert page.status_code == 200
        assert page.headers["content-security-policy"] == CSP
        assert page.headers["x-frame-options"] == "DENY"
        cookie = page.headers.get("set-cookie", "") or ""
        assert cookie == "" or (
            "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
        )
        assert "test-only-web" not in page.text  # no token reaches the browser
        assert "<script>" not in page.text
        assert " on" + "click=" not in page.text
        refused = await s.web.post("/agent/runs", data={"task": "Pay Acme Paper $1.00"})
        assert refused.status_code == 403  # no CSRF token
        wrong = await s.web.post("/agent/runs", data={"task": "x", "csrf": "0" * 64})
        assert wrong.status_code == 403
        for path in (
            "/static/css/app.css",
            "/static/js/app.js",
            "/static/vendor/htmx-2.0.4.min.js",
        ):
            assert (await s.web.get(path)).status_code == 200
        for path in (
            "/",
            "/approvals",
            "/agent",
            "/policy",
            "/learn",
            "/ledger",
            "/audit",
            "/demo",
        ):
            response = await s.web.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert 'aria-current="page"' in response.text


async def test_the_overview_and_ledger_show_the_proof(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        empty = await s.web.get("/")
        assert "No decisions yet" in empty.text
        small = await s.rig.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
        await s.rig.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
        await s.checkpoint()
        page = await s.web.get("/")
        assert "Proven through record 2" in page.text
        assert "payments-ops called create_payment: $120.00 to acme-paper" in page.text
        ledger = await s.web.get("/ledger?kind=gate.decision")
        assert ledger.text.count("<tr>") == 3  # the header and two decisions
        record = await s.web.get(f"/ledger/records/{small.record.record_id}")
        assert "This record is proven" in record.text
        assert "amount_cents&#34;: 12000" in record.text
        missing = await s.web.get("/ledger/records/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert missing.status_code == 404
        assert "Not found" in missing.text
        await s.rig.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
        newest = (await s.web.get("/ledger")).text
        assert "1 newer records wait for the next checkpoint" in newest


async def test_a_supervisor_approves_a_held_payment(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        held = await s.rig.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
        assert held.escalation is not None
        queue = await s.web.get("/approvals/queue")
        assert "payments-ops wants to run create_payment" in queue.text
        await switch(s, "a.chen")  # the wrong role: Gate refuses, the console says so
        refused = await s.post(
            f"/approvals/{held.escalation.id}/verdict", data={"verdict": "approve"}
        )
        assert refused.status_code == 403
        assert refused.headers["hx-retarget"] == "#flash"
        assert "gate.approver_not_allowed" in refused.text
        await switch(s, "j.ortiz")
        bad = await s.post(f"/approvals/{held.escalation.id}/verdict", data={"verdict": "maybe"})
        assert bad.status_code == 400
        done = await s.post(f"/approvals/{held.escalation.id}/verdict", data={"verdict": "approve"})
        assert done.status_code == 200
        assert "Approved by j.ortiz" in done.text
        assert done.headers["hx-trigger"] == "queue-changed"
        assert "Nothing is waiting" in (await s.web.get("/approvals/queue")).text


async def test_beat_1_in_the_console(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        page = await s.web.get("/policy")
        assert "act as a compliance officer" in page.text
        assert "Continue as a.chen" in page.text  # one click, not a hunt for the menu
        await switch(s, "a.chen")
        missing = await s.post("/policy/drafts", data={})
        assert missing.status_code == 400
        assert "choose a PDF" in missing.text
        drafted = await s.post("/policy/drafts", data={"use_demo": "1", "inject_fault": "1"})
        assert drafted.status_code == 204
        draft_path = drafted.headers["hx-location"]
        draft = await s.web.get(draft_path)
        assert "Approval is refused until these are fixed" in draft.text
        assert "clause number" in draft.text
        assert "amount up to $50,000" in draft.text
        draft_id = draft_path.rsplit("/", 1)[1]
        refused = await s.post(f"/policy/drafts/{draft_id}/approve")
        assert refused.status_code == 409
        yaml_text = (await s.console.backend.policy_draft(PERSONAS["a.chen"], draft_id))["record"]
        fixed = yaml_text["body"]["rules_yaml"].replace("lte: 50000", "lte: 5000")
        revised = await s.post(f"/policy/drafts/{draft_id}/revisions", htmx=False,
                               data={"rules_yaml": fixed})  # fmt: skip
        assert revised.status_code == 303
        clean = await s.web.get(revised.headers["location"])
        assert "No findings" in clean.text
        revision_id = revised.headers["location"].rsplit("/", 1)[1]
        approved = await s.post(f"/policy/drafts/{revision_id}/approve")
        assert approved.status_code == 200
        assert "Gate now enforces these 8 rules" in approved.text
        assert s.rig.gate.policy_record_id is not None
        await s.checkpoint()
        listing = await s.web.get("/policy")
        assert "8 rules approved by a.chen" in listing.text
        # The rules in force are on the page for anyone, read from the verified ledger:
        # the approval Gate restores at startup (D75), not a call that needs the officer role.
        await switch(s, "j.ortiz")
        anyone = (await s.web.get("/policy")).text
        assert "8 rules, approved by a.chen" in anyone
        assert "pay-over-5k-01" in anyone
        assert "Clause 3.2, page 1" in anyone


async def test_the_learner_page_runs_and_explains(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        await switch(s, "a.chen")
        refused = await s.post("/learn/run", data={})
        assert refused.status_code == 409
        assert "learn.no_active_policy" in refused.text
        page = await s.web.get("/learn")
        assert "No suggestions yet" in page.text


async def test_a_proposal_page_shows_counts_claims_replay_and_review(tmp_path: Path) -> None:
    from tests.unit.test_learn import OFFICER, mock_drafter, seeded_week

    async with stack(tmp_path) as s:
        w = await seeded_week()
        run = await w.service(mock_drafter(tmp_path)).run(OFFICER)
        loosen = run.proposals[0]

        async def proposal(persona: Persona, proposal_id: str) -> dict[str, Any]:
            return {"proposal": loosen.to_json(), "approvals": []}

        s.console.backend.proposal = proposal  # type: ignore[method-assign]
        await switch(s, "a.chen")
        page = await s.web.get(f"/learn/proposals/{loosen.record_id}")
        assert page.status_code == 200, page.text[:400]
        text = page.text
        assert "Allow routine requests in this pattern without approval" in text
        assert "Matches" in text
        assert "Does not match" not in text
        assert text.count("claim--held") == 3
        assert "Holds no longer needed" in text
        assert "<strong>44</strong>" in text
        assert "Approve as a.chen" in text
        assert "two different compliance officers" in text


async def test_the_agent_page_runs_a_task_step_by_step(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        started = await s.post(
            "/agent/runs", data={"task": "Pay Acme Paper $120.00 for invoice INV-2."}
        )
        assert started.status_code == 200
        run_id = re.search(r'hx-get="/agent/runs/([0-9a-z]+)"', started.text)
        assert run_id is not None
        await s.console.extras["agent_runs"].wait()
        shown = await s.web.get(f"/agent/runs/{run_id.group(1)}", headers={"hx-request": "true"})
        assert shown.text.count('class="step step--ok"') == 3
        assert "Paid $120.00 to Acme Paper" in shown.text
        assert "every 1s" not in shown.text
        full = await s.web.get(f"/agent/runs/{run_id.group(1)}")
        assert "<h1>Agent run</h1>" in full.text
        assert "Pay Acme Paper $120.00" in (await s.web.get("/agent")).text
        too_long = await s.post("/agent/runs", data={"task": "x" * 3000})
        assert too_long.status_code == 400
        plain = await s.post("/agent/runs", htmx=False, data={"task": "Pay nobody"})
        assert plain.status_code == 303
        assert (await s.web.get("/agent/runs/nope")).status_code == 404
        await s.console.extras["agent_runs"].wait()


async def test_the_audit_page_proves_or_rejects_each_claim(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        held = await s.rig.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
        assert held.escalation is not None
        from tests.gate_helpers import SUPERVISOR

        await s.rig.gate.verdict(held.escalation.id, SUPERVISOR, "approve")
        await s.checkpoint()
        question = "Show every payment over $10,000 approved this week, and who approved it."
        answer = await s.post("/audit/ask", data={"question": question})
        assert answer.status_code == 200, answer.text[:300]
        assert "1 of 1 claims proven" in answer.text
        assert "claim--held" in answer.text
        faulty = await s.post("/audit/ask", data={"question": question, "inject_fault": "1"})
        assert "claim--rejected" in faulty.text
        assert "planted fault" in faulty.text
        blank = await s.post("/audit/ask", data={"question": "   "})
        assert blank.status_code == 400


async def test_the_attacks_page_needs_demo_mode_and_a_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stack(tmp_path) as s:
        await s.rig.gate.decide(AGENT, "create_payment", SMALL_PAYMENT)
        await s.checkpoint()
        assert "Set ADMIN_DATABASE_URL" in (await s.web.get("/demo")).text
        no_admin = await s.post("/demo/tamper", data={"seq": "1", "confirm": "tamper"})
        assert no_admin.status_code == 503
        s.console.extras["admin_database_url"] = "postgresql://test-only"
        unconfirmed = await s.post("/demo/tamper", data={"seq": "1", "confirm": "yes"})
        assert unconfirmed.status_code == 400

        async def fake_tamper(url: str, stream: str, seq: int) -> int:
            assert (url, stream, seq) == ("postgresql://test-only", STREAM, 1)
            return 1

        monkeypatch.setattr(demo, "tamper", fake_tamper)
        tampered = await s.post("/demo/tamper", data={"seq": "1", "confirm": "Tamper"})
        assert "Record 1 was edited in the database" in tampered.text
        rogue = await s.post("/demo/rogue")
        assert "Caught: the witnesses refused" in rogue.text
    async with stack(tmp_path / "plain", demo_mode=False) as s:
        assert (await s.web.get("/demo")).status_code == 404
        assert "Attacks" not in (await s.web.get("/")).text


async def test_the_console_says_when_gate_is_down(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        down = httpx.AsyncClient(base_url="http://127.0.0.1:9", timeout=0.5)
        s.console.backend_ = Backend(down, s.ledger, stream=STREAM, trust=TRUST, personas=PERSONAS)
        page = await s.web.get("/")
        assert page.status_code == 200
        assert "Unreachable" in page.text
        approvals = await s.web.get("/approvals")
        assert approvals.status_code == 503
        assert "Gate is not reachable" in approvals.text
        await down.aclose()


async def test_the_real_rogue_operator_scenario_is_caught() -> None:
    run = await demo.rogue_operator()
    assert run.caught, run.lines
    assert any("refused" in line for line in run.lines)


def test_the_tamper_sql_edits_one_payment_amount_only() -> None:
    assert "seq = %s" in demo.TAMPER_SQL
    assert "type = 'gate.decision'" in demo.TAMPER_SQL
    assert asyncio.iscoroutinefunction(demo.tamper)
    assert COOKIE == "pl_session"
    assert T0.tzinfo is not None


async def test_pl_web_wires_itself_from_the_dev_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.dev_keys import ensure_keys
    from web.app import app_from_settings

    keys = tmp_path / "keys"
    ensure_keys(keys)
    for name, value in {
        "KEYS_DIR": str(keys), "DATABASE_URL": "postgresql://test-only@127.0.0.1:1/none",
        "MODEL_PROFILE": "mock", "DEMO_MODE": "true", "WEB_GATE_URL": "http://127.0.0.1:9",
        "WEB_LEDGER_URL": "http://127.0.0.1:9", "WEB_TIMEOUT_SECONDS": "0.5",
        "ADMIN_DATABASE_URL": "postgresql://test-only-admin@127.0.0.1:1/none",
    }.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    app = app_from_settings()
    made: Console = app.state.console
    assert set(made.extras) == {"agent_runs", "audit", "admin_database_url"}
    assert set(made.sessions.personas) == {"j.ortiz", "m.lee", "a.chen", "r.patel"}
    with pytest.raises(WebError):
        _ = made.backend  # not until the app starts
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://web") as web,
    ):
        page = await web.get("/")
        assert page.status_code == 200
        assert page.text.count("Unreachable") == 2  # Gate and the ledger, both down
        assert "Attacks" in page.text
    monkeypatch.setenv("KEYS_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(WebError):
        app_from_settings()


def test_a_suggestion_drafted_against_replaced_rules_is_out_of_date() -> None:
    # Live, an "In review" proposal stayed on the page after a newer policy was approved; its
    # approval would fail Gate's stale-base check (D81), so it is shown as out of date.
    from collections import Counter

    from web.routes.learn import _status

    def proposal(state: str, base: str, needed: int = 1) -> dict[str, Any]:
        body = {"state": state, "approvals_needed": needed, "base": {"policy_record_id": base}}
        return {"record_id": "p1", "body": body}

    current = {"record_id": "A2"}
    none: Counter[str] = Counter()
    assert _status(proposal("review", "A2"), none, current) == "waiting"
    assert _status(proposal("review", "A1"), none, current) == "out_of_date"
    assert _status(proposal("review", "A1", 2), Counter({"p1": 2}), current) == "approved"
    assert _status(proposal("blocked", "A2"), none, current) == "blocked"
    assert _status(proposal("review", "A2"), none, None) == "out_of_date"
