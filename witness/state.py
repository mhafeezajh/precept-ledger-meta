"""A witness's own storage: the last checkpoint it cosigned per log, and refused checkpoints.

Never the ledger's database (ARCHITECTURE "Data model"): a witness that shared the operator's
storage could be rolled back by the operator. ``FileWitnessState`` writes one JSON file per
log, replaced atomically, plus one file per refused checkpoint as evidence.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from common.errors import WitnessError

STATE_VERSION = 1


@dataclass(frozen=True)
class Cosigned:
    """The latest checkpoint a witness cosigned for one log, with its own cosignature line."""

    origin: str
    size: int
    root: bytes
    note: str  # the log-signed note as received
    cosignature: str  # this witness's signature line
    timestamp: int


class WitnessState(Protocol):
    async def latest(self, origin: str) -> Cosigned | None: ...

    async def save(self, cosigned: Cosigned) -> None:
        """Persist before the cosignature is returned (C2SP tlog-witness)."""
        ...

    async def record_refusal(self, origin: str, note: str, code: str) -> None: ...


@dataclass
class MemoryWitnessState:
    saved: dict[str, Cosigned] = field(default_factory=dict)
    refusals: list[tuple[str, str, str]] = field(default_factory=list)

    async def latest(self, origin: str) -> Cosigned | None:
        return self.saved.get(origin)

    async def save(self, cosigned: Cosigned) -> None:
        self.saved[cosigned.origin] = cosigned

    async def record_refusal(self, origin: str, note: str, code: str) -> None:
        self.refusals.append((origin, note, code))


def _invalid(detail: str) -> WitnessError:
    return WitnessError(f"witness state: {detail}", code="witness.state_invalid")


def _file_id(origin: str) -> str:
    # Origins contain '/', so files are named by a hash; the origin is inside the file.
    return hashlib.sha256(origin.encode()).hexdigest()[:32]


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)  # the rename itself must survive a crash
    finally:
        os.close(dir_fd)


def _decode(origin: str, data: bytes) -> Cosigned:
    try:
        value = json.loads(data)
        if value.pop("v") != STATE_VERSION:
            raise _invalid("unsupported version")
        value["root"] = base64.b64decode(value["root"], validate=True)
        cosigned = Cosigned(**value)
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise _invalid(f"unreadable ({type(exc).__name__})") from exc
    if cosigned.origin != origin:
        raise _invalid("file is for another log")
    return cosigned


class FileWitnessState:
    def __init__(self, directory: Path) -> None:
        self._state = directory / "state"
        self._refused = directory / "refused"

    def _path(self, origin: str) -> Path:
        return self._state / f"{_file_id(origin)}.json"

    async def latest(self, origin: str) -> Cosigned | None:
        return await asyncio.to_thread(self._latest, origin)

    def _latest(self, origin: str) -> Cosigned | None:
        path = self._path(origin)
        if not path.exists():
            return None
        return _decode(origin, path.read_bytes())

    async def save(self, cosigned: Cosigned) -> None:
        value = {**vars(cosigned), "v": STATE_VERSION}
        value["root"] = base64.b64encode(cosigned.root).decode()
        data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
        await asyncio.to_thread(_write_atomically, self._path(cosigned.origin), data)

    async def record_refusal(self, origin: str, note: str, code: str) -> None:
        # A random part, so evidence from before a restart is never overwritten.
        name = f"{_file_id(origin)}-{secrets.token_hex(8)}-{code.replace('.', '_')}.note"
        await asyncio.to_thread(_write_atomically, self._refused / name, note.encode())
