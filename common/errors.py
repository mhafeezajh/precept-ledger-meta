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


class LearnError(PreceptError):
    """The Policy Learner refused to run, propose or record an approval. Nothing changes."""

    code = "learn.invalid"


class WitnessError(PreceptError):
    """A witness refused a checkpoint, or could not be asked. Never counts as a cosignature.

    ``latest_size`` is set on ``witness.conflict``: the size the witness last cosigned, so the
    caller can retry with a consistency proof from there (C2SP tlog-witness, 409).
    """

    code = "witness.error"

    def __init__(self, message: str, *, code: str | None = None, latest_size: int | None = None):
        super().__init__(message, code=code)
        self.latest_size = latest_size


class AnchorError(PreceptError):
    """A timestamp token or OpenTimestamps proof is malformed, untrusted or does not match."""

    code = "anchor.invalid"


class GateError(PreceptError):
    """Gate could not decide, record or confirm a tool call. The call never runs (R3, R4)."""

    code = "gate.error"
