"""Demo beat 3 against a running ledger: an examiner's question, answered with proven claims.

Needs ``pl-ledger`` (``make serve``, with witnesses for final checkpoints) holding Gate records
(``make demo-gate`` first), and ``META_API_KEY`` in ``.env``. Each claim is shown as proven
(✅) or rejected (❌) with the checker's reasons; the audit is appended as ``assist.qa``.
``--inject-fault`` changes one of the model's assertions to show a wrong claim being caught.
Run: ``make demo-audit`` (``ARGS="--inject-fault"``).
"""

import argparse
import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assist.auditor import Auditor, LedgerRecorder  # noqa: E402  # path set up above for a plain script
from assist.evidence import HttpLedgerReader  # noqa: E402
from assist.llm import LlmClient, model_config  # noqa: E402
from claims.checker import Trust  # noqa: E402
from common.clock import SystemClock  # noqa: E402
from common.config import load_settings  # noqa: E402
from gate.ledger_client import HttpLedgerClient  # noqa: E402
from ledger.checkpointer import origin_for  # noqa: E402
from ledger.receipts import LOG_KEY_OWNER  # noqa: E402
from ledger.signing import KeyRing, Signer  # noqa: E402
from verify.keys import TrustedKeys, WitnessPolicy  # noqa: E402

QUESTION = (
    "Show every payment over $10,000 approved outside business hours this week,"
    " and who approved it."
)


async def run(question: str, inject_fault: bool, witnessed: bool) -> int:
    settings = load_settings()
    config = model_config(settings)
    if config is None:
        print("set META_API_KEY in .env (Standard mode) to ask Muse Spark")
        return 2
    keys_dir, stream = settings.keys_dir, settings.ledger_stream
    trusted = TrustedKeys.from_json((keys_dir / "trusted.json").read_bytes())
    witnesses = None
    if witnessed:
        witnesses = WitnessPolicy.from_text(
            (keys_dir / "witnesses.txt").read_bytes(), settings.witness_quorum
        )
    trust = Trust(trusted, origin_for(settings.ledger_origin, stream), witnesses)
    base = f"http://{settings.api_host}:{settings.api_port}"
    clock = SystemClock()
    async with httpx.AsyncClient(timeout=30) as http:
        recorder = LedgerRecorder(
            signer=Signer.from_pem_file(keys_dir / "assist-1.pem"), producer_id="assist-1",
            stream=stream, ledger=HttpLedgerClient(base, http),
            log_keys=KeyRing((LOG_KEY_OWNER, raw) for raw in trusted.log_keys), clock=clock,
        )  # fmt: skip
        auditor = Auditor(
            LlmClient(config), HttpLedgerReader(base, http, stream, final=witnessed), trust,
            recorder, clock=clock, timezone=ZoneInfo("America/New_York"),
        )  # fmt: skip
        result = await auditor.ask(question, inject_fault=inject_fault)
    print(f"Q: {result.question}")
    print(f"query ({result.rows} rows): {result.query.model_dump()}")
    for index, claim in enumerate(result.results):
        mark = "✅" if claim.accepted else "❌"
        note = "  [injected fault]" if index == result.injected else ""
        print(f"{mark} {claim.claim.text}{note}")
        for reason in claim.reasons:
            print(f"     - {reason}")
    print(f"A: {result.answer_text()}")
    if result.record is not None:
        print(f"recorded as assist.qa {result.record.record_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default=QUESTION)
    parser.add_argument("--inject-fault", action="store_true")
    parser.add_argument(
        "--no-witnesses", action="store_true", help="prove against unwitnessed checkpoints"
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args.question, args.inject_fault, not args.no_witnesses))


if __name__ == "__main__":
    sys.exit(main())
