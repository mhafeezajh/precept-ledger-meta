# Precept Ledger

Gate and tamper-evident ledger for AI agent actions in regulated finance. Entry for the Meta Global AI Developer Hackathon (Oct 27 – Nov 4, 2026), Indie Developer track. Licence: Apache-2.0.

Work in progress. Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Plan: [`docs/HACKATHON_KICKOFF.md`](docs/HACKATHON_KICKOFF.md). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Module status

| Module | Status | Docs |
| --- | --- | --- |
| `ledger/` | Partial. Canonical records, producer signatures, hash chain, receipts, memory and Postgres stores done. Checkpoints and the Postgres tree adapter next | [`docs/modules/ledger.md`](docs/modules/ledger.md) |
| `merkle/` | Done for now. RFC 9162 core, inclusion and consistency proofs, `TreeStore` port, memory adapter | [`docs/modules/merkle.md`](docs/modules/merkle.md) |
| `verify/` (`pl-verify`) | Not started | |
| `witness/` | Not started | |
| `anchor/` | Not started | |
| `gate/` | Not started | |
| `policy/` | Not started | |
| `assist/` | Not started | |
| `claims/` | Not started | |
| `learn/` | Not started | |
| `agent/` | Not started | |
| `web/` | Not started | |

## How to run what works now

Requires Python 3.12, [`uv`](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
make check              # lint, strict types, 205 unit + property tests (coverage ≥ 90%), secret scan
cp .env.example .env    # set the passwords
make test-integration   # Postgres 16 via docker compose; 7 tests incl. an admin edit caught at its seq
make vectors            # regenerate tests/vectors/ledger-v1.json
```

## Built during the hackathon (Oct 27 – Nov 4)

Every line of code in this repository was written during the building period.

| Date | Component |
| --- | --- |
| Oct 27 | Repo scaffold: `pyproject.toml`, `Makefile`, `docker-compose.yml` (Postgres), Postgres init scripts, `common/` (errors, clock, ULIDs, config), `docs/DECISIONS.md` |
| Oct 27 | `ledger/format.py`: record format v1, RFC 8785 canonical JSON, strict I-JSON parsing, hash formulas, signing inputs |
| Oct 27 | `ledger/signing.py`: Ed25519 signers, producer signatures, key ring |
| Oct 27 | `ledger/chain.py`, `ledger/receipts.py`: hash chain, stream verification with first-break reporting, signed receipts, conflicting-receipt detection |
| Oct 27 | `ledger/store.py`, `ledger/service.py`, `ledger/pg_store.py`, `ledger/schema.sql`: append service, memory store, Postgres store with insert-only triggers |
| Oct 27 | Tests: unit, property (`hypothesis`), tamper suite, test vectors (`tests/vectors/ledger-v1.json`), Postgres integration |
| Oct 27 | `merkle/`: RFC 9162 hashing, roots, inclusion and consistency proofs, `TreeStore` port, `LogTree`, memory adapter; vectors in `tests/vectors/merkle-rfc9162.json` (published data, see D26) |

Meta tooling used so far: none. Model calls start with Gate and the demo agent (day 4). Muse Code will be used for at least one slice and recorded here.
