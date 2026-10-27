"""Tree heads and proofs computed through a ``TreeStore`` adapter.

Each call reads the leaves it needs and runs the pure functions in ``merkle.proofs``. That is
O(n) per call, which is fine at demo scale (thousands of records); an adapter that caches
complete subtrees can replace it later without changing this interface.
"""

from dataclasses import dataclass

from common.errors import MerkleError
from merkle.proofs import consistency_proof, inclusion_proof, root_hash
from merkle.store import TreeStore


@dataclass(frozen=True)
class TreeHead:
    size: int
    root: bytes


@dataclass(frozen=True)
class InclusionProof:
    index: int
    size: int
    hashes: list[bytes]


@dataclass(frozen=True)
class ConsistencyProof:
    old_size: int
    new_size: int
    hashes: list[bytes]


class LogTree:
    def __init__(self, store: TreeStore) -> None:
        self._store = store

    async def head(self, size: int | None = None) -> TreeHead:
        leaves = await self._leaves(size)
        return TreeHead(len(leaves), root_hash(leaves))

    async def prove_inclusion(self, index: int, size: int) -> InclusionProof:
        leaves = await self._leaves(size)
        return InclusionProof(index, size, inclusion_proof(index, leaves))

    async def prove_consistency(self, old_size: int, new_size: int) -> ConsistencyProof:
        leaves = await self._leaves(new_size)
        return ConsistencyProof(old_size, new_size, consistency_proof(old_size, leaves))

    async def _leaves(self, size: int | None) -> list[bytes]:
        stored = await self._store.size()
        wanted = stored if size is None else size
        if not 0 <= wanted <= stored:
            raise MerkleError(f"size {wanted} not in 0..{stored}", code="merkle.bad_size")
        leaves = await self._store.leaf_hashes(0, wanted)
        if len(leaves) != wanted:
            # Fail closed (R4): never build a head from a short or padded read.
            raise MerkleError(
                f"store returned {len(leaves)} leaves, expected {wanted}", code="merkle.bad_store"
            )
        return leaves
