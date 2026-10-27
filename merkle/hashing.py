"""RFC 9162 §2.1.1 hashing with SHA-256. The one place leaf and node hashes are defined."""

import hashlib

from common.errors import MerkleError

HASH_SIZE = 32
# Domain separation (skill §4): a leaf can never be passed off as an interior node.
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_ROOT = hashlib.sha256(b"").digest()


def leaf_hash(data: bytes) -> bytes:
    """``SHA-256(0x00 ‖ data)``."""
    return hashlib.sha256(LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """``SHA-256(0x01 ‖ left ‖ right)``."""
    if len(left) != HASH_SIZE or len(right) != HASH_SIZE:
        raise MerkleError("node children must be 32-byte hashes", code="merkle.bad_hash")
    return hashlib.sha256(NODE_PREFIX + left + right).digest()
