# TEST ONLY keys

**These keys are public and worthless. Never use them outside tests and the published test vectors.**

`test-keys.json` holds 32-byte Ed25519 seeds. Each seed is `SHA-256("PL TEST ONLY <name>")`, so anyone can
re-derive them and reproduce `tests/vectors/`. Real keys live in `KEYS_DIR`, outside the repo.
