"""Normalizer: a tool call becomes an action class and typed facts, or it is refused.

The facts are exactly the Cedar context (``gate/schema.cedarschema``) and exactly what the
decision record shows as ``resource``. They hold numbers, flags and ids, never free text:
memos and email bodies are covered only by the argument digest (skill §7). Unmapped tools,
arguments outside a tool's contract and unreachable fact sources all raise ``GateError``,
which Gate records as a deny (R4).
"""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from common.clock import Clock
from common.errors import FormatError, GateError
from ledger.format import canonical_bytes, check_ijson

ARGS_DIGEST_CONTEXT = b"PL-args-v1\x00"  # skill §4: its own context, never a record hash
MAX_ARGUMENT_BYTES = 16 * 1024
_AMOUNT = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.IGNORECASE)

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]


@dataclass(frozen=True)
class Normalized:
    action: str
    facts: dict[str, Any]


class PayeeDirectory(Protocol):
    async def is_known(self, payee_id: str) -> bool: ...


class StaticPayees:
    """Payees the bank has paid before, from a JSON file (``{"v": 1, "known": [...]}``)."""

    def __init__(self, known: Iterable[str]) -> None:
        self.known = frozenset(known)

    @classmethod
    def from_file(cls, path: Path) -> "StaticPayees":
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise GateError(f"cannot read payees: {exc}", code="gate.config") from exc
        known = data.get("known") if isinstance(data, dict) and data.get("v") == 1 else None
        if not isinstance(known, list) or not all(isinstance(p, str) for p in known):
            raise GateError("payees file must be {v: 1, known: [ids]}", code="gate.config")
        return cls(known)

    async def is_known(self, payee_id: str) -> bool:
        return payee_id in self.known


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CreatePayment(_Args):
    payee_id: Slug
    amount: str | int  # a decimal string or whole units; floats are refused (ledger is I-JSON)
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    memo: Annotated[str, StringConstraints(max_length=280)] = ""


class SendEmail(_Args):
    to: Annotated[
        list[Annotated[str, StringConstraints(max_length=254)]], Field(min_length=1, max_length=20)
    ]
    subject: Annotated[str, StringConstraints(max_length=200)]
    body: Annotated[str, StringConstraints(max_length=10_000)]


class LookupVendor(_Args):
    query: Annotated[str, StringConstraints(min_length=1, max_length=100)]


def _bad(message: str) -> GateError:
    return GateError(message, code="gate.bad_arguments")


def amount_cents(amount: str | int) -> int:
    """Exact cents from ``"48000.00"`` or ``48000``. No rounding: a cent over a limit counts."""
    text = str(amount) if isinstance(amount, int) else amount
    match = _AMOUNT.fullmatch(text)
    if match is None:
        raise _bad("amount must be a positive decimal with at most two places")
    whole, fraction = match.group(1), (match.group(2) or ".")[1:]
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if cents <= 0:
        raise _bad("amount must be positive")
    return cents


def _canonical_arguments(arguments: Mapping[str, Any]) -> bytes:
    try:
        check_ijson(arguments, "$.arguments")
        return canonical_bytes(dict(arguments))
    except FormatError as exc:
        raise _bad(f"arguments are not I-JSON ({exc.code})") from exc


def args_digest(arguments: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical arguments. Binds a decision to one exact call."""
    data = _canonical_arguments(arguments)
    return "sha256:" + hashlib.sha256(ARGS_DIGEST_CONTEXT + data).hexdigest()


class Normalizer:
    def __init__(
        self,
        payees: PayeeDirectory,
        *,
        internal_domains: Iterable[str],
        clock: Clock,
        timezone: ZoneInfo,
    ) -> None:
        self._payees = payees
        self._internal = frozenset(d.lower() for d in internal_domains)
        self._clock = clock
        self._timezone = timezone
        self._tools: dict[str, Callable[[dict[str, Any]], Awaitable[Normalized]]] = {
            "create_payment": self._payment,
            "send_email": self._email,
            "lookup_vendor": self._vendor_lookup,
        }

    @property
    def tools(self) -> frozenset[str]:
        return frozenset(self._tools)

    async def normalize(self, tool: str, arguments: Mapping[str, Any]) -> Normalized:
        handler = self._tools.get(tool)
        if handler is None:
            raise GateError(
                f"tool {tool!r} is not mapped to an action class", code="gate.unmapped_tool"
            )
        if len(_canonical_arguments(arguments)) > MAX_ARGUMENT_BYTES:
            raise _bad(f"arguments exceed {MAX_ARGUMENT_BYTES} bytes")
        return await handler(dict(arguments))

    def _time_facts(self) -> dict[str, int]:
        local = self._clock.now().astimezone(self._timezone)
        return {"local_hour": local.hour, "local_weekday": local.isoweekday()}

    async def _payment(self, arguments: dict[str, Any]) -> Normalized:
        args = _parse(CreatePayment, arguments)
        cents = amount_cents(args.amount)
        try:
            known = await self._payees.is_known(args.payee_id)
        except Exception as exc:  # any failure to learn a fact is a deny, never a guess (R4)
            raise GateError("payee directory unavailable", code="gate.fact_unavailable") from exc
        facts = {
            "amount_cents": cents,
            "currency": args.currency,
            "payee_id": args.payee_id,
            "payee_new": not known,
            **self._time_facts(),
        }
        return Normalized("value.transfer", facts)

    async def _email(self, arguments: dict[str, Any]) -> Normalized:
        args = _parse(SendEmail, arguments)
        if not all(_EMAIL.fullmatch(address) for address in args.to):
            raise _bad("every recipient must be an email address")
        external = sum(a.rsplit("@", 1)[1].lower() not in self._internal for a in args.to)
        facts = {"recipients": len(args.to), "external_recipients": external, **self._time_facts()}
        return Normalized("comm.external", facts)

    async def _vendor_lookup(self, arguments: dict[str, Any]) -> Normalized:
        _parse(LookupVendor, arguments)
        return Normalized("data.read", {"data_class": "vendor", **self._time_facts()})


def _parse[M: _Args](model: type[M], arguments: dict[str, Any]) -> M:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        # Field names only: Pydantic's messages can echo the rejected value.
        fields = sorted({".".join(str(p) for p in e["loc"]) or "$" for e in exc.errors()})
        raise _bad(f"invalid argument(s) for this tool: {fields}") from None
