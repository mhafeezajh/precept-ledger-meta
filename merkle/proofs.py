"""RFC 9162 §2.1 roots, inclusion proofs and consistency proofs.

Generation follows the recursive definitions (MTH, PATH, SUBPROOF) over a list of leaf
hashes. Verification follows the iterative algorithms of §2.1.3.2 and §2.1.4.2 and returns a
plain bool: any malformed input is ``False``, never an exception, so a verifier cannot be
tricked into skipping a check by an error path (R4).
"""

import hmac
from collections.abc import Sequence

from common.errors import MerkleError
from merkle.hashing import EMPTY_ROOT, HASH_SIZE, node_hash


def _split(size: int) -> int:
    """Largest power of two strictly less than ``size`` (``size`` ≥ 2)."""
    return 1 << ((size - 1).bit_length() - 1)


def _check_leaves(leaves: Sequence[bytes]) -> None:
    if any(len(leaf) != HASH_SIZE for leaf in leaves):
        raise MerkleError("leaf hashes must be 32 bytes", code="merkle.bad_hash")


def _mth(leaves: Sequence[bytes]) -> bytes:
    if len(leaves) == 1:
        return leaves[0]
    k = _split(len(leaves))
    return node_hash(_mth(leaves[:k]), _mth(leaves[k:]))


def root_hash(leaves: Sequence[bytes]) -> bytes:
    """MTH over leaf hashes; the empty tree's root is SHA-256 of the empty string."""
    _check_leaves(leaves)
    return _mth(leaves) if leaves else EMPTY_ROOT


def _path(index: int, leaves: Sequence[bytes]) -> list[bytes]:
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if index < k:
        return [*_path(index, leaves[:k]), _mth(leaves[k:])]
    return [*_path(index - k, leaves[k:]), _mth(leaves[:k])]


def inclusion_proof(index: int, leaves: Sequence[bytes]) -> list[bytes]:
    """Audit path for leaf ``index`` in the tree of ``len(leaves)`` leaves."""
    if not 0 <= index < len(leaves):
        raise MerkleError(f"index {index} not in a tree of {len(leaves)}", code="merkle.bad_index")
    _check_leaves(leaves)
    return _path(index, leaves)


def _subproof(old: int, leaves: Sequence[bytes], complete: bool) -> list[bytes]:
    if old == len(leaves):
        return [] if complete else [_mth(leaves)]
    k = _split(len(leaves))
    if old <= k:
        return [*_subproof(old, leaves[:k], complete), _mth(leaves[k:])]
    return [*_subproof(old - k, leaves[k:], False), _mth(leaves[:k])]


def consistency_proof(old_size: int, leaves: Sequence[bytes]) -> list[bytes]:
    """Proof that the tree of ``old_size`` leaves is a prefix of the tree of ``leaves``."""
    if not 0 <= old_size <= len(leaves):
        raise MerkleError(f"old size {old_size} not in 0..{len(leaves)}", code="merkle.bad_size")
    _check_leaves(leaves)
    if old_size == 0:
        return []  # every tree extends the empty tree
    return _subproof(old_size, leaves, True)


def _is_hash(value: bytes) -> bool:
    return isinstance(value, bytes) and len(value) == HASH_SIZE


def verify_inclusion(
    leaf: bytes, index: int, size: int, proof: Sequence[bytes], root: bytes
) -> bool:
    """RFC 9162 §2.1.3.2."""
    if not 0 <= index < size or not all(map(_is_hash, [leaf, root, *proof])):
        return False
    fn, sn, r = index, size - 1, leaf
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            while not fn & 1 and fn != 0:
                fn, sn = fn >> 1, sn >> 1
        else:
            r = node_hash(r, p)
        fn, sn = fn >> 1, sn >> 1
    return sn == 0 and hmac.compare_digest(r, root)


def verify_consistency(
    old_size: int, new_size: int, old_root: bytes, new_root: bytes, proof: Sequence[bytes]
) -> bool:
    """RFC 9162 §2.1.4.2, plus the trivial cases for an empty or unchanged old tree."""
    if not 0 <= old_size <= new_size or not all(map(_is_hash, [old_root, new_root, *proof])):
        return False
    if old_size == 0:
        return not proof and hmac.compare_digest(old_root, EMPTY_ROOT)
    if old_size == new_size:
        return not proof and hmac.compare_digest(old_root, new_root)
    if not proof:
        return False
    path = list(proof)
    if old_size & (old_size - 1) == 0:  # old tree is complete: its root is the first node
        path.insert(0, old_root)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn, sn = fn >> 1, sn >> 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr, sr = node_hash(c, fr), node_hash(c, sr)
            while not fn & 1 and fn != 0:
                fn, sn = fn >> 1, sn >> 1
        else:
            sr = node_hash(sr, c)
        fn, sn = fn >> 1, sn >> 1
    return sn == 0 and hmac.compare_digest(fr, old_root) and hmac.compare_digest(sr, new_root)
