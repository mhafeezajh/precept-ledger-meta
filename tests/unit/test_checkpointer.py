"""Checkpointer: signs tree heads, refuses to sign a rewritten history, writes once."""

import stat
from pathlib import Path

import pytest

from common.errors import LedgerError, SignatureError, StoreError
from ledger.checkpoint import Checkpoint, NoteKey, open_checkpoint, sign_checkpoint
from ledger.checkpointer import Checkpointer, DirectorySink, SignedCheckpoint, origin_for
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import MemoryStore
from merkle.hashing import EMPTY_ROOT, leaf_hash
from merkle.log import LogTree
from merkle.proofs import verify_consistency, verify_inclusion
from merkle.store import MemoryTreeStore
from tests.helpers import STREAM, clock, make_submission, signer_for

GATE, LOG = signer_for("gate-1"), signer_for("log")
ORIGIN = origin_for("precept-ledger.local", STREAM)
LOG_KEY = NoteKey(ORIGIN, LOG.public_key_raw)


def tree_of(count: int, *, edit_at: int | None = None) -> MemoryTreeStore:
    store = MemoryTreeStore()
    for i in range(count):
        store.append(leaf_hash(b"edited" if i == edit_at else str(i).encode()))
    return store


def checkpointer(tree: MemoryTreeStore, directory: Path) -> Checkpointer:
    return Checkpointer(LogTree(tree), LOG, ORIGIN, [DirectorySink(directory)])


def test_origin_for() -> None:
    assert ORIGIN == "precept-ledger.local/harbor-federal/decisions"


async def test_empty_log_gets_a_size_zero_checkpoint(tmp_path: Path) -> None:
    signed = await checkpointer(tree_of(0), tmp_path).publish()
    assert signed.checkpoint == Checkpoint(ORIGIN, 0, EMPTY_ROOT)
    assert open_checkpoint(signed.note, LOG_KEY) == signed.checkpoint
    assert [p.name for p in tmp_path.iterdir()] == ["00000000000000000000.note"]


async def test_receipts_prove_inclusion_under_the_checkpoint(tmp_path: Path) -> None:
    # Demo flow D: append, checkpoint, then every receipt's leaf verifies against the root.
    store, clk = MemoryStore(), clock()
    ledger = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    receipts = [await ledger.append(STREAM, make_submission(GATE, clk)) for _ in range(5)]
    tree = LogTree(store.tree(STREAM))
    signed = await Checkpointer(tree, LOG, ORIGIN, [DirectorySink(tmp_path)]).publish()
    assert signed.checkpoint.size == 5
    for receipt in receipts:
        proof = await tree.prove_inclusion(receipt.seq - 1, 5)
        assert verify_inclusion(
            receipt.leaf_hash_bytes(), receipt.seq - 1, 5, proof.hashes, signed.checkpoint.root
        )


async def test_no_growth_republishes_the_same_note(tmp_path: Path) -> None:
    cp = checkpointer(tree_of(3), tmp_path)
    first, second = await cp.publish(), await cp.publish()
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


async def test_growth_yields_a_consistent_checkpoint(tmp_path: Path) -> None:
    tree = tree_of(3)
    cp = checkpointer(tree, tmp_path)
    old = (await cp.publish()).checkpoint
    tree.append(leaf_hash(b"3"))
    tree.append(leaf_hash(b"4"))
    new = (await cp.publish()).checkpoint
    proof = await LogTree(tree).prove_consistency(3, 5)
    assert new.size == 5
    assert verify_consistency(3, 5, old.root, new.root, proof.hashes)
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.parametrize(
    ("edit_at", "new_size"),
    [(1, 4), (0, 3), (2, 6), (None, 2)],
    ids=["rewrite-and-grow", "rewrite-same-size", "rewrite-last-and-grow", "truncate"],
)
async def test_refuses_to_sign_a_history_inconsistent_with_the_last_checkpoint(
    tmp_path: Path, edit_at: int | None, new_size: int
) -> None:
    # Even the operator's own checkpointer will not sign a fork (rogue operator, beat 5). A
    # rogue operator can bypass it; witnesses are the real check (day 3).
    await checkpointer(tree_of(3), tmp_path).publish()
    with pytest.raises(LedgerError) as err:
        await checkpointer(tree_of(new_size, edit_at=edit_at), tmp_path).publish()
    assert err.value.code == "checkpoint.inconsistent"
    assert len(list(tmp_path.iterdir())) == 1


async def test_previous_checkpoint_signed_by_another_key_fails_closed(tmp_path: Path) -> None:
    forged = sign_checkpoint(Checkpoint(ORIGIN, 1, leaf_hash(b"0")), signer_for("rogue"))
    (tmp_path / "00000000000000000001.note").write_text(forged)
    with pytest.raises(SignatureError):
        await checkpointer(tree_of(3), tmp_path).publish()


async def test_directory_sink_is_write_once(tmp_path: Path) -> None:
    sink = DirectorySink(tmp_path)
    one = Checkpoint(ORIGIN, 1, leaf_hash(b"0"))
    signed = SignedCheckpoint(one, sign_checkpoint(one, LOG))
    await sink.write(signed)
    await sink.write(signed)  # identical rewrite is a no-op
    path = tmp_path / "00000000000000000001.note"
    assert not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    other = Checkpoint(ORIGIN, 1, leaf_hash(b"x"))
    with pytest.raises(LedgerError) as err:
        await sink.write(SignedCheckpoint(other, sign_checkpoint(other, LOG)))
    assert err.value.code == "checkpoint.conflict"  # two signed roots for one size
    assert path.read_text() == signed.note


async def test_directory_sink_latest_is_the_largest_size(tmp_path: Path) -> None:
    sink = DirectorySink(tmp_path / "log")
    assert await sink.latest() is None
    for size in (2, 10, 3):
        cp = Checkpoint(ORIGIN, size, leaf_hash(str(size).encode()))
        await sink.write(SignedCheckpoint(cp, sign_checkpoint(cp, LOG)))
    (tmp_path / "log" / "notes.txt").write_text("ignored")
    latest = await sink.latest()
    assert latest is not None
    assert "\n10\n" in latest


class FailingSink:
    def __init__(self) -> None:
        self.fail = True
        self.notes: list[str] = []

    async def write(self, signed: SignedCheckpoint) -> None:
        if self.fail:
            raise StoreError("sink down")
        self.notes.append(signed.note)

    async def latest(self) -> str | None:
        return None

    async def notes(self) -> list[str]:
        return []


async def test_sink_failure_fails_the_publish_and_the_next_publish_heals(tmp_path: Path) -> None:
    flaky = FailingSink()
    cp = Checkpointer(LogTree(tree_of(2)), LOG, ORIGIN, [DirectorySink(tmp_path), flaky])
    with pytest.raises(StoreError):
        await cp.publish()
    flaky.fail = False
    signed = await cp.publish()  # unchanged head, rewritten to every sink
    assert flaky.notes == [signed.note]


def test_needs_a_sink() -> None:
    with pytest.raises(LedgerError):
        Checkpointer(LogTree(tree_of(0)), LOG, ORIGIN, [])
