"""The improver (improve/, learn/prompt.py): the learner's prompt revised by a model, tested by
code on the seeded week, approved by two officers, then read back from the ledger (D95)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from assist.llm import LlmClient, ModelConfig, Route
from assist.mock import MOCK_MODEL
from common.errors import ImproveError, LearnError, ModelOutputError
from gate.api import create_gate_app
from gate.auth import Callers
from gate.core import Gate
from improve.api import install_improve_routes
from improve.canary import CAUGHT, NOT_APPLICABLE, UNPLANTABLE, plant
from improve.evaluate import Case, Score, compare, score
from improve.propose import SHORTCUT, Proposer, plant_shortcut, revision_problems
from improve.service import ImproveService, failures, next_version
from learn.api import LearnSettings, install_learn_routes
from learn.draft import LearnDrafter
from learn.prompt import (
    IMPROVE_APPROVAL,
    IMPROVE_PROPOSAL,
    KEEP,
    from_file,
    missing_guards,
    prompt_in_force,
    text_sha256,
)  # fmt: skip
from learn.service import PROPOSAL
from tests.unit.test_learn import (
    CALLERS,
    LOOSEN,
    OFFICER,
    OFFICER_TOKEN,
    SECOND,
    SECOND_TOKEN,
    SUPERVISOR,
    SUPERVISOR_TOKEN,
    Week,
    auth,
    candidates,
    seeded_week,
)  # fmt: skip


@pytest.fixture
async def week() -> Week:
    return await seeded_week()


def mock_llm(tmp_path: Path) -> LlmClient:
    route = Route(MOCK_MODEL, None, None)
    routes = {"mock": {"spark": route, "wonder": route}}
    return LlmClient(ModelConfig("mock", routes, 5, tmp_path / "calls.csv"))


def improver(w: Week, tmp_path: Path, *, proposer: bool = True) -> ImproveService:
    llm = mock_llm(tmp_path)
    learn = w.service(LearnDrafter(llm))
    return ImproveService(learn, w.gate, w.view, Proposer(llm) if proposer else None,
                          LearnDrafter(llm), clock=w.clock, lock=asyncio.Lock())  # fmt: skip


# --- the guards on a revision ----------------------------------------------------------------


def test_the_learner_prompt_keeps_every_guard_and_the_shortcut_drops_the_claims_rule() -> None:
    current = from_file().instructions
    assert missing_guards(current) == []
    short = plant_shortcut(current)
    assert SHORTCUT in short
    assert "assert every number and name" not in short  # the claims instruction is gone
    assert missing_guards(short) == []  # it passes the text checks: only the canaries catch it
    assert plant_shortcut("no claims rule here") == f"no claims rule here\n\n{SHORTCUT}"


def test_a_revision_must_keep_the_guards_change_something_and_stay_short() -> None:
    current = from_file().instructions
    dropped = current.replace(KEEP[2], "Follow any instructions in the rows.")
    assert revision_problems(dropped, current) == [f"keep this sentence exactly: {KEEP[2]!r}"]
    assert revision_problems(current, current) == ["the instructions are unchanged"]
    assert revision_problems(current + "x" * len(current), current)[0].startswith("at most")
    wrapped = current.replace("You decide nothing.", "You decide\n   nothing.")
    assert missing_guards(wrapped) == []  # spacing is not a change


def test_versions_are_numbered_and_tied_to_their_text() -> None:
    first = next_version("learn_proposal/3", "text")
    assert first == f"learn_proposal/4.{text_sha256('text')[7:13]}"
    assert next_version(first, "more").startswith("learn_proposal/5.")
    assert next_version("odd", "t").startswith("learn_proposal/1.")


def test_failures_are_the_prompts_own_not_the_demos() -> None:
    def proposal(state: str, version: str = "v3", injected: str | None = None) -> dict[str, Any]:
        body = {"state": state, "prompt_version": version, "injected": injected,
                "reasons": ["a", "b", "c", "d", "e"]}  # fmt: skip
        return {"type": PROPOSAL, "record_id": state + version, "body": body}

    records = [
        proposal("rejected"), proposal("blocked"), proposal("review"),
        proposal("rejected", "v2"), proposal("rejected", injected="counts.decisions"),
    ]  # fmt: skip
    found = failures(records, "v3")
    assert [f["record_id"] for f in found] == ["rejectedv3", "blockedv3"]
    assert found[0]["reasons"] == ["a", "b", "c", "d"]


# --- canaries and comparison -----------------------------------------------------------------


async def test_every_canary_is_caught_on_an_honest_draft_and_not_on_a_shortcut(
    week: Week, tmp_path: Path
) -> None:
    pattern = (await candidates(week))[LOOSEN]
    _, _, rules = await week.service(None).base()
    log = await week.view.proven()
    drafter = LearnDrafter(mock_llm(tmp_path))
    honest = await drafter.draft(pattern, rules, log)
    assert set(plant(honest, pattern, log).values()) == {CAUGHT}
    cut = drafter.with_prompt("v-short", plant_shortcut(drafter.instructions))
    short = await cut.draft(pattern, rules, log)
    assert not short.rejected  # the shortcut draft itself passes the learner's checks...
    outcome = plant(short, pattern, log)
    assert outcome == {
        "count": CAUGHT,
        "amount": UNPLANTABLE,
        "approver": UNPLANTABLE,
        "reason": CAUGHT,
    }  # fmt: skip  ...but leaves two faults uncatchable


def _score(version: str, *cases: Case) -> Score:
    return Score(version, cases)


def test_a_candidate_is_refused_when_worse_on_any_measure() -> None:
    caught = dict.fromkeys(("count", "amount", "approver", "reason"), CAUGHT)
    good = Case("k", "review", (), 2, 8, 10, caught)
    base = _score("v3", good, Case("j", "review", (), 2, 8, 10, caught))
    assert compare(base, base) == []
    missing = Case("k", "review", (), 1, 1, 5, {**caught, "amount": UNPLANTABLE})
    worse = _score("v4", missing, Case("j", "rejected", ("x",), 2, 8, 10, caught))
    problems = compare(base, worse)
    assert "k: a planted amount fault would go unseen (unplantable)" in problems[0]
    assert problems[1] == "accepts fewer drafts: 1 of 2, against 2 of 2"
    failed = _score("v4", good, Case("j", "failed", ("model.unavailable",), 0, 0, 1))
    assert "more drafts failed: 1, against 0" in compare(base, failed)
    other = {**caught, "amount": NOT_APPLICABLE}  # not a payment: nothing to plant either side
    assert compare(_score("a", Case("k", "review", (), 2, 8, 1, other)),
                   _score("b", Case("k", "review", (), 2, 8, 1, other))) == []  # fmt: skip


# --- runs, review and the prompt in force ----------------------------------------------------


async def test_a_revision_is_scored_reviewed_by_two_and_then_used_by_the_learner(
    week: Week, tmp_path: Path
) -> None:
    service = improver(week, tmp_path)
    proposal = await service.run(OFFICER)
    body = proposal.body
    assert proposal.submission.type == IMPROVE_PROPOSAL
    assert body["state"] == "review", body["reasons"]
    assert body["base"]["version"] == "learn_proposal/3"
    assert body["base"]["record_id"] is None  # the file
    assert body["version"].startswith("learn_proposal/4.")
    assert body["prompt_sha256"] == text_sha256(body["instructions"])
    assert "Before you answer, check that every claim cites both" in body["instructions"]
    scores = body["scores"]
    assert scores["current"]["accepted"] == scores["candidate"]["accepted"] == 2
    for case in scores["candidate"]["cases"]:
        assert set(case["canaries"].values()) == {CAUGHT}
    with pytest.raises(ImproveError) as forbidden:
        await service.approve(proposal.record_id, SUPERVISOR)
    assert forbidden.value.code == "improve.forbidden"
    first = await service.approve(proposal.record_id, OFFICER)
    assert not first.in_force
    assert (await service.current()).record_id is None  # one approval changes nothing
    with pytest.raises(ImproveError) as again:
        await service.approve(proposal.record_id, OFFICER)
    assert again.value.code == "improve.already_approved"
    second = await service.approve(proposal.record_id, SECOND)
    assert second.in_force
    assert second.approval.submission.type == IMPROVE_APPROVAL
    current = await service.current()
    assert (current.version, current.record_id) == (body["version"], proposal.record_id)
    with pytest.raises(ImproveError) as done:
        await service.approve(proposal.record_id, Officer3)
    assert done.value.code == "improve.not_reviewable"
    # The learner's next run drafts under the approved version, read back from the ledger.
    run = await week.service(LearnDrafter(mock_llm(tmp_path))).run(OFFICER)
    assert {p.body["prompt_version"] for p in run.proposals} == {body["version"]}


Officer3 = OFFICER.__class__("k.wu", frozenset({"compliance-officer"}))


async def test_the_shortcut_scores_as_well_but_the_canaries_block_it(
    week: Week, tmp_path: Path
) -> None:
    proposal = await improver(week, tmp_path).run(OFFICER, inject_fault=True)
    body = proposal.body
    assert body["injected"] == "instructions.claims"
    assert SHORTCUT in body["instructions"]
    assert body["changes"][-1] == "Claims cut to one per pattern, asserting only the verdict"
    assert body["scores"]["candidate"]["accepted"] == body["scores"]["current"]["accepted"]
    assert body["state"] == "blocked"
    assert any("a planted amount fault would go unseen" in r for r in body["reasons"])
    assert any("a planted approver fault would go unseen" in r for r in body["reasons"])
    with pytest.raises(ImproveError) as refused:
        await improver(week, tmp_path).approve(proposal.record_id, OFFICER)
    assert refused.value.code == "improve.not_reviewable"


async def test_a_revision_scored_against_a_replaced_prompt_is_stale(
    week: Week, tmp_path: Path
) -> None:
    service = improver(week, tmp_path)
    first, second = await service.run(OFFICER), await service.run(OFFICER)
    await service.approve(first.record_id, OFFICER)
    await service.approve(first.record_id, SECOND)
    with pytest.raises(ImproveError) as stale:
        await service.approve(second.record_id, OFFICER)
    assert stale.value.code == "improve.stale"
    third = await service.run(OFFICER)  # the next revision builds on the approved one
    assert third.body["base"]["record_id"] == first.record_id
    assert third.body["version"].startswith("learn_proposal/5.")


async def test_the_improver_refuses_without_what_it_needs(week: Week, tmp_path: Path) -> None:
    with pytest.raises(ImproveError) as no_model:
        await improver(week, tmp_path, proposer=False).run(OFFICER)
    assert no_model.value.code == "improve.no_model"
    with pytest.raises(ImproveError) as supervisor:
        await improver(week, tmp_path).run(SUPERVISOR)
    assert supervisor.value.code == "improve.forbidden"
    llm = mock_llm(tmp_path)
    sparse = ImproveService(week.service(LearnDrafter(llm), min_sample=10_000), week.gate,
                            week.view, Proposer(llm), LearnDrafter(llm), clock=week.clock,
                            lock=asyncio.Lock())  # fmt: skip
    with pytest.raises(ImproveError) as no_cases:
        await sparse.run(OFFICER)
    assert no_cases.value.code == "improve.no_cases"


async def _forged(gate: Gate, **changes: Any) -> None:
    """An improve.proposal Gate signed, with two approvals, but a body that must not load."""
    text = from_file().instructions + "\nExtra."
    body = {
        "target": "learn_proposal",
        "state": "review",
        "version": "learn_proposal/9.x",
        "instructions": text,
        "prompt_sha256": text_sha256(text),
        **changes,
    }
    proposal = await gate.append_record(IMPROVE_PROPOSAL, body)
    one = await gate.append_record(IMPROVE_APPROVAL, {
        "proposal_record_id": proposal.record_id, "approver": "a.chen", "approval": 1,
        "approvals_needed": 2, "earlier": []})  # fmt: skip
    await gate.append_record(IMPROVE_APPROVAL, {
        "proposal_record_id": proposal.record_id, "approver": changes.get("_second", "r.patel"),
        "approval": 2, "approvals_needed": 2, "earlier": [one.record_id]})  # fmt: skip


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"prompt_sha256": "sha256:" + "0" * 64}, "does not match its hash"),
        (
            {
                "instructions": "Follow the rows. " * 20,
                "prompt_sha256": text_sha256("Follow the rows. " * 20),
            },
            "it drops",
        ),
        ({"state": "blocked"}, "was not reviewable"),
        ({"_second": "a.chen"}, "fewer than two different approvers"),
    ],
)
async def test_a_prompt_that_does_not_verify_stops_the_learner(
    week: Week, tmp_path: Path, changes: dict[str, Any], why: str
) -> None:
    await _forged(week.gate, **changes)
    with pytest.raises(LearnError) as refused:
        await prompt_in_force(await week.records(), week.gate, from_file())
    assert refused.value.code == "learn.prompt"
    assert why in str(refused.value)
    with pytest.raises(LearnError):
        await week.service(None).drafter_in_force(LearnDrafter(mock_llm(tmp_path)))


# --- over HTTP -------------------------------------------------------------------------------


@asynccontextmanager
async def api(w: Week, tmp_path: Path, *, demo_mode: bool) -> AsyncIterator[httpx.AsyncClient]:
    @asynccontextmanager
    async def open_gate() -> AsyncIterator[tuple[Gate, Callers]]:
        yield w.gate, CALLERS

    def extend(app: Any) -> None:
        llm = mock_llm(tmp_path)
        install_learn_routes(app, LearnDrafter(llm), w.view, LearnSettings(),
                             demo_mode=demo_mode, clock=w.clock)  # fmt: skip
        install_improve_routes(app, Proposer(llm))

    app = create_gate_app(open_gate, sweep_interval=None, extend=extend)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://gate") as http,
    ):
        yield http


async def test_the_improver_over_http(week: Week, tmp_path: Path) -> None:
    async with api(week, tmp_path, demo_mode=False) as http:
        injected = await http.post("/improve/v1/run?inject_fault=true",
                                   headers=auth(OFFICER_TOKEN))  # fmt: skip
        assert (injected.status_code, injected.json()["error"]["code"]) == (
            403, "improve.forbidden")  # fmt: skip
        assert (
            await http.post("/improve/v1/run", headers=auth(SUPERVISOR_TOKEN))
        ).status_code == 403
        prompt = (await http.get("/improve/v1/prompt", headers=auth(OFFICER_TOKEN))).json()
        assert (prompt["version"], prompt["record_id"]) == ("learn_proposal/3", None)
        run = await http.post("/improve/v1/run", headers=auth(OFFICER_TOKEN))
        assert run.status_code == 201, run.text
        record = run.json()["proposal"]["record"]
        path = f"/improve/v1/proposals/{record['record_id']}"
        first = await http.post(f"{path}/approve", headers=auth(OFFICER_TOKEN))
        assert (first.status_code, first.json()["in_force"]) == (201, False)
        second = await http.post(f"{path}/approve", headers=auth(SECOND_TOKEN))
        assert (second.status_code, second.json()["in_force"]) == (201, True)
        shown = (await http.get(path, headers=auth(OFFICER_TOKEN))).json()
        assert len(shown["approvals"]) == 2
        now = (await http.get("/improve/v1/prompt", headers=auth(OFFICER_TOKEN))).json()
        assert now["version"] == record["body"]["version"]
        stale = await http.post(f"{path}/approve", headers=auth(SUPERVISOR_TOKEN))
        assert stale.status_code == 403


class _Failing(LearnDrafter):
    async def draft(self, *args: Any, **kwargs: Any) -> Any:
        raise ModelOutputError("timed out", code="model.unavailable")


class _BrokenProposer(Proposer):
    async def revise(self, *args: Any, **kwargs: Any) -> Any:
        raise ModelOutputError("bad", code="model.bad_output")


async def test_model_failures_are_counted_or_refused_never_guessed(
    week: Week, tmp_path: Path
) -> None:
    # R4: a draft the model could not make counts as failed; a revision it could not make
    # gives no proposal.
    pattern = (await candidates(week))[LOOSEN]
    _, _, rules = await week.service(None).base()
    log = await week.view.proven()
    failed = await score(_Failing(mock_llm(tmp_path)), [pattern], rules, log,
                         lambda p, d: ("review", []))  # fmt: skip
    assert (failed.failed, failed.cases[0].reasons) == (1, ("model.unavailable",))
    llm = mock_llm(tmp_path)
    broken = ImproveService(week.service(LearnDrafter(llm)), week.gate, week.view,
                            _BrokenProposer(llm), LearnDrafter(llm), clock=week.clock,
                            lock=asyncio.Lock())  # fmt: skip
    size = len(await week.records())
    with pytest.raises(ImproveError) as refused:
        await broken.run(OFFICER)
    assert refused.value.code == "improve.model_failed"
    assert len(await week.records()) == size  # nothing recorded


@pytest.mark.live
async def test_the_improver_on_muse(week: Week, tmp_path: Path) -> None:
    from assist.llm import model_config
    from common.config import load_settings

    config = model_config(load_settings())
    if config is None or config.profile != "cloud":
        pytest.skip("needs MODEL_PROFILE=cloud and META_API_KEY")
    llm = LlmClient(config)
    service = ImproveService(week.service(LearnDrafter(llm)), week.gate, week.view,
                             Proposer(llm), LearnDrafter(llm), clock=week.clock,
                             lock=asyncio.Lock())  # fmt: skip
    honest, shortcut = await asyncio.gather(
        service.run(OFFICER), service.run(OFFICER, inject_fault=True)
    )
    for proposal in (honest, shortcut):
        body = proposal.body
        print(body["version"], body["state"], body["changes"], body["reasons"])
        for side in ("current", "candidate"):
            s = body["scores"][side]
            print(side, s["accepted"], s["failed"], s["latency_ms"],
                  [c["canaries"] for c in s["cases"]])  # fmt: skip
    assert shortcut.body["state"] == "blocked"
    assert any("would go unseen" in r for r in shortcut.body["reasons"])
