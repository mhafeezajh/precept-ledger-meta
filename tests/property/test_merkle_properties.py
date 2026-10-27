"""Merkle properties (skill §6): proofs verify for random trees and fail when tampered."""

import hashlib

from hypothesis import given, settings
from hypothesis import strategies as st

from merkle.hashing import EMPTY_ROOT, leaf_hash, node_hash
from merkle.proofs import (
    consistency_proof,
    inclusion_proof,
    root_hash,
    verify_consistency,
    verify_inclusion,
)


def reference_root(leaves: list[bytes]) -> bytes:
    """RFC 9162 §2.1.1 MTH, written straight from the definition and never optimized."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    return hashlib.sha256(
        b"\x01" + reference_root(leaves[:k]) + reference_root(leaves[k:])
    ).digest()


trees = st.lists(st.binary(max_size=8), min_size=1, max_size=70).map(
    lambda inputs: [leaf_hash(i) for i in inputs]
)


def flip(data: bytes, position: int) -> bytes:
    position %= len(data)
    return data[:position] + bytes([data[position] ^ 0x01]) + data[position + 1 :]


@given(st.lists(st.binary(max_size=8), max_size=70))
def test_root_matches_reference_definition(inputs: list[bytes]) -> None:
    leaves = [leaf_hash(i) for i in inputs]
    assert root_hash(leaves) == reference_root(leaves)
    assert root_hash([]) == EMPTY_ROOT


@settings(max_examples=300)
@given(trees, st.data())
def test_inclusion_verifies_and_any_tamper_fails(leaves: list[bytes], data: st.DataObject) -> None:
    size = len(leaves)
    index = data.draw(st.integers(0, size - 1))
    root = root_hash(leaves)
    proof = inclusion_proof(index, leaves)
    assert verify_inclusion(leaves[index], index, size, proof, root)
    assert not verify_inclusion(
        flip(leaves[index], data.draw(st.integers(0, 31))), index, size, proof, root
    )
    assert not verify_inclusion(
        leaves[index], index, size, proof, flip(root, data.draw(st.integers(0, 31)))
    )
    if proof:
        at = data.draw(st.integers(0, len(proof) - 1))
        tampered = [*proof[:at], flip(proof[at], data.draw(st.integers(0, 31))), *proof[at + 1 :]]
        assert not verify_inclusion(leaves[index], index, size, tampered, root)
    other = data.draw(st.integers(0, size - 1))
    if leaves[other] != leaves[index]:
        assert not verify_inclusion(leaves[other], index, size, proof, root)


@settings(max_examples=300)
@given(trees, st.data())
def test_consistency_verifies_and_any_tamper_fails(
    leaves: list[bytes], data: st.DataObject
) -> None:
    new = len(leaves)
    old = data.draw(st.integers(0, new))
    old_root, new_root = root_hash(leaves[:old]), root_hash(leaves)
    proof = consistency_proof(old, leaves)
    assert verify_consistency(old, new, old_root, new_root, proof)
    if 0 < old < new:
        at = data.draw(st.integers(0, len(proof) - 1))
        tampered = [*proof[:at], flip(proof[at], data.draw(st.integers(0, 31))), *proof[at + 1 :]]
        assert not verify_consistency(old, new, old_root, new_root, tampered)
        assert not verify_consistency(old, new, flip(old_root, 0), new_root, proof)
        assert not verify_consistency(old, new, old_root, flip(new_root, 0), proof)


@settings(max_examples=200)
@given(trees, st.data())
def test_rewritten_history_has_no_consistency_proof(
    leaves: list[bytes], data: st.DataObject
) -> None:
    # The rogue-operator core: change any leaf inside the old tree and grow it. The proof the
    # rewritten tree yields does not link it to the old root. (That no proof at all can is
    # SHA-256 collision resistance, which a test cannot show.)
    new = len(leaves)
    old = data.draw(st.integers(1, new))
    old_root = root_hash(leaves[:old])
    edited_at = data.draw(st.integers(0, old - 1))
    rewritten = [
        *leaves[:edited_at],
        leaf_hash(b"forged" + leaves[edited_at]),
        *leaves[edited_at + 1 :],
    ]
    proof = consistency_proof(old, rewritten)
    assert not verify_consistency(old, new, old_root, root_hash(rewritten), proof)


@given(st.binary(min_size=32, max_size=32), st.binary(min_size=32, max_size=32))
def test_node_hash_is_order_sensitive(a: bytes, b: bytes) -> None:
    if a != b:
        assert node_hash(a, b) != node_hash(b, a)
