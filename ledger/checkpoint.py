"""Checkpoints (C2SP tlog-checkpoint) and signed notes (C2SP signed-note), Ed25519 only.

Pure, like ``ledger.format``: the verifier and the witnesses import it (D28). A checkpoint
is three or more lines of text (origin, tree size, base64 root hash, optional extensions)
signed as a note. The signature key name is the origin, so one log key yields a distinct
key hash per stream.
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from common.errors import FormatError, SignatureError
from merkle.hashing import HASH_SIZE

NOTE_ALG_ED25519 = 0x01  # signed-note signature type for Ed25519
KEY_HASH_SIZE = 4
MAX_NOTE_BYTES = 64 * 1024
MAX_SIGNATURES = 32
MAX_TREE_SIZE = 2**63 - 1

_NAME = re.compile(r"^[^\s+\x00-\x1f\x7f]+$")
_SIG_LINE = re.compile(r"^— ([^ ]+) ([A-Za-z0-9+/]+=*)$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")  # Unicode Cc except newline


class NoteSigner(Protocol):
    """What signing needs from a key. ``ledger.signing.Signer`` fits; no import needed here."""

    public_key_raw: bytes

    def sign(self, message: bytes) -> bytes: ...


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str, code: str) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FormatError("not base64", code=code) from exc
    if _b64(data) != text:  # D19: one encoding per byte string
        raise FormatError("non-canonical base64", code=code)
    return data


def _check_text(text: str, code: str) -> None:
    """Note text: non-empty, newline-terminated, no blank lines, no other control characters.

    Forbidding NUL is also what keeps receipt and record signing inputs, which contain NUL,
    from ever being valid note text under the same log key (D28).
    """
    if not text.endswith("\n") or "" in text[:-1].split("\n") or _CONTROL.search(text):
        raise FormatError("note text is malformed", code=code)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FormatError("note text is not valid Unicode", code=code) from exc


# --- keys -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class NoteKey:
    """A trusted Ed25519 public key under a signed-note key name."""

    name: str
    public_key_raw: bytes

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise FormatError(f"bad key name {self.name!r}", code="note.bad_key")
        if len(self.public_key_raw) != 32:
            raise FormatError("Ed25519 public keys are 32 bytes", code="note.bad_key")

    @property
    def key_hash(self) -> bytes:
        material = self.name.encode() + b"\n" + bytes([NOTE_ALG_ED25519]) + self.public_key_raw
        return hashlib.sha256(material).digest()[:KEY_HASH_SIZE]

    def verifier(self) -> str:
        """``<name>+<hex key hash>+<base64(0x01 ‖ key)>``, the signed-note verifier key."""
        encoded = _b64(bytes([NOTE_ALG_ED25519]) + self.public_key_raw)
        return f"{self.name}+{self.key_hash.hex()}+{encoded}"

    @classmethod
    def from_verifier(cls, text: str) -> "NoteKey":
        parts = text.split("+", 2)  # base64 may contain '+', names and hashes may not
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{8}", parts[1]):
            raise FormatError("malformed verifier key", code="note.bad_key")
        raw = _unb64(parts[2], "note.bad_key")
        if len(raw) != 33 or raw[0] != NOTE_ALG_ED25519:
            raise FormatError("verifier key is not Ed25519", code="note.bad_key")
        key = cls(parts[0], raw[1:])
        if key.key_hash.hex() != parts[1]:
            raise FormatError("verifier key hash does not match", code="note.bad_key")
        return key


# --- signed notes ---------------------------------------------------------------------------


def _verifies(key: NoteKey, signature: bytes, text: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(key.public_key_raw).verify(signature, text)
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_note(text: str, signer: NoteSigner, name: str) -> str:
    _check_text(text, "note.malformed")
    key = NoteKey(name, signer.public_key_raw)
    signature = signer.sign(text.encode("utf-8"))
    return f"{text}\n— {name} {_b64(key.key_hash + signature)}\n"


def _split_note(note: str) -> tuple[str, list[tuple[str, bytes]]]:
    if len(note.encode("utf-8", "surrogatepass")) > MAX_NOTE_BYTES:
        raise FormatError("note is too large", code="note.malformed")
    cut = note.rfind("\n\n")
    if cut < 0 or not note.endswith("\n") or cut + 2 == len(note):
        raise FormatError("note has no signature block", code="note.malformed")
    text, lines = note[: cut + 1], note[cut + 2 : -1].split("\n")
    _check_text(text, "note.malformed")
    if len(lines) > MAX_SIGNATURES:
        raise FormatError("too many signatures", code="note.malformed")
    signatures = []
    for line in lines:
        match = _SIG_LINE.match(line)
        if not match or not _NAME.match(match[1]):
            raise FormatError("malformed signature line", code="note.malformed")
        raw = _unb64(match[2], "note.malformed")
        if len(raw) <= KEY_HASH_SIZE:
            raise FormatError("signature too short", code="note.malformed")
        signatures.append((match[1], raw))
    return text, signatures


def open_note(note: str, keys: Iterable[NoteKey]) -> tuple[str, frozenset[str]]:
    """Return the note text and the names of trusted keys whose signatures verified.

    Signatures from unknown keys are ignored (signed-note). A bad signature from a trusted key
    rejects the whole note, and so does a note no trusted key signed (R4).
    """
    text, signatures = _split_note(note)
    trusted = {(key.name, key.key_hash): key for key in keys}
    verified: set[str] = set()
    for name, raw in signatures:
        key = trusted.get((name, raw[:KEY_HASH_SIZE]))
        if key is None:
            continue
        if name in verified:
            raise FormatError(f"two signatures from {name}", code="note.duplicate_signature")
        if not _verifies(key, raw[KEY_HASH_SIZE:], text.encode("utf-8")):
            raise SignatureError(
                f"signature from {name} does not verify", code="note.bad_signature"
            )
        verified.add(name)
    if not verified:
        raise SignatureError("no trusted key signed this note", code="note.unverified")
    return text, frozenset(verified)


# --- checkpoints ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkpoint:
    origin: str
    size: int
    root: bytes
    extensions: tuple[str, ...] = ()

    def body(self) -> str:
        """The checkpoint text that gets signed. Refuses to render an invalid checkpoint."""
        lines = [self.origin, str(self.size), _b64(self.root), *self.extensions]
        text = "\n".join(lines) + "\n"
        valid = (
            bool(self.origin)
            and 0 <= self.size <= MAX_TREE_SIZE
            and len(self.root) == HASH_SIZE
            and len(lines) == text.count("\n")  # no field smuggles in its own newline
        )
        if not valid:
            raise FormatError("invalid checkpoint", code="checkpoint.malformed")
        _check_text(text, "checkpoint.malformed")
        return text


def parse_checkpoint(body: str) -> Checkpoint:
    _check_text(body, "checkpoint.malformed")
    lines = body[:-1].split("\n")
    if len(lines) < 3 or not _DECIMAL.match(lines[1]) or int(lines[1]) > MAX_TREE_SIZE:
        raise FormatError("malformed checkpoint", code="checkpoint.malformed")
    root = _unb64(lines[2], "checkpoint.malformed")
    if len(root) != HASH_SIZE:
        raise FormatError("root hash must be 32 bytes", code="checkpoint.malformed")
    return Checkpoint(lines[0], int(lines[1]), root, tuple(lines[3:]))


def sign_checkpoint(checkpoint: Checkpoint, signer: NoteSigner) -> str:
    return sign_note(checkpoint.body(), signer, checkpoint.origin)


def open_checkpoint(note: str, log_key: NoteKey) -> Checkpoint:
    """Verify a checkpoint note under the log key and parse it; its origin must be the key name."""
    text, _ = open_note(note, [log_key])
    checkpoint = parse_checkpoint(text)
    if checkpoint.origin != log_key.name:
        raise SignatureError(
            f"checkpoint origin {checkpoint.origin!r} is not {log_key.name!r}",
            code="checkpoint.wrong_origin",
        )
    return checkpoint
