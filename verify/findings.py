"""One problem pl-verify found. Shared by the record, checkpoint, witness and anchor checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str
    seq: int | None = None  # None when the problem cannot be pinned to one record
    record_id: str | None = None
