"""Policy Learner (learn/), demo beat 6: the seeded week, played through the real Gate code with
an approved policy active, is counted by SQL, drafted into proposals (by the mock, or a fake
model), checked, replayed and reviewed; the approved change becomes a policy draft that still
needs a policy approval before Gate enforces it."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from assist.llm import LlmClient, ModelConfig, Route
from assist.mock import MOCK_MODEL, _learner_proposal
from claims.checker import Assertion, Claim, ClaimResult, Trust, check_summary
from claims.log import ProvenLog
from common.errors import LearnError, PolicyError
from gate.api import create_gate_app
from gate.auth import Callers, token_hash
from gate.core import Gate
from gate.decide import SCHEMA_FILE, PolicySet
from gate.records import DECISION, VERDICT
from learn import patterns
from learn.api import ExportView, LearnSettings, install_learn_routes
from learn.draft import LearnDrafter, model_input
from learn.patterns import Pattern, Scope, Window, approver_stats, find_patterns, load
from learn.replay import replay
from learn.service import APPROVAL, PROPOSAL, LearnService
from policy.checks import check_rules
from policy.rules import parse_rules, rules_from_data, to_context, to_yaml
from policy.service import APPROVE, DRAFT, Officer, PolicyService, draft_body
from scripts.seed_week import SimClock, plan_week, play
from tests.gate_helpers import LocalLedger, rig
from tests.helpers import STREAM
from tests.unit.test_assist_brief import llm
from tests.unit.test_assist_llm import FakeCompletion
from tests.unit.test_claims_checker import TRUST, World, world
from tests.unit.test_seed_week import END, gate_on

__all__ = ["world"]  # fixture
ROOT = Path(__file__).resolve().parents[2]
SEED_YAML = (ROOT / "deploy" / "policies" / "harbor-federal.yaml").read_text()
OFFICER = Officer("a.chen", frozenset({"compliance-officer"}))
SECOND = Officer("r.patel", frozenset({"compliance-officer"}))
OWNER = Officer("j.ortiz", frozenset({"compliance-officer", "payments-supervisor"}))
SUPERVISOR = Officer("m.lee", frozenset({"payments-supervisor"}))
OWNERS = {"payments-ops": "j.ortiz"}
LOOSEN, TIGHTEN = (
    "payments-ops/value.transfer/known/business",
    "payments-ops/value.transfer/new/after-hours",
)
SINCE = END - timedelta(days=7)


class MemoryView:
    """The learner's view of a ledger in memory; every record counts as proven."""

    def __init__(self, ledger: LocalLedger) -> None:
        self.ledger = ledger

    async def records(self) -> list[dict[str, Any]]:
        return await self.ledger.store.read_raw(STREAM)

    async def proven(self) -> ProvenLog:
        records = tuple(await self.records())
        by_id = {r["record_id"]: r for r in records}
        return ProvenLog(STREAM, len(records), bytes(32), records, 0, by_id)


@dataclass
class Week:
    gate: Gate
    ledger: LocalLedger
    clock: SimClock
    view: MemoryView
    policy: PolicyService

    async def records(self) -> list[dict[str, Any]]:
        return await self.view.records()

    def service(self, drafter: LearnDrafter | None, **kwargs: Any) -> LearnService:
        import asyncio

        return LearnService(self.gate, drafter, self.view, clock=self.clock, owners=OWNERS,
                            lock=asyncio.Lock(), **kwargs)  # fmt: skip


async def approve_seed_rules(gate: Gate, policy: PolicyService) -> str:
    rules = parse_rules(SEED_YAML)
    doc = {"name": "payments_policy.pdf", "sha256": "sha256:" + "0" * 64, "pages": 2}
    body = draft_body(to_yaml(rules), check_rules(rules), doc, model=None, prompt_version=None,
                      injected=None, revises=None, author=OFFICER.name)  # fmt: skip
    draft = await gate.append_record(DRAFT, body)
    return (await policy.approve(draft.record_id, OFFICER)).record_id


@pytest.fixture
async def week() -> Week:
    return await seeded_week()


async def seeded_week() -> Week:
    """The demo week, seeded through Gate with the seed rules approved first."""
    clock = SimClock(SINCE - timedelta(hours=1))
    gate, ledger = gate_on(clock)
    policy = PolicyService(gate, None)
    await approve_seed_rules(gate, policy)
    await play(gate, clock, plan_week(END))
    clock.advance_to(END)
    return Week(gate, ledger, clock, MemoryView(ledger), policy)


def mock_drafter(tmp_path: Path) -> LearnDrafter:
    route = Route(MOCK_MODEL, None, None)
    config = ModelConfig("mock", {"mock": {"spark": route}}, 5, tmp_path / "calls.csv")
    return LearnDrafter(LlmClient(config))


def body_of(records: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
    return next(r["body"] for r in records if r["record_id"] == record_id)


async def candidates(w: Week) -> dict[str, Pattern]:
    return {p.scope.key(): p for p in find_patterns(load(await w.records()), Window(SINCE))}


# --- patterns: SQL and the fatigue filter -------------------------------------------------


async def test_sql_finds_the_demo_patterns_and_sets_rubber_stamps_aside(week: Week) -> None:
    db = load(await week.records())
    stats = {a["approver"]: a for a in approver_stats(db, Window(SINCE))}
    assert set(stats) == {"j.ortiz", "m.lee"}
    assert all(a["approved"] < a["verdicts"] for a in stats.values())  # both reject sometimes
    found = {p.scope.key(): p for p in find_patterns(db, Window(SINCE))}
    loosen, tighten = found[LOOSEN], found[TIGHTEN]
    assert (loosen.direction, tighten.direction) == ("loosen", "tighten")
    assert loosen.counts == {"decisions": 38, "approved": 38, "rejected": 0, "expired": 0,
                             "fast_dropped": 6, "stamped_dropped": 0}  # fmt: skip
    assert {r["verdict_seconds"] < 5 for r in loosen.dropped} == {True}
    assert len(loosen.rows) == 38
    assert (loosen.scope.floor_cents, loosen.scope.ceiling_cents) == (500_000, 1_500_000)
    assert loosen.scope.statement() == (
        "Payments to known payees over $5,000 and up to $15,000 by payments-ops,"
        " Monday to Friday from 9:00 to 17:00."
    )
    assert tighten.counts["decisions"] == 20
    assert (tighten.counts["approved"], tighten.counts["rejected"]) == (6, 14)
    assert tighten.scope.unless() == patterns.BUSINESS
    assert tighten.scope.ceiling_cents is None
    skipped = {k: p.skipped for k, p in found.items() if p.direction is None}
    assert skipped == {
        "payments-ops/comm.external/any/business":
            "5 decisions after the fatigue filter; at least 20 needed",
        "payments-ops/value.transfer/known/after-hours":
            "2 decisions after the fatigue filter; at least 20 needed",
        "payments-ops/value.transfer/new/business":
            "6 decisions after the fatigue filter; at least 20 needed",
    }  # fmt: skip
    # The window is SQL too: nothing counts before it.
    later = find_patterns(db, Window(END - timedelta(hours=1)))
    assert all(p.counts["decisions"] < 20 for p in later)


def synthetic(n: int, approver: str, verdict: str, *, start: int, payee_new: bool = False,
              seconds: int = 60, amount: int = 900_000) -> list[dict[str, Any]]:  # fmt: skip
    out = []
    t0 = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)  # Tuesday 11:00 in New York
    for i in range(start, start + n):
        at = t0 + timedelta(minutes=i)
        decision_id, verdict_id = f"D{i:025d}", f"V{i:025d}"
        facts = {"amount_cents": amount, "payee_id": "acme-paper", "payee_new": payee_new,
                 "local_hour": 11, "local_weekday": 2}  # fmt: skip
        out.append({"type": DECISION, "record_id": decision_id, "ts": _ts(at),
                    "body": {"agent": "payments-ops", "tool": "create_payment",
                             "action": "value.transfer", "decision": "escalate",
                             "reason": "policy.escalate", "policy_ids": ["pay-over-5k-01"],
                             "resource": facts}})  # fmt: skip
        out.append({"type": VERDICT, "record_id": verdict_id,
                    "ts": _ts(at + timedelta(seconds=seconds)),
                    "body": {"decision_record_id": decision_id, "verdict": verdict,
                             "approver": approver}})  # fmt: skip
    return out


def _ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_an_approver_who_approves_everything_is_set_aside() -> None:
    since = datetime(2026, 10, 27, tzinfo=UTC)
    stamps = synthetic(24, "x.always", "approve", start=0)
    careful = synthetic(10, "y.careful", "approve", start=100, payee_new=True)
    careful += synthetic(10, "y.careful", "reject", start=200, payee_new=True)
    db = load(stamps + careful)
    (known,) = [p for p in find_patterns(db, Window(since)) if p.scope.payee_new is False]
    assert known.stamped_approvers == ("x.always",)
    assert (known.counts["decisions"], known.counts["stamped_dropped"]) == (0, 24)
    assert known.direction is None  # nothing left to learn from
    (new,) = [p for p in find_patterns(db, Window(since)) if p.scope.payee_new is True]
    assert (new.direction, new.counts["rejected"]) == ("tighten", 10)  # rejections never dropped
    # Half approved and half rejected is not enough to tighten if it falls short of the share.
    mixed = load(synthetic(15, "y.careful", "approve", start=0)
                 + synthetic(6, "y.careful", "reject", start=50))  # fmt: skip
    (pattern,) = find_patterns(mixed, Window(since))
    assert pattern.direction is None
    assert "neither all approved" in str(pattern.skipped)


def test_amounts_above_the_cap_are_never_loosened() -> None:
    since = datetime(2026, 10, 27, tzinfo=UTC)
    big = synthetic(25, "y.careful", "approve", start=0, amount=30_000_000)
    db = load(big + synthetic(3, "y.careful", "reject", start=100, payee_new=True))
    (pattern, _) = find_patterns(db, Window(since))
    assert pattern.direction is None
    assert pattern.skipped == "amounts above $250,000 are never loosened"


# --- beat 6 end to end --------------------------------------------------------------------


async def test_beat_6_proposals_replayed_reviewed_and_enforced(week: Week, tmp_path: Path) -> None:
    service = week.service(mock_drafter(tmp_path))
    run = await service.run(OFFICER, window_days=7)
    assert [p["key"] for p in run.skipped] == [
        "payments-ops/comm.external/any/business", "payments-ops/value.transfer/known/after-hours",
        "payments-ops/value.transfer/new/business",
    ]  # fmt: skip
    assert run.failed == ()
    loosen, tighten = (p.body for p in run.proposals)
    assert (loosen["direction"], tighten["direction"]) == ("loosen", "tighten")
    for body in (loosen, tighten):
        assert body["state"] == "review", body["reasons"]
        assert all(c["accepted"] for c in body["claims"])
        assert len(body["claims"]) == 3
        assert body["model"] == MOCK_MODEL
        assert body["injected"] is None
        assert body["base"]["policy_record_id"] == week.gate.policy_record_id
    assert loosen["stated_counts"] == {"decisions": 38, "approved": 38, "fast_dropped": 6}
    assert loosen["pattern"]["counts"]["decisions"] == 38
    assert len(loosen["pattern"]["fatigue"]["dropped_decision_record_ids"]) == 6
    replayed = loosen["replay"]
    assert (replayed["escalations_removed"], replayed["rejections_newly_allowed"]) == (44, 0)
    assert (replayed["outside_scope"], replayed["new_denials"], replayed["blocked"]) == (0, 0, [])
    assert replayed["replayed"] == 169
    assert (tighten["replay"]["new_denials"], tighten["replay"]["approvals_now_denied"]) == (20, 6)
    assert (loosen["approvals_needed"], tighten["approvals_needed"]) == (2, 1)
    changed = [r.id for r in parse_rules(loosen["rules_yaml"])]
    assert changed == ["pay-known-business-allow-01", "pay-known-payee-01", "pay-over-5k-01"]
    learned = parse_rules(loosen["rules_yaml"])[0]
    assert (learned.source.doc, learned.source.clause) == ("learner", "learned")
    amended = parse_rules(loosen["rules_yaml"])[2]
    assert amended.source.clause == "3.2"
    assert "Learned exception:" in amended.source.quote

    loosen_id, tighten_id = (p.record_id for p in run.proposals)
    # Review: the owner may not loosen; one approval is not enough; nobody approves twice.
    with pytest.raises(LearnError) as owner:
        await service.approve(loosen_id, OWNER)
    assert owner.value.code == "learn.owner"
    with pytest.raises(LearnError) as role:
        await service.approve(loosen_id, SUPERVISOR)
    assert role.value.code == "learn.forbidden"
    first = await service.approve(loosen_id, OFFICER)
    assert first.policy_draft is None
    assert first.approval.body["approval"] == 1
    with pytest.raises(LearnError) as twice:
        await service.approve(loosen_id, OFFICER)
    assert twice.value.code == "learn.already_approved"
    tightened = await service.approve(tighten_id, OWNER)  # tightening: one approver, owner allowed
    assert tightened.policy_draft is not None
    second = await service.approve(loosen_id, SECOND)
    assert second.policy_draft is not None
    with pytest.raises(LearnError) as done:
        await service.approve(loosen_id, OWNER)
    assert done.value.code in ("learn.not_reviewable", "learn.owner")
    assert [a.body["approver"] for a in await service.approvals(loosen_id)] == ["a.chen", "r.patel"]

    # The learner activated nothing: Gate still holds a routine $9,000 payment...
    routine = to_context("value.transfer", {"amount": 9000, "payee_new": False})
    assert (
        week.gate.policies.evaluate("payments-ops", "value.transfer", "pay", routine).outcome
        == "escalate"
    )
    # ...until a compliance officer approves the learner's policy draft, like any policy.
    draft = second.policy_draft.body
    assert draft["proposal_record_id"] == loosen_id
    assert draft["author"] == "learner"
    assert draft["learn_approvals"] == [first.approval.record_id, second.approval.record_id]
    assert draft["findings"] == []
    await week.policy.approve(second.policy_draft.record_id, OFFICER)
    assert (
        week.gate.policies.evaluate("payments-ops", "value.transfer", "pay", routine).outcome
        == "allow"
    )
    night = to_context("value.transfer", {"amount": 9000, "payee_new": False, "local_hour": 20})
    assert (
        week.gate.policies.evaluate("payments-ops", "value.transfer", "pay", night).outcome
        == "escalate"
    )
    # The tightening draft was built on the set that is no longer active: refused, not merged.
    with pytest.raises(PolicyError) as stale:
        await week.policy.approve(tightened.policy_draft.record_id, OFFICER)
    assert stale.value.code == "policy.stale_base"
    # The history still holds the 38 approvals; the active set settles them, so no re-proposal.
    rerun = await service.run(OFFICER)
    assert [p.body["direction"] for p in rerun.proposals] == ["tighten"]
    assert {"key": LOOSEN, "reason": "the active policy no longer holds these requests",
            "counts": loosen["pattern"]["counts"]} in rerun.skipped  # fmt: skip
    types = [r["type"] for r in await week.records()]
    assert types.count(PROPOSAL) == 3  # two, and the rerun's tightening
    assert types.count(APPROVAL) == 3
    assert types.count(APPROVE) == 2


async def test_the_model_sees_each_row_as_its_two_records_with_the_checked_fields(
    week: Week,
) -> None:
    # Live, the model asserted body.decision 'approved' on decision records: the verdict sits
    # on its own record. Rows now show each field under the path the claim checker reads.
    records = await week.records()
    for pattern in (await candidates(week)).values():
        rows = json.loads(model_input(pattern, parse_rules(SEED_YAML)).split("Pattern:\n", 1)[1])
        for row in rows["rows"]:
            assert set(row) == {"decision_record", "verdict_record", "seconds_to_verdict"}
            assert row["decision_record"]["body.decision"] == "escalate"
            for shown in (row["decision_record"], row["verdict_record"]):
                body = body_of(records, shown["record_id"])
                for path, value in shown.items():
                    if path == "record_id":
                        continue
                    held: Any = {"body": body}
                    for part in path.split("."):
                        held = held[part]
                    assert held == value, (path, value)
            assert row["verdict_record"]["body.verdict"] in ("approve", "reject", "expire")


async def test_code_marks_the_fixtures_a_change_would_move(week: Week) -> None:
    # Live, the model kept a fixture the new allow rule now covers, at every reasoning effort.
    found = await candidates(week)
    for key, expected in ((LOOSEN, {"pay-known-payee-01": [3]}), (TIGHTEN, {})):
        view = json.loads(model_input(found[key], parse_rules(SEED_YAML)).split("Pattern:\n")[1])
        marked = {r["id"]: r["fixtures_to_move"] for r in view["active_rules"]
                  if r["fixtures_to_move"]}  # fmt: skip
        assert marked == expected


def test_the_reason_may_use_the_learners_own_words_but_no_invented_name(week: Week) -> None:
    from learn.draft import reason_names

    scope = Scope("payments-ops", "value.transfer", False, True, 500000, 1500000)
    words = reason_names(scope, ["pay-known-payee-01"])
    text = (
        "Known-payee payments in business-hours were approved; fast-dropped and "
        "stamped_dropped approvals were set aside, unlike after-hours new-payee ones."
    )
    assert check_summary(text, [], names=words) == []
    stray = check_summary(text + " One went to cobalt-freight.", [], names=words)
    assert stray == ["names in the text that no assertion establishes: ['cobalt-freight']"]


async def test_patterns_are_drafted_together_and_recorded_in_order(
    week: Week, tmp_path: Path
) -> None:
    # Live, one draft takes about 40 s at medium effort; one after another missed the
    # console's timeout. Records still go to the ledger in pattern order.
    import asyncio

    started: list[str] = []
    both = asyncio.Event()

    class Together(LearnDrafter):
        async def draft(self, pattern: Pattern, *args: Any, **kwargs: Any) -> Any:
            started.append(pattern.scope.key())
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2)  # times out if drafted in turn
            return await super().draft(pattern, *args, **kwargs)

    route = Route(MOCK_MODEL, None, None)
    config = ModelConfig("mock", {"mock": {"spark": route}}, 5, tmp_path / "calls.csv")
    run = await week.service(Together(LlmClient(config))).run(OFFICER)
    assert [p.body["direction"] for p in run.proposals] == ["loosen", "tighten"]
    assert sorted(started) == [LOOSEN, TIGHTEN]


async def test_an_inflated_count_rejects_the_proposal(week: Week, tmp_path: Path) -> None:
    service = week.service(mock_drafter(tmp_path))
    run = await service.run(OFFICER, inject_fault=True)
    loosen = run.proposals[0].body
    assert loosen["state"] == "rejected"
    assert loosen["injected"] == "counts.decisions"
    assert loosen["stated_counts"]["decisions"] == 48
    assert loosen["reasons"] == ["count decisions: stated 48, SQL counted 38"]
    with pytest.raises(LearnError) as refused:
        await service.approve(run.proposals[0].record_id, OFFICER)
    assert refused.value.code == "learn.not_reviewable"


def answer_for(pattern: Pattern, base: str = SEED_YAML) -> dict[str, Any]:
    return _learner_proposal(model_input(pattern, parse_rules(base)))


async def test_a_wider_rule_or_a_wrong_claim_is_caught(week: Week, tmp_path: Path) -> None:
    found = await candidates(week)
    wide = answer_for(found[LOOSEN])
    for rule in wide["rules"]:  # the model widens $15,000 to $50,000
        for c in [*rule["when"], *rule["unless"]]:
            if c["field"] == "amount" and c["value"] == 15000:
                c["value"] = 50000
    wrong = answer_for(found[TIGHTEN])
    for a in wrong["claims"][0]["assertions"]:
        if a["field"] == "body.approver":
            a["value"] = "k.wu" if a["value"] != "k.wu" else "j.ortiz"
    fake = FakeCompletion(json.dumps(wide), json.dumps(wrong))
    run = await week.service(LearnDrafter(llm(tmp_path, fake))).run(OFFICER)
    loosen, tighten = (p.body for p in run.proposals)
    assert loosen["state"] == "blocked"
    assert any("clause_number" in r and "50000" in r for r in loosen["reasons"]), loosen["reasons"]
    assert tighten["state"] == "rejected"
    assert tighten["reasons"][0].startswith("claim 1 rejected:")
    assert "body.approver eq 'k.wu' is false" in tighten["reasons"][0]


async def test_structure_gets_one_retry_and_a_model_failure_gives_no_proposal(
    week: Week, tmp_path: Path
) -> None:
    found = await candidates(week)
    stray = answer_for(found[LOOSEN])
    stray["claims"][0]["record_ids"].append("01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    fake = FakeCompletion(json.dumps(stray), json.dumps(answer_for(found[LOOSEN])),
                          RuntimeError("model down"))  # fmt: skip
    run = await week.service(LearnDrafter(llm(tmp_path, fake))).run(OFFICER)
    assert [p.body["state"] for p in run.proposals] == ["review"]
    retry = fake.calls[1]["messages"][-1]["content"]
    assert "not in the pattern's rows" in retry
    assert run.failed == ({"key": TIGHTEN, "code": "model.refused"},)


async def test_the_learner_refuses_without_what_it_needs(week: Week, tmp_path: Path) -> None:
    service = week.service(mock_drafter(tmp_path))
    with pytest.raises(LearnError) as role:
        await service.run(SUPERVISOR)
    assert role.value.code == "learn.forbidden"
    with pytest.raises(LearnError) as window:
        await service.run(OFFICER, window_days=0)
    assert window.value.code == "learn.window"
    with pytest.raises(LearnError) as model:
        await week.service(None).run(OFFICER)
    assert model.value.code == "learn.no_model"
    bare = rig()  # Gate on its policy file: nothing approved to learn against
    view = MemoryView(bare.ledger)
    import asyncio

    plain = LearnService(bare.gate, mock_drafter(tmp_path), view, clock=bare.clock, owners={},
                         lock=asyncio.Lock())  # fmt: skip
    with pytest.raises(LearnError) as none:
        await plain.run(OFFICER)
    assert none.value.code == "learn.no_active_policy"
    week.gate.activate(bare.gate.policies, str(week.gate.policy_record_id))  # out of step
    with pytest.raises(LearnError) as mismatch:
        await service.run(OFFICER)
    assert mismatch.value.code == "learn.base_mismatch"


# --- replay ------------------------------------------------------------------------------


def _set(rules: Any) -> PolicySet:
    return PolicySet(check_rules(rules).text, SCHEMA_FILE.read_text())


async def test_replay_blocks_what_a_loosening_or_tightening_must_not_do(week: Week) -> None:
    records = await week.records()
    base = parse_rules(SEED_YAML)
    before = _set(base)
    scope = (await candidates(week))[LOOSEN].scope
    # Drop every escalation rule and allow every transfer up to the cap: past rejections pass.
    loose = [r for r in base if r.id not in ("pay-over-5k-01", "pay-new-payee-01")]
    loose += rules_from_data([{
        "id": "pay-anything-01", "source": {"doc": "t", "page": 1, "clause": "t", "quote": "t"},
        "agent": "payments-ops", "when": {"action": "value.transfer"}, "then": "allow",
        "tests": [{"expect": "allow"}],
    }])  # fmt: skip
    result = replay(records, SINCE, base_rules=base, before=before, after=_set(loose),
                    scope=scope, direction="loosen")  # fmt: skip
    counts = result.counts()
    assert counts["rejections_newly_allowed"] == 14
    assert counts["outside_scope"] > 0
    assert result.blocked[0] == "it would allow 14 past request(s) a person rejected or let expire"
    assert any("outside the pattern" in b for b in result.blocked)
    assert any("active fixture(s)" in b for b in result.blocked)
    as_tightening = replay(records, SINCE, base_rules=base, before=before, after=_set(loose),
                           scope=scope, direction="tighten")  # fmt: skip
    assert as_tightening.blocked[0].startswith("a tightening that makes")
    same = replay(records, SINCE, base_rules=base, before=before, after=before, scope=scope,
                  direction="loosen")  # fmt: skip
    assert same.blocked == ("it changes no decision in the window",)
    strict = replay(records, SINCE, base_rules=base, before=_set(loose), after=before,
                    scope=scope, direction="loosen")  # fmt: skip
    assert any(b.startswith("a loosening that makes") for b in strict.blocked)


# --- the HTTP API and the export view ------------------------------------------------------

OFFICER_TOKEN, SECOND_TOKEN = "test-only-learn-officer", "test-only-learn-second"  # TEST ONLY
SUPERVISOR_TOKEN = "test-only-learn-supervisor"  # TEST ONLY
CALLERS = Callers.from_text(
    f"approver a.chen compliance-officer {token_hash(OFFICER_TOKEN)}\n"
    f"approver r.patel compliance-officer {token_hash(SECOND_TOKEN)}\n"
    f"approver m.lee payments-supervisor {token_hash(SUPERVISOR_TOKEN)}\n"
)


@asynccontextmanager
async def api(w: Week, tmp_path: Path, *, demo_mode: bool) -> AsyncIterator[httpx.AsyncClient]:
    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield w.gate, CALLERS

    def extend(app: Any) -> None:
        install_learn_routes(app, mock_drafter(tmp_path), w.view, LearnSettings(),
                             demo_mode=demo_mode, clock=w.clock)  # fmt: skip

    app = create_gate_app(open_gate, sweep_interval=None, extend=extend)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gate") as http,
    ):
        yield http


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_beat_6_over_http(week: Week, tmp_path: Path) -> None:
    async with api(week, tmp_path, demo_mode=False) as http:
        injected = await http.post("/learn/v1/run?inject_fault=true", headers=auth(OFFICER_TOKEN))
        assert (injected.status_code, injected.json()["error"]["code"]) == (403, "learn.forbidden")
        bad = await http.post("/learn/v1/run?window=week", headers=auth(OFFICER_TOKEN))
        assert (bad.status_code, bad.json()["error"]["code"]) == (400, "learn.window")
        supervisor = await http.post("/learn/v1/run", headers=auth(SUPERVISOR_TOKEN))
        assert supervisor.status_code == 403
        assert (await http.post("/learn/v1/run")).status_code == 401
        size = len(await week.records())
        run = await http.post("/learn/v1/run?window=7d", headers=auth(OFFICER_TOKEN))
        assert run.status_code == 201, run.text
        assert run.json()["log"]["size"] == size  # counted before its own proposals
        assert len(run.json()["proposals"]) == 2
        loosen_id = run.json()["proposals"][0]["record"]["record_id"]
        path = f"/learn/v1/proposals/{loosen_id}"
        first = await http.post(f"{path}/approve", headers=auth(OFFICER_TOKEN))
        assert first.status_code == 201
        assert first.json()["policy_draft"] is None
        again = await http.post(f"{path}/approve", headers=auth(OFFICER_TOKEN))
        assert (again.status_code, again.json()["error"]["code"]) == (409, "learn.already_approved")
        second = await http.post(f"{path}/approve", headers=auth(SECOND_TOKEN))
        assert second.status_code == 201
        assert second.json()["policy_draft"]["record"]["type"] == DRAFT
        shown = (await http.get(path, headers=auth(OFFICER_TOKEN))).json()
        assert shown["proposal"]["record"]["body"]["direction"] == "loosen"
        assert [a["record"]["body"]["approver"] for a in shown["approvals"]] == [
            "a.chen",
            "r.patel",
        ]
        missing = await http.get("/learn/v1/proposals/01ZZZZZZZZZZZZZZZZZZZZZZZZ",
                                 headers=auth(OFFICER_TOKEN))  # fmt: skip
        assert missing.status_code == 404


async def test_the_export_view_counts_only_a_proven_log(world: World) -> None:
    view = ExportView("http://test", STREAM, TRUST, 5, http=world.http)
    log = await view.proven()
    assert log.get(world.decision_id) is not None
    assert log.size >= 3
    assert {r["type"] for r in await view.records()} >= {DECISION, VERDICT}
    other = Trust(TRUST.keys, "another-origin", TRUST.witnesses)
    with pytest.raises(LearnError) as unproven:
        await ExportView("http://test", STREAM, other, 5, http=world.http).proven()
    assert unproven.value.code == "learn.log_unproven"
    with pytest.raises(LearnError) as down:
        await ExportView("http://127.0.0.1:9", STREAM, TRUST, 0.5).proven()
    assert down.value.code == "learn.ledger_unavailable"


def test_settings_parse_agent_owners() -> None:
    settings = LearnSettings(agent_owners="payments-ops=j.ortiz, treasury-bot = k.wu,junk")
    assert settings.owners() == {"payments-ops": "j.ortiz", "treasury-bot": "k.wu"}


def test_a_summary_may_name_ids_code_supplied() -> None:
    held = ClaimResult(
        Claim("x", ("R",), (Assertion("R", "body.approver", "eq", "j.ortiz"),)), True
    )
    text = "j.ortiz approved; pay-over-5k-01 changes."
    assert check_summary(text, [held]) != []
    assert check_summary(text, [held], names=["pay-over-5k-01"]) == []


def test_scope_statements_for_every_action() -> None:
    email = Scope("payments-ops", "comm.external", None, True, None, None)
    assert email.statement() == (
        "Email sent outside the bank by payments-ops, Monday to Friday from 9:00 to 17:00."
    )
    assert email.when() == patterns.BUSINESS
    assert email.unless() == {}
    reads = Scope("payments-ops", "data.read", None, False, None, None)
    assert reads.statement().startswith("Data reads by payments-ops outside business hours")
    assert reads.key() == "payments-ops/data.read/any/after-hours"
    small = Scope("payments-ops", "value.transfer", False, True, 0, 500_000)
    assert small.when()["amount"] == {"lte": 5000}
    assert "up to $5,000" in small.statement()
    assert "over" not in small.statement()


@pytest.mark.live
async def test_beat_6_on_muse_spark(week: Week, tmp_path: Path) -> None:
    from assist.llm import model_config
    from common.config import load_settings

    config = model_config(load_settings())
    if config is None or config.profile != "cloud":
        pytest.skip("needs MODEL_PROFILE=cloud and META_API_KEY")
    run = await week.service(LearnDrafter(LlmClient(config))).run(OFFICER)
    for proposal in run.proposals:
        body = proposal.body
        print(body["direction"], body["state"], body["title"], body["reasons"])
        print(body["rules_yaml"])
    assert run.proposals, run.failed
