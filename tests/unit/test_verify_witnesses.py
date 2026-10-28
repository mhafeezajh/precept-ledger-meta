"""pl-verify with witness keys: quorum, forged and missing cosignatures, the rogue operator."""

import json
from pathlib import Path

import pytest

from common.errors import VerificationError
from ledger.checkpointer import Checkpointer, DirectorySink
from ledger.cosignature import CosignerKey
from ledger.export import export_bundle
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import MemoryStore
from ledger.witnessing import MemoryCosignatureStore, Witnessing, WitnessSet
from merkle.log import LogTree
from scripts.demo_rogue_operator import build
from tests.bundles import GATE, LOG, ORIGIN, members, pack, trusted_keys_json
from tests.helpers import STREAM, clock, make_submission
from tests.unit.test_witnessing import KEYS, Down, witnesses
from verify.bundle import read_bundle
from verify.checks import Report, verify_bundle
from verify.cli import main
from verify.keys import TrustedKeys, WitnessPolicy

TRUSTED = TrustedKeys.from_json(trusted_keys_json())


async def witnessed_bundle(
    directory: Path,
    sizes: tuple[int, ...],
    clients: list[object],
    *,
    witnessed_through: int | None = None,
) -> bytes:
    """Records and a checkpoint at each size; witnesses asked up to ``witnessed_through``."""
    store = MemoryStore()
    ledger = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    tree = LogTree(store.tree(STREAM))
    sink, cosignatures = DirectorySink(directory), MemoryCosignatureStore()
    checkpointer = Checkpointer(tree, LOG, ORIGIN, [sink])
    witnessing = Witnessing(tree, WitnessSet(KEYS, 2, cosignatures), clients)  # type: ignore[arg-type]
    done, clk = 0, clock()
    for size in sizes:
        for _ in range(size - done):
            await ledger.append(STREAM, make_submission(GATE, clk))
        signed = await checkpointer.publish()
        if witnessed_through is None or size <= witnessed_through:
            await witnessing.collect(signed)
        done = size
    return await export_bundle(store, sink, STREAM, ORIGIN, cosignatures)


def check(bundle: bytes, quorum: int = 2, keys: tuple[CosignerKey, ...] = KEYS) -> Report:
    return verify_bundle(read_bundle(bundle), TRUSTED, witnesses=WitnessPolicy(keys, quorum))


def checks(report: Report) -> list[str]:
    return [f.check for f in report.findings]


async def test_witnessed_bundle_verifies_and_reports_the_final_checkpoint(tmp_path: Path) -> None:
    report = check(await witnessed_bundle(tmp_path, (2, 4), witnesses()))
    assert report.ok, report.findings
    assert report.final is not None
    assert (report.final.size, report.final_cosigners, report.unwitnessed) == (
        4, ("witness-1", "witness-2", "witness-3"), 0,
    )  # fmt: skip


async def test_quorum_higher_than_the_cosigners_fails(tmp_path: Path) -> None:
    w = witnesses()
    bundle = await witnessed_bundle(tmp_path, (3,), [w[0], w[1], Down("witness-3")])
    assert check(bundle, quorum=2).ok
    report = check(bundle, quorum=3)
    assert checks(report) == ["below_quorum"]
    assert report.final is None
    assert report.unwitnessed == 3


async def test_unwitnessed_latest_checkpoint_fails_even_if_an_older_one_is_final(
    tmp_path: Path,
) -> None:
    bundle = await witnessed_bundle(tmp_path, (2, 3), witnesses(), witnessed_through=2)
    report = check(bundle)
    assert checks(report) == ["below_quorum"]
    assert report.final is not None
    assert (report.final.size, report.unwitnessed) == (2, 1)


async def test_cosigners_not_in_the_policy_do_not_count(tmp_path: Path) -> None:
    bundle = await witnessed_bundle(tmp_path, (2,), witnesses())
    assert checks(check(bundle, quorum=1, keys=(KEYS[0],))) == []
    stranger = CosignerKey("witness-9", GATE.public_key_raw)
    assert checks(check(bundle, quorum=1, keys=(stranger,))) == ["below_quorum"]


async def test_forged_cosignature_is_named(tmp_path: Path) -> None:
    bundle = await witnessed_bundle(tmp_path, (2,), witnesses())
    files = members(bundle)
    name = next(n for n in files if n.startswith("checkpoints/"))
    note = files[name].decode()
    line = next(x for x in note.splitlines() if x.startswith("— witness-2 "))
    forged = line[:-4] + ("AAA=" if not line.endswith("AAA=") else "BBB=")
    files[name] = note.replace(line, forged).encode()
    report = check(pack(files))
    assert "bad_cosignature" in checks(report)


async def test_stripped_cosignatures_fail_the_quorum(tmp_path: Path) -> None:
    bundle = await witnessed_bundle(tmp_path, (2,), witnesses())
    files = members(bundle)
    for name in [n for n in files if n.startswith("checkpoints/")]:
        text = files[name].decode()
        files[name] = "".join(
            x for x in text.splitlines(keepends=True) if not x.startswith("— witness-")
        ).encode()
    assert checks(check(pack(files))) == ["below_quorum"]
    # Without witness keys, the same bundle still passes: witnesses are what catch this.
    assert verify_bundle(read_bundle(pack(files)), TRUSTED).ok


async def test_records_without_any_checkpoint_fail_the_quorum(tmp_path: Path) -> None:
    bundle = await witnessed_bundle(tmp_path, (), witnesses())
    assert check(bundle).ok  # empty ledger: nothing to witness
    store = MemoryStore()
    ledger = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    await ledger.append(STREAM, make_submission(GATE, clock()))
    report = check(await export_bundle(store, DirectorySink(tmp_path / "none"), STREAM, ORIGIN))
    assert checks(report) == ["below_quorum"]


def test_witness_policy_parsing() -> None:
    text = f"# trusted witnesses\n\n{KEYS[0].verifier()}\n{KEYS[1].verifier()}\n".encode()
    policy = WitnessPolicy.from_text(text, 2)
    assert policy.keys == KEYS[:2]
    bad = [
        (b"", 1),
        (text, 3),
        (text, 0),
        (b"garbage\n", 1),
        (b"\xff", 1),
        (f"{KEYS[0].verifier()}\n{KEYS[0].verifier()}\n".encode(), 1),
    ]
    for data, quorum in bad:
        with pytest.raises(VerificationError) as exc:
            WitnessPolicy.from_text(data, quorum)
        assert exc.value.code == "verify.bad_witness_keys"


# --- the rogue operator, end to end through the CLI (demo beat 5) ----------------------------


async def test_rogue_operator_is_caught_by_witnesses_and_by_a_held_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = await build(tmp_path)
    assert set(s.rogue_refusals.values()) == {"witness.inconsistent"}
    base = [str(s.rogue), "--keys", str(s.keys)]
    assert main(base) == 0  # every signature in the rewrite is valid
    capsys.readouterr()
    assert main([*base, "--witness-keys", str(s.witness_keys), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert (report["first_break"]["check"], report["final"], report["quorum"]) == (
        "below_quorum", None, 2,
    )  # fmt: skip
    assert main([*base, "--trusted-checkpoint", str(s.held)]) == 1
    assert "rewritten_since_trusted" in capsys.readouterr().out
    honest = [str(s.honest), "--keys", str(s.keys), "--witness-keys", str(s.witness_keys)]
    assert main(honest) == 0
    out = capsys.readouterr().out
    assert "final: size 3, 3 of quorum 2 (witness-1, witness-2, witness-3)" in out
    assert main([*honest, "--quorum", "3"]) == 0


def test_cli_rejects_bad_witness_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "b.tar"
    bundle.write_bytes(pack({}))
    keys = tmp_path / "k.json"
    keys.write_bytes(trusted_keys_json())
    assert main([str(bundle), "--keys", str(keys), "--quorum", "2"]) == 2
    assert "--quorum needs --witness-keys" in capsys.readouterr().err
    wk = tmp_path / "w.txt"
    wk.write_text(KEYS[0].verifier() + "\n")
    assert main([str(bundle), "--keys", str(keys), "--witness-keys", str(wk)]) == 2  # quorum 2 > 1
