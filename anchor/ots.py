"""OpenTimestamps proofs: read a calendar's timestamp for our digest, and upgrade it.

Pure; the verifier imports it (D46). A timestamp is a tree of operations applied to a
message, starting from our digest (``anchor_digest``), ending in attestations:

- ``0xf0 <varbytes>`` append, ``0xf1 <varbytes>`` prepend, ``0x08`` SHA-256. Other operations
  (SHA-1, RIPEMD-160, Keccak, reverse, hexlify) are refused: the Bitcoin calendars do not use
  them on these paths, and an operation we cannot run is never skipped (R4).
- ``0xff`` before an item marks a fork: more items follow at the same message.
- ``0x00 <8-byte tag> <varbytes payload>`` is an attestation. Pending (a calendar URL) and
  Bitcoin block header (a block height) are understood; others are reported as unknown.

``varuint`` is 7 bits per byte, least significant first; ``varbytes`` is a varuint length
then that many bytes. Reading this proves only that the proof *commits to our digest*. That a
Bitcoin block's merkle root equals the attested message needs the block header, which the
offline verifier does not have: it reports the height and message for checking elsewhere.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from common.errors import AnchorError

PENDING_TAG = bytes.fromhex("83dfe30d2ef90c8e")
BITCOIN_TAG = bytes.fromhex("0588960d73d71901")
MAX_PROOF_BYTES = 64 * 1024
MAX_DEPTH = 256
MAX_ARGUMENT = 4096  # also the longest message an operation may produce (OpenTimestamps)
_URI = re.compile(r"^https://[A-Za-z0-9.\-_/:]{1,1000}$")


@dataclass(frozen=True)
class Attestation:
    kind: str  # "pending", "bitcoin" or "unknown"
    commitment: bytes  # the message the attestation is about
    calendar: str | None  # pending: the calendar to ask for an upgrade
    height: int | None  # bitcoin: the block height


def _bad(detail: str) -> AnchorError:
    return AnchorError(f"OpenTimestamps proof: {detail}", code="anchor.bad_ots")


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise _bad("truncated")
        self.pos += 1
        return self.data[self.pos - 1]

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise _bad("truncated")
        self.pos += count
        return self.data[self.pos - count : self.pos]

    def varuint(self) -> int:
        value = shift = 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise _bad("varuint too large")

    def varbytes(self, limit: int = MAX_ARGUMENT) -> bytes:
        length = self.varuint()
        if length > limit:
            raise _bad("field too long")
        return self.take(length)


def _attestation(reader: _Reader, message: bytes) -> Attestation:
    tag, payload = reader.take(8), _Reader(reader.varbytes())
    if tag == PENDING_TAG:
        uri = payload.varbytes(1000)
        try:
            text = uri.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _bad("calendar URL is not ASCII") from exc
        if not _URI.match(text):
            raise _bad("calendar URL is not an https URL")
        found = Attestation("pending", message, text, None)
    elif tag == BITCOIN_TAG:
        found = Attestation("bitcoin", message, None, payload.varuint())
    else:
        return Attestation("unknown", message, None, None)
    if payload.pos != len(payload.data):
        raise _bad("attestation payload has trailing bytes")
    return found


_UNARY: dict[int, Callable[[bytes], bytes]] = {0x08: lambda m: hashlib.sha256(m).digest()}


def _apply(reader: _Reader, tag: int, message: bytes) -> bytes:
    if tag in (0xF0, 0xF1):
        argument = reader.varbytes()
        result = message + argument if tag == 0xF0 else argument + message
    elif tag in _UNARY:
        result = _UNARY[tag](message)
    else:
        raise _bad(f"operation {tag:#04x} is not supported")
    if len(result) > MAX_ARGUMENT:
        raise _bad("message too long")
    return result


@dataclass
class _Node:
    """Where one timestamp node's bytes are, for upgrading a pending-only node in place."""

    message: bytes
    start: int
    end: int
    attestations: list[Attestation]


def _walk(reader: _Reader, message: bytes, depth: int, out: list[Attestation],
          nodes: list[_Node]) -> None:  # fmt: skip
    if depth > MAX_DEPTH:
        raise _bad("too deep")
    start, here = reader.pos, len(out)

    def item(tag: int) -> None:
        if tag == 0x00:
            out.append(_attestation(reader, message))
        else:
            _walk(reader, _apply(reader, tag, message), depth + 1, out, nodes)

    tag = reader.byte()
    while tag == 0xFF:
        item(reader.byte())
        tag = reader.byte()
    item(tag)
    nodes.append(_Node(message, start, reader.pos, out[here:]))


def _parse(data: bytes, start: bytes) -> tuple[list[Attestation], list[_Node]]:
    if len(data) > MAX_PROOF_BYTES:
        raise _bad("too large")
    if not 0 < len(start) <= MAX_ARGUMENT:
        raise _bad("bad starting message")
    reader, attestations, nodes = _Reader(data), list[Attestation](), list[_Node]()
    _walk(reader, start, 0, attestations, nodes)
    if reader.pos != len(data):
        raise _bad("trailing bytes")
    return attestations, nodes


def read_timestamp(data: bytes, digest: bytes) -> list[Attestation]:
    """Every attestation in a timestamp that starts from ``digest``, in proof order."""
    if len(digest) != 32:
        raise _bad("digest must be 32 bytes")
    return _parse(data, digest)[0]


def upgrade_timestamp(data: bytes, digest: bytes, commitment: bytes, upgrade: bytes) -> bytes:
    """Replace the pending attestation at ``commitment`` with the calendar's ``upgrade``
    (a timestamp starting from ``commitment``), and check the result still parses."""
    _parse(upgrade, commitment)  # the upgrade must itself be a timestamp of the commitment
    _, nodes = _parse(data, digest)
    for node in nodes:
        only_pending = [a.kind for a in node.attestations] == ["pending"]
        exact = data[node.start] == 0x00  # the node is that one attestation and nothing else
        if node.message == commitment and only_pending and exact:
            upgraded = data[: node.start] + upgrade + data[node.end :]
            read_timestamp(upgraded, digest)
            return upgraded
    raise _bad("no pending attestation at that commitment")
