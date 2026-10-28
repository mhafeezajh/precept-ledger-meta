"""A witness's own signing key and the log keys it trusts, loaded from files it controls.

The witness does not use ``ledger.signing`` (skill §2: witnesses import only the pure ledger
format code), so it has its own small loader. The private key file must not be readable by
group or others; the key is never logged, printed or returned.
"""

import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from common.errors import ConfigError, LedgerError
from ledger.checkpoint import NoteKey


class WitnessSigner:
    """Signs cosignatures with the witness's Ed25519 key (fits ``NoteSigner``)."""

    __slots__ = ("_key", "public_key_raw")

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self.public_key_raw = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def __repr__(self) -> str:
        return "WitnessSigner(<private key hidden>)"


def load_signer(path: Path) -> WitnessSigner:
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ConfigError(
                f"{path.name} is readable by group or others; chmod 600 it", code="config.keys"
            )
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"cannot load witness key {path.name}", code="config.keys") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ConfigError(f"{path.name} is not an Ed25519 key", code="config.keys")
    return WitnessSigner(key)


def load_log_keys(path: Path) -> list[NoteKey]:
    """One signed-note verifier key per line (``origin+hash+base64``); ``#`` starts a comment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        keys = [
            NoteKey.from_verifier(line.strip())
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeDecodeError, LedgerError) as exc:
        raise ConfigError(f"cannot read log keys from {path.name}", code="config.keys") from exc
    if not keys:
        raise ConfigError(f"{path.name} lists no log keys", code="config.keys")
    return keys
