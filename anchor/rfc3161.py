"""RFC 3161 timestamp tokens: build requests, and verify tokens offline.

Pure (no network, no files): the verifier imports it (D44). What gets timestamped is
``anchor_digest(checkpoint)``, the SHA-256 of the C2SP checkpoint body, so anyone can
recompute it from the signed note.

``verify_token`` accepts a token only if every one of these holds, else ``AnchorError``:

1. It is CMS SignedData wrapping a TSTInfo (id-ct-TSTInfo), with exactly one signer.
2. The TSTInfo's message imprint is SHA-256 of exactly our digest.
3. Signed attributes are present: content type is TSTInfo, message digest matches the
   encapsulated TSTInfo, and an ESS signing-certificate (v1 or v2) attribute names the
   signer's certificate.
4. The signature over the signed attributes verifies under the signer certificate
   (RSA PKCS#1 v1.5, ECDSA or Ed25519).
5. The signer certificate has the extended key usage timeStamping, marked critical.
6. The certificate chain reaches a root the caller trusts, every certificate valid at the
   token's own ``genTime`` and every issuer a CA. Revocation is not checked (known gap).
"""

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from asn1crypto import cms, tsp
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from common.errors import AnchorError
from ledger.checkpoint import Checkpoint

MAX_TOKEN_BYTES = 64 * 1024
MAX_CHAIN = 6
_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}


def anchor_digest(checkpoint: Checkpoint) -> bytes:
    """What RFC 3161 and OpenTimestamps commit to: SHA-256 of the checkpoint body."""
    return hashlib.sha256(checkpoint.body().encode()).digest()


@dataclass(frozen=True)
class TimestampInfo:
    gen_time: datetime
    tsa: str  # signer certificate common name (or full subject), for display only
    serial: int
    policy: str


def _bad(detail: str, code: str = "anchor.bad_timestamp") -> AnchorError:
    return AnchorError(f"timestamp token: {detail}", code=code)


def timestamp_request(digest: bytes, nonce: int) -> bytes:
    """DER TimeStampReq for a SHA-256 digest, asking for the TSA's certificates."""
    if len(digest) != 32:
        raise _bad("digest must be 32 bytes")
    request = tsp.TimeStampReq(
        {
            "version": 1,
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": digest,
            },
            "nonce": nonce,
            "cert_req": True,
        }
    )
    return bytes(request.dump())


def token_from_response(response: bytes, digest: bytes, nonce: int) -> bytes:
    """The DER token from a TimeStampResp, if granted and it answers our request."""
    if len(response) > MAX_TOKEN_BYTES:
        raise _bad("response too large")
    try:
        parsed = tsp.TimeStampResp.load(response, strict=True)
        status = parsed["status"]["status"].native
        token = parsed["time_stamp_token"]
        tst = _tst_info(token)
    except (ValueError, TypeError, KeyError) as exc:
        raise _bad(f"unreadable response ({type(exc).__name__})") from exc
    if status not in ("granted", "granted_with_mods"):
        raise _bad(f"TSA answered {status}", code="anchor.tsa_refused")
    if tst["nonce"].native != nonce:
        raise _bad("nonce does not match the request")
    _check_imprint(tst, digest)
    return bytes(token.dump())


def _tst_info(token: cms.ContentInfo) -> tsp.TSTInfo:
    if token["content_type"].native != "signed_data":
        raise _bad("not CMS SignedData")
    encap = token["content"]["encap_content_info"]
    if encap["content_type"].native != "tst_info":
        raise _bad("does not wrap a TSTInfo")
    info = encap["content"].parsed
    if not isinstance(info, tsp.TSTInfo):
        raise _bad("does not wrap a TSTInfo")
    return info


def _check_imprint(tst: tsp.TSTInfo, digest: bytes) -> None:
    imprint = tst["message_imprint"]
    if imprint["hash_algorithm"]["algorithm"].native != "sha256":
        raise _bad("message imprint is not SHA-256")
    if not hmac.compare_digest(imprint["hashed_message"].native, digest):
        raise _bad("timestamps a different digest", code="anchor.wrong_digest")


def _attrs(signer: cms.SignerInfo) -> dict[str, list[object]]:
    found: dict[str, list[object]] = {}
    for attr in signer["signed_attrs"]:
        name = attr["type"].native
        if name in found:
            raise _bad(f"signed attribute {name} appears twice")
        found[name] = list(attr["values"])
    return found


def _signer_cert(signed: cms.SignedData, signer: cms.SignerInfo) -> asn1_x509.Certificate:
    sid = signer["sid"]
    for choice in signed["certificates"] or []:
        if choice.name != "certificate":
            continue
        cert = choice.chosen
        if sid.name == "issuer_and_serial_number":
            match = (
                cert.issuer == sid.chosen["issuer"]
                and cert.serial_number == sid.chosen["serial_number"].native
            )
        else:
            match = cert.key_identifier == sid.chosen.native
        if match:
            return cert
    raise _bad("signer certificate is not in the token")


def _check_ess(attrs: dict[str, list[object]], cert_der: bytes) -> None:
    """ESS signing-certificate (RFC 2634, v1, SHA-1) or v2 (RFC 5035): binds the cert."""
    if "signing_certificate_v2" in attrs:
        entry = attrs["signing_certificate_v2"][0]["certs"][0]  # type: ignore[index]
        algorithm = entry["hash_algorithm"]["algorithm"].native
        if algorithm not in _HASHES:
            raise _bad(f"ESS certificate hash {algorithm} unsupported")
        expected = hashlib.new(algorithm, cert_der).digest()
        found = entry["cert_hash"].native
    elif "signing_certificate" in attrs:
        entry = attrs["signing_certificate"][0]["certs"][0]  # type: ignore[index]
        expected = hashlib.sha1(cert_der, usedforsecurity=False).digest()  # RFC 2634 fixes SHA-1
        found = entry["cert_hash"].native
    else:
        raise _bad("no ESS signing-certificate attribute")
    if not hmac.compare_digest(found, expected):
        raise _bad("ESS attribute names another certificate")


def _verify_signature(
    cert: x509.Certificate, signer: cms.SignerInfo, digest_name: str, message: bytes
) -> None:
    algorithm = signer["signature_algorithm"]
    kind, signature, key = algorithm.signature_algo, signer["signature"].native, cert.public_key()
    try:
        if kind == "rsassa_pkcs1v15" and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, message, padding.PKCS1v15(), _HASHES[digest_name]())
        elif kind == "ecdsa" and isinstance(key, ec.EllipticCurvePublicKey):
            hash_name = algorithm.hash_algo
            if hash_name not in _HASHES:
                raise _bad(f"ECDSA with {hash_name} unsupported")
            key.verify(signature, message, ec.ECDSA(_HASHES[hash_name]()))
        elif kind == "ed25519" and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, message)
        else:
            raise _bad(f"signature algorithm {kind} unsupported for this key")
    except InvalidSignature as exc:
        raise _bad("signature does not verify", code="anchor.bad_signature") from exc


def _check_eku(cert: x509.Certificate) -> None:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound as exc:
        raise _bad("signer certificate has no extended key usage") from exc
    if not eku.critical or ExtendedKeyUsageOID.TIME_STAMPING not in eku.value:
        raise _bad("signer certificate is not a critical timeStamping certificate")


def _valid_at(cert: x509.Certificate, moment: datetime) -> bool:
    return cert.not_valid_before_utc <= moment <= cert.not_valid_after_utc


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _issued_by(child: x509.Certificate, issuer: x509.Certificate) -> bool:
    if child.issuer != issuer.subject or not _is_ca(issuer):
        return False
    try:
        child.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def _same_key(a: x509.Certificate, b: x509.Certificate) -> bool:
    return a.subject == b.subject and a.public_key() == b.public_key()


def _check_chain(
    leaf: x509.Certificate,
    included: Sequence[x509.Certificate],
    roots: Sequence[x509.Certificate],
    moment: datetime,
) -> None:
    """Walk issuer links from the signer to a trusted root, all valid at ``moment``."""
    current = leaf
    for _ in range(MAX_CHAIN):
        if not _valid_at(current, moment):
            raise _bad(f"certificate {current.subject.rfc4514_string()} not valid at genTime")
        if any(_same_key(current, root) for root in roots):
            return  # a trusted root itself (a cross-signed copy of one counts too)
        root = next((r for r in roots if _issued_by(current, r)), None)
        if root is not None:
            if not _valid_at(root, moment):
                raise _bad("trusted root not valid at genTime")
            return
        issuer = next((c for c in included if c != current and _issued_by(current, c)), None)
        if issuer is None:
            raise _bad("certificate chain does not reach a trusted root", code="anchor.untrusted")
        current = issuer
    raise _bad("certificate chain too long", code="anchor.untrusted")


def verify_token(token: bytes, digest: bytes, roots: Sequence[x509.Certificate]) -> TimestampInfo:
    """Check a DER timestamp token for ``digest`` against trusted TSA roots (see module doc)."""
    if len(token) > MAX_TOKEN_BYTES:
        raise _bad("token too large")
    try:
        info = cms.ContentInfo.load(token, strict=True)
        tst = _tst_info(info)
        signed = info["content"]
        signers = list(signed["signer_infos"])
        gen_time = tst["gen_time"].native
    except (ValueError, TypeError, KeyError) as exc:
        raise _bad(f"unreadable ({type(exc).__name__})") from exc
    if len(signers) != 1:
        raise _bad("expected exactly one signer")
    _check_imprint(tst, digest)
    signer = signers[0]
    digest_name = signer["digest_algorithm"]["algorithm"].native
    if digest_name not in _HASHES or not signer["signed_attrs"]:
        raise _bad("unsupported digest or no signed attributes")
    attrs = _attrs(signer)
    econtent = signed["encap_content_info"]["content"].contents
    if [v.native for v in attrs.get("content_type", [])] != ["tst_info"]:  # type: ignore[attr-defined]
        raise _bad("content-type attribute is not TSTInfo")
    message_digest = [v.native for v in attrs.get("message_digest", [])]  # type: ignore[attr-defined]
    expected = hashlib.new(digest_name, econtent).digest()
    if len(message_digest) != 1 or not hmac.compare_digest(message_digest[0], expected):
        raise _bad("message-digest attribute does not match the TSTInfo")
    signer_asn1 = _signer_cert(signed, signer)
    cert_der = bytes(signer_asn1.dump())
    _check_ess(attrs, cert_der)
    cert = x509.load_der_x509_certificate(cert_der)
    # Signed attributes are signed as a DER SET (tag 0x31), not as the [0] they travel in.
    _verify_signature(cert, signer, digest_name, b"\x31" + signer["signed_attrs"].dump()[1:])
    _check_eku(cert)
    included = [
        x509.load_der_x509_certificate(c.chosen.dump())
        for c in signed["certificates"] or []
        if c.name == "certificate"
    ]
    _check_chain(cert, included, roots, gen_time)
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return TimestampInfo(
        gen_time=gen_time,
        tsa=str(names[0].value) if names else cert.subject.rfc4514_string(),
        serial=int(tst["serial_number"].native),
        policy=str(tst["policy"].native),
    )


def load_roots(pem: bytes) -> list[x509.Certificate]:
    """Trusted TSA roots from a PEM bundle the auditor controls."""
    try:
        roots = x509.load_pem_x509_certificates(pem)
    except ValueError as exc:
        raise AnchorError("cannot read TSA roots", code="anchor.bad_roots") from exc
    return roots
