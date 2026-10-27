"""The ledger service: accept signed submissions, chain them, and return signed receipts.

Every failure raises. A caller (Gate) that gets an exception instead of a receipt must treat
the action as denied (R3, R4); nothing here ever returns a partial or unsigned result.
"""

from common.clock import Clock, format_ts
from common.errors import FormatError, LedgerError
from ledger.chain import ChainedRecord, ChainHead, ChainReport, link, verify_chain
from ledger.format import MAX_RECORD_BYTES, Receipt, Record, Submission, parse_json_strict
from ledger.receipts import issue_receipt
from ledger.signing import KeyRing, Signer
from ledger.store import Store


class Ledger:
    def __init__(self, store: Store, producers: KeyRing, log_signer: Signer, clock: Clock) -> None:
        self._store = store
        self._producers = producers
        self._log_signer = log_signer
        self._clock = clock

    async def append(self, stream: str, submission: Submission) -> Receipt:
        if submission.stream != stream:
            raise LedgerError(
                f"submission is for {submission.stream!r}, not {stream!r}",
                code="ledger.stream_mismatch",
            )
        # Layer 2: the ledger never stores a record its producer did not sign.
        self._producers.verify_submission(submission)

        def build(head: ChainHead) -> tuple[ChainedRecord, Receipt]:
            item = link(head, submission)
            return item, issue_receipt(self._log_signer, item, format_ts(self._clock.now()))

        return await self._store.append(stream, build)

    async def append_json(self, stream: str, data: bytes) -> Receipt:
        """API boundary: size limit, strict JSON parse, exact fields, then ``append``."""
        if len(data) > MAX_RECORD_BYTES:
            raise FormatError(f"request exceeds {MAX_RECORD_BYTES} bytes", code="format.too_large")
        return await self.append(stream, Submission.from_json(parse_json_strict(data)))

    async def get(self, record_id: str) -> tuple[Record, Receipt]:
        found = await self._store.get(record_id)
        if found is None:
            raise LedgerError(f"no record {record_id}", code="ledger.not_found")
        return found

    async def verify_stream(self, stream: str) -> ChainReport:
        """Recompute the whole stream from what the store holds right now."""
        return verify_chain(await self._store.read_raw(stream), self._producers, stream=stream)
