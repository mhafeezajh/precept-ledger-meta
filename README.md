# Precept Ledger

Gate and tamper-evident ledger for AI agent actions in regulated finance. Entry for the Meta Global AI Developer Hackathon (Oct 27 – Nov 4, 2026), Indie Developer track. Licence: Apache-2.0.

Work in progress. Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Knowledge base (one page per module): [`docs/modules/`](docs/modules/README.md). Plan: [`docs/HACKATHON_KICKOFF.md`](docs/HACKATHON_KICKOFF.md). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Module status

| Module | Status | Docs |
| --- | --- | --- |
| `ledger/` | Partial. Canonical records, producer signatures, hash chain, receipts, memory and Postgres stores, C2SP signed checkpoints, the Postgres tree adapter, the HTTP API (`pl-ledger`) and the 10 s checkpoint loop done. Batch append and read auth later | [`docs/modules/ledger.md`](docs/modules/ledger.md), [`docs/modules/checkpoints.md`](docs/modules/checkpoints.md), [`docs/modules/api.md`](docs/modules/api.md) |
| `merkle/` | Done for now. RFC 9162 core, inclusion and consistency proofs, `TreeStore` port, memory adapter | [`docs/modules/merkle.md`](docs/modules/merkle.md) |
| `verify/` (`pl-verify`) | Done for now. Export bundle v1, `pl-export`, offline `pl-verify` naming the first broken seq and record; witness quorum later | [`docs/modules/verify.md`](docs/modules/verify.md) |
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
make check              # lint, strict types, 402 unit + property tests (coverage ≥ 90%), secret scan
cp .env.example .env    # set the passwords
make test-integration   # Postgres 16 via docker compose; 18 tests incl. demo beat 4 end to end
                        # (edit a row, export, pl-verify names the record)
make vectors            # regenerate tests/vectors/ledger-v1.json and checkpoint-v1.json
make dev-keys           # keys in KEYS_DIR (outside the repo), incl. trusted.json for pl-verify
make serve              # ledger API on 127.0.0.1:8080, checkpoint every 10 s
uv run pl-export --out export.tar && uv run pl-verify export.tar --keys "$KEYS_DIR/trusted.json"
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
| Oct 27 | `ledger/checkpoint.py`, `ledger/checkpointer.py`, `ledger/pg_tree.py`: C2SP signed checkpoints, consistency-checked checkpointer, write-once directory and Postgres sinks, `checkpoint` table, Postgres and memory tree adapters; vectors in `tests/vectors/checkpoint-v1.json` and `signed-note-published.json` (D29) |
| Oct 27 | `ledger/canonical.py`: canonical JSON, encodings and hashes split out of `ledger/format.py` (size limit), no format change |
| Oct 27 | `docs/modules/README.md`: knowledge-base index and table of every signing context |
| Oct 27 | `ledger/export.py`, `ledger/export_cli.py` (`pl-export`), `verify/` (`pl-verify`): export bundle v1, hostile-input bundle reader, trusted keys, independent chain/receipt/checkpoint checks, CLI; tamper suite, differential test against `ledger.chain`, network-forbidden subprocess test |
| Oct 27 | `ledger/api.py`, `ledger/checkpoint_loop.py`, `ledger/server.py` (`pl-ledger`), `common/logging.py`, `scripts/dev_keys.py`: ledger HTTP API, 10 s checkpoint loop, JSON logs with trace ids, dev keys |

Meta tooling used so far: none. Model calls start with Gate and the demo agent (day 4). Muse Code will be used for at least one slice and recorded here.
