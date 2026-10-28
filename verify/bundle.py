"""Read an export bundle (v1 or v2, see ``ledger/export.py``) as hostile input.

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
BUNDLE_VERSIONS = (1, 2)  # v2 adds anchors/ (D47); v1 bundles stay readable
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 100_000
REQUIRED = ("manifest.json", "records.jsonl", "receipts.jsonl")
_CHECKPOINT_NAME = re.compile(r"^checkpoints/[A-Za-z0-9._-]+\.note$")
_ANCHOR_NAME = re.compile(r"^anchors/([0-9]{20})-(rfc3161|ots)-([0-9]{2,6})\.(tsr|ots)$")
_COUNT_KEYS = {1: {"records", "receipts", "checkpoints"},
               2: {"records", "receipts", "checkpoints", "anchors"}}  # fmt: skip
MAX_ANCHOR_BYTES = 64 * 1024


@dataclass(frozen=True)
class BundleAnchor:
    size: int  # from the member name: a label the verifier checks against the token
    kind: str  # rfc3161 | ots
    data: bytes


@dataclass(frozen=True)
class Bundle:
    stream: str
    origin: str
    records: tuple[bytes, ...]  # one canonical JSON record per entry, newline removed
    receipts: tuple[bytes, ...]
    checkpoints: tuple[str, ...]  # signed notes, ordered by member name
    anchors: tuple[BundleAnchor, ...] = ()


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
                known = name in REQUIRED or _CHECKPOINT_NAME.match(name) or _ANCHOR_NAME.match(name)
                if not info.isfile() or not known:
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


def _manifest(data: bytes) -> tuple[int, str, str, dict[str, int]]:
    try:
        value = parse_json_strict(data)
    except FormatError as exc:
        raise _error(f"manifest: {exc}", "bad_manifest") from exc
    if not isinstance(value, dict) or set(value) != {"format", "v", "stream", "origin", "counts"}:
        raise _error("manifest fields are wrong", "bad_manifest")
    stream, origin, counts = value["stream"], value["origin"], value["counts"]
    version = value["v"]
    valid = (
        value["format"] == BUNDLE_FORMAT
        and version in BUNDLE_VERSIONS
        and isinstance(stream, str)
        and isinstance(origin, str)
        and origin.endswith("/" + stream)  # one tree per stream (D28)
        and isinstance(counts, dict)
        and set(counts) == _COUNT_KEYS[version]
        and all(isinstance(n, int) and n >= 0 for n in counts.values())
    )
    if not valid:
        raise _error("manifest is not a v1 or v2 export manifest", "bad_manifest")
    return version, stream, origin, counts


def _anchors(files: dict[str, bytes], version: int) -> tuple[BundleAnchor, ...]:
    anchors = []
    for name in sorted(files):
        match = _ANCHOR_NAME.match(name)
        if match is None:
            continue
        if version < 2:
            raise _error("v1 bundles carry no anchors", "bad_member")
        if (match[2] == "rfc3161") != (match[4] == "tsr") or len(files[name]) > MAX_ANCHOR_BYTES:
            raise _error(f"anchor member {name!r} is malformed", "bad_member")
        anchors.append(BundleAnchor(int(match[1]), match[2], files[name]))
    return tuple(anchors)


def read_bundle(data: bytes) -> Bundle:
    files = _read_tar(data)
    for name in REQUIRED:
        if name not in files:
            raise _error(f"{name} is missing", "missing")
    version, stream, origin, counts = _manifest(files["manifest.json"])
    anchors = _anchors(files, version)
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
        anchors,
    )
    found = {
        "records": len(bundle.records),
        "receipts": len(bundle.receipts),
        "checkpoints": len(checkpoints),
    }
    if version >= 2:
        found["anchors"] = len(anchors)
    if found != counts:
        raise _error(f"manifest counts {counts} but bundle has {found}", "count_mismatch")
    return bundle
