"""Export bundle v1: everything ``pl-verify`` needs about one stream, in one plain tar (D31).

Members, in this order:

- ``manifest.json``: ``{format, v, stream, origin, counts}``, canonical JSON.
- ``records.jsonl``: every record as stored (D21), one canonical JSON line each, seq order.
- ``receipts.jsonl``: every receipt as stored, same encoding.
- ``checkpoints/<20-digit size>.note``: every signed checkpoint.

No keys travel in the bundle: the verifier gets trusted keys from the auditor, never from the
party being audited. The tar is deterministic (fixed order, zero times and owners).
"""

import io
import tarfile
from typing import Any

from ledger.checkpoint import peek_checkpoint
from ledger.checkpointer import CheckpointSink
from ledger.format import canonical_bytes
from ledger.store import Store

EXPORT_FORMAT = "precept-ledger-export"
EXPORT_VERSION = 1


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    # A row an admin edited into non-I-JSON cannot be canonicalized: the export fails loudly
    # rather than hiding the row (R4).
    return b"".join(canonical_bytes(row) + b"\n" for row in rows)


def _tar(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o444, 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def export_bundle(store: Store, sink: CheckpointSink, stream: str, origin: str) -> bytes:
    records = await store.read_raw(stream)
    receipts = await store.read_receipts(stream)
    notes = await sink.notes()
    checkpoints = [
        (f"checkpoints/{peek_checkpoint(note).size:020d}.note", note.encode("utf-8"))
        for note in notes
    ]
    manifest = {
        "format": EXPORT_FORMAT,
        "v": EXPORT_VERSION,
        "stream": stream,
        "origin": origin,
        "counts": {
            "records": len(records),
            "receipts": len(receipts),
            "checkpoints": len(checkpoints),
        },
    }
    return _tar(
        [
            ("manifest.json", canonical_bytes(manifest)),
            ("records.jsonl", _jsonl(records)),
            ("receipts.jsonl", _jsonl(receipts)),
            *checkpoints,
        ]
    )
