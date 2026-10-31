"""The two web apps (web/), against the real thing: the ledger API with three witnesses, a real
pl-gate app (Gate, policy and learner routes) writing to it, and the Console and the Approvals
app in front of both, all in process. Pages are fetched as a browser would, with the session
cookie and CSRF token."""

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
from improve.api import install_improve_routes
from improve.propose import Proposer
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
from web.nav import APPROVALS, current, navigation
from web.security import COOKIE, COOKIES, CSP, Sessions

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
    web: httpx.AsyncClient  # the Console
    approvals: Console
    inbox: httpx.AsyncClient  # the Approvals app
    csrf: str = ""
    inbox_csrf: str = ""

    async def start(self) -> None:
        self.csrf = await _csrf_of(self.web, "/ledger")
        self.inbox_csrf = await _csrf_of(self.inbox, "/decided")

    async def post(
        self, path: str, *, htmx: bool = True, inbox: bool = False, **kwargs: Any
    ) -> httpx.Response:
        csrf, client = (self.inbox_csrf, self.inbox) if inbox else (self.csrf, self.web)
        headers = {"x-csrf-token": csrf, **({"hx-request": "true"} if htmx else {})}
        return await client.post(path, headers=headers, **kwargs)

    async def checkpoint(self) -> None:
        await publish(self.svc)


async def _csrf_of(client: httpx.AsyncClient, path: str) -> str:
    page = await client.get(path)
    found = _CSRF.search(page.text)
    assert found is not None, page.text[:500]
    return found.group(1)


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
        install_improve_routes(app, Proposer(LlmClient(config)))

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
        signing = Console(backend, sessions, ZONE, demo_mode, None, WebSettings(), {}, APPROVALS)
        transport = httpx.ASGITransport(app=create_web_app(made), raise_app_exceptions=False)
        inbox_transport = httpx.ASGITransport(app=create_web_app(signing),
                                              raise_app_exceptions=False)  # fmt: skip
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://web") as web,
            httpx.AsyncClient(transport=inbox_transport, base_url="http://inbox") as inbox,
        ):
            s = Stack(r, svc, ledger, made, web, signing, inbox)
            await s.start()
            yield s


async def switch(s: Stack, persona: str, *, inbox: bool = False) -> None:
    response = await s.post("/session/persona", htmx=False, inbox=inbox,
                            data={"persona": persona, "back": "/"})  # fmt: skip
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
    signing = navigation(APPROVALS, demo_mode=True)  # no attacks, drafting or ledger pages
    assert [(g, [p.title for p in pages]) for g, pages in signing] == [
        ("Decide", ["Waiting for you", "Decided"])
    ]
    assert current("/decided", APPROVALS).title == "Decided"  # type: ignore[union-attr]
    assert current("/approvals") is None  # moved to the Approvals app (D94)


def _draft(record_id: str, *, revises: str | None = None, findings: int = 0) -> dict[str, Any]:
    body = {"revises": revises, "findings": [{"rule_id": "r"}] * findings}
    return {"type": "policy.draft", "record_id": record_id, "body": body}


def test_the_inbox_lists_drafts_ready_to_approve() -> None:
    approval = {"type": "policy.approve", "record_id": "A1", "body": {}}
    newest_first = [
        _draft("D4", revises="D3"),  # the fixed revision: ready
        _draft("D3", findings=1),  # revised since, and Gate would refuse it anyway
        _draft("D2", findings=1),  # findings: fixed in the Console, not approved here
        approval,
        _draft("D1"),  # older than the rules in force
    ]
    assert [d["record_id"] for d in views.drafts_to_approve(newest_first, approval)] == ["D4"]
    assert [d["record_id"] for d in views.drafts_to_approve(newest_first, None)] == ["D4", "D1"]


def test_decided_lists_only_this_persons_signatures() -> None:
    records = [
        {"type": "gate.verdict", "record_id": "V1", "body": {"approver": "j.ortiz"}},
        {"type": "gate.verdict", "record_id": "V2", "body": {"approver": "m.lee"}},
        {"type": "policy.approve", "record_id": "P1", "body": {"approver": "j.ortiz"}},
        {"type": "gate.decision", "record_id": "G1", "body": {"approver": "j.ortiz"}},
    ]
    assert [r["record_id"] for r in views.decided_by(records, "j.ortiz")] == ["V1", "P1"]


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
        for path in ("/", "/agent", "/policy", "/learn", "/improve", "/ledger", "/audit", "/demo"):
            response = await s.web.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert 'aria-current="page"' in response.text
        for path in ("/", "/decided"):
            response = await s.inbox.get(path)
            assert response.status_code == 200, (path, response.text[:300])
            assert 'aria-current="page"' in response.text
            assert response.headers["content-security-policy"] == CSP


async def test_each_app_serves_only_its_own_routes(tmp_path: Path) -> None:
    # The Approvals app mounts no drafting, learner run, ledger, audit, agent or attack route,
    # and the Console cannot sign: separation of duties by what is mounted (D94).
    async with stack(tmp_path) as s:
        for path in ("/agent", "/policy", "/learn", "/improve", "/ledger", "/audit", "/demo",
                     "/approvals"):  # fmt: skip
            assert (await s.inbox.get(path)).status_code == 404, path
        for path in ("/policy/drafts", "/learn/run", "/improve/run", "/agent/runs", "/audit/ask",
                     "/demo/tamper", "/policy/drafts/x/revisions"):  # fmt: skip
            assert (await s.post(path, inbox=True, data={})).status_code == 404, path
        for path in ("/escalations/x/verdict", "/policy/drafts/x/approve",
                     "/learn/proposals/x/approve", "/improve/proposals/x/approve"):  # fmt: skip
            assert (await s.post(path, data={"verdict": "approve"})).status_code == 404, path
        for path in ("/approvals", "/decided", "/queue"):
            assert (await s.web.get(path)).status_code == 404, path
        console_page = await s.web.get("/")
        inbox_page = await s.inbox.get("/")
        assert "Open Approvals" in console_page.text
        assert "Open the Console" in inbox_page.text
        assert "Harbor Federal · Approvals" in inbox_page.text
        # Cookies ignore ports: each app has its own, so one never resets the other's persona.
        assert COOKIES["approvals"] != COOKIE
        fresh = httpx.ASGITransport(app=s.inbox._transport.app)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(transport=fresh, base_url="http://inbox") as anew:
            issued = (await anew.get("/decided")).headers["set-cookie"]
        assert issued.startswith(COOKIES["approvals"] + "=")


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


async def test_a_plain_form_switches_the_persona(tmp_path: Path) -> None:
    # a browser sends the CSRF token in the form body, not a header: the route must still see
    # the form after the middleware has read it
    async with stack(tmp_path) as s:
        for client, csrf in ((s.web, s.csrf), (s.inbox, s.inbox_csrf)):
            response = await client.post("/session/persona", data={
                "persona": "a.chen", "back": "/ledger" if client is s.web else "/decided",
                "csrf": csrf})  # fmt: skip
            assert response.status_code == 303
            assert response.headers["location"] in ("/ledger", "/decided")
            page = await client.get(response.headers["location"])
            assert "You are <strong>a.chen</strong>" in page.text


async def test_a_supervisor_approves_a_held_payment(tmp_path: Path) -> None:
    async with stack(tmp_path) as s:
        held = await s.rig.gate.decide(AGENT, "create_payment", DEMO_PAYMENT)
        assert held.escalation is not None
        inbox = await s.inbox.get("/")
        assert "payments-ops wants to run create_payment" in inbox.text
        assert "No rule changes are waiting" in inbox.text
        verdict = f"/escalations/{held.escalation.id}/verdict"
        await switch(s, "a.chen", inbox=True)  # the wrong role: Gate refuses, the app says so
        refused = await s.post(verdict, inbox=True, data={"verdict": "approve"})
        assert refused.status_code == 403
        assert refused.headers["hx-retarget"] == "#flash"
        assert "gate.approver_not_allowed" in refused.text
        await switch(s, "j.ortiz", inbox=True)
        bad = await s.post(verdict, inbox=True, data={"verdict": "maybe"})
        assert bad.status_code == 400
        done = await s.post(verdict, inbox=True, data={"verdict": "approve"})
        assert done.status_code == 200
        assert "Approved by j.ortiz" in done.text
        assert done.headers["hx-trigger"] == "queue-changed"
        assert "Nothing is waiting" in (await s.inbox.get("/queue")).text
        decided = await s.inbox.get("/decided")
        assert "create_payment by payments-ops: approve by j.ortiz" in decided.text
        assert "http://127.0.0.1:8090/ledger/records/" in decided.text  # proof is in the Console


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
        assert "Fix the findings first" in draft.text
        assert "Approve and enforce" not in draft.text  # the Console does not sign
        await switch(s, "a.chen", inbox=True)
        refused = await s.post(f"/policy/drafts/{draft_id}/approve", inbox=True)
        assert refused.status_code == 409
        assert "No rule changes are waiting" in (await s.inbox.get("/")).text  # findings
        yaml_text = (await s.console.backend.policy_draft(PERSONAS["a.chen"], draft_id))["record"]
        fixed = yaml_text["body"]["rules_yaml"].replace("lte: 50000", "lte: 5000")
        revised = await s.post(f"/policy/drafts/{draft_id}/revisions", htmx=False,
                               data={"rules_yaml": fixed})  # fmt: skip
        assert revised.status_code == 303
        clean = await s.web.get(revised.headers["location"])
        assert "No findings" in clean.text
        revision_id = revised.headers["location"].rsplit("/", 1)[1]
        assert f"http://127.0.0.1:8094/policy/drafts/{revision_id}" in clean.text
        await s.checkpoint()
        inbox = await s.inbox.get("/")
        assert f'href="/policy/drafts/{revision_id}"' in inbox.text
        assert "Policy draft" in inbox.text
        review = await s.inbox.get(f"/policy/drafts/{revision_id}")
        assert "Approve and enforce" in review.text
        assert "Save as a new draft" not in review.text  # revising is in the Console
        approved = await s.post(f"/policy/drafts/{revision_id}/approve", inbox=True)
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
        console_page = await s.web.get(f"/learn/proposals/{loosen.record_id}")
        assert "Approve in the Approvals app" in console_page.text
        assert "Approve as a.chen" not in console_page.text
        await switch(s, "a.chen", inbox=True)
        page = await s.inbox.get(f"/learn/proposals/{loosen.record_id}")
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


async def _improvement(tmp_path: Path, *, inject_fault: bool) -> dict[str, Any]:
    """An improve.proposal body scored on the seeded week (in its own ledger)."""
    from tests.unit.test_improve import improver
    from tests.unit.test_learn import OFFICER, seeded_week

    week = await seeded_week()
    return (await improver(week, tmp_path).run(OFFICER, inject_fault=inject_fault)).body


async def test_a_prompt_change_is_shown_with_its_scores_and_approved_by_two(
    tmp_path: Path,
) -> None:
    async with stack(tmp_path) as s:
        page = await s.web.get("/improve")
        assert "The prompt file" in page.text
        assert "act as a compliance officer" in page.text
        await switch(s, "a.chen")
        refused = await s.post("/improve/run", data={})
        assert refused.status_code == 409  # no approved policy on this stack yet
        assert "learn.no_active_policy" in refused.text
        # A change scored on the seeded week, recorded on this stack's ledger by its Gate.
        honest = await s.rig.gate.append_record(
            "improve.proposal", await _improvement(tmp_path, inject_fault=False)
        )
        shortcut = await s.rig.gate.append_record(
            "improve.proposal", await _improvement(tmp_path, inject_fault=True)
        )

        async def run_improver(persona: Persona, *, inject_fault: bool) -> dict[str, Any]:
            return {"proposal": honest.to_json()}  # the live run takes minutes on the model

        s.console.backend.run_improver = run_improver  # type: ignore[method-assign]
        ran = await s.post("/improve/run", data={})
        assert ran.headers["hx-location"] == f"/improve/proposals/{honest.record_id}"
        plain = await s.post("/improve/run", htmx=False, data={})
        assert plain.headers["location"] == f"/improve/proposals/{honest.record_id}"
        listing = (await s.web.get("/improve")).text
        assert "Waiting" in listing
        assert "Blocked" in listing
        shown = (await s.web.get(f"/improve/proposals/{honest.record_id}")).text
        assert "learn_proposal/3 to learn_proposal/4." in shown
        assert "Approve in the Approvals app" in shown  # the Console does not sign
        assert shown.count("Caught") == 16  # 2 patterns x 2 versions x 4 faults
        assert 'class="diff-add"' in shown
        blocked = (await s.web.get(f"/improve/proposals/{shortcut.record_id}")).text
        assert "Blocked by code (a planted shortcut)" in blocked
        assert "Nothing to check" in blocked
        assert "a planted amount fault would go unseen" in blocked
        assert (await s.web.get("/improve/proposals/01ZZZZZZZZZZZZZZZZZZZZZZZZ")).status_code == 404
        inbox = (await s.inbox.get("/")).text
        assert f'href="/improve/proposals/{honest.record_id}"' in inbox
        assert shortcut.record_id not in inbox
        await switch(s, "a.chen", inbox=True)
        path = f"/improve/proposals/{honest.record_id}"
        assert "Approve as a.chen" in (await s.inbox.get(path)).text
        first = await s.post(f"{path}/approve", inbox=True)
        assert first.status_code == 200, first.text[:300]
        assert "One more compliance officer must approve" in first.text
        await switch(s, "r.patel", inbox=True)
        second = await s.post(f"{path}/approve", inbox=True)
        assert "the learner uses this version" in second.text
        assert "In force" in (await s.web.get("/improve")).text
        assert "No prompt changes are waiting" in (await s.inbox.get("/")).text
        decided = (await s.inbox.get("/decided")).text
        assert "Prompt change approval 2 of 2 by r.patel" in decided
        assert (await s.post(f"{path}/approve", data={})).status_code == 404  # not in the Console


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
        s.approvals.backend_ = s.console.backend_
        page = await s.web.get("/")
        assert page.status_code == 200
        assert "Unreachable" in page.text
        approvals = await s.inbox.get("/")
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
    from web.app import app_from_settings, approvals_from_settings

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
    signing = approvals_from_settings()
    assert signing.state.console.app == APPROVALS
    assert signing.state.console.extras == {}  # no agent runner, auditor or admin connection
    paths = {getattr(r, "path", "") for r in signing.routes}
    assert "/agent" not in paths
    assert "/demo" not in paths
    monkeypatch.setenv("KEYS_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(WebError):
        app_from_settings()


def test_a_suggestion_drafted_against_replaced_rules_is_out_of_date() -> None:
    # Live, an "In review" proposal stayed on the page after a newer policy was approved; its
    # approval would fail Gate's stale-base check (D81), so it is shown as out of date.
    from collections import Counter

    _status = views.proposal_status

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


def test_a_prompt_change_is_waiting_in_force_replaced_or_out_of_date() -> None:
    def change(record_id: str, base: str | None, state: str = "review") -> dict[str, Any]:
        body = {"state": state, "approvals_needed": 2, "base": {"record_id": base}}
        return {"type": "improve.proposal", "record_id": record_id, "body": body}

    def approve(record_id: str, n: int) -> dict[str, Any]:
        body = {"proposal_record_id": record_id, "approval": n, "approvals_needed": 2}
        return {"type": "improve.approve", "record_id": f"a{record_id}{n}", "body": body}

    newest_first = [approve("C2", 2), approve("C2", 1), approve("C1", 2), approve("C1", 1)]
    assert views.improvement_in_force(newest_first) == "C2"
    assert views.improvement_in_force([approve("C1", 1)]) is None  # one approval is not enough
    counts = views.improvement_approvals(newest_first)
    status = views.improvement_status
    assert status(change("C2", "C1"), counts, "C2") == "in_force"
    assert status(change("C1", None), counts, "C2") == "replaced"
    assert status(change("C3", "C2"), counts, "C2") == "waiting"
    assert status(change("C4", "C1"), counts, "C2") == "out_of_date"
    assert status(change("C5", "C2", "blocked"), counts, "C2") == "blocked"
    diff = views.prompt_diff("a\nb\nc", "a\nB\nc")
    assert ("del", "b") in diff
    assert ("add", "B") in diff
    assert diff[0][0] == "hunk"
