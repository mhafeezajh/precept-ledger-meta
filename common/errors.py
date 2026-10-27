"""One exception hierarchy for the whole project.

Every error carries a machine-readable ``code`` (dotted, e.g. ``format.unknown_field``)
so callers and the UI can branch on the reason without parsing messages.
"""


class PreceptError(Exception):
    """Base class for every error this project raises on purpose."""

    code: str = "precept.error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {self.args[0]}"


class ConfigError(PreceptError):
    code = "config.invalid"


class LedgerError(PreceptError):
    code = "ledger.error"


class FormatError(LedgerError):
    """A record, receipt or JSON value breaks the v1 format (non-I-JSON, unknown field, ...)."""

    code = "format.invalid"


class SignatureError(LedgerError):
    """A signature is missing, made with an unknown key, or does not verify."""

    code = "signature.invalid"


class StoreError(LedgerError):
    """The ledger store is unreachable or refused the write. Callers must fail closed."""

    code = "store.unavailable"


class MerkleError(PreceptError):
    """Bad tree size, index or hash, or a tree store that returned inconsistent data."""

    code = "merkle.invalid"


class VerificationError(PreceptError):
    code = "verify.failed"


class PolicyError(PreceptError):
    code = "policy.invalid"


class ModelOutputError(PreceptError):
    code = "model.bad_output"
