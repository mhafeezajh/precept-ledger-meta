"""pl-verify command line: output, exit codes, and running with the network unavailable."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.format import canonical_bytes
from tests.bundles import demo_bundle, lines, rewrite, trusted_keys_json
from verify.cli import main

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def files(tmp_path: Path) -> dict[str, Path]:
    bundle = await demo_bundle(tmp_path / "log")

    def edit(contents: dict[str, bytes]) -> None:
        rows = lines(contents["records.jsonl"])
        record = json.loads(rows[1])
        record["body"]["amount"] = 480
        rows[1] = canonical_bytes(record) + b"\n"
        contents["records.jsonl"] = b"".join(rows)

    paths = {
        "good": tmp_path / "good.tar",
        "tampered": tmp_path / "tampered.tar",
        "keys": tmp_path / "keys.json",
        "held": tmp_path / "held.note",
    }
    paths["good"].write_bytes(bundle)
    paths["tampered"].write_bytes(rewrite(bundle, edit))
    paths["keys"].write_bytes(trusted_keys_json())
    paths["held"].write_bytes((tmp_path / "log" / "00000000000000000003.note").read_bytes())
    return paths


def test_good_bundle_exits_0(files: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code = main([str(files["good"]), "--keys", str(files["keys"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "RESULT  OK" in out
    assert "verified through seq 5" in out


def test_tampered_bundle_names_the_record(
    files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(files["tampered"]), "--keys", str(files["keys"])])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED: first break at seq 2 (record " in out
    assert "bad_producer_signature" in out
    assert "checkpoint_mismatch" in out


def test_json_output(files: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    code = main([str(files["tampered"]), "--keys", str(files["keys"]), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["ok"] is False
    assert (report["first_break"]["seq"], report["first_break"]["check"]) == (
        2,
        "bad_producer_signature",
    )
    assert report["latest"] is None  # neither checkpoint matches the edited records


def test_trusted_checkpoint_option(
    files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            str(files["good"]),
            "--keys",
            str(files["keys"]),
            "--trusted-checkpoint",
            str(files["held"]),
        ]
    )
    assert code == 0
    assert "RESULT  OK" in capsys.readouterr().out


@pytest.mark.parametrize("problem", ["missing", "not_tar", "bad_keys", "bad_held"])
def test_cannot_verify_exits_2(
    files: dict[str, Path], tmp_path: Path, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "junk"
    junk.write_bytes(b"junk" * 200)
    args = {
        "missing": [str(tmp_path / "nope.tar"), "--keys", str(files["keys"])],
        "not_tar": [str(junk), "--keys", str(files["keys"])],
        "bad_keys": [str(files["good"]), "--keys", str(junk)],
        "bad_held": [
            str(files["good"]),
            "--keys",
            str(files["keys"]),
            "--trusted-checkpoint",
            str(junk),
        ],
    }[problem]
    assert main(args) == 2
    assert "cannot verify" in capsys.readouterr().err


def test_oversized_input_exits_2(files: dict[str, Path], tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1024 * 1024 + 1))
    assert main([str(files["good"]), "--keys", str(big)]) == 2


OFFLINE = """
import sys

def no_network(event, args):
    if event.startswith("socket."):
        raise RuntimeError("network touched: " + event)

sys.addaudithook(no_network)
if sys.argv[1] == "control":
    import socket
    socket.socket()
from verify.cli import main
sys.exit(main(sys.argv[1:]))
"""


def run_offline(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed interpreter and arguments built by this test
        [sys.executable, "-c", OFFLINE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_runs_with_the_network_forbidden(files: dict[str, Path]) -> None:
    # Positive control first: the hook really does stop socket use.
    control = run_offline("control")
    assert control.returncode != 0
    assert "network touched" in control.stderr
    good = run_offline(str(files["good"]), "--keys", str(files["keys"]))
    assert good.returncode == 0, good.stderr
    tampered = run_offline(str(files["tampered"]), "--keys", str(files["keys"]))
    assert tampered.returncode == 1
    assert "first break at seq 2" in tampered.stdout


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "verify", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert "pl-verify" in result.stdout
