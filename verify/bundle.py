"""Read an export bundle (v1, see ``ledger/export.py``) as hostile input.

Nothing is extracted to disk. Only regular files with expected names are read, each at most
``MAX_MEMBER_BYTES``; links, directories, duplicates and compression are refused. Structure is
checked here; every cryptographic check is in ``verify.checks``.
"""

import io
import re
import tarfile
from dataclasses import dataclass

from common.errors import FormatError, VerificationError
from ledger.format import parse_json_strict

BUNDLE_FORMAT = "precept-ledger-export"  # must match ledger.export (round-trip tested)
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 100_000
REQUIRED = ("manifest.json", "records.jsonl", "receipts.jsonl")
_CHECKPOINT_NAME = re.compile(r"^checkpoints/[A-Za-z0-9._-]+\.note$")
_COUNT_KEYS = {"records", "receipts", "checkpoints"}


@dataclass(frozen=True)
class Bundle:
    stream: str
    origin: str
    records: tuple[bytes, ...]  # one canonical JSON record per entry, newline removed
    receipts: tuple[bytes, ...]
    checkpoints: tuple[str, ...]  # signed notes, ordered by member name


def _error(detail: str, code: str) -> VerificationError:
    return VerificationError(detail, code=f"bundle.{code}")


def _read_tar(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_BUNDLE_BYTES:
        raise _error("bundle is too large", "too_large")
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for count, info in enumerate(tar, start=1):
                name = info.name
                if count > MAX_MEMBERS:
                    raise _error("too many members", "too_large")
                if not info.isfile() or not (name in REQUIRED or _CHECKPOINT_NAME.match(name)):
                    raise _error(f"unexpected member {name!r}", "bad_member")
                if name in files:
                    raise _error(f"member {name!r} appears twice", "duplicate")
                if info.size > MAX_MEMBER_BYTES:
                    raise _error(f"member {name!r} is too large", "too_large")
                extracted = tar.extractfile(info)
                files[name] = extracted.read() if extracted is not None else b""
    except tarfile.TarError as exc:
        raise _error(f"not an uncompressed tar: {exc}", "not_tar") from exc
    return files


def _lines(data: bytes, name: str) -> tuple[bytes, ...]:
    if not data:
        return ()
    if not data.endswith(b"\n"):
        raise _error(f"{name} must end with a newline", "bad_lines")
    return tuple(data[:-1].split(b"\n"))


def _manifest(data: bytes) -> tuple[str, str, dict[str, int]]:
    try:
        value = parse_json_strict(data)
    except FormatError as exc:
        raise _error(f"manifest: {exc}", "bad_manifest") from exc
    if not isinstance(value, dict) or set(value) != {"format", "v", "stream", "origin", "counts"}:
        raise _error("manifest fields are wrong", "bad_manifest")
    stream, origin, counts = value["stream"], value["origin"], value["counts"]
    valid = (
        value["format"] == BUNDLE_FORMAT
        and value["v"] == BUNDLE_VERSION
        and isinstance(stream, str)
        and isinstance(origin, str)
        and origin.endswith("/" + stream)  # one tree per stream (D28)
        and isinstance(counts, dict)
        and set(counts) == _COUNT_KEYS
        and all(isinstance(n, int) and n >= 0 for n in counts.values())
    )
    if not valid:
        raise _error("manifest is not a v1 export manifest", "bad_manifest")
    return stream, origin, counts


def read_bundle(data: bytes) -> Bundle:
    files = _read_tar(data)
    for name in REQUIRED:
        if name not in files:
            raise _error(f"{name} is missing", "missing")
    stream, origin, counts = _manifest(files["manifest.json"])
    try:
        checkpoints = tuple(
            files[name].decode("utf-8") for name in sorted(files) if name.startswith("checkpoints/")
        )
    except UnicodeDecodeError as exc:
        raise _error("checkpoint note is not UTF-8", "bad_lines") from exc
    bundle = Bundle(
        stream,
        origin,
        _lines(files["records.jsonl"], "records.jsonl"),
        _lines(files["receipts.jsonl"], "receipts.jsonl"),
        checkpoints,
    )
    found = {
        "records": len(bundle.records),
        "receipts": len(bundle.receipts),
        "checkpoints": len(checkpoints),
    }
    if found != counts:
        raise _error(f"manifest counts {counts} but bundle has {found}", "count_mismatch")
    return bundle
