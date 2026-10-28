"""Normalizer (gate/normalizer.py): tool calls become an action class and typed facts."""

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.clock import FixedClock
from common.errors import GateError
from gate.decide import SCHEMA_FILE, Outcome, PolicySet
from gate.normalizer import Normalizer, StaticPayees, args_digest

# Thursday 2026-10-29 21:14 UTC is 17:14 in New York (EDT).
NOW = datetime(2026, 10, 29, 21, 14, 3, tzinfo=UTC)
PAYEES = StaticPayees({"acme-paper", "harbor-cleaning"})
PAY = {"payee_id": "northwind-supply", "amount": "48000.00", "currency": "USD"}


class DownPayees:
    async def is_known(self, payee_id: str) -> bool:
        raise OSError("vendor master unreachable")


def normalizer(payees: object = PAYEES) -> Normalizer:
    return Normalizer(
        payees,  # type: ignore[arg-type]
        internal_domains={"harborfederal.example"},
        clock=FixedClock(NOW),
        timezone=ZoneInfo("America/New_York"),
    )


async def test_the_demo_payment_becomes_a_value_transfer() -> None:
    result = await normalizer().normalize("create_payment", {**PAY, "memo": "Invoice 7731"})
    assert result.action == "value.transfer"
    assert result.facts == {
        "amount_cents": 4_800_000,
        "currency": "USD",
        "payee_id": "northwind-supply",
        "payee_new": True,
        "local_hour": 17,
        "local_weekday": 4,
    }


async def test_free_text_never_reaches_the_facts() -> None:
    # Memos and email bodies may hold PII; only the argument digest covers them (skill §5, §7).
    result = await normalizer().normalize("create_payment", {**PAY, "memo": "acct 0042-7781"})
    assert "acct" not in json.dumps(result.facts)


@pytest.mark.parametrize(
    ("amount", "cents"),
    [("48000.00", 4_800_000), ("10000.4", 1_000_040), ("0.01", 1), (48000, 4_800_000)],
)
async def test_amounts_are_exact_cents(amount: object, cents: int) -> None:
    result = await normalizer().normalize("create_payment", {**PAY, "amount": amount})
    assert result.facts["amount_cents"] == cents


@pytest.mark.parametrize(
    "amount", ["0", "0.00", "-5", "1.001", "1e3", " 5", "5,000", "", "0012", 0, -1, True, 1.5]
)
async def test_malformed_or_non_positive_amounts_are_refused(amount: object) -> None:
    with pytest.raises(GateError) as caught:
        await normalizer().normalize("create_payment", {**PAY, "amount": amount})
    assert caught.value.code == "gate.bad_arguments"


async def test_a_known_payee_is_not_new() -> None:
    result = await normalizer().normalize("create_payment", {**PAY, "payee_id": "acme-paper"})
    assert result.facts["payee_new"] is False


async def test_an_unreachable_payee_directory_fails_closed() -> None:
    with pytest.raises(GateError) as caught:
        await normalizer(DownPayees()).normalize("create_payment", PAY)
    assert caught.value.code == "gate.fact_unavailable"


async def test_email_counts_external_recipients() -> None:
    to = ["ops@harborfederal.example", "ap@northwind.example", "AP@Other.Example"]
    result = await normalizer().normalize(
        "send_email", {"to": to, "subject": "Remittance", "body": "Paid."}
    )
    assert result.action == "comm.external"
    assert result.facts["recipients"] == 3
    assert result.facts["external_recipients"] == 2


async def test_vendor_lookup_is_a_data_read() -> None:
    result = await normalizer().normalize("lookup_vendor", {"query": "Northwind"})
    assert (result.action, result.facts["data_class"]) == ("data.read", "vendor")


async def test_an_unmapped_tool_is_refused() -> None:
    with pytest.raises(GateError) as caught:
        await normalizer().normalize("delete_ledger", {})
    assert caught.value.code == "gate.unmapped_tool"


BAD_ARGUMENTS = [
    ("create_payment", {**PAY, "extra": 1}),
    ("create_payment", {"payee_id": "northwind-supply", "amount": "5.00"}),
    ("create_payment", {**PAY, "currency": "usd"}),
    ("create_payment", {**PAY, "payee_id": "Northwind Supply"}),
    ("create_payment", {**PAY, "memo": "x" * 281}),
    ("send_email", {"to": [], "subject": "s", "body": "b"}),
    ("send_email", {"to": ["not-an-address"], "subject": "s", "body": "b"}),
    ("send_email", {"to": ["a@b.example"] * 21, "subject": "s", "body": "b"}),
    ("lookup_vendor", {"query": "x" * 101}),
    ("lookup_vendor", {"query": 5}),
    ("lookup_vendor", {"query": "x", "nested": {"value": 1.5}}),  # floats are not I-JSON
    ("lookup_vendor", {"query": "x" * 50, "pad": "y" * 17_000}),
]


@pytest.mark.parametrize(("tool", "arguments"), BAD_ARGUMENTS)
async def test_arguments_outside_the_tool_contract_are_refused(tool: str, arguments: dict) -> None:
    with pytest.raises(GateError) as caught:
        await normalizer().normalize(tool, arguments)
    assert caught.value.code == "gate.bad_arguments"


async def test_refusals_never_echo_argument_values() -> None:
    with pytest.raises(GateError) as caught:
        await normalizer().normalize("create_payment", {**PAY, "amount": "acct-0042-7781"})
    assert "0042" not in str(caught.value)


def test_the_argument_digest_is_domain_separated_and_order_independent() -> None:
    first = args_digest({"a": 1, "b": "x"})
    assert first == args_digest({"b": "x", "a": 1})
    assert first.startswith("sha256:")
    assert first != args_digest({"a": 1, "b": "y"})
    with pytest.raises(GateError):
        args_digest({"a": 0.5})


async def test_every_mapped_tool_produces_facts_the_schema_accepts() -> None:
    # The schema and the normalizer must agree, or Cedar returns NoDecision for every call.
    policies = PolicySet(
        '@id("all")\npermit (principal, action, resource);', SCHEMA_FILE.read_text()
    )
    samples = {
        "create_payment": PAY,
        "send_email": {"to": ["a@b.example"], "subject": "s", "body": "b"},
        "lookup_vendor": {"query": "acme"},
    }
    assert set(samples) == normalizer().tools
    for tool, arguments in samples.items():
        result = await normalizer().normalize(tool, arguments)
        evaluation = policies.evaluate("payments-ops", result.action, tool, result.facts)
        assert evaluation.outcome is Outcome.ALLOW, tool


def test_payees_load_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "payees.json"
    path.write_text(json.dumps({"v": 1, "known": ["acme-paper"]}))
    assert StaticPayees.from_file(path).known == frozenset({"acme-paper"})
    path.write_text(json.dumps({"v": 2, "known": []}))
    with pytest.raises(GateError):
        StaticPayees.from_file(path)
    with pytest.raises(GateError):
        StaticPayees.from_file(tmp_path / "missing.json")
