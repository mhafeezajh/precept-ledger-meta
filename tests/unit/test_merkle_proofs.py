"""merkle core against published RFC 6962 / RFC 9162 vectors (tests/vectors/merkle-rfc9162.json)."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from common.errors import MerkleError
from merkle.hashing import EMPTY_ROOT, LEAF_PREFIX, NODE_PREFIX, leaf_hash, node_hash
from merkle.proofs import (
    consistency_proof,
    inclusion_proof,
    root_hash,
    verify_consistency,
    verify_inclusion,
)

V = json.loads((Path(__file__).parents[1] / "vectors" / "merkle-rfc9162.json").read_text())
LEAVES = [leaf_hash(bytes.fromhex(h)) for h in V["leaf_inputs_hex"]]
ROOTS = [bytes.fromhex(h) for h in V["root_hashes_hex"]]


def hexes(items: list[bytes]) -> list[str]:
    return [i.hex() for i in items]


def test_hashing_formulas() -> None:
    assert LEAF_PREFIX == b"\x00"
    assert NODE_PREFIX == b"\x01"
    assert leaf_hash(b"abc") == hashlib.sha256(b"\x00abc").digest()
    a, b = bytes(32), bytes([1]) * 32
    assert node_hash(a, b) == hashlib.sha256(b"\x01" + a + b).digest()
    assert hashlib.sha256(b"").digest() == EMPTY_ROOT


def test_node_hash_rejects_wrong_sizes() -> None:
    with pytest.raises(MerkleError):
        node_hash(b"short", bytes(32))


def test_leaf_hashes_match_published_level_zero() -> None:
    assert hexes(LEAVES) == V["node_hashes_hex"][0]


@pytest.mark.parametrize("size", range(9))
def test_roots_match_published_vectors(size: int) -> None:
    assert root_hash(LEAVES[:size]) == ROOTS[size]


@pytest.mark.parametrize("vec", V["inclusion"], ids=lambda v: f"{v['index']}of{v['size']}")
def test_inclusion_proofs_match_and_verify(vec: dict[str, Any]) -> None:
    index, size = vec["index"], vec["size"]
    proof = inclusion_proof(index, LEAVES[:size])
    assert hexes(proof) == vec["proof"]
    assert verify_inclusion(LEAVES[index], index, size, proof, ROOTS[size])


@pytest.mark.parametrize("vec", V["consistency"], ids=lambda v: f"{v['old_size']}to{v['new_size']}")
def test_consistency_proofs_match_and_verify(vec: dict[str, Any]) -> None:
    old, new = vec["old_size"], vec["new_size"]
    proof = consistency_proof(old, LEAVES[:new])
    assert hexes(proof) == vec["proof"]
    assert verify_consistency(old, new, ROOTS[old], ROOTS[new], proof)


def test_inclusion_rejects_wrong_inputs() -> None:
    proof = inclusion_proof(5, LEAVES)
    assert not verify_inclusion(LEAVES[4], 5, 8, proof, ROOTS[8])  # wrong leaf
    assert not verify_inclusion(LEAVES[5], 4, 8, proof, ROOTS[8])  # wrong index
    assert not verify_inclusion(LEAVES[5], 5, 7, proof, ROOTS[7])  # wrong size
    assert not verify_inclusion(LEAVES[5], 5, 8, proof, ROOTS[7])  # wrong root
    assert not verify_inclusion(LEAVES[5], 5, 8, proof[:-1], ROOTS[8])  # short proof
    assert not verify_inclusion(LEAVES[5], 5, 8, [*proof, bytes(32)], ROOTS[8])  # long proof
    assert not verify_inclusion(LEAVES[5], 8, 8, proof, ROOTS[8])  # index out of range
    assert not verify_inclusion(LEAVES[5], -1, 8, proof, ROOTS[8])
    assert not verify_inclusion(LEAVES[5], 5, 8, [b"short", *proof[1:]], ROOTS[8])
    assert not verify_inclusion(b"short", 5, 8, proof, ROOTS[8])


def test_consistency_rejects_wrong_inputs() -> None:
    proof = consistency_proof(6, LEAVES)
    assert not verify_consistency(6, 8, ROOTS[5], ROOTS[8], proof)  # wrong old root
    assert not verify_consistency(6, 8, ROOTS[6], ROOTS[7], proof)  # wrong new root
    assert not verify_consistency(5, 8, ROOTS[5], ROOTS[8], proof)  # wrong old size
    assert not verify_consistency(6, 8, ROOTS[6], ROOTS[8], proof[:-1])
    assert not verify_consistency(6, 8, ROOTS[6], ROOTS[8], [*proof, bytes(32)])
    assert not verify_consistency(6, 8, ROOTS[6], ROOTS[8], [])
    assert not verify_consistency(8, 6, ROOTS[8], ROOTS[6], proof)  # shrinking
    assert not verify_consistency(-1, 8, ROOTS[0], ROOTS[8], proof)
    assert not verify_consistency(8, 8, ROOTS[8], ROOTS[8], [ROOTS[8]])  # same size: empty only
    assert not verify_consistency(8, 8, ROOTS[7], ROOTS[8], [])
    assert not verify_consistency(6, 8, b"short", ROOTS[8], proof)


def test_empty_tree_is_consistent_with_everything_and_has_no_inclusion() -> None:
    assert consistency_proof(0, LEAVES) == []
    assert verify_consistency(0, 8, EMPTY_ROOT, ROOTS[8], [])
    assert not verify_consistency(0, 8, EMPTY_ROOT, ROOTS[8], [bytes(32)])
    assert not verify_consistency(0, 8, ROOTS[1], ROOTS[8], [])  # old root must be the empty root
    assert not verify_inclusion(LEAVES[0], 0, 0, [], EMPTY_ROOT)


def test_proof_generation_rejects_bad_arguments() -> None:
    with pytest.raises(MerkleError):
        inclusion_proof(8, LEAVES)
    with pytest.raises(MerkleError):
        inclusion_proof(-1, LEAVES)
    with pytest.raises(MerkleError):
        consistency_proof(9, LEAVES)
    with pytest.raises(MerkleError):
        consistency_proof(-1, LEAVES)
    with pytest.raises(MerkleError):
        root_hash([b"short"])
