"""The ``TreeStore`` port, and an in-memory adapter.

The port is read-only: the tree never writes. Whatever owns the leaves (the ledger's
Postgres table, a tile store, a file) exposes them through an adapter, and the leaf at index
``i`` never changes once written.
"""

from typing import Protocol

from common.errors import MerkleError
from merkle.hashing import HASH_SIZE


class TreeStore(Protocol):
    async def size(self) -> int:
        """Number of leaves currently stored."""
        ...

    async def leaf_hashes(self, start: int, end: int) -> list[bytes]:
        """Leaf hashes for indexes ``start`` (inclusive) to ``end`` (exclusive)."""
        ...


class MemoryTreeStore:
    """Process-local adapter for tests and the offline demo."""

    def __init__(self) -> None:
        self._leaves: list[bytes] = []

    def append(self, leaf: bytes) -> None:
        if len(leaf) != HASH_SIZE:
            raise MerkleError("leaf hashes must be 32 bytes", code="merkle.bad_hash")
        self._leaves.append(leaf)

    async def size(self) -> int:
        return len(self._leaves)

    async def leaf_hashes(self, start: int, end: int) -> list[bytes]:
        return self._leaves[start:end]
