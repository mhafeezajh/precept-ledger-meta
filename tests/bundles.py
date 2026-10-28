"""Build real export bundles for tests, and rewrite them like an attacker would."""

import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ledger.checkpointer import Checkpointer, DirectorySink, origin_for
from ledger.export import export_bundle
from ledger.format import b64encode
from ledger.service import Ledger
from ledger.signing import KeyRing, Signer
from ledger.store import MemoryStore
from merkle.log import LogTree
from tests.helpers import STREAM, clock, make_submission, signer_for

GATE, LOG = signer_for("gate-1"), signer_for("log")
ORIGIN = origin_for("precept-ledger.local", STREAM)


@dataclass
class Demo:
    """A ledger with records, receipts and checkpoints, in memory."""

    directory: Path
    store: MemoryStore = field(default_factory=MemoryStore)

    def __post_init__(self) -> None:
        self.ledger = Ledger(self.store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
        self.sink = DirectorySink(self.directory)
        self.checkpointer = Checkpointer(LogTree(self.store.tree(STREAM)), LOG, ORIGIN, [self.sink])
        self._clock = clock()
        self._amount = 0

    async def append(self, count: int) -> None:
        for _ in range(count):
            self._amount += 1000
            body = {"action": "value.transfer", "amount": self._amount}
            await self.ledger.append(STREAM, make_submission(GATE, self._clock, body=body))

    async def checkpoint(self) -> None:
        await self.checkpointer.publish()

    async def export(self) -> bytes:
        return await export_bundle(self.store, self.sink, STREAM, ORIGIN)


async def demo_bundle(directory: Path, *, sizes: tuple[int, ...] = (3, 5)) -> bytes:
    """Records 1..max(sizes), with a checkpoint at each size."""
    demo = Demo(directory)
    done = 0
    for size in sizes:
        await demo.append(size - done)
        await demo.checkpoint()
        done = size
    return await demo.export()


def members(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:") as tar:
        out = {}
        for info in tar.getmembers():
            extracted = tar.extractfile(info)
            assert extracted is not None
            out[info.name] = extracted.read()
        return out


def pack(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def rewrite(bundle: bytes, edit: Callable[[dict[str, bytes]], None]) -> bytes:
    """Unpack, let ``edit`` change the files, fix up manifest counts, repack."""
    files = members(bundle)
    edit(files)
    manifest = json.loads(files["manifest.json"])
    manifest["counts"] = {
        "records": len(lines(files["records.jsonl"])),
        "receipts": len(lines(files["receipts.jsonl"])),
        "checkpoints": sum(name.startswith("checkpoints/") for name in files),
    }
    if manifest["v"] >= 2:
        manifest["counts"]["anchors"] = sum(name.startswith("anchors/") for name in files)
    files["manifest.json"] = canonical(manifest)
    return pack(files)


def canonical(value: object) -> bytes:
    from ledger.format import canonical_bytes

    return canonical_bytes(value)


def trusted_keys_json(*, log: Signer = LOG, producers: dict[str, Signer] | None = None) -> bytes:
    producers = producers if producers is not None else {"gate-1": GATE}
    return json.dumps(
        {
            "v": 1,
            "producers": {n: [b64encode(s.public_key_raw)] for n, s in producers.items()},
            "log": [b64encode(log.public_key_raw)],
        }
    ).encode()
