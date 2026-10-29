"""Policy Learner runs and reviews (ARCHITECTURE flow E, D8, D80, D81).

A run proves the stream's export up to its final checkpoint, finds patterns with SQL
(``learn.patterns``), has Muse Spark draft a proposal for each candidate (``learn.draft``),
checks it (policy checks, counts, claims) and replays the window under it (``learn.replay``).
Every drafted proposal is appended as ``learn.proposal``, signed by Gate, whatever its state:

- ``rejected``: a restated count does not match SQL, a claim or the reason does not hold;
- ``blocked``: the rules have findings, or replay blocks them;
- ``review``: waiting for compliance officers.

Review: a tightening needs one approver; a loosening needs two distinct approvers, neither of
them the agent's owner. Each approval is a ``learn.approve`` record. The last approval turns
the proposal into a ``policy.draft``: the active rules with the change applied, checked again.
The learner never activates anything (D8): that draft goes through the same approval as any
policy (``policy.service``), which refuses it once another set has become active.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from claims.log import ProvenLog
from common.clock import Clock, format_ts
from common.errors import LearnError, ModelOutputError
from gate.core import Gate
from gate.decide import SCHEMA_FILE, PolicySet
from gate.records import Receipted
from learn.draft import Drafted, LearnDrafter, merge_rules
from learn.patterns import FAST_SECONDS, MIN_SAMPLE, Pattern, Window, find_patterns, load
from learn.replay import Replay, replay
from policy.checks import PolicyCheck, check_rules
from policy.rules import PolicyRule, parse_rules, to_yaml
from policy.service import DRAFT, OFFICER_ROLE, Officer, active_draft, draft_body

PROPOSAL = "learn.proposal"
APPROVAL = "learn.approve"
NEEDED = {"tighten": 1, "loosen": 2}  # from code, never from the stored proposal
MAX_WINDOW_DAYS = 90


class LedgerView(Protocol):
    async def proven(self) -> ProvenLog: ...  # verified up to the latest final checkpoint

    async def records(self) -> list[dict[str, Any]]: ...  # every record, unverified


@dataclass(frozen=True)
class LearnRun:
    window_days: int
    log: ProvenLog
    proposals: tuple[Receipted, ...]
    skipped: tuple[dict[str, Any], ...]  # patterns with no proposal, and why
    failed: tuple[dict[str, Any], ...]  # candidates the model could not draft

    def to_json(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "log": {"size": self.log.size, "root": self.log.root_b64, "newer": self.log.newer},
            "proposals": [p.to_json() for p in self.proposals],
            "skipped": list(self.skipped),
            "failed": list(self.failed),
        }


@dataclass(frozen=True)
class Approved:
    approval: Receipted
    policy_draft: Receipted | None  # set by the approval that completes the review


def _require_officer(officer: Officer) -> None:
    if OFFICER_ROLE not in officer.roles:
        raise LearnError(f"needs the {OFFICER_ROLE} role", code="learn.forbidden")


def _pattern_json(pattern: Pattern, fast_seconds: int, min_sample: int) -> dict[str, Any]:
    return {
        "key": pattern.scope.key(),
        "statement": pattern.scope.statement(),
        "scope": asdict(pattern.scope),
        "counts": pattern.counts,
        "decision_record_ids": [r["decision_record_id"] for r in pattern.rows],
        "fatigue": {
            "fast_seconds": fast_seconds,
            "min_sample": min_sample,
            "dropped_decision_record_ids": [r["decision_record_id"] for r in pattern.dropped],
            "rubber_stamp_approvers": list(pattern.stamped_approvers),
        },
    }


def _blocked(report: PolicyCheck | None, result: Replay | None) -> list[str]:
    blocked = [] if report is not None else ["the proposal has no valid rules"]
    if report is not None:
        blocked += [f"{f.rule_id}: {f.check}: {f.detail}" for f in report.findings]
    blocked += list(result.blocked) if result is not None else []
    return blocked


class LearnService:
    def __init__(
        self,
        gate: Gate,
        drafter: LearnDrafter | None,
        view: LedgerView,
        *,
        clock: Clock,
        owners: Mapping[str, str],  # agent -> its owner, who may not approve its loosening
        lock: asyncio.Lock,  # one review step at a time: approvals are counted, then appended
        min_sample: int = MIN_SAMPLE,
        fast_seconds: int = FAST_SECONDS,
    ) -> None:
        self._gate, self._drafter, self._view, self._clock = gate, drafter, view, clock
        self._owners, self._lock = dict(owners), lock
        self._min_sample, self._fast_seconds = min_sample, fast_seconds

    async def _base(self) -> tuple[Receipted, Receipted, tuple[PolicyRule, ...]]:
        active = await active_draft(self._gate)
        if active is None:
            raise LearnError("no approved policy is active", code="learn.no_active_policy")
        approval, draft = active
        rules = parse_rules(draft.body["rules_yaml"])
        if check_rules(rules).digest != self._gate.policies.digest:  # what Gate enforces now
            raise LearnError("the active draft is not the set Gate enforces",
                             code="learn.base_mismatch")  # fmt: skip
        return approval, draft, rules

    async def run(
        self, officer: Officer, *, window_days: int = 7, inject_fault: bool = False
    ) -> LearnRun:
        _require_officer(officer)
        if not 1 <= window_days <= MAX_WINDOW_DAYS:
            raise LearnError(f"window must be 1 to {MAX_WINDOW_DAYS} days", code="learn.window")
        if self._drafter is None:
            raise LearnError("no model is configured for the learner", code="learn.no_model")
        approval, draft, rules = await self._base()
        log = await self._view.proven()
        since = self._clock.now() - timedelta(days=window_days)
        window = Window(since, self._min_sample, self._fast_seconds)
        proposals, skipped, failed, live = [], [], [], []
        for pattern in find_patterns(load(log.records), window):
            why = pattern.skipped or self._settled(pattern, log)
            if why is None:
                live.append(pattern)
            else:
                skipped.append({"key": pattern.scope.key(), "reason": why,
                                "counts": pattern.counts})  # fmt: skip
        # Drafted together (each takes tens of seconds live), recorded in pattern order.
        drafts = await asyncio.gather(
            *(self._drafter.draft(p, rules, log, inject_fault=inject_fault) for p in live),
            return_exceptions=True,
        )
        for pattern, drafted in zip(live, drafts, strict=True):
            if isinstance(drafted, ModelOutputError):  # no proposal, not a guessed one (R4)
                failed.append({"key": pattern.scope.key(), "code": drafted.code})
                continue
            if isinstance(drafted, BaseException):
                raise drafted
            body = self._proposal_body(pattern, drafted, rules, log, window_days, since)
            body["base"] = {"policy_record_id": approval.record_id,
                            "draft_record_id": draft.record_id,
                            "policy_set": self._gate.policies.digest}  # fmt: skip
            proposals.append(await self._gate.append_record(PROPOSAL, body))
        return LearnRun(window_days, log, tuple(proposals), tuple(skipped), tuple(failed))

    def _settled(self, pattern: Pattern, log: ProvenLog) -> str | None:
        """Why the active set already does what the pattern suggests, or None. History keeps
        the old escalations after a change is approved; they must not be proposed again."""
        outcomes = set()
        for row in pattern.rows:
            body = (log.get(row["decision_record_id"]) or {}).get("body") or {}
            args = (str(body.get("agent")), str(body.get("action")), str(body.get("tool")))
            outcomes.add(self._gate.policies.evaluate(*args, body.get("resource") or {}).outcome)
        if pattern.direction == "loosen" and "escalate" not in outcomes:
            return "the active policy no longer holds these requests"
        if pattern.direction == "tighten" and outcomes == {"deny"}:
            return "the active policy already denies these requests"
        return None

    def _proposal_body(
        self,
        pattern: Pattern,
        drafted: Drafted,
        base: Sequence[PolicyRule],
        log: ProvenLog,
        window_days: int,
        since: datetime,
    ) -> dict[str, Any]:
        direction = str(pattern.direction)
        report = check_rules(drafted.merged) if drafted.changed else None
        result = None
        if report is not None and report.digest is not None:
            after = PolicySet(report.text, SCHEMA_FILE.read_text("utf-8"))
            result = replay(
                log.records, since, base_rules=base, before=self._gate.policies,
                after=after, scope=pattern.scope, direction=direction,
            )  # fmt: skip
        rejected, blocked = drafted.rejected, _blocked(report, result)
        state = "rejected" if rejected else "blocked" if blocked else "review"
        return {
            "agent": pattern.scope.agent,
            "direction": direction,
            "title": drafted.answer.title,
            "reason": drafted.answer.reason,
            "pattern": _pattern_json(pattern, self._fast_seconds, self._min_sample),
            "window": {
                "days": window_days,
                "since": format_ts(since),
                "log_size": log.size,
                "log_root": log.root_b64,
            },
            "stated_counts": drafted.stated,
            "claims": [
                {
                    "text": r.claim.text,
                    "record_ids": list(r.claim.record_ids),
                    "accepted": r.accepted,
                    "reasons": list(r.reasons),
                }
                for r in drafted.claims
            ],
            "rules_yaml": to_yaml(drafted.changed) if drafted.changed else "",
            "policy_set": None if report is None else report.digest,
            "replay": None if result is None else result.to_json(),
            "state": state,
            "reasons": [*rejected, *blocked],
            "approvals_needed": NEEDED[direction],
            "model": drafted.model,
            "prompt_version": drafted.prompt_version,
            "injected": drafted.injected,
        }

    async def approvals(self, proposal_id: str) -> list[Receipted]:
        """The proposal's ``learn.approve`` records, each verified against Gate's key."""
        found = [
            r["record_id"]
            for r in await self._view.records()
            if r.get("type") == APPROVAL
            and (r.get("body") or {}).get("proposal_record_id") == proposal_id
        ]
        return [await self._gate.read_record(i, APPROVAL) for i in found]

    async def approve(self, proposal_id: str, officer: Officer) -> Approved:
        _require_officer(officer)
        async with self._lock:
            proposal = await self._gate.read_record(proposal_id, PROPOSAL)
            body = proposal.body
            if body.get("state") != "review":
                raise LearnError(f"the proposal is {body.get('state')}",
                                 code="learn.not_reviewable")  # fmt: skip
            direction = str(body["direction"])
            needed = NEEDED[direction]
            if direction == "loosen" and self._owners.get(str(body["agent"])) == officer.name:
                raise LearnError("the agent's owner cannot approve loosening its rules",
                                 code="learn.owner")  # fmt: skip
            earlier = await self.approvals(proposal_id)
            names = {str(a.body["approver"]) for a in earlier}
            if officer.name in names:
                raise LearnError("each approver approves once", code="learn.already_approved")
            if len(names) >= needed:
                raise LearnError("the proposal is already approved", code="learn.not_reviewable")
            approval = await self._gate.append_record(APPROVAL, {
                "proposal_record_id": proposal_id,
                "approver": officer.name,
                "direction": direction,
                "approval": len(names) + 1,
                "approvals_needed": needed,
                "earlier": [a.record_id for a in earlier],
            })  # fmt: skip
            draft = None
            if len(names) + 1 == needed:
                ids = [*(a.record_id for a in earlier), approval.record_id]
                draft = await self._policy_draft(proposal, ids)
            return Approved(approval, draft)

    async def _policy_draft(self, proposal: Receipted, approval_ids: list[str]) -> Receipted:
        """The active rules with the proposal's changes, as a ``policy.draft`` to review."""
        active, base_draft, base = await self._base()
        rules = merge_rules(base, parse_rules(proposal.body["rules_yaml"]))
        body = draft_body(
            to_yaml(rules), check_rules(rules), base_draft.body["doc"],
            model=proposal.body["model"], prompt_version=proposal.body["prompt_version"],
            injected=None, revises=base_draft.record_id, author="learner",
            proposal_record_id=proposal.record_id, learn_approvals=approval_ids,
            base_policy_record=active.record_id,
        )  # fmt: skip
        return await self._gate.append_record(DRAFT, body)
