"""Delegation credentials: who authorized this agent, for what, until when (D88).

A principal (the person or team an agent acts for) signs a credential with Ed25519:

    {credential_id, principal_id, agent_id, action_classes, max_amount, allowed_payees,
     not_before, expires_at}

The signature covers ``"PL-delegation-v1\\x00" || JCS(credential)``; the credential's hash is
``"sha256:" || hex(SHA-256("PL-credential-v1\\x00" || JCS(credential)))``; a revocation is signed
over ``"PL-revocation-v1\\x00" || JCS(revocation)``. Each has its own context (skill §4), and all
JSON goes through ``ledger.format.canonical_bytes``.

Gate checks the credential an agent presents before Cedar runs, in this order, and denies on
the first failure: missing, malformed, bad signature, not issued, revoked, wrong agent, not yet
valid, expired, out of scope (action class, amount, currency, payee). Issued and revoked state is
never stored apart from the ledger: ``Delegations`` is a fold over Gate's own
``gate.delegation.issue`` and ``gate.delegation.revoke`` records, rebuilt at startup from the
ledger and updated only after a new record's receipt verifies.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from common.clock import parse_ts
from common.errors import FormatError, GateError, SignatureError
from common.ids import is_ulid
from ledger.format import ALG_ED25519, SIG_SIZE, b64decode, b64encode, canonical_bytes, check_ijson
from ledger.signing import KeyRing, Signer

ISSUE = "gate.delegation.issue"
REVOKE = "gate.delegation.revoke"
CREDENTIAL_CONTEXT = b"PL-delegation-v1\x00"
HASH_CONTEXT = b"PL-credential-v1\x00"
REVOCATION_CONTEXT = b"PL-revocation-v1\x00"
ACTION_CLASSES = frozenset({"value.transfer", "comm.external", "data.read"})
MAX_LIFETIME = timedelta(days=90)
MAX_PAYEES = 200
CURRENCY = "USD"  # max_amount is in US dollars; any other currency is out of scope
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DOLLARS = re.compile(r"^\d{1,12}\.\d{2}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
CREDENTIAL_FIELDS = frozenset({
    "credential_id", "principal_id", "agent_id", "action_classes", "max_amount",
    "allowed_payees", "not_before", "expires_at",
})  # fmt: skip
REVOCATION_FIELDS = frozenset({"credential_id", "credential_hash", "principal_id", "revoked_at"})


def _bad(message: str, code: str = "delegation.malformed") -> GateError:
    return GateError(message, code=code)


def _object(value: object, keys: frozenset[str], what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _bad(f"{what} must have exactly the fields {sorted(keys)}")
    return value


def _name(value: object, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _bad(f"{field} must be lowercase letters, digits and . _ -")
    return value


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise _bad(f"{field} must be an RFC 3339 UTC time")
    try:
        return parse_ts(value)
    except FormatError as exc:
        raise _bad(f"{field} must be an RFC 3339 UTC time with ms and Z") from exc


def _names(value: object, field: str, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_PAYEES:
        raise _bad(f"{field} must be a list")
    names = tuple(_name(v, field) for v in value)
    if len(set(names)) != len(names):
        raise _bad(f"{field} has a repeated entry")
    if allowed is not None and (not names or not set(names) <= allowed):
        raise _bad(f"{field} must name at least one of {sorted(allowed)}")
    return names


@dataclass(frozen=True)
class Credential:
    credential_id: str
    principal_id: str
    agent_id: str
    action_classes: tuple[str, ...]
    max_amount: str  # US dollars as an exact decimal string, e.g. "5000.00"
    allowed_payees: tuple[str, ...]
    not_before: str
    expires_at: str

    @property
    def max_amount_cents(self) -> int:
        return int(Decimal(self.max_amount) * 100)

    def to_json(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "principal_id": self.principal_id,
            "agent_id": self.agent_id,
            "action_classes": list(self.action_classes),
            "max_amount": self.max_amount,
            "allowed_payees": list(self.allowed_payees),
            "not_before": self.not_before,
            "expires_at": self.expires_at,
        }


def parse_credential(value: object) -> Credential:
    """A credential from untrusted JSON, or ``GateError(delegation.malformed)``."""
    data = _object(value, CREDENTIAL_FIELDS, "credential")
    if not isinstance(data["credential_id"], str) or not is_ulid(data["credential_id"]):
        raise _bad("credential_id must be a ULID")
    amount = data["max_amount"]
    if not isinstance(amount, str) or not _DOLLARS.fullmatch(amount):
        raise _bad('max_amount must be dollars as a string with two places, e.g. "5000.00"')
    start, end = _time(data["not_before"], "not_before"), _time(data["expires_at"], "expires_at")
    if not start < end <= start + MAX_LIFETIME:
        raise _bad(f"expires_at must be after not_before, within {MAX_LIFETIME.days} days")
    return Credential(
        credential_id=data["credential_id"],
        principal_id=_name(data["principal_id"], "principal_id"),
        agent_id=_name(data["agent_id"], "agent_id"),
        action_classes=_names(data["action_classes"], "action_classes", ACTION_CLASSES),
        max_amount=amount,
        allowed_payees=_names(data["allowed_payees"], "allowed_payees"),
        not_before=data["not_before"],
        expires_at=data["expires_at"],
    )


def credential_payload(credential: Credential) -> bytes:
    """The exact bytes a principal signs."""
    return CREDENTIAL_CONTEXT + canonical_bytes(credential.to_json())


def credential_hash(credential: Credential) -> str:
    """What decision records cite: one hash per credential, whatever its signature."""
    digest = hashlib.sha256(HASH_CONTEXT + canonical_bytes(credential.to_json())).hexdigest()
    return "sha256:" + digest


@dataclass(frozen=True)
class Signed:
    """A signed envelope: ``{v, alg, key_id, sig, <kind>: {...}}``. ``alg`` is kept as given
    so that a verifier refuses an unknown one rather than guessing (skill §4)."""

    alg: str
    key_id: str
    sig: str

    def _envelope(self, kind: str, inner: dict[str, Any]) -> dict[str, Any]:
        return {"v": 1, "alg": self.alg, "key_id": self.key_id, "sig": self.sig, kind: inner}


@dataclass(frozen=True)
class SignedCredential(Signed):
    credential: Credential

    @property
    def hash(self) -> str:
        return credential_hash(self.credential)

    def to_json(self) -> dict[str, Any]:
        return self._envelope("credential", self.credential.to_json())


def _envelope(value: object, kind: str) -> tuple[Mapping[str, Any], tuple[str, str, str]]:
    data = _object(value, frozenset({"v", "alg", "key_id", "sig", kind}), f"signed {kind}")
    fields = (data["alg"], data["key_id"], data["sig"])
    if data["v"] != 1 or not all(isinstance(f, str) for f in fields):
        raise _bad(f"signed {kind} must be version 1 with alg, key_id and sig strings")
    try:
        check_ijson(dict(data), "$")
    except FormatError as exc:
        raise _bad(f"signed {kind} is not I-JSON") from exc
    return data, (str(fields[0]), str(fields[1]), str(fields[2]))


def parse_signed_credential(value: object) -> SignedCredential:
    """Shape only; ``verify_signed_credential`` checks the algorithm and the signature."""
    data, (alg, key_id, sig) = _envelope(value, "credential")
    return SignedCredential(alg, key_id, sig, parse_credential(data["credential"]))


def _verify(
    alg: str, key_id: str, sig: str, payload: bytes, owner: str, principals: KeyRing
) -> None:
    if alg != ALG_ED25519:
        raise _bad(f"unknown signature algorithm {alg!r}", "delegation.bad_signature")
    try:
        raw = b64decode(sig, size=SIG_SIZE, field="sig")
        principals.verify(key_id, payload, raw, owner=owner)
    except (FormatError, SignatureError) as exc:
        raise _bad(f"not signed by {owner}'s trusted key", "delegation.bad_signature") from exc


def verify_signed_credential(signed: SignedCredential, principals: KeyRing) -> None:
    """Raise ``GateError(delegation.bad_signature)`` unless the named principal signed it."""
    credential = signed.credential
    _verify(signed.alg, signed.key_id, signed.sig, credential_payload(credential),
            credential.principal_id, principals)  # fmt: skip


def sign_credential(signer: Signer, credential: Credential) -> SignedCredential:
    sig = b64encode(signer.sign(credential_payload(credential)))
    return SignedCredential(ALG_ED25519, signer.key_id, sig, credential)


@dataclass(frozen=True)
class Revocation:
    credential_id: str
    credential_hash: str
    principal_id: str
    revoked_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "credential_hash": self.credential_hash,
            "principal_id": self.principal_id,
            "revoked_at": self.revoked_at,
        }


@dataclass(frozen=True)
class SignedRevocation(Signed):
    revocation: Revocation

    def to_json(self) -> dict[str, Any]:
        return self._envelope("revocation", self.revocation.to_json())


def _revocation_payload(revocation: Revocation) -> bytes:
    return REVOCATION_CONTEXT + canonical_bytes(revocation.to_json())


def parse_signed_revocation(value: object) -> SignedRevocation:
    data, (alg, key_id, sig) = _envelope(value, "revocation")
    inner = _object(data["revocation"], REVOCATION_FIELDS, "revocation")
    if not isinstance(inner["credential_id"], str) or not is_ulid(inner["credential_id"]):
        raise _bad("credential_id must be a ULID")
    digest = inner["credential_hash"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise _bad("credential_hash must be sha256:<hex>")
    _time(inner["revoked_at"], "revoked_at")
    principal = _name(inner["principal_id"], "principal_id")
    revocation = Revocation(inner["credential_id"], digest, principal, inner["revoked_at"])
    return SignedRevocation(alg, key_id, sig, revocation)


def verify_revocation(signed: SignedRevocation, principals: KeyRing) -> None:
    r = signed.revocation
    _verify(signed.alg, signed.key_id, signed.sig, _revocation_payload(r), r.principal_id,
            principals)  # fmt: skip


def sign_revocation(signer: Signer, revocation: Revocation) -> SignedRevocation:
    sig = b64encode(signer.sign(_revocation_payload(revocation)))
    return SignedRevocation(ALG_ED25519, signer.key_id, sig, revocation)


def check_scope(
    credential: Credential, *, agent: str, action: str, facts: Mapping[str, Any], now: datetime
) -> str | None:
    """The first way this call falls outside the credential, as a code, or None."""
    if agent != credential.agent_id:
        return "delegation.wrong_agent"
    if now < parse_ts(credential.not_before):
        return "delegation.not_yet_valid"
    if now >= parse_ts(credential.expires_at):
        return "delegation.expired"
    if action not in credential.action_classes:
        return "delegation.out_of_scope"
    if action == "value.transfer":
        amount = facts.get("amount_cents")
        if not isinstance(amount, int) or isinstance(amount, bool):
            return "delegation.out_of_scope"  # an amount Gate cannot read is not within a limit
        if facts.get("currency") != CURRENCY or amount > credential.max_amount_cents:
            return "delegation.out_of_scope"
        if facts.get("payee_id") not in credential.allowed_payees:
            return "delegation.out_of_scope"
    return None


@dataclass(frozen=True)
class Checked:
    code: str | None  # None: the call is within an issued, unrevoked, valid credential
    credential: SignedCredential | None

    def record(self) -> dict[str, Any]:
        """What the decision record says about the delegation."""
        c = None if self.credential is None else self.credential.credential
        return {
            "credential_id": None if c is None else c.credential_id,
            "credential_hash": None if self.credential is None else self.credential.hash,
            "principal_id": None if c is None else c.principal_id,
            "result": self.code or "ok",
        }


class Delegations:
    """Issued and revoked credentials, folded from Gate's own ledger records, and the check."""

    def __init__(self, principals: KeyRing) -> None:
        self.principals = principals
        self._issued: dict[str, SignedCredential] = {}  # by credential hash
        self._by_id: dict[str, str] = {}  # credential id -> hash
        self._revoked: dict[str, str] = {}  # hash -> the revoke record id

    def find(self, credential_id: str) -> tuple[SignedCredential, bool] | None:
        """The issued credential with this id, and whether it is revoked."""
        digest = self._by_id.get(credential_id)
        if digest is None:
            return None
        return self._issued[digest], digest in self._revoked

    def is_issued(self, signed: SignedCredential) -> bool:
        return signed.hash in self._issued or signed.credential.credential_id in self._by_id

    def is_revoked(self, digest: str) -> bool:
        return digest in self._revoked

    def apply(self, record_type: str, body: Mapping[str, Any], record_id: str) -> None:
        """Fold one verified Gate record into the state."""
        if record_type == ISSUE:
            signed = parse_signed_credential(body["credential"])
            self._issued[signed.hash] = signed
            self._by_id[signed.credential.credential_id] = signed.hash
        elif record_type == REVOKE:
            self._revoked[str(body["credential_hash"])] = record_id

    def check(
        self, presented: object, *, agent: str, action: str, facts: Mapping[str, Any],
        now: datetime,
    ) -> Checked:  # fmt: skip
        if presented is None:
            return Checked("delegation.missing", None)
        try:
            signed = parse_signed_credential(presented)
        except GateError as exc:
            return Checked(exc.code, None)
        try:
            verify_signed_credential(signed, self.principals)
        except GateError as exc:
            return Checked(exc.code, signed)
        if signed.hash not in self._issued:
            return Checked("delegation.not_issued", signed)
        if signed.hash in self._revoked:
            return Checked("delegation.revoked", signed)
        code = check_scope(signed.credential, agent=agent, action=action, facts=facts, now=now)
        return Checked(code, signed)
