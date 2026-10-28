"""Demo beat 5, from the command line: a rogue operator rewrites history and is caught.

Everything runs in one process with throwaway keys generated on the spot (never written
anywhere but the output directory, and only public keys are). Steps:

1. Gate appends three payment decisions. The ledger checkpoints them and three witnesses
   cosign: the checkpoint is final (2 of 3 needed).
2. An auditor keeps the checkpoint witness-1 last cosigned (``held.note``).
3. The operator, who holds the log key, rebuilds the ledger without decision 2 (re-chained,
   re-receipted, re-checkpointed: every signature is valid) and asks the witnesses to cosign.
   They refuse: the new tree does not extend the one they cosigned.
4. ``pl-verify`` on the rogue export: with only the keys it passes (that is the attack a hash
   chain alone cannot stop); with the witness keys it fails ``below_quorum``; with the held
   checkpoint it fails ``rewritten_since_trusted``.

Run: ``make demo-rogue`` (or ``uv run python scripts/demo_rogue_operator.py [OUT_DIR]``).
"""

import argparse
import asyncio
import contextlib
import io
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.clock import SystemClock, format_ts  # noqa: E402  # path set up above
from common.errors import WitnessError  # noqa: E402
from common.ids import new_ulid  # noqa: E402
from common.logging import configure_logging  # noqa: E402
from ledger.checkpoint import NoteKey  # noqa: E402
from ledger.checkpointer import Checkpointer, DirectorySink, origin_for  # noqa: E402
from ledger.export import export_bundle  # noqa: E402
from ledger.format import Submission, b64encode  # noqa: E402
from ledger.service import Ledger  # noqa: E402
from ledger.signing import KeyRing, Signer, sign_submission  # noqa: E402
from ledger.store import MemoryStore  # noqa: E402
from ledger.witnessing import (  # noqa: E402
    MemoryCosignatureStore,
    WitnessedPublisher,
    Witnessing,
    WitnessSet,
)
from merkle.log import LogTree  # noqa: E402
from verify.cli import main as pl_verify  # noqa: E402
from witness.core import Witness  # noqa: E402
from witness.state import FileWitnessState  # noqa: E402

STREAM = "harbor-federal/decisions"
ORIGIN = origin_for("precept-ledger.local", STREAM)
AMOUNTS = (1_200, 48_000, 950)  # decision 2 is the one the operator wants gone


@dataclass(frozen=True)
class Scenario:
    honest: Path
    rogue: Path
    keys: Path
    witness_keys: Path
    held: Path
    rogue_refusals: dict[str, str]


@dataclass
class _Log:
    ledger: Ledger
    store: MemoryStore
    sink: DirectorySink
    cosignatures: MemoryCosignatureStore
    publisher: WitnessedPublisher
    witnessing: Witnessing
    checkpointer: Checkpointer


def _log(directory: Path, gate: Signer, log: Signer, witnesses: list[Witness]) -> _Log:
    store = MemoryStore()
    ledger = Ledger(store, KeyRing([("gate-1", gate.public_key_raw)]), log, SystemClock())
    sink, cosignatures = DirectorySink(directory), MemoryCosignatureStore()
    tree = LogTree(store.tree(STREAM))
    checkpointer = Checkpointer(tree, log, ORIGIN, [sink])
    witness_set = WitnessSet(tuple(w.key for w in witnesses), 2, cosignatures)
    witnessing = Witnessing(tree, witness_set, witnesses)
    publisher = WitnessedPublisher(checkpointer, witnessing)
    return _Log(ledger, store, sink, cosignatures, publisher, witnessing, checkpointer)


def _decision(gate: Signer, amount: int) -> Submission:
    return sign_submission(
        gate,
        producer_id="gate-1",
        stream=STREAM,
        record_id=new_ulid(SystemClock()),
        ts=format_ts(SystemClock().now()),
        record_type="gate.decision",
        body={"action": "value.transfer", "amount": amount, "currency": "USD"},
    )


def _write_public_keys(out: Path, gate: Signer, log_key: Signer, witnesses: list[Witness]) -> None:
    trusted = {
        "v": 1,
        "producers": {"gate-1": [b64encode(gate.public_key_raw)]},
        "log": [b64encode(log_key.public_key_raw)],
    }
    (out / "keys.json").write_text(json.dumps(trusted, indent=2) + "\n")
    (out / "witnesses.txt").write_text("".join(w.key.verifier() + "\n" for w in witnesses))


async def build(out: Path) -> Scenario:
    gate, log_key = Signer.generate(), Signer.generate()
    witnesses = [
        Witness(
            f"witness-{n}",
            Signer.generate(),
            [NoteKey(ORIGIN, log_key.public_key_raw)],
            FileWitnessState(out / "witness-state" / f"witness-{n}"),
            SystemClock(),
        )
        for n in (1, 2, 3)
    ]
    _write_public_keys(out, gate, log_key, witnesses)

    honest = _log(out / "honest-checkpoints", gate, log_key, witnesses)
    submissions = [_decision(gate, amount) for amount in AMOUNTS]
    for submission in submissions:
        await honest.ledger.append(STREAM, submission)
    await honest.publisher.publish()  # final: all three witnesses cosign size 3
    bundle = await export_bundle(honest.store, honest.sink, STREAM, ORIGIN, honest.cosignatures)
    (out / "honest.tar").write_bytes(bundle)
    held = await witnesses[0].latest_note(ORIGIN)
    if held is None:
        raise WitnessError("witness-1 cosigned nothing", code="witness.not_found")
    (out / "held.note").write_text(held)

    # The rogue operator: same log key and Gate submissions, decision 2 gone, one padding
    # decision so the tree is the same size again. Every signature in it is valid.
    rogue = _log(out / "rogue-checkpoints", gate, log_key, witnesses)
    for submission in (submissions[0], submissions[2], _decision(gate, 700)):
        await rogue.ledger.append(STREAM, submission)
    result = await rogue.witnessing.collect(await rogue.checkpointer.publish())
    bundle = await export_bundle(rogue.store, rogue.sink, STREAM, ORIGIN, rogue.cosignatures)
    (out / "rogue.tar").write_bytes(bundle)
    return Scenario(
        honest=out / "honest.tar",
        rogue=out / "rogue.tar",
        keys=out / "keys.json",
        witness_keys=out / "witnesses.txt",
        held=out / "held.note",
        rogue_refusals=result.refused,
    )


def verify(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = pl_verify(list(args))
    return code, out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo beat 5: the rogue operator is caught.")
    parser.add_argument("out", nargs="?", type=Path, help="output directory (default: temp)")
    args = parser.parse_args(argv)
    out = args.out or Path(tempfile.mkdtemp(prefix="pl-rogue-"))
    out.mkdir(parents=True, exist_ok=True)
    configure_logging("CRITICAL")  # the witness alerts, as JSON on stderr
    scenario = asyncio.run(build(out))
    print(f"files in {out}\n")
    print("1. Honest ledger: 3 decisions, checkpoint cosigned by 3 witnesses (final).")
    print("2. Auditor keeps witness-1's latest cosigned checkpoint: held.note")
    print("3. Operator rebuilds history without decision 2 and re-signs it with the log key.")
    for name, code in sorted(scenario.rogue_refusals.items()):
        print(f"   {name} refused: {code}")
    caught = 0
    runs = [
        ("keys only (a hash chain + log signature can't tell)", []),
        ("with witness keys, quorum 2", ["--witness-keys", str(scenario.witness_keys)]),
        ("with the held checkpoint", ["--trusted-checkpoint", str(scenario.held)]),
    ]
    for step, (label, extra) in enumerate(runs, start=4):
        code, text = verify(str(scenario.rogue), "--keys", str(scenario.keys), *extra)
        result = next(line for line in text.splitlines() if line.startswith("RESULT"))
        print(f"{step}. pl-verify rogue.tar {label}\n   exit {code}  {result}")
        caught += code == 1 and step > 4
    honest_code, _ = verify(
        str(scenario.honest), "--keys", str(scenario.keys),
        "--witness-keys", str(scenario.witness_keys),
    )  # fmt: skip
    print(f"7. pl-verify honest.tar with witness keys\n   exit {honest_code}")
    return 0 if caught == 2 and honest_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
