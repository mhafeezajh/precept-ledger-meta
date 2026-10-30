"""Improvement runs and reviews: the learner's prompt, one bounded step at a time (D95).

A run:

1. reads the learner's recent failures from the ledger (``failures``): rejected or blocked
   proposals made under the prompt in force, leaving out demo-planted faults;
2. has Muse Wonder revise the prompt (``improve.propose``);
3. scores the prompt in force and the revision on the same patterns from the same proven log,
   with the learner's own checks and the planted-fault canaries (``improve.evaluate``);
4. appends one ``improve.proposal``, signed by Gate: ``review`` if the revision is no worse on
   every measure and keeps every guard sentence, ``blocked`` otherwise, with the reasons.

Review: two different compliance officers, each an ``improve.approve`` record. The second makes
the revision the prompt in force for every later learner run (``learn.prompt``). An approval is
refused once the prompt in force has changed since the proposal was scored.

Bounds, all in code: the only prompt in scope is the learner's (``learn.prompt.TARGET``); the
improver's own prompt, the checks, the canaries, the approval counts and Gate are never in
scope, so the loop is one level deep. It drafts; it never activates anything on its own.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from common.clock import Clock, format_ts
from common.errors import ImproveError, ModelOutputError
from gate.core import Gate
from gate.records import Receipted
from improve.evaluate import compare, score
from improve.propose import Proposer, plant_shortcut
from learn.draft import Drafted, LearnDrafter
from learn.patterns import Pattern
from learn.prompt import (
    APPROVALS_NEEDED,
    IMPROVE_APPROVAL,
    IMPROVE_PROPOSAL,
    TARGET,
    PromptInForce,
    from_file,
    missing_guards,
    prompt_in_force,
    text_sha256,
)  # fmt: skip
from learn.service import PROPOSAL, LearnService, LedgerView
from policy.service import OFFICER_ROLE, Officer

WINDOW_DAYS = 7
MAX_FAILURES = 10


def failures(records: Sequence[dict[str, Any]], version: str) -> list[dict[str, Any]]:
    """The newest rejected or blocked learner proposals made under ``version``."""
    found = [
        {"record_id": r["record_id"], "state": r["body"]["state"],
         "reasons": list(r["body"].get("reasons") or [])[:4]}
        for r in records
        if r.get("type") == PROPOSAL
        and (r.get("body") or {}).get("state") in ("rejected", "blocked")
        and r["body"].get("prompt_version") == version
        and not r["body"].get("injected")  # a demo-planted fault is not the prompt's failing
    ]  # fmt: skip
    return found[-MAX_FAILURES:]


def next_version(current: str, instructions: str) -> str:
    """``learn_proposal/3`` -> ``learn_proposal/4.1a2b3c``: numbered, and tied to the text."""
    number = current.rsplit("/", 1)[-1].split(".", 1)[0]
    following = int(number) + 1 if number.isdigit() else 1
    return f"{TARGET}/{following}.{text_sha256(instructions)[7:13]}"


@dataclass(frozen=True)
class Approved:
    approval: Receipted
    in_force: bool  # the last approval: later learner runs use the revision


def _require_officer(officer: Officer) -> None:
    if OFFICER_ROLE not in officer.roles:
        raise ImproveError("only a compliance officer may do this", code="improve.forbidden")


class ImproveService:
    def __init__(
        self,
        learn: LearnService,
        gate: Gate,
        view: LedgerView,
        proposer: Proposer | None,
        drafter: LearnDrafter | None,
        *,
        clock: Clock,
        lock: asyncio.Lock,  # one review step at a time: approvals are counted, then appended
    ) -> None:
        self._learn, self._gate, self._view = learn, gate, view
        self._proposer, self._drafter, self._clock, self._lock = proposer, drafter, clock, lock

    async def current(self) -> PromptInForce:
        return await prompt_in_force(await self._view.records(), self._gate, from_file())

    async def run(self, officer: Officer, *, inject_fault: bool = False) -> Receipted:
        _require_officer(officer)
        if self._proposer is None or self._drafter is None:
            raise ImproveError("no model is configured", code="improve.no_model")
        approval, _, rules = await self._learn.base()
        log = await self._view.proven()
        since = self._clock.now() - timedelta(days=WINDOW_DAYS)
        patterns, _ = self._learn.live_patterns(log, since)
        if not patterns:
            raise ImproveError("no patterns to test a prompt on", code="improve.no_cases")
        records = await self._view.records()
        current = await prompt_in_force(records, self._gate, from_file())
        seen = failures(records, current.version)
        try:
            revision = await self._proposer.revise(current, seen)
        except ModelOutputError as exc:  # no proposal, not a guessed one (R4)
            raise ImproveError(f"the proposer failed ({exc.code})",
                               code="improve.model_failed") from exc  # fmt: skip
        text, changes, injected = revision.instructions, list(revision.changes), None
        if inject_fault:  # demo only: a revision that asks the learner for less evidence
            text, injected = plant_shortcut(text), "instructions.claims"
            changes.append("Claims cut to one per pattern, asserting only the verdict")
        version = next_version(current.version, text)

        def assess(pattern: Pattern, drafted: Drafted) -> tuple[str, list[str]]:
            body = self._learn.proposal_body(pattern, drafted, rules, log, WINDOW_DAYS, since)
            return str(body["state"]), list(body["reasons"])

        before, after = await asyncio.gather(
            score(self._drafter.with_prompt(current.version, current.instructions),
                  patterns, rules, log, assess),
            score(self._drafter.with_prompt(version, text), patterns, rules, log, assess),
        )  # fmt: skip
        reasons = [f"drops the guard sentence {k!r}" for k in missing_guards(text)]
        reasons += compare(before, after)
        return await self._gate.append_record(IMPROVE_PROPOSAL, {
            "target": TARGET,
            # the base text too, so anyone can diff the change from this record alone
            "base": {"version": current.version, "sha256": current.sha256,
                     "record_id": current.record_id, "policy_record_id": approval.record_id,
                     "instructions": current.instructions},
            "version": version,
            "instructions": text,
            "prompt_sha256": text_sha256(text),
            "changes": changes,
            "rationale": revision.rationale,
            "failures": seen,
            "window": {"days": WINDOW_DAYS, "since": format_ts(since), "log_size": log.size,
                       "log_root": log.root_b64},
            "scores": {"current": before.to_json(), "candidate": after.to_json()},
            "state": "blocked" if reasons else "review",
            "reasons": reasons,
            "approvals_needed": APPROVALS_NEEDED,
            "model": self._proposer.model,
            "prompt_version": self._proposer.prompt_version,
            "injected": injected,
        })  # fmt: skip

    async def approvals(self, proposal_id: str) -> list[Receipted]:
        """The proposal's ``improve.approve`` records, each verified against Gate's key."""
        found = [
            r["record_id"]
            for r in await self._view.records()
            if r.get("type") == IMPROVE_APPROVAL
            and (r.get("body") or {}).get("proposal_record_id") == proposal_id
        ]
        return [await self._gate.read_record(i, IMPROVE_APPROVAL) for i in found]

    async def approve(self, proposal_id: str, officer: Officer) -> Approved:
        _require_officer(officer)
        async with self._lock:
            proposal = await self._gate.read_record(proposal_id, IMPROVE_PROPOSAL)
            body = proposal.body
            if body.get("state") != "review":
                raise ImproveError(f"the proposal is {body.get('state')}",
                                   code="improve.not_reviewable")  # fmt: skip
            earlier = await self.approvals(proposal_id)
            names = {str(a.body["approver"]) for a in earlier}
            if officer.name in names:
                raise ImproveError("each approver approves once", code="improve.already_approved")
            if len(names) >= APPROVALS_NEEDED:
                raise ImproveError("the proposal is already approved",
                                   code="improve.not_reviewable")  # fmt: skip
            if (await self.current()).sha256 != body["base"]["sha256"]:
                raise ImproveError("the learner's prompt changed since this was scored",
                                   code="improve.stale")  # fmt: skip
            approval = await self._gate.append_record(IMPROVE_APPROVAL, {
                "proposal_record_id": proposal_id,
                "approver": officer.name,
                "approval": len(names) + 1,
                "approvals_needed": APPROVALS_NEEDED,
                "earlier": [a.record_id for a in earlier],
            })  # fmt: skip
            return Approved(approval, len(names) + 1 == APPROVALS_NEEDED)
