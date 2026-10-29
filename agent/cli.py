"""``pl-agent "<task>"``: run the payments-ops agent once against a running pl-gate.

The agent authenticates to Gate with its own token (``KEYS_DIR/tokens/<agent>.token``) and
checks Gate's and the log's signatures with the public keys in ``KEYS_DIR/trusted.json``; it
holds no signing key and no ledger write path (threat model: "Agent deletes or edits its own
trail"). The model comes from the active profile (``MODEL_PROFILE``: mock, cloud or onprem).
A held payment prints its decision record and waits for an approver.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.bank import DemoBank, load_vendors
from agent.graph import AgentRun, PaymentsAgent, StepRecord
from agent.steps import AgentStep, StepPlanner
from assist.llm import LlmClient, model_config
from common.config import Settings, load_settings
from common.errors import ConfigError, PreceptError
from gate.client import HttpGateClient
from gate.delegation import parse_signed_credential
from gate.governed import Governor
from ledger.receipts import LOG_KEY_OWNER
from ledger.signing import KeyRing
from verify.keys import TrustedKeys


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", frozen=True
    )

    id: str = "payments-ops"
    gate_url: str = "http://127.0.0.1:8070"
    gate_producer_id: str = "gate-1"
    vendors_file: Path = Path("deploy/demo/vendors.json")
    max_steps: int = 8
    gate_timeout_seconds: float = 30.0  # longer than one escalation long poll (25 s)
    # The principal's signed delegation this agent presents with every call (D88, D89);
    # default KEYS_DIR/credentials/<id>.json, written by make delegate. None if absent.
    credential_file: Path | None = None


def _keys(keys_dir: Path, agent: str) -> tuple[str, KeyRing, KeyRing]:
    try:
        token = (keys_dir / "tokens" / f"{agent}.token").read_text("utf-8").strip()
        trusted = TrustedKeys.from_json((keys_dir / "trusted.json").read_bytes())
    except OSError as exc:
        raise ConfigError(f"agent keys missing ({exc.strerror}); run make dev-keys",
                          code="config.keys") from exc  # fmt: skip
    gate_keys = KeyRing(trusted.producers.values())
    return token, gate_keys, KeyRing((LOG_KEY_OWNER, raw) for raw in trusted.log_keys)


def load_credential(path: Path) -> dict[str, Any] | None:
    """The saved signed credential, checked for shape (Gate checks everything else)."""
    if not path.is_file():
        return None
    try:
        envelope = json.loads(path.read_text("utf-8"))
        parse_signed_credential(envelope)
    except (OSError, ValueError, PreceptError) as exc:
        raise ConfigError(f"unreadable delegation credential {path.name}",
                          code="config.credential") from exc  # fmt: skip
    return dict(envelope)


@asynccontextmanager
async def open_agent(
    settings: Settings, agent: AgentSettings | None = None
) -> AsyncIterator[PaymentsAgent]:
    """The agent wired to Gate over HTTP, the active model profile and the demo bank."""
    agent = agent or AgentSettings()
    config = model_config(settings)
    if config is None:
        raise ConfigError("no model for the agent: set MODEL_PROFILE=mock or a key",
                          code="config.model_profile")  # fmt: skip
    token, gate_keys, log_keys = _keys(settings.keys_dir, agent.id)
    path = agent.credential_file or settings.keys_dir / "credentials" / f"{agent.id}.json"
    credential = load_credential(path)
    async with httpx.AsyncClient(timeout=agent.gate_timeout_seconds) as http:
        governor = Governor(
            HttpGateClient(agent.gate_url, token, http), agent=agent.id, gate_keys=gate_keys,
            log_keys=log_keys, producer_id=agent.gate_producer_id, credential=credential,
        )  # fmt: skip
        bank = DemoBank(load_vendors(agent.vendors_file))
        yield PaymentsAgent(
            StepPlanner(LlmClient(config)), governor, bank, max_steps=agent.max_steps
        )


class Printer:
    """Prints each step as it happens; never prints tokens or keys."""

    def started(self, n: int, step: AgentStep) -> None:
        print(f"{n}. {step.tool} {step.arguments()}")
        if step.note:
            print(f"   why: {step.note}")

    def held(self, n: int, decision_record_id: str) -> None:
        print(f"   held by Gate: decision {decision_record_id}; waiting for an approver")

    def finished(self, record: StepRecord) -> None:
        gate = f"Gate {record.decision}" if record.decision else "Gate not reached"
        if record.verdict_record_id:
            gate += f", approved in {record.verdict_record_id}"
        detail = record.code if record.code else record.result
        print(f"   {record.outcome} ({gate}): {detail}")


def show(run: AgentRun) -> None:
    print(f"reply: {run.reply}")
    if run.stopped:
        print(f"stopped: {run.stopped}")


async def run(task: str) -> int:
    async with open_agent(load_settings()) as agent:
        result = await agent.run(task, observer=Printer())
    show(result)
    return 0 if result.stopped is None else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help='e.g. "Pay Northwind Supply $48,000 for invoice INV-4471"')
    args = parser.parse_args(argv)
    return asyncio.run(run(args.task))


if __name__ == "__main__":
    sys.exit(main())
