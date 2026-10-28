# Trusted timestamp authority roots

`roots.pem` holds the public root certificates of the two RFC 3161 timestamp authorities the demo uses. `pl-ledger` checks every token against them before storing it (`TSA_ROOTS_FILE`). Auditors should get roots from the TSAs themselves and pass their own file to `pl-verify --tsa-roots`. Trust does not come from this repository.

| Root | SHA-256 fingerprint | Downloaded from (2026-10-28) | Expires |
| --- | --- | --- | --- |
| DigiCert Trusted Root G4 | `552f7bdcf1a7af9e6ce672017f4f12abf77240c78e761ac203d1d9d20ac89988` | https://cacerts.digicert.com/DigiCertTrustedRootG4.crt.pem | 2038-01-15 |
| Free TSA Root CA (www.freetsa.org) | `a6379e7cecc05faa3cbf076013d745e327bbbaa38c0b9af22469d4701d18aabc` | https://freetsa.org/files/cacert.pem | 2041-03-07 |

Check a fingerprint with `openssl x509 -in cert.pem -noout -fingerprint -sha256`.
