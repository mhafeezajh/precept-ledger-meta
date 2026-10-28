"""Export bundle v1: everything ``pl-verify`` needs about one stream, in one plain tar (D31).

Members, in this order:

- ``manifest.json``: ``{format, v, stream, origin, counts}``, canonical JSON.
- ``records.jsonl``: every record as stored (D21), one canonical JSON line each, seq order.
- ``receipts.jsonl``: every receipt as stored, same encoding.
- ``checkpoints/<20-digit size>.note``: every signed checkpoint, with the witness
  cosignature lines stored for it appended (D39). Readers that do not know the witness keys
  ignore those lines (signed-note).
- ``anchors/<20-digit size>-<rfc3161|ots>-<nn>.<tsr|ots>``: every stored anchor of that
  checkpoint: a DER RFC 3161 token, or an OpenTimestamps timestamp (D47). New in v2.

No keys travel in the bundle: the verifier gets trusted keys from the auditor, never from the
party being audited. The tar is deterministic (fixed order, zero times and owners).
"""

import io
import tarfile
from typing import Any

from ledger.anchoring import AnchorStore
from ledger.checkpoint import peek_checkpoint
from ledger.checkpointer import CheckpointSink
from ledger.cosignature import add_cosignatures
from ledger.format import canonical_bytes
from ledger.store import Store
from ledger.witnessing import CosignatureStore

EXPORT_FORMAT = "precept-ledger-export"
EXPORT_VERSION = 2


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


async def _with_cosignatures(note: str, cosignatures: CosignatureStore | None) -> str:
    if cosignatures is None:
        return note
    lines = await cosignatures.lines(peek_checkpoint(note).size)
    # Exported as stored: the verifier, not the exporter, decides which lines verify.
    return add_cosignatures(note, [lines[name] for name in sorted(lines)])


async def export_bundle(
    store: Store,
    sink: CheckpointSink,
    stream: str,
    origin: str,
    cosignatures: CosignatureStore | None = None,
    anchors: AnchorStore | None = None,
) -> bytes:
    records = await store.read_raw(stream)
    receipts = await store.read_receipts(stream)
    checkpoints = []
    for note in await sink.notes():
        full = await _with_cosignatures(note, cosignatures)
        name = f"checkpoints/{peek_checkpoint(note).size:020d}.note"
        checkpoints.append((name, full.encode("utf-8")))
    anchor_files = []
    for n, row in enumerate([] if anchors is None else await anchors.rows()):
        extension = "tsr" if row.kind == "rfc3161" else "ots"
        anchor_files.append((f"anchors/{row.size:020d}-{row.kind}-{n:02d}.{extension}", row.token))
    manifest = {
        "format": EXPORT_FORMAT,
        "v": EXPORT_VERSION,
        "stream": stream,
        "origin": origin,
        "counts": {
            "records": len(records),
            "receipts": len(receipts),
            "checkpoints": len(checkpoints),
            "anchors": len(anchor_files),
        },
    }
    return _tar(
        [
            ("manifest.json", canonical_bytes(manifest)),
            ("records.jsonl", _jsonl(records)),
            ("receipts.jsonl", _jsonl(receipts)),
            *checkpoints,
            *anchor_files,
        ]
    )
