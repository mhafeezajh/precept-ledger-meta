"""A TEST ONLY timestamp authority: a CA, a TSA certificate and RFC 3161 tokens on demand,
with switches to break every property ``anchor.rfc3161.verify_token`` checks."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from asn1crypto import cms, tsp
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

T0 = datetime(2026, 10, 29, 21, 0, 0, tzinfo=UTC)

type PrivateKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _sign_alg(key: PrivateKey) -> hashes.SHA256 | None:
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def make_cert(
    subject: str,
    key: PrivateKey,
    issuer_key: PrivateKey,
    issuer: x509.Certificate | None,
    *,
    ca: bool,
    eku: list[x509.ObjectIdentifier] | None = None,
    eku_critical: bool = True,
    not_before: datetime = T0 - timedelta(days=30),
    not_after: datetime = T0 + timedelta(days=365),
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer.subject if issuer else _name(subject))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if eku is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=eku_critical)
    return builder.sign(issuer_key, _sign_alg(issuer_key))


def _sign(key: PrivateKey, message: bytes) -> tuple[bytes, str]:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(message, ec.ECDSA(hashes.SHA256())), "sha256_ecdsa"
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(message, PKCS1v15(), hashes.SHA256()), "rsassa_pkcs1v15"
    return key.sign(message), "ed25519"


@dataclass
class FakeTSA:
    """Defaults give a valid ECDSA TSA under one CA. Every field can be broken on purpose."""

    key: PrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    ca_key: PrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    eku: list[x509.ObjectIdentifier] | None = field(
        default_factory=lambda: [ExtendedKeyUsageOID.TIME_STAMPING]
    )
    eku_critical: bool = True
    cert_not_after: datetime = T0 + timedelta(days=365)
    intermediates: int = 0
    root_not_after: datetime = T0 + timedelta(days=3650)

    def __post_init__(self) -> None:
        self.root = make_cert("Test TSA Root", self.ca_key, self.ca_key, None, ca=True,
                              not_after=self.root_not_after)  # fmt: skip
        issuer_key: PrivateKey = self.ca_key
        issuer = self.root
        self.chain: list[x509.Certificate] = []
        for n in range(self.intermediates):
            mid_key = ec.generate_private_key(ec.SECP256R1())
            mid = make_cert(f"Test TSA Intermediate {n}", mid_key, issuer_key, issuer, ca=True)
            self.chain.insert(0, mid)
            issuer_key, issuer = mid_key, mid
        self.cert = make_cert(
            "Test TSA",
            self.key,
            issuer_key,
            issuer,
            ca=False,
            eku=self.eku,
            eku_critical=self.eku_critical,
            not_after=self.cert_not_after,
        )

    @property
    def root_pem(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.PEM)

    def token(self, digest: bytes, **options: Any) -> bytes:
        return bytes(self.response(digest, **options)["time_stamp_token"].dump())

    def response(
        self,
        digest: bytes,
        *,
        nonce: int | None = 7,
        gen_time: datetime = T0,
        status: str = "granted",
        imprint_alg: str = "sha256",
        ess: str = "v2",
        ess_cert: x509.Certificate | None = None,
        bad_message_digest: bool = False,
        bad_signature: bool = False,
        include_certs: bool = True,
        content_type: str = "tst_info",
        signers: int = 1,
        algorithm_override: str | None = None,
        duplicate_attribute: bool = False,
        digest_alg: str = "sha256",
    ) -> tsp.TimeStampResp:
        tst = tsp.TSTInfo(
            {
                "version": 1,
                "policy": "1.3.6.1.4.1.99999.1",
                "message_imprint": {
                    "hash_algorithm": {"algorithm": imprint_alg},
                    "hashed_message": digest,
                },
                "serial_number": 42,
                "gen_time": gen_time,
                "nonce": nonce,
            }
        )
        econtent = tst.dump()
        der = self.cert.public_bytes(serialization.Encoding.DER)
        bound = (ess_cert or self.cert).public_bytes(serialization.Encoding.DER)
        attrs: list[dict[str, Any]] = [
            {"type": "content_type", "values": [content_type]},
            {
                "type": "message_digest",
                "values": [
                    hashlib.sha256(econtent + (b"x" if bad_message_digest else b"")).digest()
                ],
            },
        ]
        if ess == "v2":
            attrs.append(
                {
                    "type": "signing_certificate_v2",
                    "values": [{"certs": [{"cert_hash": hashlib.sha256(bound).digest()}]}],
                }
            )
        elif ess == "v1":
            attrs.append(
                {
                    "type": "signing_certificate",
                    "values": [{"certs": [{"cert_hash": hashlib.sha1(bound).digest()}]}],  # noqa: S324  # RFC 2634
                }
            )
        if duplicate_attribute:
            attrs.append(attrs[0])
        signed_attrs = cms.CMSAttributes(attrs)
        signature, algorithm = _sign(self.key, signed_attrs.dump())
        algorithm = algorithm_override or algorithm
        if bad_signature:
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        cert = asn1_x509.Certificate.load(der)
        signer = cms.SignerInfo(
            {
                "version": "v1",
                "sid": cms.SignerIdentifier(
                    {
                        "issuer_and_serial_number": {
                            "issuer": cert.issuer,
                            "serial_number": cert.serial_number,
                        }
                    }
                ),
                "digest_algorithm": {"algorithm": digest_alg},
                "signed_attrs": signed_attrs,
                "signature_algorithm": {"algorithm": algorithm},
                "signature": signature,
            }
        )
        certs = [cert, *(asn1_x509.Certificate.load(c.public_bytes(serialization.Encoding.DER))
                         for c in self.chain)] if include_certs else []  # fmt: skip
        signed = cms.SignedData(
            {
                "version": "v3",
                "digest_algorithms": [{"algorithm": "sha256"}],
                "encap_content_info": {"content_type": "tst_info", "content": tst},
                "certificates": certs,
                "signer_infos": [signer] * signers,
            }
        )
        token = cms.ContentInfo({"content_type": "signed_data", "content": signed})
        return tsp.TimeStampResp({"status": {"status": status}, "time_stamp_token": token})
