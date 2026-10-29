"""The demo bank behind the agent's three tools: a vendor directory, payments and an outbox.

It stands in for the bank's real systems so that beat 2 has something to change. It is
deliberately plain: it trusts its caller, because its caller is ``gate.governed.Governor``,
which runs a tool only after verifying Gate's records (R3). It keeps what it did in memory
so a run (or a test) can show exactly which effects happened.

Lookups return the last four digits of an account, never the number: whatever a tool returns
goes back to the model.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.clock import Clock, SystemClock
from common.ids import new_ulid

AP_EMAIL = "accounts-payable@harborfederal.example"  # internal: clause 4.1 allows it
MAX_MATCHES = 5


class ToolFailed(Exception):
    """The tool ran and failed. Gate records it as an ``error`` result."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    email: str
    onboarded: str
    account_last4: str

    def shown(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "email": self.email,
                "onboarded": self.onboarded, "account": "****" + self.account_last4}  # fmt: skip


def load_vendors(path: Path) -> tuple[Vendor, ...]:
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError(f"{path}: expected a v1 vendors file")
    return tuple(Vendor(**item) for item in data["vendors"])


class DemoBank:
    def __init__(self, vendors: tuple[Vendor, ...], clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._vendors = {v.id: v for v in vendors}
        self.lookups: list[str] = []
        self.payments: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool == "lookup_vendor":
            return self._lookup(str(arguments["query"]))
        if tool == "create_payment":
            return self._pay(arguments)
        if tool == "send_email":
            return self._send(arguments)
        raise ToolFailed(f"no tool {tool!r}", "bank.unknown_tool")

    def _lookup(self, query: str) -> dict[str, Any]:
        self.lookups.append(query)
        words = query.lower().split()
        found = [
            v.shown() for v in self._vendors.values()
            if all(w in f"{v.id} {v.name.lower()}" for w in words)
        ]  # fmt: skip
        return {"vendors": found[:MAX_MATCHES]}

    def _pay(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payee = self._vendors.get(str(arguments["payee_id"]))
        if payee is None:
            raise ToolFailed("the payee is not in the vendor directory", "bank.unknown_payee")
        payment = {"payment_id": "pay-" + new_ulid(self._clock).lower(), "status": "sent",
                   "payee_id": payee.id, "amount": arguments["amount"],
                   "currency": arguments["currency"]}  # fmt: skip
        self.payments.append({**payment, "memo": arguments.get("memo", "")})
        return payment

    def _send(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        message = {"message_id": "msg-" + new_ulid(self._clock).lower(), "status": "queued"}
        email = {"to": list(arguments["to"]), "subject": arguments["subject"],
                 "body": arguments["body"]}  # fmt: skip
        self.outbox.append({**message, **email})
        return message
