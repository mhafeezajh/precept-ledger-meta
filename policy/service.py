"""Policy review: draft, revise, approve, activate (ARCHITECTURE flow B steps 4 and 5, D73, D74).

Everything is a ledger record signed by Gate, the way verdicts are: the officer's token gives
the authority, and the record names the officer.

- ``policy.draft``: the rules as YAML, the compiled Cedar, its digest, every finding, the
  document's name and SHA-256, and who or what wrote it (Muse Wonder, or the officer when a
  draft is revised). A revision points to the draft it replaces.
- ``policy.approve``: the approved draft, the exact Cedar text, its digest and the approver.

Approval never trusts a stored finding list: it re-reads the draft from the ledger, parses
the YAML and runs every check again, and refuses unless there are no findings (R4). Gate then
decides with that set. At startup, Gate restores the latest approval from its own records.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from common.errors import PolicyError
from common.logging import log_event
from gate.core import Gate
from gate.decide import SCHEMA_FILE, PolicySet
from gate.records import Receipted
from policy.checks import PolicyCheck, check_rules
from policy.draft import Drafter
from policy.pdf import read_pdf
from policy.rules import parse_rules, to_yaml

DRAFT = "policy.draft"
APPROVE = "policy.approve"
OFFICER_ROLE = "compliance-officer"
logger = logging.getLogger("pl.policy")


class HistorySource(Protocol):
    async def read_raw(self, stream: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Officer:
    name: str
    roles: frozenset[str]


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_officer(officer: Officer) -> None:
    if OFFICER_ROLE not in officer.roles:
        raise PolicyError(f"needs the {OFFICER_ROLE} role", code="policy.forbidden")


def draft_body(
    yaml_text: str, report: PolicyCheck, doc: dict[str, Any], **about: Any
) -> dict[str, Any]:
    return {
        "doc": doc,
        "rules_yaml": yaml_text,
        "rules_sha256": _sha256(yaml_text),
        "cedar": report.text,
        "policy_set": report.digest,
        "findings": [
            {"rule_id": f.rule_id, "check": f.check, "detail": f.detail} for f in report.findings
        ],
        **about,
    }


class PolicyService:
    def __init__(self, gate: Gate, drafter: Drafter | None) -> None:
        self._gate = gate
        self._drafter = drafter

    async def submit(
        self, pdf: bytes, name: str, officer: Officer, *, inject_fault: bool = False
    ) -> Receipted:
        _require_officer(officer)
        if self._drafter is None:
            raise PolicyError("no drafting model is configured", code="policy.no_drafter")
        document = read_pdf(pdf, name)
        draft = await self._drafter.draft(document, inject_fault=inject_fault)
        doc = {"name": document.name, "sha256": document.sha256, "pages": document.pages}
        body = draft_body(
            to_yaml(draft.rules), draft.report, doc, model=draft.model,
            prompt_version=draft.prompt_version, injected=draft.injected, revises=None,
            author=officer.name,
        )  # fmt: skip
        return await self._gate.append_record(DRAFT, body)

    async def revise(self, draft_id: str, yaml_text: str, officer: Officer) -> Receipted:
        """The officer's edit as a new draft; the checks run again on what they wrote."""
        _require_officer(officer)
        earlier = await self._gate.read_record(draft_id, DRAFT)
        rules = parse_rules(yaml_text)
        body = draft_body(
            to_yaml(rules), check_rules(rules), earlier.body["doc"], model=None,
            prompt_version=None, injected=None, revises=draft_id, author=officer.name,
        )  # fmt: skip
        return await self._gate.append_record(DRAFT, body)

    async def approve(self, draft_id: str, officer: Officer) -> Receipted:
        _require_officer(officer)
        draft = await self._gate.read_record(draft_id, DRAFT)
        base = draft.body.get("base_policy_record")
        if base is not None and base != self._gate.policy_record_id:
            # A learner draft is the active set plus a change; approving it once another set
            # is active would silently undo that set (D81).
            raise PolicyError("the draft was built on a set that is no longer active",
                              code="policy.stale_base")  # fmt: skip
        rules = parse_rules(draft.body["rules_yaml"])
        report = check_rules(rules)  # never the stored findings
        if not report.ok or report.digest is None:
            listed = "; ".join(f"{f.rule_id}: {f.check}" for f in report.findings)
            raise PolicyError(f"the draft has findings: {listed}", code="policy.not_clean")
        policies = PolicySet(report.text, _schema())
        body = {
            "draft_record_id": draft_id,
            "approver": officer.name,
            "doc": draft.body["doc"],
            "rules": [r.id for r in rules],
            "rules_sha256": _sha256(draft.body["rules_yaml"]),
            "cedar": report.text,
            "policy_set": report.digest,
        }
        approval = await self._gate.append_record(APPROVE, body)
        self._gate.activate(policies, approval.record_id)
        return approval


async def active_draft(gate: Gate) -> tuple[Receipted, Receipted] | None:
    """The active ``policy.approve`` and the ``policy.draft`` it approved, both verified, or
    None while Gate runs on its policy file."""
    if gate.policy_record_id is None:
        return None
    approval = await gate.read_record(gate.policy_record_id, APPROVE)
    return approval, await gate.read_record(approval.body["draft_record_id"], DRAFT)


def _schema() -> str:
    return SCHEMA_FILE.read_text("utf-8")


async def restore_active(gate: Gate, history: HistorySource) -> str | None:
    """Activate the stream's latest ``policy.approve``, read back and verified, if there is one.
    A record that does not verify, or whose Cedar does not match its digest, stops startup."""
    records: Sequence[dict[str, Any]] = await history.read_raw(gate.stream)
    approvals = [r for r in records if r.get("type") == APPROVE]
    if not approvals:
        return None
    record = await gate.read_record(approvals[-1]["record_id"], APPROVE)
    policies = PolicySet(record.body["cedar"], _schema())
    if policies.digest != record.body["policy_set"]:
        raise PolicyError("approved policy text does not match its digest", code="policy.digest")
    gate.activate(policies, record.record_id)
    log_event(logger, logging.INFO, "policy restored", record_id=record.record_id)
    return record.record_id
