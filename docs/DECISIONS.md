# Decisions

Each entry: ID, decision, alternatives rejected, why, date. Newest decisions go at the bottom of each table.
Entries marked **default** were adopted from Claude's recommendation when the owner said "start building"
without answering the question. The owner can overturn them; the code changes with them.

## Hard rules (from the kickoff, section 0)

These are not up for trade-off. Every module is checked against them before it is called done.

| ID | Rule |
| --- | --- |
| H1 | **New work only.** Every line of code is written from Oct 27, 2026 onward; the demo shows only work built during the event. No code from `precept-insight` or any other private source. The README's "Built during the hackathon" section lists every component. |
| H2 | **Commercial core stays out.** No commercial Gate runtime, bundle signing, SPIFFE/mTLS or Terraform. |
| H3 | **Code decides, models extract.** No model call in the allow / deny / escalate path, in verification or in the claim checker. |
| H4 | **Fail closed.** Ledger write fails → action does not run. Model output fails schema or proof checks → rejected, never silently repaired. |
| H5 | **Write before effect.** An allowed tool call reaches the tool only after its decision record has a ledger receipt. |
| H6 | **Meta models only, through LiteLLM, Standard mode.** Contributor mode is never enabled. |
| H7 | **Public repo, Apache-2.0, from day 1.** Small, frequent commits; history is evidence of new work. |

## Architecture decisions (from `docs/ARCHITECTURE.md`)

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D1 | Transparency log with witness quorum and external anchors | Public blockchain; permissioned chain (Hyperledger Fabric) | Same tamper guarantees without exposing data, fees or running consensus nodes in 9 days | 2026-10-25 |
| D2 | Python for the whole build | Go, as in the product | Speed for a solo 9-day build; AI libraries are Python-native | 2026-10-25 |
| D3 | Postgres with insert-only triggers | SQLite; an immutable database product | One engine, real permission model for the tamper demo | 2026-10-25 |
| D4 | C2SP checkpoint and cosignature formats | Custom format | Interoperates with existing witness networks and tooling | 2026-10-25 |
| D5 | 2-of-3 witness quorum | 1 witness; 3 of 3 | Survives one witness being down; one witness alone is a single point of trust | 2026-10-25 |
| D6 | Cedar via `cedarpy` | OPA; custom rule engine | Formal analysis, same engine as the product | 2026-10-25 |
| D7 | New repository, Apache 2.0 | Reuse the Precept Insight monorepo | Hackathon new-work rule and the organizers' licence to submissions | 2026-10-25 |
| D8 | Learner proposes, humans approve; counts from SQL, text from the model | Self-updating rules; training a model on the logs | Keeps code in charge of decisions; every lesson is provable and reversible | 2026-10-25 |

## Ledger record format (step 1, Oct 27)

These resolve conflicts C1–C9 found between the kickoff, `docs/ARCHITECTURE.md` and the engineering skill.

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D9 (C1, **default**) | The producer signs the record **without** `seq`, `prev_hash` and `producer.sig`. The ledger assigns `seq` and `prev_hash`; the chain and leaf hashes cover the full stored record including the signature. | Producer signs the whole record minus `sig` (as ARCHITECTURE first read) | The producer cannot know `seq`/`prev_hash` without locking the ledger head, which breaks concurrent appends. Consequence: the operator can reorder or drop records and recompute the chain without breaking producer signatures; that attack is caught by receipts held by producers (signed `seq` + `chain_hash`) and, from day 2–3, by checkpoints and witnesses. Tested in the tamper suite. | 2026-10-27 |
| D10 (C2, **default**) | Envelopes carry `alg`: `producer.alg` on records, top-level `alg` on receipts. Only `ed25519` is accepted. | No `alg` field | Algorithm agility (skill §4); a later hybrid with ML-DSA needs no hash change. Unknown `alg` is rejected, never guessed. | 2026-10-27 |
| D11 (C3, **default**) | Receipt = `{v, alg, stream, record_id, seq, leaf_hash, chain_hash, received_at, key_id, sig}`, table `receipt`. | Kickoff's `ledger_receipt(record_id, seq, hash, signature, key_id)` | Follows ARCHITECTURE; `stream` added because `seq` is only unique within a stream; `v`/`alg`/`key_id` make the receipt self-describing. | 2026-10-27 |
| D12 (C4, **default**) | Record types are dotted names: `gate.decision`, `gate.approval`, `policy.approve`, `qa.answer`, … | Bare `decision` | Matches ARCHITECTURE; namespaces by producing component. | 2026-10-27 |
| D13 (C5, **default**) | Test vectors live in `tests/vectors/`. | `verify/testdata/` | Skill §6; one place for all vectors. | 2026-10-27 |
| D14 (C6, **default**) | `docs/ARCHITECTURE.md` is kept as exported and gets a change log; it is not regenerated from kickoff section 2. | Rewrite it from the kickoff | The export is the more detailed source of truth. | 2026-10-27 |
| D15 (C7, **default**) | Cut order follows ARCHITECTURE (on-prem → OpenTimestamps → MCP proxy → learner tightening). mypy `--strict` uses the skill's wider scope (`ledger verify witness claims gate anchor`, plus `common`). | Skill's cut order; kickoff's narrower mypy scope | ARCHITECTURE is the source of truth for plan; stricter typing costs nothing. | 2026-10-27 |
| D16 (C8, **default**) | First record in a stream has `seq = 1` and `prev_hash = h_0` (32 zero bytes). Merkle leaf index = `seq − 1`. | `seq = 0` first | Matches the formula's `h_0` genesis value. | 2026-10-27 |
| D17 (C9, **default**) | Records allow integers only, within ±(2^53 − 1); no floats. Amounts are whole dollars as in the existing examples. | Floats; decimal strings; integer cents | Floats are risky for money and for cross-language canonicalization. Whole dollars match every example in ARCHITECTURE. | 2026-10-27 |
| D18 | Signing inputs get context strings: records `"PL-record-v1\x00"`, receipts `"PL-receipt-v1\x00"`, prepended to the JCS bytes. | Sign bare JCS bytes | Domain separation (skill §4): a record signature can never be replayed as a receipt signature or vice versa. ARCHITECTURE did not name these strings; added to its change log. | 2026-10-27 |
| D19 | `key_id = "ed25519:" + hex(SHA-256(raw 32-byte public key))`. Hashes and signatures are standard base64 with padding, and must be the canonical encoding (re-encoding must give the same text). | Base64url; hex for hashes | ARCHITECTURE shows `ed25519:9f2c…` (hex) and `base64...`; C2SP notes use standard base64. Canonical-encoding check closes the gap where two base64 strings decode to the same bytes, so a one-byte edit can never go unnoticed. | 2026-10-27 |
| D20 | Exports and the verifier read records as canonical JSON lines; a line that is not byte-identical to its own canonical form is rejected at its sequence. | Accept any JSON that parses | Makes "any single-byte edit fails at its sequence" hold for every byte, including whitespace and escapes. | 2026-10-27 |
| D21 | Postgres stores records as typed columns plus `body jsonb`, and the record is rebuilt from those columns for verification. | Store the canonical bytes as an authoritative blob | Demo beat 4 edits a row directly; that edit must change what the verifier sees. A blob next to editable columns would hide the edit. | 2026-10-27 |
| D22 | Appends are serialized per stream with `pg_advisory_xact_lock`, backed by `UNIQUE (stream, seq)` and `UNIQUE (record_id)`. | `SERIALIZABLE` retries; a head table with `FOR UPDATE` | Simplest correct gap-free sequence; the unique constraints make any bug fail loudly. | 2026-10-27 |
| D23 | The ledger store is async (`psycopg` 3 async pool); `Ledger.append` is a coroutine. | Sync store now, async later | FastAPI handlers are async and must not block (skill §3); avoids a rewrite on day 2. | 2026-10-27 |
| D24 | ULIDs are generated by `common/ids.py` (20 lines) with an injected clock and `secrets`. | `python-ulid` | Removes a dependency; keeps time injectable and randomness on `secrets` (skill §3, §4). | 2026-10-27 |

## Merkle tree (step 2, Oct 27)

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D25 | Merkle is its own top-level package `merkle/`: a pure core (`hashing`, `proofs`) plus a read-only `TreeStore` port and `LogTree` over it. Adapters live with the data they read (the Postgres one in `ledger/`). `ledger.format` takes leaf hashing and the `0x00`/`0x01` prefixes from `merkle.hashing`. | `ledger/merkle.py`, as skill §2 lays out | Owner direction. The verifier side depends on a small pure core, not on the ledger; storage can change (tiles, another log) by writing an adapter. Skill §2 still says `ledger.merkle`; it is read-only, so the owner updates it. | 2026-10-27 |
| D26 | Merkle vectors: leaf inputs, node hashes and roots from transparency-dev/merkle `testonly/constants.go` (Apache-2.0); expected proofs derived by hand from those node hashes per RFC 9162 and listed with node coordinates. A property test also checks roots against a direct, unoptimized transcription of the RFC's MTH definition. | Self-generated vectors only | Published data catches errors that self-consistency cannot. This is test data, not code (H1). | 2026-10-27 |
| D27 | Proof verification returns `bool` and treats every malformed input as `False`; proof generation raises `MerkleError`. | Raise on bad input during verification | A verifier with no exception paths cannot be steered into skipping a check (R4). | 2026-10-27 |

## Checkpoints (step 3, Oct 27)

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D28 | Checkpoints are C2SP tlog-checkpoint text in a C2SP signed note, signed with the log key; the key name is the origin `<LEDGER_ORIGIN>/<stream>`. The format code is the pure module `ledger/checkpoint.py`, added to the verifier-side import allowlist. Note text is stricter than the spec: no blank lines and no Unicode control characters except newline. Because the signature has no context prefix (the spec signs the bare text), domain separation from receipts and records under the same key comes from their signing inputs containing NUL, which note text may not. | A JSON checkpoint with a `PL-checkpoint-v1` context; a separate checkpoint key; putting the code in `ledger/format.py` | D4 interoperability with witness tooling; one tree per stream because `seq` is per stream (D16); `format.py` is already over 400 lines. Skill §2 lists only `ledger.format` and `ledger.merkle` for the verifier; the owner needs to add `ledger.checkpoint` there (the skill file is read-only). | 2026-10-27 |
| D29 | Signed-note vectors: the published example from the Go `golang.org/x/mod/sumdb/note` documentation (public verifier key and signature only), kept hand-maintained in `tests/vectors/signed-note-published.json`. Our own checkpoint vector commits to the Merkle root of the three `ledger-v1.json` records and is generated by `make vectors`. | Self-generated vectors only | A published vector catches key-hash and line-format mistakes that a round trip cannot (same reasoning as D26). Test data, not code (H1). | 2026-10-27 |
| D30 | The checkpointer refuses to sign a tree that does not extend its last stored checkpoint (consistency proof), and writes every checkpoint to all sinks on every publish: the insert-only `checkpoint` table and a write-once directory, one file per tree size. Writes are idempotent; a different note for a stored size raises `checkpoint.conflict`. The `checkpoint` table stores the full signed note and no separate `log_signature` column. | Trust the database head and sign it; a `log_signature` column as in ARCHITECTURE | Defence in depth for demo beat 4: a rewritten table cannot get a fresh signature from the honest process. Rewriting all sinks lets a failed sink heal. One authoritative `note` column avoids two copies that could disagree after an edit. | 2026-10-27 |

## Export and pl-verify (step 4, Oct 27)

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D31 | Export bundle v1 is one uncompressed, deterministic tar: `manifest.json`, `records.jsonl`, `receipts.jsonl`, `checkpoints/*.note`. Always the whole stream from seq 1. It carries no keys; trusted keys come from the auditor in a separate JSON file (`{v, producers: {id: [keys]}, log: [keys]}`). The reader treats it as hostile: regular files with expected names only, size and count limits, nothing extracted. | A directory; zip; keys inside the bundle; range exports now | ARCHITECTURE names `export.tar`. Keys in the bundle would let the audited party choose what the auditor trusts. Whole-stream export makes every checkpoint checkable by recomputing roots, so no proofs need to travel yet; ranges are a known gap. | 2026-10-27 |
| D32 | `pl-verify` is a second, independent implementation of the chain, receipt and checkpoint checks, built only on `ledger.format`, `ledger.checkpoint` and `merkle.proofs`. Record findings use the same names and order as `ledger.chain`, and a property test requires both to name the same first break. The chain walk stops at the first break; receipts and checkpoints are still checked against the records as exported. | Reuse `ledger.chain` in the verifier; report every cascading break | Skill §2 import rule; two implementations that must agree catch bugs in either. Stopping the walk avoids one edit producing a page of false breaks, while receipts and checkpoints still point at the edited record independently. | 2026-10-27 |
| D33 | Exit codes: 0 verified, 1 findings, 2 cannot verify. `--trusted-checkpoint` takes a signed checkpoint the auditor kept; a held checkpoint that does not verify is a usage error (2), never a pass. The offline guarantee is tested two ways: no network imports in `verify/`, and a subprocess run under an audit hook that raises on any `socket.*` event, with a positive control. | One failure exit code; only the import test | "Could not check" must never read as "checked" (R4). An audit hook catches network use from any imported library, not just direct imports. | 2026-10-27 |

## Ledger API (step 5, Oct 27)

| ID | Decision | Rejected | Why | Date |
| --- | --- | --- | --- | --- |
| D34 | The append endpoint validates bodies with the ledger's own strict parser (`Ledger.append_json`: 64 KiB limit, duplicate keys, floats and unknown fields rejected), not a Pydantic model. Query parameters use FastAPI/Pydantic. Validation errors get our error body (`request.invalid`), never FastAPI's, which echoes the input. | A Pydantic `Submission` model | Skill §7 asks for Pydantic at boundaries, but the bytes that are signed must be parsed exactly once, strictly; Pydantic would accept duplicate keys and floats. Recorded here as a deliberate exception. | 2026-10-27 |
| D35 | Errors map from their machine code to a status: 400 format and request problems, 403 signatures, 404 not found, 409 duplicates and not yet checkpointed, 413 size, 415 media type, 503 store and the ledger's own integrity failures, 500 for everything else with no detail. `POST` takes one record per request. A proof's `size` must be a stored checkpoint size, and the response includes that signed checkpoint. | 500 for integrity failures; batch append now; proofs against any tree size | A client must be able to tell "my fault" from "the ledger cannot vouch for itself". One record per request keeps "2xx means receipt" exact. A proof against an unsigned size proves nothing to an auditor. | 2026-10-27 |
| D36 | A background `CheckpointLoop` checkpoints every served stream every `CHECKPOINT_INTERVAL_SECONDS` (10). A `PreceptError` skips that stream for one tick and is logged; any other exception stops the loop, and `/ledger/v1/health` returns 503 `stopped`. | Checkpoint on every append; swallow all exceptions and keep looping | ARCHITECTURE flow D. A bug must be visible, not retried forever in silence (R4). | 2026-10-27 |
| D37 | `pl-ledger` reads `KEYS_DIR/log.pem` and `KEYS_DIR/trusted.json`; the latter is the same format as `pl-verify --keys`, parsed by `verify.keys.TrustedKeys` (so `ledger.server` imports `verify.keys`). It refuses to start if the log key is not listed there. `scripts/dev_keys.py` creates development keys outside the repo, never overwrites and prints only key ids. Bind address defaults to 127.0.0.1. | Separate key formats for the ledger and the verifier; generating keys at server start | One public-key file that both the ledger and auditors use cannot drift. Keys generated at start would silently change identity on restart. Reads are unauthenticated in the demo. | 2026-10-27 |

## Dependencies

| Package | Version | Licence | Why | Added |
| --- | --- | --- | --- | --- |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Ed25519, SHA-256 | 2026-10-27 |
| `rfc8785` | 0.1.4 | Apache-2.0 | RFC 8785 canonical JSON | 2026-10-27 |
| `psycopg[binary]` | 3.3.6 | LGPL-3.0 | Postgres driver (used unmodified as a library) | 2026-10-27 |
| `psycopg-pool` | 3.3.3 | LGPL-3.0 | Async connection pool | 2026-10-27 |
| `pydantic` | 2.14.1 | MIT | Boundary validation (API, model outputs) | 2026-10-27 |
| `pydantic-settings` | 2.16.1 | MIT | `common/config.py` | 2026-10-27 |
| `pytest` (dev) | 9.1.1 | MIT | Tests | 2026-10-27 |
| `pytest-asyncio` (dev) | 1.4.1 | Apache-2.0 | Async tests | 2026-10-27 |
| `pytest-cov` (dev) | 7.1.0 | MIT | Coverage floors | 2026-10-27 |
| `hypothesis` (dev) | 6.170.12 | MPL-2.0 | Property tests | 2026-10-27 |
| `ruff` (dev) | 0.17.3 | MIT | Lint and format | 2026-10-27 |
| `mypy` (dev) | 2.4.0 | MIT | Type checking | 2026-10-27 |
| `fastapi` | 0.162.0 | MIT | Ledger API (and later witness, Gate, web) | 2026-10-27 |
| `uvicorn` | 0.54.0 | BSD-3-Clause | ASGI server for `pl-ledger` | 2026-10-27 |
| `starlette` (via fastapi) | 1.10.0 | BSD-3-Clause | FastAPI's foundation | 2026-10-27 |
| `opentelemetry-api` (via fastapi) | 1.45.1 | Apache-2.0 | Pulled in by fastapi; not used directly | 2026-10-27 |
| `httpx` (dev) | 0.28.1 | BSD-3-Clause | In-process API tests | 2026-10-27 |
