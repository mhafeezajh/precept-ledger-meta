"""Demo beat 2 with the real agent: payments-ops (LangGraph) pays $48,000 to a new vendor.

Needs ``make serve`` and ``make gate`` running, ``make dev-keys``, and a model profile
(``MODEL_PROFILE=mock`` or ``cloud`` for Muse Spark). Gate holds the payment; j.ortiz approves
it here after reading the brief, unless ``--manual`` (approve yourself with curl or the web
page). Run: ``make demo-agent`` (``ARGS='--task "Pay Acme Paper $300,000 for INV-9"'`` for a
denial).
"""

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.cli import AgentSettings, Printer, open_agent, show  # noqa: E402  # path set up above
from common.clock import SystemClock, format_ts  # noqa: E402
from common.config import load_settings  # noqa: E402
from scripts.demo_gate import approve_ours  # noqa: E402

TASK = "Pay Northwind Supply $48,000.00 for invoice INV-4471."


async def run(task: str, manual: bool) -> int:
    settings = load_settings()
    since = format_ts(SystemClock().now())
    print(f"task: {task}")
    async with (
        open_agent(settings, AgentSettings()) as agent,
        httpx.AsyncClient(timeout=30) as http,
    ):
        approver = None
        if not manual:
            approver = asyncio.create_task(approve_ours(http, settings.keys_dir, since))
        result = await agent.run(task, observer=Printer())
        if approver is not None:
            approver.cancel()  # nothing was held (allowed or denied outright), or already done
    show(result)
    return 0 if result.stopped is None else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--manual", action="store_true", help="approve it yourself")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.task, args.manual))


if __name__ == "__main__":
    sys.exit(main())
