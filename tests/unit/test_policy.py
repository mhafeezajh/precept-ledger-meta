"""Policy rules, compiler and checks (policy/), demo beat 1 without the model: YAML rules that
cite their clause compile to Cedar, and code flags a rule whose fixtures fail, that allows
everything, or whose numbers are not the clause's."""

import itertools
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from common.errors import PolicyError
from gate.decide import PolicySet, load_policy_set
from policy.checks import check_rules
from policy.compiler import compile_rule, compile_rules
from policy.rules import FIELDS, clause_numbers, parse_rules, to_context

ROOT = Path(__file__).resolve().parents[2]
SEED_YAML = ROOT / "deploy" / "policies" / "harbor-federal.yaml"
SEED_CEDAR = ROOT / "deploy" / "policies" / "harbor-federal.cedar"
POLICY_TEXT = (ROOT / "deploy" / "demo" / "payments_policy.txt").read_text()
SCHEMA = (ROOT / "gate" / "schema.cedarschema").read_text()

RULE = """
- id: pay-over-5k-01
  source: { doc: payments_policy.pdf, page: 1, clause: "3.2",
            quote: "Any payment over $5,000 requires approval by a payments supervisor." }
  agent: payments-ops
  when: { action: value.transfer, amount: { gt: 5000 } }
  then: escalate
  approver_role: payments-supervisor
  tests:
    - { amount: 48000, expect: escalate }
    - { amount: 4999, expect: deny }
"""


def one(text: str = RULE, **changes: Any) -> str:
    """RULE with top-level lines replaced, e.g. one(when="{ action: value.transfer }")."""
    lines = text.splitlines()
    for key, value in changes.items():
        lines = [f"  {key}: {value}" if line.startswith(f"  {key}:") else line for line in lines]
    return "\n".join(lines)


def seed() -> str:
    return SEED_YAML.read_text()


def test_the_seed_rules_parse_and_quote_the_policy_text() -> None:
    rules = parse_rules(seed())
    assert [r.id for r in rules][:3] == ["vendor-lookup-01", "pay-known-payee-01", "pay-over-5k-01"]
    assert len(rules) == 8
    for rule in rules:
        assert rule.source.quote in " ".join(POLICY_TEXT.split()), rule.id


def test_a_rule_compiles_to_exactly_this_cedar() -> None:
    (rule,) = parse_rules(RULE)
    assert compile_rule(rule) == (
        "// payments_policy.pdf p.1 clause 3.2\n"
        '@id("pay-over-5k-01")\n'
        "@escalate\n"
        '@approver_role("payments-supervisor")\n'
        'permit (principal == Agent::"payments-ops", action == Action::"value.transfer",'
        " resource)\n"
        "when { context.amount_cents > 500000 };\n"
    )


def test_deny_unless_and_any_agent_compile() -> None:
    text = one(
        then="deny",
        agent="null",
        when="{ action: value.transfer, currency: { ne: USD }, payee_new: true }",
    ).replace("  approver_role: payments-supervisor\n", "")
    text += "\n  unless: { local_hour: { gte: 9, lt: 17 } }"
    (rule,) = parse_rules(text)
    assert compile_rule(rule).splitlines()[1:] == [
        '@id("pay-over-5k-01")',
        'forbid (principal, action == Action::"value.transfer", resource)',
        'when { context.currency != "USD" && context.payee_new == true }',
        "unless { context.local_hour >= 9 && context.local_hour < 17 };",
    ]


def probes(action: str) -> list[dict[str, Any]]:
    grid = {
        "value.transfer": {
            "amount": [0, 120, 5000, "5000.01", 48000, 250000, "250000.01"],
            "currency": ["USD", "EUR"],
            "payee_new": [False, True],
        },
        "comm.external": {"recipients": [1], "external_recipients": [0, 1, 5]},
        "data.read": {"data_class": ["vendor", "customer"]},
    }[action]
    return [dict(zip(grid, values, strict=True)) for values in itertools.product(*grid.values())]


@pytest.mark.parametrize("agent", ["payments-ops", "other-agent"])
def test_compiled_seed_decides_like_the_hand_written_cedar(agent: str) -> None:
    compiled = PolicySet(compile_rules(parse_rules(seed())), SCHEMA)
    hand = load_policy_set(SEED_CEDAR)
    for action in FIELDS:
        for probe in probes(action):
            facts = to_context(action, probe)
            a = compiled.evaluate(agent, action, "probe", facts)
            b = hand.evaluate(agent, action, "probe", facts)
            assert (a.outcome, a.policy_ids, a.approver_roles) == (
                b.outcome,
                b.policy_ids,
                b.approver_roles,
            ), (action, probe)


def test_compiling_is_deterministic_and_the_digest_is_gates() -> None:
    report = check_rules(parse_rules(seed()))
    assert report.text == compile_rules(parse_rules(seed()))
    assert report.digest == PolicySet(report.text, SCHEMA).digest
    assert report.findings == ()
    assert report.ok


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (one(when="{ action: value.transfer, amount: { gt: 5000.5 } }"), "policy.rule_value"),
        (one(when='{ action: value.transfer, amount: { gt: "5000.001" } }'), "policy.rule_value"),
        (one(when="{ action: value.transfer, memo: x }"), "policy.rule_field"),
        (one(when="{ action: money.burn }"), "policy.rule_action"),
        # Live, a model wrote the action as a condition: refused, never a TypeError (R4).
        (one(when="{ action: { eq: value.transfer } }"), "policy.rule_action"),
        (one(when="{ action: [value.transfer] }"), "policy.rule_action"),
        (one(when="{ action: value.transfer, amount: { about: 5 } }"), "policy.rule_op"),
        (one(when="{ action: value.transfer, payee_new: { gt: true } }"), "policy.rule_op"),
        (one(when="{ action: value.transfer, payee_new: 1 }"), "policy.rule_value"),
        (
            one(when='{ action: value.transfer, payee_id: "a\\" || true || \\"" }'),
            "policy.rule_value",
        ),
        (one(agent='"ops\\") permit (principal"'), "policy.rule_value"),
        (one(then="allow"), "policy.rule_shape"),  # approver_role on a non-escalation
        (one().replace("  approver_role: payments-supervisor\n", ""), "policy.rule_shape"),
        (one(then="maybe"), "policy.rule_shape"),
        (one() + "\n  notes: hi", "policy.rule_shape"),
        (RULE + RULE, "policy.duplicate_id"),
        ("- [1, 2]", "policy.rule_shape"),
        ("a: [unclosed", "policy.yaml"),
        ("[]", "policy.empty"),
        (RULE.split("  tests:")[0] + "  tests: []", "policy.rule_shape"),
        (one(unless="[1]").replace("  agent:", "  unless: [1]\n  agent:"), "policy.rule_shape"),
        (RULE.replace('clause: "3.2",', ""), "policy.rule_shape"),
        (RULE.replace("page: 1", "page: 0"), "policy.rule_value"),
        (RULE.replace('quote: "Any', 'quote: " ", x: "Any'), "policy.rule_shape"),
        (
            RULE.replace(
                "Any payment over $5,000 requires approval by a payments supervisor.", " "
            ),
            "policy.rule_value",
        ),
        (RULE.replace("expect: deny", "expect: maybe"), "policy.rule_shape"),
        ("x: 1", "policy.rule_shape"),
        ("#" * (256 * 1024 + 1), "policy.yaml"),
    ],
)
def test_malformed_or_unsafe_rules_are_refused(text: str, code: str) -> None:
    with pytest.raises(PolicyError) as caught:
        parse_rules(text)
    assert caught.value.code == code


def findings(text: str) -> list[tuple[str, str, str]]:
    return [(f.rule_id, f.check, f.detail) for f in check_rules(parse_rules(text)).findings]


def test_a_threshold_the_clause_does_not_state_is_flagged() -> None:
    # The drafter's fixtures agree with its own wrong threshold, so only the clause check can
    # catch it: the clause says $5,000.
    planted = one(when="{ action: value.transfer, amount: { gt: 50000 } }")
    planted = planted.replace("48000, expect", "60000, expect").replace("4999,", "49999,")
    (finding,) = findings(planted)
    assert finding[:2] == ("pay-over-5k-01", "clause_number")
    assert "50000" in finding[2]
    assert "5000" in finding[2]


def test_a_rule_that_allows_everything_is_flagged() -> None:
    broad = one(then="allow", when="{ action: value.transfer }").replace(
        "  approver_role: payments-supervisor\n", ""
    )
    broad = broad.replace("48000, expect: escalate", "48000, expect: allow")
    broad = broad.replace("4999, expect: deny", "4999, expect: allow")
    checks = {f[1] for f in findings(broad)}
    assert "allows_everything" in checks
    known = broad.replace(
        "{ action: value.transfer }", "{ action: value.transfer, payee_new: false }"
    )
    assert "allows_everything" not in {f[1] for f in findings(known)}


def test_a_failing_fixture_is_flagged_with_what_happened() -> None:
    wrong = one().replace("48000, expect: escalate", "48000, expect: allow")
    (finding,) = findings(wrong)
    assert finding[:2] == ("pay-over-5k-01", "fixture")
    assert "expected allow, got escalate (pay-over-5k-01)" in finding[2]


def test_a_rule_its_fixtures_never_trigger_is_flagged() -> None:
    untested = one().replace("48000, expect: escalate", "10, expect: deny")
    assert ("pay-over-5k-01", "untested") in {f[:2] for f in findings(untested)}
    always = one().replace("4999, expect: deny", "6000, expect: escalate")
    assert ("pay-over-5k-01", "one_sided") in {f[:2] for f in findings(always)}


def test_a_set_that_fails_the_schema_is_flagged() -> None:
    old_schema = SCHEMA.replace("    amount_cents: Long,\n", "")
    (finding,) = check_rules(parse_rules(RULE), schema=old_schema).findings
    assert (finding.rule_id, finding.check) == ("*", "schema")
    assert not check_rules(parse_rules(RULE), schema=old_schema).ok


def test_clause_numbers_read_amounts_hours_and_weekdays() -> None:
    assert clause_numbers("over $5,000 and up to $250,000.50") == {5000, Decimal("250000.50")}
    hours = clause_numbers("9:00 a.m. to 5:00 p.m., Monday to Friday")
    assert {9, 17, 1, 5} <= hours
    assert 12 in clause_numbers("noon to 12:30 p.m.")
    assert clause_numbers("from 08:30 until 18:00") == {8, 18}


def test_business_hours_rules_must_match_the_clause() -> None:
    text = one(then="deny").replace("  approver_role: payments-supervisor\n", "")
    text = text.replace(
        "Any payment over $5,000 requires approval by a payments supervisor.",
        "Payments over $5,000 are not allowed outside 9:00 a.m. to 5:00 p.m., Monday to Friday.",
    )
    text = text.replace("48000, expect: escalate", "48000, local_hour: 20, expect: deny")
    text = text.replace("4999, expect: deny", "48000, local_hour: 10, expect: deny")
    hours = "\n  unless: { local_hour: { gte: 9, lt: 17 }, local_weekday: { lte: 5 } }"
    assert [f for f in findings(text + hours) if f[1] == "clause_number"] == []
    late = hours.replace("lt: 17", "lt: 18")
    assert [f[1] for f in findings(text + late) if f[1] == "clause_number"] == ["clause_number"]


def test_fixture_contexts_fill_defaults_in_cedar_units() -> None:
    assert to_context("value.transfer", {"amount": "48000.50", "payee_new": True}) == {
        "amount_cents": 4_800_050,
        "currency": "USD",
        "payee_id": "fixture-payee",
        "payee_new": True,
        "local_hour": 10,
        "local_weekday": 2,
    }
    with pytest.raises(PolicyError):
        to_context("value.transfer", {"memo": "x"})
