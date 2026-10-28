"""pl-verify with anchors: trusted timestamps, back-dating, mislabelled and forged anchors,
OpenTimestamps claims, bundle v2 rules, and the CLI."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from anchor.rfc3161 import load_roots, verify_token
from common.errors import VerificationError
from ledger.anchoring import Anchorer, MemoryAnchorStore
from ledger.export import export_bundle
from tests.bundles import ORIGIN, Demo, members, pack, rewrite, trusted_keys_json
from tests.helpers import STREAM, T0
from tests.tsa import FakeTSA
from tests.unit.test_anchoring import LocalCalendar
from tests.unit.test_ots import SHA256, bitcoin
from verify.anchors import AnchorPolicy
from verify.bundle import read_bundle
from verify.checks import Report, verify_bundle
from verify.cli import main
from verify.keys import TrustedKeys

TSA = FakeTSA()
ROOTS = tuple(load_roots(TSA.root_pem))
TRUSTED = TrustedKeys.from_json(trusted_keys_json())


class ClockedTSA:
    """Signs with the fake TSA at a chosen time (records in tests.helpers start at T0)."""

    def __init__(self, at: datetime) -> None:
        self.name, self.at = "test-tsa", at

    async def timestamp(self, digest: bytes) -> bytes:
        token = TSA.token(digest, gen_time=self.at)
        verify_token(token, digest, ROOTS)
        return token


class FixedClock:
    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current


async def anchored_bundle(
    directory: Path, *, at: datetime, sizes: tuple[int, ...] = (2, 4), ots: bool = False
) -> bytes:
    demo = Demo(directory)
    store = MemoryAnchorStore()
    calendars = [LocalCalendar()] if ots else []
    anchorer = Anchorer(store, [ClockedTSA(at)], calendars, {c.url: c for c in calendars},
                        clock=FixedClock(), ots_interval_seconds=0)  # fmt: skip
    done = 0
    for size in sizes:
        await demo.append(size - done)
        await anchorer.anchor(await demo.checkpointer.publish())
        done = size
    return await export_bundle(demo.store, demo.sink, STREAM, ORIGIN, None, store)


def check(bundle: bytes, **policy: object) -> Report:
    anchors = AnchorPolicy(ROOTS, **policy)  # type: ignore[arg-type]
    return verify_bundle(read_bundle(bundle), TRUSTED, anchors=anchors)


def checks(report: Report) -> list[str]:
    return [f.check for f in report.findings]


async def test_timely_timestamps_verify(tmp_path: Path) -> None:
    report = check(await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5)))
    assert report.ok, report.findings
    assert report.anchors is not None
    latest = report.anchors.latest
    assert latest is not None
    assert (latest.size, latest.gen_time, latest.tsa) == (
        4,
        T0 + timedelta(seconds=5),
        "Test TSA",
    )


async def test_without_roots_tokens_are_counted_not_trusted(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5))
    report = verify_bundle(read_bundle(bundle), TRUSTED)
    assert report.ok
    assert report.anchors is not None
    assert (report.anchors.checked, report.anchors.tokens, report.anchors.timestamps) == (
        False, 2, (),
    )  # fmt: skip


async def test_backdated_history_is_caught(tmp_path: Path) -> None:
    # Operator and every witness rebuild history two days later. Records still claim T0,
    # but no TSA will sign a time before the rebuild.
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(days=2))
    report = check(bundle)
    assert checks(report) == ["late_timestamp"]
    first = report.findings[0]
    assert first.seq == 1
    assert first.record_id is not None
    assert "4 records" in first.detail
    assert check(bundle, max_lag_seconds=3 * 86400).ok


async def test_record_claiming_a_time_after_its_timestamp(tmp_path: Path) -> None:
    report = check(await anchored_bundle(tmp_path, at=T0 - timedelta(hours=1)))
    assert checks(report) == ["record_after_timestamp"]
    assert report.findings[0].seq == 1


async def test_untrusted_tsa_is_a_finding(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5))
    other = tuple(load_roots(FakeTSA().root_pem))
    report = verify_bundle(read_bundle(bundle), TRUSTED, anchors=AnchorPolicy(other))
    assert checks(report) == ["bad_timestamp", "bad_timestamp", "not_timestamped"]


async def test_token_relabelled_to_another_checkpoint_fails(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5))

    def relabel(files: dict[str, bytes]) -> None:
        name = next(n for n in files if n.startswith(f"anchors/{2:020d}"))
        files[name.replace(f"{2:020d}", f"{4:020d}").replace("-00.", "-07.")] = files.pop(name)

    report = check(rewrite(bundle, relabel))
    assert checks(report) == ["bad_timestamp"]
    assert "different digest" in report.findings[0].detail


async def test_anchor_for_a_missing_checkpoint(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5))

    def orphan(files: dict[str, bytes]) -> None:
        files[f"anchors/{9:020d}-rfc3161-09.tsr"] = files[f"anchors/{2:020d}-rfc3161-00.tsr"]

    assert "anchor_without_checkpoint" in checks(check(rewrite(bundle, orphan)))


async def test_records_without_any_timestamp(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(2)
    await demo.checkpoint()
    assert checks(check(await demo.export())) == ["not_timestamped"]
    empty = Demo(tmp_path / "empty")
    assert check(await empty.export()).ok


async def test_ots_claims_are_reported_and_bad_proofs_named(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5), ots=True)
    report = check(bundle)
    assert report.ok, report.findings
    assert report.anchors is not None
    assert {(c.size, c.status) for c in report.anchors.bitcoin} == {(2, "pending"), (4, "pending")}

    def corrupt(files: dict[str, bytes]) -> None:
        name = next(n for n in files if n.endswith(".ots"))
        files[name] = b"\x99" + files[name]

    assert checks(check(rewrite(bundle, corrupt))) == ["bad_ots"]

    def confirm(files: dict[str, bytes]) -> None:
        name = next(n for n in files if n.endswith(".ots"))
        files[name] = SHA256 + bitcoin(912_345)

    claims = check(rewrite(bundle, confirm)).anchors
    assert claims is not None
    assert any(c.status == "bitcoin" and c.height == 912_345 for c in claims.bitcoin)


async def test_bundle_rules_for_anchor_members(tmp_path: Path) -> None:
    bundle = await anchored_bundle(tmp_path, at=T0 + timedelta(seconds=5))
    files = members(bundle)
    name = next(n for n in files if n.startswith("anchors/"))
    for bad in (name.replace(".tsr", ".ots"), "anchors/2-rfc3161-0.tsr"):
        changed = dict(files)
        changed[bad] = changed.pop(name)
        with pytest.raises(VerificationError):
            read_bundle(pack(changed))
    v1 = dict(files)
    manifest = json.loads(v1["manifest.json"])
    manifest["v"] = 1
    del manifest["counts"]["anchors"]
    v1["manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(VerificationError) as exc:  # anchors in a v1 bundle
        read_bundle(pack(v1))
    assert exc.value.code == "bundle.bad_member"
    big = dict(files)
    big[name] = b"x" * 70_000
    with pytest.raises(VerificationError):
        read_bundle(rewrite(pack(big), lambda f: None))


async def test_cli_reports_timestamps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = tmp_path / "b.tar"
    bundle.write_bytes(await anchored_bundle(tmp_path / "log", at=T0 + timedelta(days=2), ots=True))
    keys, roots = tmp_path / "keys.json", tmp_path / "roots.pem"
    keys.write_bytes(trusted_keys_json())
    roots.write_bytes(TSA.root_pem)
    base = [str(bundle), "--keys", str(keys)]
    assert main(base) == 0
    assert "RFC 3161 tokens, not checked (no --tsa-roots)" in capsys.readouterr().out
    assert main([*base, "--tsa-roots", str(roots)]) == 1
    out = capsys.readouterr().out
    assert "latest: size 4 at 2026-10-31" in out
    assert "late_timestamp" in out
    assert "pending at https://alice.example" in out
    assert main([*base, "--tsa-roots", str(roots), "--max-timestamp-lag", "300000", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["anchors"]["timestamps"][-1]["size"] == 4
    assert {c["status"] for c in report["anchors"]["bitcoin"]} == {"pending"}
    roots.write_bytes(b"junk")
    assert main([*base, "--tsa-roots", str(roots)]) == 2
