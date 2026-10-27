"""pl-verify checks on real bundles: clean, and every tamper the skill's suite names (§6)."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from common.errors import VerificationError
from ledger.checkpoint import Checkpoint, sign_checkpoint
from ledger.format import canonical_bytes
from merkle.hashing import leaf_hash
from tests.bundles import (
    GATE,
    LOG,
    ORIGIN,
    Demo,
    demo_bundle,
    lines,
    members,
    rewrite,
    trusted_keys_json,
)
from tests.helpers import signer_for
from verify.bundle import read_bundle
from verify.checks import Report, verify_bundle
from verify.keys import TrustedKeys

KEYS = TrustedKeys.from_json(trusted_keys_json())


def check(bundle: bytes, keys: TrustedKeys = KEYS, trusted: str | None = None) -> Report:
    return verify_bundle(read_bundle(bundle), keys, trusted_checkpoint=trusted)


def first(report: Report) -> tuple[int | None, str]:
    assert report.first_break is not None, "expected a break"
    return report.first_break.seq, report.first_break.check


def edit_lines(
    name: str, change: Callable[[list[bytes]], object]
) -> Callable[[dict[str, bytes]], None]:
    def apply(files: dict[str, bytes]) -> None:
        current = lines(files[name])
        change(current)
        files[name] = b"".join(current)

    return apply


def record_json(files: dict[str, bytes], seq: int) -> dict[str, object]:
    return json.loads(lines(files["records.jsonl"])[seq - 1])


@pytest.fixture
async def bundle(tmp_path: Path) -> bytes:
    return await demo_bundle(tmp_path / "log")


async def test_clean_bundle_verifies(bundle: bytes) -> None:
    report = check(bundle)
    assert report.ok, report.findings
    assert (report.records, report.chain_verified_through, report.receipts) == (5, 5, 5)
    assert report.latest is not None
    assert (report.checkpoints, report.latest.size, report.unanchored) == (2, 5, 0)


async def test_records_after_the_last_checkpoint_are_counted_as_unanchored(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(3)
    await demo.checkpoint()
    await demo.append(2)
    report = check(await demo.export())
    assert report.ok
    assert report.unanchored == 2


async def test_empty_bundle_verifies(tmp_path: Path) -> None:
    report = check(await Demo(tmp_path).export())
    assert report.ok
    assert (report.records, report.latest) == (0, None)


# --- tamper suite: each one names the right seq and reason ----------------------------------


async def test_edited_amount_is_named_at_its_seq(bundle: bytes) -> None:
    # Demo beat 4: one amount changed in the database.
    def edit(files: dict[str, bytes]) -> None:
        record = record_json(files, 2)
        record["body"]["amount"] = 480  # type: ignore[index]
        rows = lines(files["records.jsonl"])
        rows[1] = canonical_bytes(record) + b"\n"
        files["records.jsonl"] = b"".join(rows)

    report = check(rewrite(bundle, edit))
    assert first(report) == (2, "bad_producer_signature")
    assert report.first_break is not None
    assert report.first_break.record_id == record_json(members(bundle), 2)["record_id"]
    checks = {(f.seq, f.check) for f in report.findings}
    assert (2, "receipt_mismatch") in checks
    assert sum(f.check == "checkpoint_mismatch" for f in report.findings) == 2
    assert report.chain_verified_through == 1


async def test_non_canonical_line_is_malformed(bundle: bytes) -> None:
    tampered = rewrite(bundle, edit_lines("records.jsonl", lambda r: r.__setitem__(1, b" " + r[1])))
    assert first(check(tampered)) == (2, "malformed")


async def test_deleted_record(bundle: bytes) -> None:
    tampered = rewrite(bundle, edit_lines("records.jsonl", lambda r: r.pop(2)))
    assert first(check(tampered)) == (3, "seq_mismatch")


async def test_inserted_record(bundle: bytes) -> None:
    tampered = rewrite(bundle, edit_lines("records.jsonl", lambda r: r.insert(2, r[1])))
    assert first(check(tampered)) == (3, "seq_mismatch")


async def test_reordered_records(bundle: bytes) -> None:
    def swap(rows: list[bytes]) -> None:
        rows[1], rows[2] = rows[2], rows[1]

    assert first(check(rewrite(bundle, edit_lines("records.jsonl", swap)))) == (2, "seq_mismatch")


async def test_truncated_export_is_caught_by_the_checkpoint(bundle: bytes) -> None:
    def drop_last(files: dict[str, bytes]) -> None:
        for name in ("records.jsonl", "receipts.jsonl"):
            files[name] = b"".join(lines(files[name])[:-1])

    report = check(rewrite(bundle, drop_last))
    assert first(report) == (5, "checkpoint_beyond_records")


async def test_records_dropped_but_receipts_kept(bundle: bytes) -> None:
    report = check(rewrite(bundle, edit_lines("records.jsonl", lambda r: r.pop())))
    assert first(report) == (5, "receipt_without_record")


async def test_missing_receipt(bundle: bytes) -> None:
    report = check(rewrite(bundle, edit_lines("receipts.jsonl", lambda r: r.pop(1))))
    assert first(report) == (2, "missing_receipt")


async def test_receipt_signed_by_another_key(bundle: bytes, tmp_path: Path) -> None:
    rogue_bundle = await demo_bundle(tmp_path / "rogue")
    rogue_keys = TrustedKeys.from_json(trusted_keys_json(log=signer_for("rogue")))
    report = check(rogue_bundle, rogue_keys)
    assert first(report) == (1, "bad_receipt_signature")


async def test_unknown_producer_key(bundle: bytes) -> None:
    keys = TrustedKeys.from_json(trusted_keys_json(producers={"gate-1": signer_for("rogue")}))
    assert first(check(bundle, keys)) == (1, "bad_producer_signature")


async def test_producer_key_registered_to_someone_else(bundle: bytes) -> None:
    keys = TrustedKeys.from_json(trusted_keys_json(producers={"gate-2": GATE}))
    assert first(check(bundle, keys)) == (1, "bad_producer_signature")


async def test_manifest_claims_another_stream(bundle: bytes) -> None:
    def other_stream(files: dict[str, bytes]) -> None:
        manifest = json.loads(files["manifest.json"])
        manifest["stream"] = "other-bank/decisions"
        manifest["origin"] = "precept-ledger.local/other-bank/decisions"
        files["manifest.json"] = canonical_bytes(manifest)

    report = check(rewrite(bundle, other_stream))
    assert first(report) == (1, "wrong_stream")


async def test_forged_checkpoint(bundle: bytes) -> None:
    def forge(files: dict[str, bytes]) -> None:
        cp = Checkpoint(ORIGIN, 4, leaf_hash(b"forged"))
        files["checkpoints/00000000000000000004.note"] = sign_checkpoint(
            cp, signer_for("rogue")
        ).encode()

    report = check(rewrite(bundle, forge))
    assert [f.check for f in report.findings] == ["bad_checkpoint"]
    assert report.checkpoints == 2  # the forged one is not counted


async def test_two_signed_roots_for_one_size(bundle: bytes) -> None:
    # Split view: the log key signed two different trees of size 3.
    def fork(files: dict[str, bytes]) -> None:
        cp = Checkpoint(ORIGIN, 3, leaf_hash(b"other history"))
        files["checkpoints/00000000000000000003-b.note"] = sign_checkpoint(cp, LOG).encode()

    report = check(rewrite(bundle, fork))
    checks = [f.check for f in report.findings]
    assert "checkpoint_conflict" in checks
    assert "checkpoint_mismatch" in checks


async def test_rogue_operator_rewrite_is_caught_by_a_held_checkpoint(tmp_path: Path) -> None:
    # Demo beat 5 without witnesses yet: the operator rebuilds a different history and
    # re-signs everything with the real log key. The bundle is internally consistent; only
    # a checkpoint the auditor held from before shows the rewrite.
    honest = Demo(tmp_path / "honest")
    await honest.append(3)
    await honest.checkpoint()
    held = (await honest.sink.latest()) or ""
    rogue = Demo(tmp_path / "rogue")
    await rogue.append(1)
    await rogue.append(4)  # new record ids from seq 1, and two more records
    await rogue.checkpoint()
    rewritten = await rogue.export()
    assert check(rewritten).ok
    report = check(rewritten, trusted=held)
    assert [f.check for f in report.findings] == ["rewritten_since_trusted"]


async def test_held_checkpoint_beyond_the_export_means_truncation(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(4)
    await demo.checkpoint()
    held = (await demo.sink.latest()) or ""

    def keep_two(files: dict[str, bytes]) -> None:
        for name in ("records.jsonl", "receipts.jsonl"):
            files[name] = b"".join(lines(files[name])[:2])
        del files["checkpoints/00000000000000000004.note"]

    shorter = rewrite(await demo.export(), keep_two)
    report = check(shorter, trusted=held)
    assert first(report) == (3, "truncated_since_trusted")


async def test_trusted_checkpoint_that_does_not_verify_is_a_usage_error(bundle: bytes) -> None:
    bad = sign_checkpoint(Checkpoint(ORIGIN, 1, leaf_hash(b"x")), signer_for("rogue"))
    with pytest.raises(VerificationError) as err:
        check(bundle, trusted=bad)
    assert err.value.code == "verify.bad_trusted_checkpoint"


async def test_held_checkpoint_matching_the_export_passes(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(3)
    await demo.checkpoint()
    held = (await demo.sink.latest()) or ""
    await demo.append(2)
    await demo.checkpoint()
    assert check(await demo.export(), trusted=held).ok


async def test_malformed_and_duplicate_receipts(bundle: bytes) -> None:
    def edit(rows: list[bytes]) -> None:
        rows.append(rows[0])
        rows.append(b"{}\n")

    report = check(rewrite(bundle, edit_lines("receipts.jsonl", edit)))
    assert {f.check for f in report.findings} == {"duplicate_receipt", "malformed_receipt"}
    assert first(report) == (1, "duplicate_receipt")


async def test_same_checkpoint_twice_is_not_a_conflict(bundle: bytes) -> None:
    def copy(files: dict[str, bytes]) -> None:
        files["checkpoints/copy.note"] = files["checkpoints/00000000000000000005.note"]

    assert check(rewrite(bundle, copy)).ok


def test_duplicate_record_id_with_a_valid_chain() -> None:
    # The ledger refuses this at append time; an export built elsewhere could still hold it.
    from ledger.chain import link
    from tests.helpers import build_chain, clock
    from verify.bundle import Bundle

    chained = build_chain(2, GATE, clock())
    again = link(chained[-1].head, chained[1].record.submission)
    lines_ = tuple(item.record.canonical() for item in [*chained, again])
    report = verify_bundle(Bundle("harbor-federal/decisions", ORIGIN, lines_, (), ()), KEYS)
    record_findings = [(f.seq, f.check) for f in report.findings if f.check != "missing_receipt"]
    assert record_findings == [(3, "duplicate_record_id")]


def test_log_signed_receipt_with_a_wrong_chain_hash() -> None:
    # Only the log key can produce this; it still must not pass.
    import dataclasses

    from ledger.receipts import issue_receipt
    from tests.helpers import build_chain, clock
    from verify.bundle import Bundle

    chained = build_chain(2, GATE, clock())
    wrong = dataclasses.replace(chained[1], chain_hash=bytes(32))
    receipts = [
        issue_receipt(LOG, item, "2026-10-29T21:14:03.140Z") for item in [chained[0], wrong]
    ]
    bundle_ = Bundle(
        "harbor-federal/decisions",
        ORIGIN,
        tuple(item.record.canonical() for item in chained),
        tuple(canonical_bytes(r.to_json()) for r in receipts),
        (),
    )
    report = verify_bundle(bundle_, KEYS)
    assert [(f.seq, f.check, f.detail) for f in report.findings] == [
        (2, "receipt_mismatch", "chain hash differs from the receipt")
    ]
