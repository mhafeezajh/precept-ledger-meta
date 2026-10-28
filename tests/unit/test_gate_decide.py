"""Cedar decisions (gate/decide.py): allow, deny, escalate, and every way to fail closed."""

from pathlib import Path

import pytest

from common.errors import PolicyError
from gate.decide import SCHEMA_FILE, Outcome, PolicySet, load_policy_set

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "deploy" / "policies" / "harbor-federal.cedar"
SCHEMA = SCHEMA_FILE.read_text("utf-8")
AGENT = "payments-ops"


def payment(amount_cents: int, *, payee_new: bool = False, currency: str = "USD") -> dict:
    return {
        "amount_cents": amount_cents,
        "currency": currency,
        "payee_id": "northwind-supply",
        "payee_new": payee_new,
        "local_hour": 17,
        "local_weekday": 4,
    }


@pytest.fixture(scope="module")
def seed() -> PolicySet:
    return PolicySet(SEED.read_text("utf-8"), SCHEMA)


def test_the_demo_payment_escalates_to_a_payments_supervisor(seed: PolicySet) -> None:
    result = seed.evaluate(
        AGENT, "value.transfer", "create_payment", payment(4_800_000, payee_new=True)
    )
    assert result.outcome is Outcome.ESCALATE
    assert result.reason == "policy.escalate"
    assert result.policy_ids == ("pay-new-payee-01", "pay-over-5k-01")
    assert result.approver_roles == ("payments-supervisor",)


def test_a_small_payment_to_a_known_payee_is_allowed(seed: PolicySet) -> None:
    result = seed.evaluate(AGENT, "value.transfer", "create_payment", payment(50_000))
    assert (result.outcome, result.reason) == (Outcome.ALLOW, "policy.permit")
    assert result.policy_ids == ("pay-known-payee-01",)
    assert result.approver_roles == ()


def test_the_threshold_is_exact_to_the_cent(seed: PolicySet) -> None:
    at = seed.evaluate(AGENT, "value.transfer", "create_payment", payment(500_000))
    over = seed.evaluate(AGENT, "value.transfer", "create_payment", payment(500_001))
    assert (at.outcome, over.outcome) == (Outcome.ALLOW, Outcome.ESCALATE)


@pytest.mark.parametrize(
    ("facts", "policy"),
    [(payment(30_000_000), "pay-cap-01"), (payment(100, currency="EUR"), "pay-usd-only-01")],
)
def test_a_forbid_wins_over_any_permit(seed: PolicySet, facts: dict, policy: str) -> None:
    result = seed.evaluate(AGENT, "value.transfer", "create_payment", facts)
    assert (result.outcome, result.reason, result.policy_ids) == (
        Outcome.DENY, "policy.forbid", (policy,)
    )  # fmt: skip


def test_an_agent_no_policy_names_is_denied_by_default(seed: PolicySet) -> None:
    result = seed.evaluate("marketing-bot", "value.transfer", "create_payment", payment(100))
    assert (result.outcome, result.reason, result.policy_ids) == (
        Outcome.DENY, "policy.no_permit", ()
    )  # fmt: skip


def test_facts_that_do_not_match_the_schema_are_denied(seed: PolicySet) -> None:
    # Cedar returns NoDecision for a request that fails the schema; Gate must never allow.
    facts = {**payment(100), "amount_cents": "100"}
    result = seed.evaluate(AGENT, "value.transfer", "create_payment", facts)
    assert (result.outcome, result.reason) == (Outcome.DENY, "gate.policy_error")
    missing = seed.evaluate(AGENT, "value.transfer", "create_payment", {"amount_cents": 1})
    assert missing.outcome is Outcome.DENY


def test_an_unknown_action_is_denied(seed: PolicySet) -> None:
    result = seed.evaluate(AGENT, "record.modify", "edit_record", {})
    assert (result.outcome, result.reason) == (Outcome.DENY, "gate.policy_error")


def test_an_evaluation_error_denies_even_if_a_permit_matched() -> None:
    # Cedar skips a forbid that errors and may still answer Allow (checked in the cedarpy
    # sandbox, D49). Without a schema the bad attribute is not caught at load time.
    text = (
        '@id("allow-all")\npermit (principal, action, resource);\n'
        '@id("broken")\nforbid (principal, action, resource) when { context.nope == 1 };\n'
    )
    policies = PolicySet(text, schema=None)
    result = policies.evaluate(AGENT, "value.transfer", "create_payment", payment(100))
    assert (result.outcome, result.reason) == (Outcome.DENY, "gate.policy_error")


def test_the_digest_names_the_exact_policy_text(seed: PolicySet) -> None:
    assert seed.digest.startswith("sha256:")
    assert len(seed.digest) == len("sha256:") + 64
    changed = PolicySet(SEED.read_text("utf-8") + "\n", SCHEMA)
    assert changed.digest != seed.digest


def test_rules_are_listed_with_their_effects(seed: PolicySet) -> None:
    rules = {rule.id: rule for rule in seed.rules}
    assert rules["pay-new-payee-01"].escalate
    assert rules["pay-cap-01"].effect == "forbid"
    assert not rules["pay-known-payee-01"].escalate


REJECTED = [
    ("permit (", "policy.syntax"),
    ("permit (principal, action, resource);", "policy.missing_id"),
    ('@id("Bad ID")\npermit (principal, action, resource);', "policy.bad_id"),
    (
        '@id("a")\npermit (principal, action, resource);\n'
        '@id("a")\nforbid (principal, action, resource);',
        "policy.duplicate_id",
    ),
    ('@id("a")\n@escalate\npermit (principal, action, resource);', "policy.missing_approver_role"),
    (
        '@id("a")\n@escalate\n@approver_role("x")\nforbid (principal, action, resource);',
        "policy.escalate_on_forbid",
    ),
    (
        '@id("a")\n@escalate("no")\n@approver_role("x")\npermit (principal, action, resource);',
        "policy.bad_annotation",
    ),
    (
        '@id("a")\n@approver_role("x")\npermit (principal, action, resource);',
        "policy.bad_annotation",
    ),
    ('@id("a")\n@owner("x")\npermit (principal, action, resource);', "policy.bad_annotation"),
    ('@id("a")\npermit (principal == ?principal, action, resource);', "policy.template"),
    (
        '@id("a")\npermit (principal, action == Action::"value.transfer", resource)'
        " when { context.nope == 1 };",
        "policy.schema",
    ),
    ("", "policy.empty"),
]


@pytest.mark.parametrize(("text", "code"), REJECTED, ids=[c for _, c in REJECTED])
def test_policy_sets_gate_cannot_trust_are_refused(text: str, code: str) -> None:
    with pytest.raises(PolicyError) as caught:
        PolicySet(text, SCHEMA)
    assert caught.value.code == code


def test_an_unreadable_schema_is_refused() -> None:
    with pytest.raises(PolicyError) as caught:
        PolicySet('@id("a")\npermit (principal, action, resource);', "entity ;")
    assert caught.value.code == "policy.schema"


def test_policy_files_load_or_fail_with_a_code(tmp_path: Path, seed: PolicySet) -> None:
    assert load_policy_set(SEED).digest == seed.digest
    with pytest.raises(PolicyError) as caught:
        load_policy_set(tmp_path / "missing.cedar")
    assert caught.value.code == "policy.unreadable"
