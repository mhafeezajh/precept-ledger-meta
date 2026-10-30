"""Planted faults: under a new prompt, is a wrong answer still caught? (D95)

A prompt can make the learner's drafts easier to accept by making them say less: fewer claims,
claims that assert nothing a checker can test. Acceptance would rise while the checks had less
to check. So each draft made during an evaluation gets four faults planted by code, each the
kind a model makes, and each must be caught by the same checks a learner run uses:

| Canary | Planted | Caught by |
| --- | --- | --- |
| count | a restated count, plus 10 | the count against SQL |
| amount | a claimed payment amount, plus $1,000 | ``claims.checker`` on the record |
| approver | a claimed approver, renamed | ``claims.checker`` on the record |
| reason | a number no record states, added to the reason | ``check_summary`` |

A fault that cannot be planted, because no claim asserts an amount or an approver, counts as
missed: a check with nothing to check lets that fault through. Pure functions, no model.
"""

from typing import Any

from assist.auditor import AssertionOut, ClaimOut, to_claim
from claims.checker import check_claims_in_log, check_summary
from claims.log import ProvenLog
from learn.draft import Drafted, count_problems, reason_names
from learn.patterns import Pattern

CANARIES = ("count", "amount", "approver", "reason")
CAUGHT, MISSED, UNPLANTABLE, NOT_APPLICABLE = "caught", "missed", "unplantable", "not_applicable"


def _plant_assertion(drafted: Drafted, log: ProvenLog, field: str) -> str:
    """Change the first assertion on ``field`` to a wrong value; the claim must be rejected."""
    for claim in drafted.answer.claims:
        for j, a in enumerate(claim.assertions):
            if a.field != field or a.value is None or isinstance(a.value, bool):
                continue
            wrong: Any = a.value + 100_000 if isinstance(a.value, int) else f"{a.value}-x"
            assertions = [*claim.assertions]
            assertions[j] = AssertionOut(record_id=a.record_id, field=a.field, op=a.op,
                                         value=wrong)  # fmt: skip
            changed: ClaimOut = claim.model_copy(update={"assertions": assertions})
            held = check_claims_in_log([to_claim(changed)], log)[0].accepted
            return MISSED if held else CAUGHT
    return UNPLANTABLE


def plant(drafted: Drafted, pattern: Pattern, log: ProvenLog) -> dict[str, str]:
    """Each canary's outcome on this draft: caught, missed, unplantable or not_applicable."""
    stated = dict(drafted.stated)
    first = next(iter(stated))  # the schema requires at least one restated count
    stated[first] += 10
    numbers = {*pattern.counts.values(), *pattern.scope.numbers()}
    odd = max(numbers | {0}) * 10 + 7  # a number neither SQL nor the scope states
    names = reason_names(pattern.scope, [r.id for r in drafted.merged])
    results = check_claims_in_log([to_claim(c) for c in drafted.answer.claims], log)
    reason = f"{drafted.answer.reason} It covers {odd} decisions."
    payments = pattern.scope.action == "value.transfer"
    return {
        "count": CAUGHT if count_problems(stated, pattern) else MISSED,
        "amount": (
            _plant_assertion(drafted, log, "body.resource.amount_cents")
            if payments
            else NOT_APPLICABLE
        ),
        "approver": _plant_assertion(drafted, log, "body.approver"),
        "reason": CAUGHT if check_summary(reason, results, numbers=numbers, names=names)
        else MISSED,
    }  # fmt: skip
