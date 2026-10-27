"""Keys pl-verify trusts, read from a file the auditor controls; never from the bundle.

File format (JSON)::

    {"v": 1, "producers": {"gate-1": ["<base64 Ed25519 key>", ...]}, "log": ["<base64>", ...]}

Lists allow rotated keys. A key may belong to one producer only.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from common.errors import FormatError, VerificationError
from ledger.checkpoint import NoteKey
from ledger.format import b64decode, key_id_for, parse_json_strict

KEYS_FILE_VERSION = 1


def _bad(detail: str) -> VerificationError:
    return VerificationError(f"trusted keys file: {detail}", code="verify.bad_keys")


def _decode_keys(value: object, field: str) -> list[bytes]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise _bad(f"{field} must be a non-empty list of base64 keys")
    try:
        return [b64decode(item, size=32, field=field) for item in value]
    except FormatError as exc:
        raise _bad(str(exc)) from exc


def _verifies(raw: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


@dataclass(frozen=True)
class TrustedKeys:
    producers: Mapping[str, tuple[str, bytes]]  # key_id -> (producer id, raw public key)
    log_keys: tuple[bytes, ...]

    @classmethod
    def from_json(cls, data: bytes) -> "TrustedKeys":
        try:
            value = parse_json_strict(data)
        except FormatError as exc:
            raise _bad(str(exc)) from exc
        if not isinstance(value, dict) or set(value) != {"v", "producers", "log"}:
            raise _bad("expected exactly the fields v, producers, log")
        if value["v"] != KEYS_FILE_VERSION or not isinstance(value["producers"], dict):
            raise _bad("unsupported version or producers is not an object")
        producers: dict[str, tuple[str, bytes]] = {}
        for owner, listed in value["producers"].items():
            for raw in _decode_keys(listed, f"producers.{owner}"):
                key_id = key_id_for(raw)
                if key_id in producers and producers[key_id][0] != owner:
                    raise _bad(f"key {key_id} is listed for two producers")
                producers[key_id] = (owner, raw)
        return cls(producers, tuple(_decode_keys(value["log"], "log")))

    def producer_owners(self) -> set[str]:
        return {owner for owner, _ in self.producers.values()}

    def check_producer(
        self, producer_id: str, key_id: str, message: bytes, signature: bytes
    ) -> str | None:
        """``None`` if ``producer_id``'s trusted key ``key_id`` signed ``message``; else why not."""
        entry = self.producers.get(key_id)
        if entry is None:
            return f"key {key_id} is not a trusted producer key"
        if entry[0] != producer_id:
            return f"key {key_id} belongs to {entry[0]}, not {producer_id}"
        return None if _verifies(entry[1], signature, message) else "signature does not verify"

    def check_log(self, key_id: str, message: bytes, signature: bytes) -> str | None:
        for raw in self.log_keys:
            if key_id_for(raw) == key_id:
                return None if _verifies(raw, signature, message) else "signature does not verify"
        return f"key {key_id} is not a trusted log key"

    def log_note_keys(self, origin: str) -> list[NoteKey]:
        return [NoteKey(origin, raw) for raw in self.log_keys]
