"""Ed25519 keys, producer signatures and the key ring the ledger trusts.

Private keys never leave a ``Signer``: no accessor returns raw private bytes, and ``repr``
shows only the key id (skill §4).
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from common.errors import FormatError, SignatureError
from ledger.format import (
    ALG_ED25519,
    Producer,
    Submission,
    b64encode,
    key_id_for,
    record_signing_payload,
)


class Signer:
    """Holds one Ed25519 private key and signs with it."""

    __slots__ = ("_key", "key_id", "public_key_raw")

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        self.public_key_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.key_id = key_id_for(self.public_key_raw)

    @classmethod
    def generate(cls) -> "Signer":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> "Signer":
        if len(seed) != 32:
            raise FormatError("Ed25519 seed must be 32 bytes", code="format.bad_key")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_pem_file(cls, path: Path) -> "Signer":
        """Load an unencrypted PKCS#8 PEM Ed25519 key from ``KEYS_DIR``."""
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise SignatureError(f"cannot load key {path.name}", code="signature.bad_key") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SignatureError(f"{path.name} is not an Ed25519 key", code="signature.bad_key")
        return cls(key)

    def private_pem(self) -> bytes:
        """PKCS#8 PEM for writing a key file. Callers must write it only under ``KEYS_DIR``."""
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def __repr__(self) -> str:
        return f"Signer(key_id={self.key_id!r})"


def sign_submission(
    signer: Signer,
    *,
    producer_id: str,
    stream: str,
    record_id: str,
    ts: str,
    record_type: str,
    body: dict[str, Any],
) -> Submission:
    """Build and sign a submission. The signature excludes ``seq`` and ``prev_hash`` (D9)."""
    unsigned = {
        "v": 1,
        "stream": stream,
        "record_id": record_id,
        "ts": ts,
        "type": record_type,
        "producer": {"id": producer_id, "alg": ALG_ED25519, "key_id": signer.key_id},
        "body": body,
    }
    sig = b64encode(signer.sign(record_signing_payload(unsigned)))
    producer = Producer(id=producer_id, key_id=signer.key_id, sig=sig)
    return Submission(
        stream=stream, record_id=record_id, ts=ts, type=record_type, producer=producer, body=body
    )


class KeyRing:
    """Public keys the caller trusts, each bound to one owner (a producer id, or ``log``)."""

    def __init__(self, entries: Iterable[tuple[str, bytes]] = ()) -> None:
        self._keys: dict[str, tuple[str, Ed25519PublicKey]] = {}
        for owner, raw in entries:
            key_id = key_id_for(raw)
            known = self._keys.get(key_id)
            if known is not None and known[0] != owner:
                raise SignatureError(
                    f"key {key_id} already belongs to {known[0]}", code="signature.key_conflict"
                )
            self._keys[key_id] = (owner, Ed25519PublicKey.from_public_bytes(raw))

    def __len__(self) -> int:
        return len(self._keys)

    def owner(self, key_id: str) -> str:
        return self._lookup(key_id)[0]

    def verify(
        self, key_id: str, message: bytes, signature: bytes, *, owner: str | None = None
    ) -> None:
        """Raise ``SignatureError`` unless ``signature`` over ``message`` verifies."""
        known_owner, public_key = self._lookup(key_id)
        if owner is not None and owner != known_owner:
            raise SignatureError(
                f"key {key_id} belongs to {known_owner}, not {owner}",
                code="signature.producer_mismatch",
            )
        try:
            public_key.verify(signature, message)
        except InvalidSignature as exc:
            raise SignatureError(f"signature does not verify under {key_id}") from exc

    def verify_submission(self, submission: Submission) -> None:
        producer = submission.producer
        self.verify(
            producer.key_id, submission.signing_bytes(), producer.sig_bytes(), owner=producer.id
        )

    def _lookup(self, key_id: str) -> tuple[str, Ed25519PublicKey]:
        entry = self._keys.get(key_id)
        if entry is None:
            raise SignatureError(f"unknown key {key_id}", code="signature.unknown_key")
        return entry
