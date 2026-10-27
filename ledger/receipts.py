"""Receipts: the log key's signed statement that a record was accepted at a sequence number.

A producer that keeps its receipts can later prove a record was accepted, and two valid
receipts that disagree about one sequence number prove the operator rewrote history.
"""

import hmac
from collections.abc import Iterable, Sequence

from common.errors import SignatureError
from ledger.chain import BreakReason, ChainBreak, ChainedRecord
from ledger.format import ALG_ED25519, RECORD_VERSION, Receipt, b64encode, receipt_signing_payload
from ledger.signing import KeyRing, Signer

LOG_KEY_OWNER = "log"


def issue_receipt(log_signer: Signer, item: ChainedRecord, received_at: str) -> Receipt:
    sub = item.record.submission
    leaf, chain = b64encode(item.leaf_hash), b64encode(item.chain_hash)
    unsigned = {
        "v": RECORD_VERSION,
        "alg": ALG_ED25519,
        "stream": sub.stream,
        "record_id": sub.record_id,
        "seq": item.record.seq,
        "leaf_hash": leaf,
        "chain_hash": chain,
        "received_at": received_at,
        "key_id": log_signer.key_id,
    }
    sig = b64encode(log_signer.sign(receipt_signing_payload(unsigned)))
    return Receipt(
        stream=sub.stream,
        record_id=sub.record_id,
        seq=item.record.seq,
        leaf_hash=leaf,
        chain_hash=chain,
        received_at=received_at,
        key_id=log_signer.key_id,
        sig=sig,
    )


def verify_receipt(receipt: Receipt, log_keys: KeyRing) -> None:
    """Raise ``SignatureError`` unless the receipt is signed by a key owned by the log."""
    log_keys.verify(
        receipt.key_id, receipt.signing_bytes(), receipt.sig_bytes(), owner=LOG_KEY_OWNER
    )


def _mismatch(item: ChainedRecord, receipt: Receipt) -> str | None:
    sub = item.record.submission
    if sub.stream != receipt.stream or sub.record_id != receipt.record_id:
        return f"receipt is for {receipt.record_id}, history has {sub.record_id}"
    if not hmac.compare_digest(item.chain_hash, receipt.chain_hash_bytes()):
        return "chain hash differs from the receipt"
    if not hmac.compare_digest(item.leaf_hash, receipt.leaf_hash_bytes()):
        return "leaf hash differs from the receipt"
    return None


def check_receipts(
    history: Sequence[ChainedRecord], receipts: Iterable[Receipt], log_keys: KeyRing
) -> ChainBreak | None:
    """Check held receipts against a history; return the break with the lowest seq, if any."""
    by_seq = {item.record.seq: item for item in history}
    breaks: list[ChainBreak] = []
    for receipt in receipts:
        try:
            verify_receipt(receipt, log_keys)
        except SignatureError as exc:
            breaks.append(ChainBreak(receipt.seq, BreakReason.BAD_RECEIPT_SIGNATURE, str(exc)))
            continue
        item = by_seq.get(receipt.seq)
        if item is None:
            detail = f"receipt for {receipt.record_id} but no record at this seq"
            breaks.append(ChainBreak(receipt.seq, BreakReason.MISSING_RECORD, detail))
            continue
        detail_or_none = _mismatch(item, receipt)
        if detail_or_none is not None:
            breaks.append(ChainBreak(receipt.seq, BreakReason.RECEIPT_MISMATCH, detail_or_none))
    return min(breaks, key=lambda b: b.seq, default=None)


def conflicting_receipts(receipts: Iterable[Receipt]) -> list[tuple[Receipt, Receipt]]:
    """Pairs of receipts for the same stream and seq that disagree, ordered by seq.

    Signatures are not checked here; callers verify each receipt first. A verified pair is
    signed proof that the operator showed two different histories.
    """
    first_seen: dict[tuple[str, int], Receipt] = {}
    pairs: list[tuple[Receipt, Receipt]] = []
    for receipt in receipts:
        key = (receipt.stream, receipt.seq)
        earlier = first_seen.setdefault(key, receipt)
        if earlier is not receipt and (
            earlier.chain_hash != receipt.chain_hash or earlier.record_id != receipt.record_id
        ):
            pairs.append((earlier, receipt))
    return sorted(pairs, key=lambda pair: pair[0].seq)
