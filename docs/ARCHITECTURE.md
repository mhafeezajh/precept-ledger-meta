# Precept Ledger: Hackathon Architecture

Oct 25, 2026 · @HAFEEZ

## Summary

Precept Ledger gates every consequential action a bank's AI agent takes and records it in a ledger with the same tamper-proof guarantees as a blockchain, without a blockchain. Meta's Muse models write the policy rules, explain approvals and answer auditors; deterministic code enforces the rules and rejects any model claim it cannot prove.

This is the hackathon build for the Meta Global AI Developer Hackathon (building period Oct 27 to Nov 4, 2026, submission due Nov 4, 9:00 PM ET). It is new code in a new public repository, in Python, separate from the private Precept Insight monorepo.

| Guarantee | How it is met | Blockchain equivalent |
| --- | --- | --- |
| No record can be changed, removed or reordered without detection | Hash chain + Merkle tree over every record | Block hashes |
| The operator cannot rewrite history, even by recomputing every hash | Independent witnesses cosign each tree head and check it only ever grows | Consensus among nodes |
| Every record has a provable time | RFC 3161 timestamps; optional anchoring of the tree head to Bitcoin via OpenTimestamps | Block timestamps |
| Any third party can check everything without trusting the operator | Open-source offline verifier, inclusion and consistency proofs | Running your own node |
| A submitter can prove the operator accepted a record | Signed receipt returned on every append | Transaction confirmation |

In scope: Gate-lite, Ledger, Verify CLI, policy drafting, approval briefs, auditor Q&A with proof-checked claims, a Policy Learner that proposes rule changes from the ledger's own history, and a web UI for one bank scenario. Out of scope: multi-tenancy, SPIFFE identity, Kubernetes, the analytics add-on and production performance.

## Principles and hard rules

The model never decides; it drafts, explains and answers, and code checks everything it produces.

| # | Rule | What it means in code |
| --- | --- | --- |
| P1 | Code decides, models extract | No model call in the allow, deny or escalate path. Cedar decides. |
| P2 | Write before effect | An allowed tool call runs only after its decision record has a signed receipt. |
| P3 | Fail closed | Ledger unreachable, schema invalid or proof missing means deny or reject, never a silent repair. |
| P4 | Verify, don't trust | Every model claim about the ledger carries record IDs and is checked against Merkle proofs and field values. |
| P5 | Trust is spread out | The ledger operator alone cannot rewrite history: witnesses, timestamps and anchors hold independent copies of the tree head. |
| P6 | Humans own consequential changes | Policies become active and risky actions proceed only after a named person approves, and that approval is itself a ledger record. |
| P7 | New work only (official rule) | All code written during the building period, Oct 27 to Nov 4, 2026. The README lists everything built during the event; the demo shows only that work. No Precept Insight code or screens. |
| P8 | Meta models, Standard mode | All model calls through LiteLLM to Meta models. Contributor mode is never used. |

## System context and components

Four services sit between people, the agent and the bank's tools; all of them write to one ledger, which three witnesses and two outside anchors hold to account.

&#91;embedded content: system context · 4 users, 4 services, ledger, witnesses, anchors\]

Gate lets a tool call through only after the ledger returns a receipt; the verifier needs nothing but an export and the public keys.

| Component | Responsibility | Key libraries |
| --- | --- | --- |
| `merkle/` | RFC 9162 core: leaf/node hashing, roots, inclusion and consistency proofs; `TreeStore` port that storage adapters implement | stdlib |
| `ledger/` | Canonical records, signatures, chain, checkpoints, receipts, API; Postgres `TreeStore` adapter | `rfc8785`, `cryptography`, `psycopg` |
| `witness/` | Independent service, own key and storage; verifies consistency and cosigns | `cryptography`, `fastapi` |
| `anchor/` | RFC 3161 timestamp requests and offline token verification; OpenTimestamps submit, upgrade and proof reading | `asn1crypto`, `cryptography`, `httpx` (D44, D46) |
| `verify/` (`pl-verify`) | Offline verification of an export; no network imports | stdlib, `cryptography` |
| `gate/` | Normalizer, Cedar, decision records, write-before-effect, approval queue, MCP proxy, `@governed` decorator | `cedarpy`, `mcp` |
| `policy/` | PDF extraction with spans, Muse Wonder drafting, YAML to Cedar compiler, fixtures | `pypdf`, `pydantic`, `litellm` |
| `assist/` | Approval briefs, auditor Q&A | `litellm`, `pydantic` |
| `claims/` | Claim checker: inclusion proofs and field assertions, used by Q&A and the learner | stdlib, `cryptography` |
| `learn/` | Policy Learner: pattern queries, fatigue filter, proposal drafting, history replay, two-person approval for loosening | `psycopg`, `cedarpy`, `litellm` |
| `agent/` | Demo payments agent on Muse Spark | `langgraph`, `litellm` |
| `web/` | Policy review, approver queue, auditor console, learner proposals, ledger viewer, tamper and rogue-operator buttons | `fastapi`, `jinja2`, `htmx` |

Runtime: one `docker compose up` starts Postgres, the ledger, three witnesses, Gate, the web app and the agent. No cloud account is needed except for model calls and the two external anchors.

## Ledger cryptographic design

The ledger is a transparency log, the design behind Certificate Transparency and Sigstore Rekor, with a witness quorum standing in for blockchain consensus. A hash chain alone stops edits but not a rogue operator who recomputes every hash and re-signs; witnesses and external anchors stop that too.

### Six layers of protection

| Layer | Mechanism | Standard | Stops |
| --- | --- | --- | --- |
| 1. Canonical record | Every record serialized as canonical JSON, so the same record always gives the same bytes | RFC 8785 (JCS) | Two encodings of one record hashing differently |
| 2. Producer signature | Gate signs each record with its own Ed25519 key before sending it | RFC 8032 | The ledger forging or altering a record in Gate's name |
| 3. Hash chain | Each record's hash covers the previous hash and the record | SHA-256 | Any edit, deletion or reordering, found at the exact sequence number |
| 4. Merkle log | Records are leaves of a Merkle tree; a signed checkpoint publishes the tree size and root | RFC 9162 hashing, C2SP tlog-checkpoint | Proving one record exists (inclusion proof) and that history only grew (consistency proof), both in log n hashes |
| 5. Witness quorum | Three independent witnesses check each new checkpoint is consistent with the last one they saw, then cosign it. A checkpoint is final with 2 of 3 cosignatures | C2SP tlog-cosignature | The operator rewriting history, even with the log's own signing key |
| 6. External time anchors | Each final checkpoint is timestamped by an outside timestamp authority and its root hash anchored in Bitcoin | RFC 3161, OpenTimestamps | Back-dating, and collusion between the operator and every witness |

### Exact formulas

| Item | Definition |
| --- | --- |
| Chain hash | `h_n = SHA-256("PL-chain-v1" ‖ h_(n-1) ‖ JCS(record_n))`, with `h_0` = 32 zero bytes |
| Merkle leaf | `SHA-256(0x00 ‖ JCS(record))` |
| Merkle node | `SHA-256(0x01 ‖ left ‖ right)` |
| Checkpoint | Text note: log origin, tree size, base64 root hash, signed by the log key as a signed note |
| Producer signature | `Ed25519(producer_key, "PL-record-v1\x00" ‖ JCS(record − {seq, prev_hash, producer.sig}))` (D9, D18) |
| Receipt | Ledger signs `"PL-receipt-v1\x00" ‖ JCS({v, alg, stream, record_id, seq, leaf_hash, chain_hash, received_at, key_id})` immediately (D11, D18); the inclusion proof follows at the next checkpoint, at most 10 seconds later in the demo |
| Cosignature | Witness signs `"cosignature/v1\ntime " ‖ t ‖ "\n" ‖ checkpoint body` (C2SP tlog-cosignature, Ed25519, key type 0x04) after verifying a consistency proof from its last cosigned size; the line `key hash ‖ uint64_be(t) ‖ sig` is appended to the checkpoint note (D38) |

The prefixes `PL-chain-v1`, `0x00` and `0x01` keep one kind of hash from ever being mistaken for another.

### Record envelope

```json
{
  "v": 1,
  "stream": "harbor-federal/decisions",
  "seq": 1042,
  "record_id": "01JB6X7Q2M9ZK3T8V4N5R6S7W0",
  "ts": "2026-10-29T21:14:03.112Z",
  "type": "gate.decision",
  "producer": {"id": "gate-1", "alg": "ed25519", "key_id": "ed25519:9f2c...", "sig": "base64..."},
  "body": {"agent": "payments-ops", "tool": "create_payment", "action": "value.transfer", "decision": "escalate", "reason": "policy.escalate", "resource": {"amount_cents": 4800000, "currency": "USD", "payee_id": "northwind-supply", "payee_new": true, "local_hour": 17, "local_weekday": 4}, "policy_ids": ["pay-new-payee-01", "pay-over-5k-01"], "policy_set": "sha256:...", "args_digest": "sha256:...", "escalation": {"id": "01M6...", "expires_at": "...", "approver_roles": ["payments-supervisor"]}},
  "prev_hash": "base64..."
}
```

The producer signature covers the canonical record without `seq`, `prev_hash` and `sig`: the ledger assigns `seq` and `prev_hash` at append time, so the producer cannot know them (D9). The chain and leaf hashes cover the full stored record, signature included. Unknown fields, unknown `alg`, floats and other non-I-JSON values are rejected. The first record in a stream has `seq` 1 and `prev_hash` = 32 zero bytes (D16). `key_id` is `"ed25519:"` + hex SHA-256 of the raw public key; hashes and signatures are canonical standard base64 (D19).

### How it compares to a blockchain

| Property | Precept Ledger | Public blockchain |
| --- | --- | --- |
| Tamper evidence | Yes, per record | Yes, per block |
| History cannot be rewritten by one party | Yes, 2-of-3 witness quorum plus Bitcoin anchor | Yes, consensus |
| Independent verification | Yes, offline verifier | Yes, full node |
| Data privacy | Records stay with the bank; only root hashes leave | Data public, or extra privacy layers needed |
| Throughput and cost | Thousands of appends per second, no fees | Limited by block space, fees per transaction |
| Operator refusing to accept a record | Not prevented. Visible, because Gate fails closed and the action never runs | Prevented, any node accepts transactions |

The one property not matched, refusal to write, does not hurt here: under write-before-effect, a refused record means a blocked action, never a hidden one.

### Keys

| Key | Holder | Demo | Production |
| --- | --- | --- | --- |
| Gate producer key | Gate | Local file | KMS or HSM |
| Log key (receipts, checkpoints) | Ledger | Local file | KMS or HSM |
| Witness keys, one per witness | Each witness | Separate containers, one optionally on a separate cloud host | Separate organizations |

Key rotation is itself a ledger record, signed by the old key and the new key. Ed25519 is not quantum-safe; the envelope carries an algorithm ID so a hybrid with ML-DSA (FIPS 204) can be added later without changing the hashes.

## Data flows

Three flows share one ledger; each ends in a signed record before anything changes in the world.

### A. Runtime decision

1. The agent calls a tool through the MCP proxy or the `@governed` decorator.
2. Gate maps the call to an action class and typed facts (amount, payee\_new, data class). Unmapped tools are denied.
3. Cedar evaluates the active policy set: allow, deny or escalate.
4. Gate signs the decision record and appends it. No receipt means deny. The agent-side wrapper checks Gate's signature, the receipt and the argument digest itself before the tool runs (D51).
5. Allow: the tool runs, and a result record with the result's digest is appended.
6. Escalate: an approval request opens and Muse Spark writes a brief from the decision's facts. The approver approves or rejects; the verdict is appended, then the request closes; on approval the tool runs. No verdict before the timeout means deny, recorded as an `expire` verdict (D52).

### B. Policy authoring

1. A compliance officer uploads a policy PDF. Text is extracted with page and character spans.
2. Muse Wonder drafts YAML rules, each with the exact source span it came from.
3. Code compiles YAML to Cedar, validates it against the schema, runs each rule's fixtures, and flags any rule that allows everything or contradicts its clause's numbers.
4. The officer reviews each rule beside its clause, edits if needed, and approves.
5. The approval is appended, and the new policy set becomes active with its hash recorded.

### C. Auditor question

1. An auditor asks a question in plain English.
2. Muse Spark calls read-only query tools (filter records by type, time, amount, approver) and returns an answer as claims, each with record IDs and field assertions.
3. The claim checker fetches each cited record, verifies its inclusion proof against the latest final checkpoint, and checks each assertion against the record's fields.
4. Claims that pass are shown with a check mark and a proof link; claims that fail are shown struck out with the reason.
5. The question, the answer and the check results are appended, so the audit itself is audited.

### D. Checkpoint and witnessing (background, every 10 seconds)

1. The ledger adds new records to the Merkle tree and signs a checkpoint.
2. Each witness requests a consistency proof from its last cosigned size, verifies it, and cosigns.
3. With 2 of 3 cosignatures the checkpoint is final; it is sent to the timestamp authority and, once an hour, to OpenTimestamps.
4. Any witness that sees an inconsistent checkpoint refuses to sign and raises an alert.

### E. Policy Learner (on demand, or nightly)

The learner proposes rule changes from the ledger's own history; it never changes a rule itself.

1. SQL queries over the ledger find patterns in escalations, approvals, rejections and denials, grouped by action, amount band, vendor status, time of day and approver. Counts come only from SQL.
2. A fatigue filter drops approvals that look like rubber-stamping: decided in under 5 seconds, or an approver with 100% approval over a large sample. Patterns with fewer than 20 decisions are skipped.
3. Muse Spark turns each surviving pattern into a proposal: a YAML rule, fixtures, and a plain-English reason that cites record IDs and states its counts as assertions.
4. The claim checker verifies every cited record's inclusion proof and that the stated counts match the SQL results. Any mismatch rejects the proposal.
5. Code replays the past window of decisions under the proposed rule and reports the deltas: escalations removed, new escalations, and past rejections that would now be allowed. A loosening proposal with any past rejection newly allowed is blocked.
6. The proposal goes to review. Tightening needs one approver; loosening needs two. The proposal, the replay result and each approval are ledger records, linked to the rule they produced.

## Meta model usage

Every model output passes through a deterministic check before it can change anything, which is what makes the model use judgeable rather than decorative.

| Model | Job | Output | Checked by |
| --- | --- | --- | --- |
| Muse Wonder | Policy PDF to YAML rules | Rules with source spans and fixtures | Compiler, schema, fixtures, human approval |
| Muse Spark 1.3 | Demo payments agent | Tool calls | Gate and Cedar |
| Muse Spark 1.3 | Approval brief | What is unusual, citing record IDs | Record IDs must exist; brief never decides |
| Muse Spark 1.3 | Auditor Q&A | Claims with record IDs and field assertions | Claim checker: Merkle inclusion proof + field match |
| Muse Spark 1.3 | Policy Learner proposals | Rule, fixtures, reason with cited records and counts | Claim checker, history replay, one or two human approvers |
| Muse Glimmer (open weights) or Muse Spark Open on Baseten or Modal | On-prem profile for briefs and Q&A (stretch) | Same schemas | Same checks |
| Muse Code | Building part of the UI | Code | Tests and review |

All calls go through LiteLLM with two profiles, `cloud` and `onprem`, switched by one setting. Standard mode only. Every call logs model, tokens and latency to a CSV so spend against the $150 credit is visible.

Model IDs, structured-output parameters and rate limits are confirmed from the Meta Model API docs on day 1 and recorded in `docs/MODELS.md`.

## Data model and API

One PostgreSQL 16 database holds the ledger and the app state; ledger tables accept only inserts, enforced by triggers.

| Table | Key fields | Write rule |
| --- | --- | --- |
| `ledger_record` | stream, seq, record\_id, v, ts, type, producer\_id, producer\_alg, producer\_key\_id, producer\_sig, body (jsonb), prev\_hash, chain\_hash, leaf\_hash | Insert only; UPDATE, DELETE and TRUNCATE rejected by trigger. The record is rebuilt from these columns for verification (D21) |
| `receipt` | record\_id, v, alg, stream, seq, leaf\_hash, chain\_hash, received\_at, key\_id, sig | Insert only; UPDATE, DELETE and TRUNCATE rejected by trigger |
| `checkpoint` | stream, tree\_size, root\_hash, note (the full signed note, signature included) | Insert only; UPDATE, DELETE and TRUNCATE rejected by trigger; also written to a write-once directory, one file per tree size (D30) |
| `cosignature` | stream, tree\_size, witness, line (the full C2SP signature line, which carries the time) | Insert only; UPDATE, DELETE and TRUNCATE rejected by trigger; verified before insert and before serving (D39) |
| `anchor` | stream, tree\_size, kind (`rfc3161`, `ots`), source, status (`verified`, `pending`, `confirmed`), token, digest | Insert only; an upgraded OpenTimestamps proof is a new `confirmed` row (D47) |
| `policy_rule` | id, version, status, yaml, cedar, source\_span, approver | Versions kept |
| `escalation` | id, stream, decision\_record\_id, agent, tool, action, approver\_roles, opened\_at, expires\_at, state, approver, verdict\_record\_id, decided\_at, brief\_record\_id | App state. Closes once from `open`; the brief attaches once; a trigger refuses other edits and deletes (D52, D58) |
| `learner_proposal` | id, direction (`tighten`, `loosen`), yaml, cited\_record\_ids, counts, replay\_result, state, approvers | State guarded; every transition also a ledger record |

Witnesses keep their own state (last cosigned size and root) in their own storage, never in the ledger's database.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/ledger/v1/streams/{stream}/records` | Append signed record(s); returns receipts |
| GET | `/ledger/v1/records/{id}` | Record + receipt |
| GET | `/ledger/v1/records/{id}/proof?size=N` | Inclusion proof against checkpoint N |
| GET | `/ledger/v1/checkpoints/latest[?final=true]` | Latest checkpoint (or latest final one) as a cosigned note, with cosignatures, quorum, `final` and anchors |
| GET | `/ledger/v1/consistency?from=M&to=N` | Consistency proof |
| GET | `/ledger/v1/export?from=M&to=N` | Bundle for offline verification |
| POST | `/witness/v1/cosign` (also `/witness/v1/add-checkpoint`) | Witness endpoint, C2SP tlog-witness body: `old <size>`, proof hashes, checkpoint in; cosignature line out; `409` with the witness's size (D40) |
| GET | `/witness/v1/latest` | The witness's last cosigned checkpoint, for auditors to hold |
| POST | `/gate/v1/decide` | Decision for an SDK caller (agent token); returns the signed record and receipt |
| GET | `/gate/v1/escalations` | Open approval requests (approver token) |
| GET | `/gate/v1/escalations/{id}?wait=S` | One request, long-polled, with its verdict record (owning agent or approver) |
| POST | `/gate/v1/escalations/{id}/verdict` | Approve or reject (approver with the required role) |
| POST | `/gate/v1/results` | Record that a cleared tool ran, with its result digest |
| POST | `/policy/v1/drafts` | Upload a PDF; returns draft rules |
| POST | `/assist/v1/ask` | Auditor question; returns checked claims |
| POST | `/learn/v1/run?window=7d` | Run the learner; returns checked, replayed proposals |
| POST | `/learn/v1/proposals/{id}/approve` | Approve a proposal (two distinct approvers for loosening) |

The offline CLI: `pl-verify export.tar --keys keys.json --witness-keys witnesses.txt --quorum 2 --tsa-roots roots.pem` checks the chain, every producer signature, every receipt, every checkpoint signature, consistency between checkpoints, the witness quorum, and the timestamp tokens, with no network access.

## Threat model

Every attack below is detected by a check anyone can run offline; the last two rows are the limits.

| Attacker | Attack | Detected by |
| --- | --- | --- |
| Database admin | Edits, deletes or reorders a record | Chain hash breaks at that sequence |
| Database admin | Recomputes every hash after an edit | Signed checkpoints no longer match |
| Ledger operator with the log key | Rewrites history and re-signs new checkpoints | Witnesses refuse: no consistency proof from their cosigned size |
| Ledger operator | Shows different histories to different auditors (split view) | Witnesses and auditors compare cosigned checkpoints; two signed roots for one size is proof of misbehavior |
| Ledger operator | Inserts a fake Gate decision | Producer signature fails against Gate's key |
| Ledger operator | Drops a record after giving a receipt | The receipt is signed proof of acceptance; missing inclusion proof after 10 s is provable |
| Operator and every witness together | Rewrite and re-cosign | RFC 3161 timestamp and Bitcoin anchor of the old root no longer match |
| Agent | Deletes or edits its own trail | Agent has no ledger write path except through Gate, and no delete path at all |
| Model | Invents a record or misstates a value | Claim checker rejects: missing inclusion proof or failed field assertion |
| Model | Drafts a wrong policy rule | Fixtures, Cedar validation and human approval |
| Model (learner) | Inflates counts to justify loosening a rule | Counts must match SQL results; claim checker rejects the proposal |
| Approver habits | Rubber-stamped approvals teach the learner to loosen | Fatigue filter, minimum sample, replay blocks any loosening that would allow a past rejection, two approvers to loosen |
| Prompt injection in a policy PDF | Steers the drafter | Drafter has no tools and no write role; output is only a draft |
| Limit | Ledger refuses to accept a record | Not prevented; Gate fails closed, so the action is blocked, never hidden |
| Limit | Gate key stolen | Forged records would verify. Mitigation: key in KMS or HSM, rotation, anomaly checks (production) |

What the ledger proves: who submitted what, when, and that nothing changed afterward. It does not prove a producer told the truth; that is why Gate-originated records, made before the action ran, carry the most weight.

## Demo, plan and testing

The demo is seven beats in under three minutes at a fictional bank, Harbor Federal; everything built serves one of them.

### Demo beats

1. **Policy to rules.** Upload the payments policy; Muse Wonder drafts rules; one bad rule is caught by its fixtures; the officer fixes and approves it. Keep to 20 seconds.
2. **Gate holds a $48,000 payment** to a new vendor before the tool runs; Muse Spark writes the brief; the supervisor approves; only then does the payment execute.
3. **Auditor question with proofs.** Every claim shows a proof; one wrong claim from the model is rejected on screen.
4. **Database edit caught.** Edit one row in Postgres; `pl-verify`, offline, names the exact record.
5. **Rogue operator caught.** Rewrite history and re-sign it with the log key; the witnesses refuse to cosign and the verifier shows the old cosigned root. This is the beat a plain hash chain cannot do.
6. **The ledger learns.** After a week of seeded decisions, the learner proposes removing 38 routine escalations and tightening after-hours new-payee payments, each with proofs and a replay showing no past rejection would now be allowed; two approvers sign off. About 25 seconds.
7. **On-prem switch (stretch).** The same Q&A runs on Muse Glimmer or Muse Spark Open on a partner endpoint.

### Build plan

| Day | Date | Deliver | Done when |
| --- | --- | --- | --- |
| 1 | Tue Oct 27 | Public repo, compose, canonical records, producer signatures, chain, receipts | Tests prove any single-byte edit fails at its sequence |
| 2 | Wed Oct 28 | Merkle tree, checkpoints, inclusion and consistency proofs, `pl-verify` | Verifier passes RFC 9162-style test vectors and catches beat 4 |
| 3 | Thu Oct 29 | Three witnesses, 2-of-3 quorum, RFC 3161 timestamps, OpenTimestamps submission | Beat 5 works from the command line |
| 4 | Fri Oct 30 | Gate-lite, demo agent on Muse Spark, approval briefs | Beat 2 works end to end |
| 5 | Sat Oct 31 | Policy pipeline with Muse Wonder | Beat 1 catches the planted bad rule |
| 6 | Sun Nov 1 | Auditor Q&A and claim checker; seed generator for a week of decisions | Beat 3 rejects a hallucinated claim |
| 7 | Mon Nov 2 | Web UI for all beats; deploy to Modal with managed Postgres | Beats 1 to 5 run at the live URL |
| 8 | Tue Nov 3 | Policy Learner (patterns, fatigue filter, proposals, replay, two-person approval); README with "Built during the hackathon" section; record the video | Beat 6 works; fresh clone runs `make demo` |
| 9 | Wed Nov 4 | Buffer; on-prem profile only if everything else is done; submit by 12:00 PM ET (official cutoff 9:00 PM ET) | Submitted |

If behind, cut in this order: on-prem profile, OpenTimestamps (keep RFC 3161), MCP proxy (keep the decorator), learner tightening proposals (keep loosening with replay). Never cut: write-before-effect, witnesses, the offline verifier, the claim checker.

### Tests

| Test | Pass condition |
| --- | --- |
| Property tests on canonical JSON | Same record gives the same bytes; any edit changes the hash |
| Tamper suite | Edit, delete, insert, reorder, truncate and fork are each rejected with the right sequence |
| Merkle vectors | Inclusion and consistency proofs match published RFC 9162-style vectors |
| Witness suite | Inconsistent checkpoint gets zero cosignatures; quorum logic correct with 0 to 3 signers |
| Rogue operator | Re-signed rewrite is detected with only the log key compromised |
| Write-before-effect | With the ledger down, no tool call reaches the tool |
| Claim checker | Planted false claims (wrong amount, unknown ID, wrong approver) all rejected |
| Learner | Inflated counts rejected; rubber-stamp approvals filtered; a loosening that would allow a past rejection is blocked; one approver cannot approve a loosening |
| Offline guarantee | `pl-verify` test fails if any network call is attempted |
| Fixtures | Every policy rule passes its allow and deny cases |

## Decisions and open items

The main choice is a witnessed transparency log instead of a blockchain: it matches a blockchain on tamper-proofing and independent verification, keeps bank data private and adds no fees.

| ID | Decision | Rejected | Why |
| --- | --- | --- | --- |
| D1 | Transparency log with witness quorum and external anchors | Public blockchain; permissioned chain such as Hyperledger Fabric | Same tamper guarantees without exposing data, fees or running consensus nodes in 9 days |
| D2 | Python for the whole build | Go, as in the product | Speed for a solo 9-day build; AI libraries are Python-native |
| D3 | Postgres with insert-only triggers | SQLite; an immutable database product | One engine, real permission model for the tamper demo |
| D4 | C2SP checkpoint and cosignature formats | A custom format | Interoperates with existing witness networks and tooling |
| D5 | 2-of-3 witness quorum | 1 witness; 3 of 3 | Survives one witness being down; one witness alone is a single point of trust |
| D6 | Cedar via `cedarpy` | OPA; a custom rule engine | Formal analysis, same engine as the product |
| D7 | New repository, Apache 2.0 | Reuse the Precept Insight monorepo | Hackathon new-work rule and the organizers' licence to submissions |
| D8 | Learner proposes, humans approve; counts from SQL, text from the model | Self-updating rules; training a model on the logs | Keeps code in charge of decisions; every lesson is provable and reversible |

### Open items

| # | Item | Needed by |
| --- | --- | --- |
| O1 | Resolved Oct 26: Official Rules require new work only, public repos, and a demo showing only work built during the event | Done |
| O2 | Exact Muse model IDs, structured-output support and rate limits; claim the $150 credits (card required, credits arrive in batches) | Oct 27 |
| O3 | Which public timestamp authority to use (free and reachable from the demo machine). Resolved: DigiCert and FreeTSA (D45) | Oct 29 |
| O4 | Whether one witness can run on a separate host (Modal or a second machine) to make independence visible | Oct 29 |
| O5 | OpenTimestamps confirmation takes hours: submit on day 3 so a confirmed Bitcoin proof is ready for the video | Oct 29 |
| O6 | Managed Postgres for the Modal deployment (Modal Volumes are not suited to a live database) | Nov 2 |

## Change log

| Date | Section | Change | Why |
| --- | --- | --- | --- |
| 2026-10-27 | Record envelope | Added `producer.alg` | Algorithm agility; unknown `alg` rejected (D10) |
| 2026-10-27 | Record envelope, Exact formulas | Producer signature excludes `seq` and `prev_hash`; added the signing formula with context string `PL-record-v1\x00` | Producer cannot know ledger-assigned fields; domain separation (D9, D18). Re-chaining attacks this allows are caught by receipts now and by witnesses from day 3 |
| 2026-10-27 | Exact formulas | Receipt adds `v`, `alg`, `stream`, `key_id`; signed with context `PL-receipt-v1\x00` | `seq` is unique only per stream; self-describing receipts (D11, D18) |
| 2026-10-27 | Record envelope | `seq` starts at 1; integers only (no floats); `key_id` and base64 encodings defined | D16, D17, D19 |
| 2026-10-27 | Data model | `ledger_record` producer split into columns; `receipt` columns listed in full; TRUNCATE also blocked | Direct row edits must change what the verifier sees (D21) |
| 2026-10-27 | Components | Merkle tree moved out of `ledger/` into its own `merkle/` package: a pure core plus a read-only `TreeStore` port. Verifier, witnesses and claim checker import only the core; storage adapters live with the data they read | Owner direction; lets the tree run over other storage (for example tiles) without touching verification (D25) |
| 2026-10-27 | Data model, Exact formulas | `checkpoint` table holds the full signed note, with no separate `log_signature` column. Checkpoint origin and signed-note key name are `<LEDGER_ORIGIN>/<stream>`: one tree per stream | One authoritative copy of the signature; `seq` is per stream (D28, D30) |
| 2026-10-27 | Components | Verifier side may import `ledger.checkpoint` (pure C2SP checkpoint and signed-note code) | Verifier and witnesses must parse and check checkpoints (D28) |
| 2026-10-27 | Flow D | The checkpointer checks a consistency proof from its last checkpoint before signing, and refuses otherwise | An edited table never gets a fresh signature from the honest process (D30) |
| 2026-10-27 | Verification, API | `pl-verify` usage today: `pl-verify export.tar --keys keys.json [--trusted-checkpoint note] [--json]`, exit 0/1/2. `--witness-keys` and `--quorum` are added with witnesses. Bundle v1 layout defined; the export is always the whole stream until range exports exist | D31, D33 |
| 2026-10-27 | Components | `pl-verify` reimplements the chain and receipt checks on top of `ledger.format` instead of importing `ledger.chain`; a differential test keeps the two in agreement | Skill §2 import rule (D32) |
| 2026-10-27 | API | `POST …/records` takes one record per request. `proof?size=N` needs `N` to be a checkpoint size and returns the signed checkpoint. `checkpoints/latest`, `consistency` and `export` take a `stream` query parameter. Added `GET /ledger/v1/health`. `export` refuses `from`/`to` until range exports exist | "2xx means receipt" stays exact; proofs only against signed sizes; the loop must be observable (D35, D36) |
| 2026-10-27 | Components | `ledger.format` split: canonical JSON, encodings and hashes now in `ledger.canonical` (re-exported by `ledger.format`); the verifier side may import it. No byte changed: vectors regenerate identically | Skill §3 module size limit |
| 2026-10-27 | Exact formulas | Cosignature is C2SP `cosignature/v1`: Ed25519 over `"cosignature/v1\ntime <t>\n"` ‖ checkpoint body, key type 0x04, appended to the note as a signature line | The spec form of "signs {checkpoint, timestamp}"; interoperable and domain-separated (D38) |
| 2026-10-27 | Data model | `cosignature` table is `(stream, tree_size, witness, line)` instead of `checkpoint_id, witness_id, signature, ts`; insert-only by trigger | The signature line carries signature and time; one authoritative column (D39) |
| 2026-10-27 | API | `/witness/v1/cosign` uses the C2SP tlog-witness text body and statuses, also served at `/witness/v1/add-checkpoint`; added `GET /witness/v1/latest` and `/witness/v1/health`. `checkpoints/latest` returns the cosigned note plus `quorum` and `final`, and takes `final=true`; proofs return the cosigned note | Interoperability; auditors need a witness-held checkpoint for `--trusted-checkpoint` (D40, D41) |
| 2026-10-27 | Verification | `--witness-keys` is a file of C2SP cosigner verifier keys, not a directory; the quorum (default 2) is required on the latest checkpoint | D42 |
| 2026-10-28 | Components | `anchor/` uses `asn1crypto` + `cryptography` (RFC 3161) and its own OpenTimestamps reader instead of `rfc3161ng` and `opentimestamps-client`; the verifier imports the pure `anchor.rfc3161` and `anchor.ots` | Keep the offline verifier free of network libraries; licence and dependency weight (D44, D46) |
| 2026-10-28 | Data model | `anchor` table columns as built; insert-only instead of "insert, then status updates" | Every proof an auditor saw stays (D47) |
| 2026-10-28 | Verification, API | Export bundle v2 adds `anchors/`; `pl-verify --tsa-roots [--max-timestamp-lag]` checks tokens and back-dating; `checkpoints/latest` lists re-verified anchors | D47, D48 |
| 2026-10-28 | Open questions | O3 resolved: DigiCert and FreeTSA | D45 |
| 2026-10-28 | Record envelope, Data model | Gate's decision body names facts `resource` with amounts in integer cents (`amount_cents`) instead of `amount: 48000`; adds `tool`, `reason`, `policy_set`, `args_digest` and `escalation`. New record types `gate.verdict` and `gate.result` | Exact thresholds and an I-JSON ledger; every decision cites its policy text and exact call (D50) |
| 2026-10-28 | Components, Flow A | Escalate is a Cedar `permit` annotated `@escalate` + `@approver_role`; any Cedar error denies. Gate is a service holding the producer key, and the `@governed` wrapper verifies records before the tool runs. MCP proxy deferred | D49, D51, D55 |
| 2026-10-28 | Data model, API | `escalation` columns as built, scoped by stream, guarded by trigger; added `GET /gate/v1/escalations`, `GET /gate/v1/escalations/{id}`, `POST /gate/v1/results`, `GET /gate/v1/health`; bearer tokens for agents and approvers | D52, D53, D54 |
| 2026-10-28 | Flow A, Meta model usage | Approval briefs are `assist.brief` ledger records written in the background after the escalation opens, grounded in a context code builds from the ledger and checked for cited ids, numbers and verdict advice; the verdict names the brief shown | The brief never sits on the decision path, and auditors can see what the approver saw (D57, D58) |
| 2026-10-28 | Components | Model settings live in `common/config.py` (`MODEL_PROFILE`, `META_API_KEY`, ...) instead of `models.yaml`; Standard mode is enforced by refusing `-contributor` model IDs; model IDs and limits in `docs/MODELS.md` | D56, D59 |
| 2026-10-28 | Build plan | Auditor Q&A is built with `claims/`, not `assist/` | D60 |
