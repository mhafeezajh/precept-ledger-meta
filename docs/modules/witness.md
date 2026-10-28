# Witnesses (`witness/`, `pl-witness`) and witnessing in the ledger

Guarantees that no checkpoint becomes final unless at least 2 of 3 independent witnesses checked that it extends the history they cosigned before. An operator who holds the log key can sign a rewritten history, but cannot get it cosigned, and an auditor can prove that offline.

Status: done for this step. Covers the cosignature format, three witness services, 2-of-3 finality in the ledger, cosignatures in the API and export, `pl-verify --witness-keys --quorum`, and demo beat 5 as a command. Still missing: docker compose services, and a witness on a separate host (O4). · Last updated: 2026-10-27 · Tests: 99 unit + property (`witness/` 92–100%, `ledger/cosignature.py` 99%, `ledger/witnessing.py` 95%, `ledger/witness_client.py` 100%), 1 integration over Postgres and real HTTP, plus a live run with three `pl-witness` processes (below)

Related pages: [`checkpoints.md`](checkpoints.md) (what gets cosigned), [`api.md`](api.md) (where cosignatures are served), [`verify.md`](verify.md) (how they are checked offline).

## Responsibilities

Owns:

| Part | File | Runs in |
| --- | --- | --- |
| Cosignature format (C2SP tlog-cosignature) and the add-checkpoint request body (C2SP tlog-witness) | `ledger/cosignature.py`, pure | Witnesses, ledger, verifier |
| The witness: check, save, cosign | `witness/core.py` | Each witness process |
| Witness storage: the last cosigned checkpoint per log, plus refused checkpoints as evidence | `witness/state.py` | Each witness process |
| Witness key and trusted log keys | `witness/keys.py` | Each witness process |
| Witness HTTP API and `pl-witness` | `witness/api.py`, `witness/server.py` | Each witness process |
| Asking witnesses, verifying their answers, storing cosignatures, finality | `ledger/witnessing.py`, `ledger/witness_client.py` | Ledger |
| `cosignature` table | `ledger/schema.sql`, `ledger/pg_tree.py` | Ledger database |
| Quorum checks in `pl-verify` | `verify/keys.py` (`WitnessPolicy`), `verify/checks.py` | Auditor's machine |
| Beat 5 demo | `scripts/demo_rogue_operator.py`, `make demo-rogue` | Anywhere |

Does not own:

- Checkpoint signing. That is `ledger/checkpointer.py`; a witness never signs a checkpoint, only cosigns one.
- Timestamps from outside parties (RFC 3161, OpenTimestamps). Those belong to `anchor/` on day 4. A cosignature's time is the witness's own claim.

A witness imports only `ledger.checkpoint`, `ledger.cosignature`, `ledger.canonical` and the `merkle` core. It cannot reach the ledger's database or its internals; the import test enforces this.

## How it works

1. Every 10 seconds the ledger signs a checkpoint (as before).
2. For each witness that has not cosigned this tree size yet, the ledger sends `old <size>`, a consistency proof from that size to the new one, and the signed checkpoint. All three requests go out at once, each with a timeout.
3. The witness checks, in this order (C2SP tlog-witness):
   - the note is a checkpoint of a log it follows, signed by that log's key;
   - `old` is not beyond the new size;
   - `old` equals the size it last cosigned;
   - the proof shows the new tree extends the old one.

   Only then does it save the new checkpoint and return its cosignature. Steps 3 and 4 and the save run under one lock.
4. If `old` is wrong, the witness answers `409` with its real size. The ledger retries once from that size, which is how a restarted ledger catches up.
5. The ledger verifies each returned line under that witness's trusted key, then stores it. With 2 of 3 stored, the checkpoint is final.
6. If the history was rewritten, the proof cannot verify. The witness refuses (`422`), keeps the rejected checkpoint as evidence and logs a `CRITICAL` alert. The log key signed the checkpoint and the witness holds the earlier one it cosigned; together they prove the operator signed two incompatible histories.

```mermaid
sequenceDiagram
    participant C as Checkpoint loop (ledger)
    participant T as Merkle tree
    participant W as Witness 1..3 (own key, own state)
    participant S as cosignature table
    C->>C: sign checkpoint (size N)
    par each witness without a cosignature for N
        C->>T: consistency proof (last size M → N)
        C->>W: POST /witness/v1/cosign "old M", proof, note
        alt M is not what W last cosigned
            W-->>C: 409 text/x.tlog.size "K"
            C->>W: retry from K
        end
        W->>W: verify log signature, proof; save N
        W-->>C: 200 "— witness-k <cosignature>"
        C->>C: verify the line under witness-k's key
        C->>S: insert (stream, N, witness-k, line)
    end
    C->>C: final if 2 of 3 lines verify, else witness.no_quorum
```

## Interfaces

### Witness HTTP API (`pl-witness`)

| Method and path | Input | Output | Errors |
| --- | --- | --- | --- |
| `POST /witness/v1/cosign` (also `/witness/v1/add-checkpoint`, the C2SP path) | Text body, at most 70 KiB: `old <size>\n`, up to 63 base64 hashes, one per line, `\n`, then the checkpoint note | `200 text/plain`: one signature line | `400 witness.bad_request`, `witness.bad_old_size`; `403 witness.bad_signature`; `404 witness.unknown_log`; `409` `text/x.tlog.size` body `"<size>\n"`; `413 format.too_large`; `422 witness.inconsistent`; `503 witness.state_invalid` |
| `GET /witness/v1/latest?origin=` | `origin` is optional when the witness follows exactly one log | `200 text/plain`: the last checkpoint this witness cosigned, with its cosignature. Auditors keep this as `--trusted-checkpoint` | `400 request.origin_required`; `404 witness.unknown_log`, `witness.not_found` |
| `GET /witness/v1/health` | | `{status, name, cosigner_key, logs: [{origin, size}]}` | `503` with `status: failing` if saved state does not verify |

The `409` and success bodies follow C2SP tlog-witness exactly. All other errors use the ledger's JSON error body. Every response carries `x-trace-id`.

### Pure functions (`ledger/cosignature.py`)

| Function | Input | Output | Errors |
| --- | --- | --- | --- |
| `cosignature_message(checkpoint, t)` | `Checkpoint`, seconds as an unsigned 64-bit integer | the signed bytes | `FormatError cosignature.malformed` |
| `cosign(checkpoint, signer, name, t)` | | `"— name base64\n"` | |
| `open_cosignatures(note, keys)` | note with any signature lines, trusted `CosignerKey`s | `{name: time}` of the lines that verify; lines from other keys are ignored | `SignatureError cosignature.bad_signature`; `FormatError cosignature.malformed`, `cosignature.duplicate` |
| `add_cosignatures(note, lines)` | | note with the lines appended, without duplicates | `FormatError cosignature.malformed` |
| `CosignerKey.verifier()` / `.from_verifier(text)` | | `name+hex+base64(0x04 ‖ key)` | `FormatError cosignature.bad_key` |
| `format_add_checkpoint` / `parse_add_checkpoint` | | request body / `(old, proof, note)` | `FormatError witness.bad_request` |

### Ledger side

| Function / endpoint | Behaviour |
| --- | --- |
| `Witnessing.collect(signed)` | Asks the witnesses that are missing for this size, verifies each answer, stores it, and returns `WitnessResult(size, cosigners, refused, quorum)` |
| `WitnessedPublisher.publish()` | Checkpoint, then collect. Raises `WitnessError witness.no_quorum` if fewer than `quorum` cosigned; the checkpoint itself is still stored |
| `GET /ledger/v1/checkpoints/latest[?final=true]` | `note` now carries the verified cosignature lines. Adds `cosignatures: [{witness, timestamp}]`, `quorum` and `final`. With `final=true`, returns the latest checkpoint that has a quorum (`404` if none) |
| `GET /ledger/v1/records/{id}/proof` | `checkpoint` is the cosigned note. Adds `final` |
| `GET /ledger/v1/health` | Adds `witnesses: {stream: {count, quorum}}`. `checkpoint_errors` shows `witness.no_quorum` while there is no quorum |
| `GET /ledger/v1/export` | Each `checkpoints/*.note` carries its stored cosignature lines |

### Commands

| Command | What it does |
| --- | --- |
| `pl-witness` | One witness, configured by `WITNESS_*` variables |
| `make witnesses` | Three witnesses on 127.0.0.1:8091–8093, each with its own key and state directory |
| `make dev-keys` | Also creates `witnesses/witness-{1,2,3}.pem`, `witnesses.txt` and `log-verifiers.txt` in `KEYS_DIR` |
| `make demo-rogue` | Demo beat 5 in one process (below) |
| `pl-verify … --witness-keys witnesses.txt [--quorum N]` | Requires `N` (default 2) valid cosignatures on the latest checkpoint |

## Data

**Witness state** (each witness's own `WITNESS_STATE_DIR`, never the ledger database):

| File | Content |
| --- | --- |
| `state/<sha256(origin)[:32]>.json` | `{v: 1, origin, size, root (base64), note, cosignature, timestamp}`. This is the last checkpoint cosigned for that log. It is replaced atomically (temp file, fsync, rename, fsync of the directory) and re-verified each time it is read |
| `refused/<id>-<random>-witness_inconsistent.note` | A log-signed checkpoint this witness refused. It is evidence of the operator signing a history that does not extend the cosigned one |

**Ledger database**: one new insert-only table.

| Column | Meaning |
| --- | --- |
| `stream`, `tree_size` | The checkpoint; foreign key to `checkpoint` |
| `witness` | Cosigner name |
| `line` | The full C2SP signature line, the only column trusted. It is verified before insert, before it is served and when the verifier reads it |

Primary key `(stream, tree_size, witness)`. The first line stored wins. Triggers reject `UPDATE`, `DELETE` and `TRUNCATE`; the app role has `INSERT` and `SELECT` only.

**Key files** in `KEYS_DIR` (outside the repo):

| File | Who reads it | Format |
| --- | --- | --- |
| `witnesses/witness-N.pem` | That witness only | PKCS#8 Ed25519, mode 0600; `pl-witness` refuses a group- or world-readable key |
| `log-verifiers.txt` | Witnesses (`WITNESS_LOGS_FILE`) | One signed-note verifier key per line: `<origin>+<hash>+base64(0x01 ‖ log key)` |
| `witnesses.txt` | `pl-ledger` and `pl-verify --witness-keys` | One cosigner verifier key per line: `<name>+<hash>+base64(0x04 ‖ witness key)` |

**Configuration**:

| Variable | Default | Used by |
| --- | --- | --- |
| `WITNESSES` | empty (no witnessing) | `pl-ledger`: `name=url` pairs, comma-separated |
| `WITNESS_QUORUM` | `2` | `pl-ledger` |
| `WITNESS_TIMEOUT_SECONDS` | `5` | `pl-ledger` |
| `WITNESS_NAME`, `WITNESS_KEY_FILE`, `WITNESS_LOGS_FILE`, `WITNESS_STATE_DIR` | required | `pl-witness` |
| `WITNESS_HOST`, `WITNESS_PORT`, `WITNESS_LOG_LEVEL` | `127.0.0.1`, `8091`, `INFO` | `pl-witness` |

## Security and failure behaviour

| Threat or failure | Behaviour | Test |
| --- | --- | --- |
| Operator rewrites history and re-signs it with the log key (beat 5) | Every witness refuses `witness.inconsistent`. Zero cosignatures, nothing final. The bundle alone still passes `pl-verify --keys`, but `--witness-keys` fails `below_quorum` and a held witness checkpoint fails `rewritten_since_trusted` | `test_rogue_operator_rewrite_gets_no_cosignature`, `test_rogue_operator_is_caught_by_witnesses_and_by_a_held_checkpoint` |
| Operator rolls the log back to a smaller tree | The witness answers `409` or `400` and never cosigns a smaller size. The ledger logs `CRITICAL` `witness.ahead` | `test_rollback_to_a_smaller_tree_is_never_cosigned`, `test_rogue_rollback_to_a_smaller_tree_is_refused` |
| Split view: two roots for one size | `422` at the witness; `checkpoint_conflict` in `pl-verify` | `test_same_size_must_have_the_same_root` |
| Checkpoint signed by another key | `403`; witness state unchanged | `test_checkpoint_not_signed_by_the_log_key_is_refused` |
| Two requests racing from the same old size | Exactly one is cosigned; the other gets `409` | `test_concurrent_requests_from_one_old_size_cosign_once` |
| One witness down | 2 of 3 is still final | `test_one_witness_down_is_still_final`, integration test, live run |
| Two witnesses down, slow, buggy, or returning a line signed by the wrong key | Not final. `witness.no_quorum` appears in health; refusals are recorded as `unreachable`, `timeout`, `error` or `bad_cosignature`. The checkpoint is still stored, and becomes final once witnesses return | `test_two_bad_witnesses_mean_no_quorum` (4 cases), live run |
| Odd or hostile witness HTTP responses | A malformed `409`, a body over 16 KiB or an unknown error code becomes `witness.bad_response` or `witness.http_<status>`. Only `witness.*` codes are passed on | `test_http_client_distrusts_odd_responses` |
| Cosignature row edited in the ledger database | API `503 cosignature.unverifiable`; `pl-verify` `bad_cosignature`. Triggers block the edit for the app role and for admins too | `test_tampered_cosignature_is_503_not_served`, `test_forged_cosignature_is_named`, integration test |
| Cosignatures stripped from the export | `below_quorum` | `test_stripped_cosignatures_fail_the_quorum` |
| Witness state file edited or corrupt | Every request gets `503 witness.state_invalid`; health shows `failing`. The witness never cosigns from state it cannot verify | `test_tampered_state_fails_closed`, `test_corrupt_state_file_fails_closed`, `test_health_reports_identity_and_fails_on_bad_state` |
| Witness restarts | State persists; a restarted ledger learns each witness's size from `409` | `test_file_state_survives_a_restart`, `test_a_restarted_ledger_learns_each_witness_size_from_409`, live run |
| Witness clock goes backwards | Cosignature times never decrease | `test_timestamps_never_go_backwards` |
| Cosignature mistaken for a note signature | Different key type (0x04 vs 0x01), different key hash and a `cosignature/v1` header. A relabelled cosignature does not verify as a note signature | `test_cosignature_cannot_pass_as_a_note_signature` |
| Private key exposure | The key file must be mode 0600. `repr` hides the key. Logs carry only origin, size and code | `test_load_signer_refuses_readable_or_missing_keys` |

What witnesses do not protect against yet:

- **The operator and 2 of 3 witnesses colluding.** In the demo all three witnesses run on one machine with keys from one `make dev-keys`, so they are independent processes, not independent organisations. Anchors (day 4) cover collusion, and O4 (a witness on a separate host) would make independence visible.
- **Cosignature times.** They are the witness's own claim, and `pl-verify` does not compare them with any clock, because it runs offline. RFC 3161 tokens (day 4) give trusted time.
- **Unauthenticated witness requests.** Anyone who can reach a witness can submit checkpoints. They must be signed by the log key to be cosigned, so the only gain is load, and witnesses bind to 127.0.0.1.

## Formulas and formats

| Item | Definition |
| --- | --- |
| Signed message | `"cosignature/v1\n" ‖ "time " ‖ decimal(t) ‖ "\n" ‖ checkpoint body` (origin, size, base64 root and any extension lines, each ending in `\n`) |
| Key hash | `SHA-256(name ‖ 0x0A ‖ 0x04 ‖ Ed25519 public key)[:4]` (type 0x04: timestamped Ed25519 cosignature, C2SP signed-note) |
| Signature line | `"— " ‖ name ‖ " " ‖ base64(key hash ‖ uint64_be(t) ‖ Ed25519(message))` ‖ `"\n"`, 76 bytes before base64 |
| Cosigned checkpoint | The log-signed note with cosignature lines appended to its signature block |
| Final | At least `quorum` distinct trusted witnesses with a verifying line |

Worked example from `tests/vectors/cosignature-v1.json` (TEST ONLY keys). It cosigns the 3-record checkpoint from `checkpoint-v1.json`:

```text
message (witness-1, t = 1793300000):
cosignature/v1
time 1793300000
precept-ledger.local/harbor-federal/decisions
3
XId9QnIn93Thb1lnOkqdn7CEMh2Ue7bbju3LcNFwOgs=

verifier key:  witness-1+a5000ff1+BJiyucOEeLXLfz80IB13SD9CbOsq3tZ5GQRC50oeCSNw
line:          — witness-1 pQAP8QAAAABq45Yg/xUVHeUNVEwhFsWnUthaqMEsQgg7EOyMmkjYcBY0b6xhnCvZbkHxIU4oUuP0561VMBErPVL4X55vn5sd3Sm8Ag==
               └ a5000ff1 (key hash) ‖ 000000006ae39620 (t) ‖ 64-byte signature
```

`test_cosignature_vectors_independently` re-checks the vector with `hashlib` and `cryptography` directly. `test_message_matches_the_spec_example` checks the message bytes against the example in the C2SP spec.

## How to run and test

```sh
make dev-keys                # adds witness keys, witnesses.txt, log-verifiers.txt to KEYS_DIR
make witnesses               # terminal 1: three witnesses on :8091-8093
make serve                   # terminal 2: pl-ledger, with WITNESSES set in .env
curl -s localhost:8080/ledger/v1/checkpoints/latest?final=true
curl -s localhost:8091/witness/v1/latest > held.note         # an auditor keeps this
curl -s -o export.tar localhost:8080/ledger/v1/export
uv run pl-verify export.tar --keys "$KEYS_DIR/trusted.json" --witness-keys "$KEYS_DIR/witnesses.txt"
make demo-rogue              # beat 5, no services needed
uv run pytest tests/unit/test_cosignature.py tests/unit/test_witness*.py tests/unit/test_api_witnessed.py tests/unit/test_verify_witnesses.py
make test-integration        # includes three real witness servers over HTTP and Postgres
```

`make demo-rogue` output (2026-10-27; witness alerts go to stderr as JSON):

```text
1. Honest ledger: 3 decisions, checkpoint cosigned by 3 witnesses (final).
2. Auditor keeps witness-1's latest cosigned checkpoint: held.note
3. Operator rebuilds history without decision 2 and re-signs it with the log key.
   witness-1 refused: witness.inconsistent
   witness-2 refused: witness.inconsistent
   witness-3 refused: witness.inconsistent
4. pl-verify rogue.tar keys only (a hash chain + log signature can't tell)
   exit 0  RESULT  OK
5. pl-verify rogue.tar with witness keys, quorum 2
   exit 1  RESULT  FAILED: first break at a checkpoint: below_quorum
6. pl-verify rogue.tar with the held checkpoint
   exit 1  RESULT  FAILED: first break at a checkpoint: rewritten_since_trusted
7. pl-verify honest.tar with witness keys
   exit 0
```

Live run on 2026-10-27. It used three `pl-witness` processes and `pl-ledger` over Postgres, with a 2-second checkpoint interval. The ledger was restarted once in the middle; witnesses were then stopped and started:

```text
after restart:              size 3 final True  ['witness-1', 'witness-2', 'witness-3']
stop witness-3, append 2:   size 5 final True  ['witness-1', 'witness-2']
stop witness-2, append 1:   size 6 final False ['witness-1']
latest final:               size 5
health: "checkpoint_errors":{"live-1793146250/decisions":"witness.no_quorum"}
pl-verify --witness-keys:   witnessed 1 records after the latest final checkpoint; final: size 5, 2 of quorum 2
                            RESULT  FAILED: below_quorum: latest checkpoint (size 6) has 1 of 2 witness cosignatures
restart witness-2 and 3:    size 6 final True  ['witness-1', 'witness-2', 'witness-3']
pl-verify --witness-keys:   RESULT  OK
```

## Decisions

D5 (2 of 3), D38, D39, D40, D41, D42, D43 in [`docs/DECISIONS.md`](../DECISIONS.md).

## Known gaps

- No docker compose services for the ledger and witnesses yet; they run as processes (`make witnesses`, `make serve`). The full compose stack is still open.
- All three demo witnesses share one machine and one key-generation step (O4 is open: a witness on a separate host).
- Witnesses keep only the latest cosigned checkpoint per log, not every one. Each refused checkpoint is kept.
- The ledger asks witnesses inside the checkpoint tick. A slow witness delays the next checkpoint by up to `WITNESS_TIMEOUT_SECONDS`.
- Cosignature times are not checked against a trusted clock (anchors, day 4).
- `pl-verify` requires the quorum on the latest checkpoint only. Older checkpoints without a quorum are counted but are not findings, because a later final checkpoint covers them through consistency.
