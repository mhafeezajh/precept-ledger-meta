"""RFC 9162 Merkle tree: a pure core plus a storage port.

``hashing`` and ``proofs`` are pure (stdlib + ``common.errors``) and are what the offline
verifier, witnesses and claim checker import. ``store`` defines the ``TreeStore`` port that
storage adapters implement; ``log`` computes heads and proofs through that port. This package
knows nothing about ledger records: callers hand it leaf hashes.
"""
