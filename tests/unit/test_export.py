"""Export bundle: deterministic tar of stored records, receipts and checkpoints."""

import io
import json
import tarfile
from pathlib import Path

from ledger.checkpoint import NoteKey, open_checkpoint
from ledger.format import canonical_bytes, parse_canonical
from tests.bundles import LOG, ORIGIN, Demo, demo_bundle, members
from tests.helpers import STREAM


async def test_bundle_layout_and_manifest(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    assert list(files) == [
        "manifest.json",
        "records.jsonl",
        "receipts.jsonl",
        "checkpoints/00000000000000000003.note",
        "checkpoints/00000000000000000005.note",
    ]
    manifest = parse_canonical(files["manifest.json"])
    assert manifest == {
        "format": "precept-ledger-export",
        "v": 2,
        "stream": STREAM,
        "origin": ORIGIN,
        "counts": {"records": 5, "receipts": 5, "checkpoints": 2, "anchors": 0},
    }


async def test_lines_are_canonical_and_in_seq_order(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(4)
    await demo.checkpoint()
    files = members(await demo.export())
    records = files["records.jsonl"].splitlines(keepends=True)
    history = demo.store.history_for_tests(STREAM)
    assert records == [item.record.canonical() + b"\n" for item in history]
    receipts = [json.loads(line) for line in files["receipts.jsonl"].splitlines()]
    assert [r["seq"] for r in receipts] == [1, 2, 3, 4]
    for line in files["receipts.jsonl"].splitlines():
        assert canonical_bytes(json.loads(line)) == line
    note = files["checkpoints/00000000000000000004.note"].decode()
    assert open_checkpoint(note, NoteKey(ORIGIN, LOG.public_key_raw)).size == 4


async def test_export_is_deterministic_and_plain(tmp_path: Path) -> None:
    demo = Demo(tmp_path)
    await demo.append(2)
    await demo.checkpoint()
    first, second = await demo.export(), await demo.export()
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as tar:
        for info in tar.getmembers():
            assert info.isfile()
            assert (info.mtime, info.uid, info.gid, info.mode) == (0, 0, 0, 0o444)


async def test_export_shows_rows_as_stored(tmp_path: Path) -> None:
    # D21: the export carries the edited row, so the verifier sees what the database holds.
    demo = Demo(tmp_path)
    await demo.append(3)
    demo.store.tamper_for_tests(STREAM, 2, lambda row: row["body"].update(amount=480))
    records = members(await demo.export())["records.jsonl"].splitlines()
    assert json.loads(records[1])["body"]["amount"] == 480


async def test_empty_stream_exports(tmp_path: Path) -> None:
    files = members(await Demo(tmp_path).export())
    assert files["records.jsonl"] == b""
    assert files["receipts.jsonl"] == b""
    assert json.loads(files["manifest.json"])["counts"] == {
        "records": 0,
        "receipts": 0,
        "checkpoints": 0,
        "anchors": 0,
    }
