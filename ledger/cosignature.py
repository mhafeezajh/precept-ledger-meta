"""Witness cosignatures (C2SP tlog-cosignature, ``cosignature/v1`` with Ed25519).

Pure, like ``ledger.checkpoint``: the witnesses sign with it and the verifier checks with it
(D38). A cosignature is one more signed-note signature line on the checkpoint note:

- signed message: ``"cosignature/v1\\n" "time " <decimal seconds> "\\n"`` then the checkpoint
  body (origin, size, base64 root, extension lines), so the time is covered by the signature;
- key hash: ``SHA-256(name ‖ 0x0A ‖ 0x04 ‖ public key)[:4]``, type 0x04 per C2SP signed-note,
  so a witness key's cosignatures never look like plain note signatures (type 0x01);
- line value: ``base64(key hash ‖ 8-byte big-endian time ‖ 64-byte Ed25519 signature)``.
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from common.errors import FormatError, SignatureError
from ledger.canonical import HASH_SIZE, b64decode
from ledger.checkpoint import (
    KEY_HASH_SIZE,
    Checkpoint,
    NoteKey,
    NoteSigner,
    parse_checkpoint,
    split_note,
)

COSIGNATURE_ALG = 0x04  # signed-note type: timestamped Ed25519 checkpoint cosignature
COSIGNATURE_HEADER = "cosignature/v1"
TIMESTAMP_SIZE = 8
SIGNATURE_SIZE = 64
MAX_TIMESTAMP = 2**64 - 1

_LINE = re.compile(r"^— ([^ ]+) ([A-Za-z0-9+/]+=*)\n$")


@dataclass(frozen=True)
class CosignerKey:
    """A witness's Ed25519 public key under its name, as a cosigner (type 0x04)."""

    name: str
    public_key_raw: bytes

    def __post_init__(self) -> None:
        NoteKey(self.name, self.public_key_raw)  # same name and key rules as any note key

    @property
    def key_hash(self) -> bytes:
        material = self.name.encode() + b"\n" + bytes([COSIGNATURE_ALG]) + self.public_key_raw
        return hashlib.sha256(material).digest()[:KEY_HASH_SIZE]

    def verifier(self) -> str:
        """``<name>+<hex key hash>+<base64(0x04 ‖ key)>``."""
        encoded = base64.b64encode(bytes([COSIGNATURE_ALG]) + self.public_key_raw).decode()
        return f"{self.name}+{self.key_hash.hex()}+{encoded}"

    @classmethod
    def from_verifier(cls, text: str) -> "CosignerKey":
        parts = text.split("+", 2)
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{8}", parts[1]):
            raise FormatError("malformed cosigner key", code="cosignature.bad_key")
        try:
            raw = base64.b64decode(parts[2], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FormatError("cosigner key is not base64", code="cosignature.bad_key") from exc
        if (
            len(raw) != 33
            or raw[0] != COSIGNATURE_ALG
            or base64.b64encode(raw).decode() != parts[2]
        ):
            raise FormatError("not an Ed25519 cosigner key", code="cosignature.bad_key")
        key = cls(parts[0], raw[1:])
        if key.key_hash.hex() != parts[1]:
            raise FormatError("cosigner key hash does not match", code="cosignature.bad_key")
        return key


def _check_timestamp(timestamp: int) -> None:
    if not 0 <= timestamp <= MAX_TIMESTAMP:
        raise FormatError("cosignature time must fit in 64 bits", code="cosignature.malformed")


def cosignature_message(checkpoint: Checkpoint, timestamp: int) -> bytes:
    _check_timestamp(timestamp)
    return f"{COSIGNATURE_HEADER}\ntime {timestamp}\n{checkpoint.body()}".encode()


def cosign(checkpoint: Checkpoint, signer: NoteSigner, name: str, timestamp: int) -> str:
    """One signature line, newline included, ready to append to the checkpoint note."""
    key = CosignerKey(name, signer.public_key_raw)
    signature = signer.sign(cosignature_message(checkpoint, timestamp))
    value = key.key_hash + timestamp.to_bytes(TIMESTAMP_SIZE, "big") + signature
    return f"— {name} {base64.b64encode(value).decode()}\n"


def add_cosignatures(note: str, lines: Sequence[str]) -> str:
    """Append signature lines to a note, skipping lines it already has. Shape-checked only."""
    out = note
    for line in lines:
        if not _LINE.match(line):
            raise FormatError("not a signature line", code="cosignature.malformed")
        if line not in out.split("\n\n", 1)[-1].splitlines(keepends=True):
            out += line
    split_note(out)  # the result must still be a well-formed note
    return out


def _verifies(key: CosignerKey, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(key.public_key_raw).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def open_cosignatures(note: str, keys: Iterable[CosignerKey]) -> dict[str, int]:
    """``{witness name: time}`` for every trusted cosigner whose cosignature verifies.

    Lines from other keys are ignored (signed-note). A trusted witness's line that is
    malformed, repeated or does not verify rejects the whole note (R4). This does not check
    the log's signature: callers open the checkpoint with ``open_checkpoint`` too.
    """
    text, signatures = split_note(note)
    checkpoint = parse_checkpoint(text)
    trusted = {(key.name, key.key_hash): key for key in keys}
    found: dict[str, int] = {}
    for name, raw in signatures:
        key = trusted.get((name, raw[:KEY_HASH_SIZE]))
        if key is None:
            continue
        if len(raw) != KEY_HASH_SIZE + TIMESTAMP_SIZE + SIGNATURE_SIZE:
            raise FormatError(f"cosignature from {name} is malformed", code="cosignature.malformed")
        if name in found:
            raise FormatError(f"two cosignatures from {name}", code="cosignature.duplicate")
        timestamp = int.from_bytes(raw[KEY_HASH_SIZE : KEY_HASH_SIZE + TIMESTAMP_SIZE], "big")
        message = cosignature_message(checkpoint, timestamp)
        if not _verifies(key, raw[KEY_HASH_SIZE + TIMESTAMP_SIZE :], message):
            raise SignatureError(
                f"cosignature from {name} does not verify", code="cosignature.bad_signature"
            )
        found[name] = timestamp
    return found


# --- add-checkpoint request body (C2SP tlog-witness) ----------------------------------------

MAX_PROOF_HASHES = 63
MAX_REQUEST_BYTES = 70 * 1024  # a 64 KiB note plus the old size and a full proof
_OLD = re.compile(r"^old (0|[1-9][0-9]*)$")


def format_add_checkpoint(old_size: int, proof: Sequence[bytes], note: str) -> bytes:
    """``old <size>``, one base64 hash per line, a blank line, then the checkpoint note."""
    lines = [f"old {old_size}", *(base64.b64encode(h).decode() for h in proof), ""]
    return ("\n".join(lines) + "\n" + note).encode("utf-8")


def _bad_request(detail: str) -> FormatError:
    return FormatError(f"add-checkpoint request: {detail}", code="witness.bad_request")


def parse_add_checkpoint(body: bytes) -> tuple[int, list[bytes], str]:
    """Shape only: the note's signatures and the proof are checked by the witness."""
    if len(body) > MAX_REQUEST_BYTES:
        raise _bad_request("too large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _bad_request("not UTF-8") from exc
    head, sep, note = text.partition("\n\n")
    lines = head.split("\n")
    old = _OLD.match(lines[0])
    if not sep or not note or old is None:
        raise _bad_request("expected 'old <size>', proof lines, a blank line and a note")
    if len(lines) - 1 > MAX_PROOF_HASHES:
        raise _bad_request(f"more than {MAX_PROOF_HASHES} proof hashes")
    proof = []
    for line in lines[1:]:
        try:
            proof.append(b64decode(line, size=HASH_SIZE, field="proof"))
        except FormatError as exc:
            raise _bad_request(str(exc.args[0])) from exc
    return int(old[1]), proof, note
