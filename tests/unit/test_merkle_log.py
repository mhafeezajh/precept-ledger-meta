"""LogTree over the TreeStore port, with the in-memory adapter and a misbehaving adapter."""

import pytest

from common.errors import MerkleError
from ledger.format import b64decode
from ledger.receipts import issue_receipt
from merkle.hashing import EMPTY_ROOT, leaf_hash
from merkle.log import LogTree, TreeHead
from merkle.proofs import root_hash, verify_consistency, verify_inclusion
from merkle.store import MemoryTreeStore
from tests.helpers import build_chain, clock, signer_for


def filled(count: int) -> tuple[MemoryTreeStore, list[bytes]]:
    store = MemoryTreeStore()
    leaves = [leaf_hash(bytes([i])) for i in range(count)]
    for leaf in leaves:
        store.append(leaf)
    return store, leaves


async def test_head_of_empty_and_filled_store() -> None:
    assert await LogTree(MemoryTreeStore()).head() == TreeHead(0, EMPTY_ROOT)
    store, leaves = filled(11)
    tree = LogTree(store)
    assert await tree.head() == TreeHead(11, root_hash(leaves))
    assert await tree.head(4) == TreeHead(4, root_hash(leaves[:4]))


async def test_proofs_from_the_log_verify() -> None:
    store, leaves = filled(11)
    tree = LogTree(store)
    head = await tree.head()
    inclusion = await tree.prove_inclusion(7, head.size)
    assert (inclusion.index, inclusion.size) == (7, 11)
    assert verify_inclusion(leaves[7], 7, 11, inclusion.hashes, head.root)
    old = await tree.head(5)
    consistency = await tree.prove_consistency(5, 11)
    assert verify_consistency(5, 11, old.root, head.root, consistency.hashes)


async def test_sizes_beyond_the_store_are_rejected() -> None:
    store, _ = filled(3)
    tree = LogTree(store)
    with pytest.raises(MerkleError):
        await tree.head(4)
    with pytest.raises(MerkleError):
        await tree.prove_inclusion(0, 4)
    with pytest.raises(MerkleError):
        await tree.prove_inclusion(3, 3)
    with pytest.raises(MerkleError):
        await tree.prove_consistency(2, 4)
    with pytest.raises(MerkleError):
        await tree.prove_consistency(3, 2)


def test_memory_store_rejects_bad_leaf_hash() -> None:
    with pytest.raises(MerkleError):
        MemoryTreeStore().append(b"short")


class LyingStore(MemoryTreeStore):
    """An adapter that returns fewer hashes than asked for."""

    async def leaf_hashes(self, start: int, end: int) -> list[bytes]:
        return (await super().leaf_hashes(start, end))[:-1]


async def test_adapter_returning_wrong_count_fails_closed() -> None:
    store = LyingStore()
    for i in range(4):
        store.append(leaf_hash(bytes([i])))
    with pytest.raises(MerkleError) as exc:
        await LogTree(store).head()
    assert exc.value.code == "merkle.bad_store"


async def test_ledger_leaf_hashes_and_receipts_plug_into_the_tree() -> None:
    # Leaf index = seq - 1 (D16); the receipt's leaf_hash is the Merkle leaf.
    clk = clock()
    chained = build_chain(6, signer_for("gate-1"), clk)
    store = MemoryTreeStore()
    for item in chained:
        assert item.leaf_hash == leaf_hash(item.record.canonical())
        store.append(item.leaf_hash)
    tree = LogTree(store)
    head = await tree.head()
    receipt = issue_receipt(signer_for("log"), chained[3], "2026-10-29T21:14:03.150Z")
    proof = await tree.prove_inclusion(receipt.seq - 1, head.size)
    receipt_leaf = b64decode(receipt.leaf_hash, size=32, field="leaf_hash")
    assert verify_inclusion(receipt_leaf, receipt.seq - 1, head.size, proof.hashes, head.root)
