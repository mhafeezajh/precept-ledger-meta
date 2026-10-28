# Knowledge base

One page per module, written when the module is finished and kept current with the code (skill §8). Each page has the same sections: responsibilities, how it works, interfaces, data, security and failure behaviour, formulas with a worked vector, how to run, decisions and known gaps.

| Page | What it covers | Status |
| --- | --- | --- |
| [ledger.md](ledger.md) | Record format v1, canonical JSON, producer signatures, hash chain, receipts, memory and Postgres stores | Partial: API and export bundle next |
| [merkle.md](merkle.md) | RFC 9162 hashing, roots, inclusion and consistency proofs, `TreeStore` port | Done for now |
| [checkpoints.md](checkpoints.md) | C2SP checkpoints and signed notes, the checkpointer, write-once sinks, Postgres tree adapter | Done for this step |
| [api.md](api.md) | Ledger HTTP API, status codes and error bodies, checkpoint loop, JSON logs, `pl-ledger`, dev keys | Done for this step: batch, ranges and read auth later |
| [verify.md](verify.md) | Export bundle, `pl-export`, the offline `pl-verify` CLI, every finding it can report, witness quorum, timestamps | Done for this step |
| [witness.md](witness.md) | C2SP cosignatures, the witness service (`pl-witness`), its own state, 2-of-3 finality in the ledger, demo beat 5 | Done for this step: compose services later |
| [anchor.md](anchor.md) | RFC 3161 timestamps (request, offline verification), OpenTimestamps (submit, upgrade, read), backdating checks, bundle v2 | Done for this step: confirmed Bitcoin vector after O5 |
| [gate.md](gate.md) | Normalizer, Cedar allow / deny / escalate, decision, verdict and result records, write-before-effect, approval queue, tokens, `pl-gate`, `@governed`, demo beat 2 | Done for this step: MCP proxy deferred |
| [assist.md](assist.md) | `assist/llm.py` (the only model entry point), Muse Spark approval briefs and their grounding checks | Partial: auditor Q&A with `claims/`; no live model run yet |

## Where to look for what

| Question | Page and section |
| --- | --- |
| Exact bytes that get hashed or signed | Each page's "Formulas and formats", plus `tests/vectors/` |
| What happens when Postgres, a sink or a key is wrong | Each page's "Security and failure behaviour" table, with the test that proves it |
| Why something is built the way it is | [`../DECISIONS.md`](../DECISIONS.md), linked from each page's "Decisions" |
| What changed from the original design | Change log at the end of [`../ARCHITECTURE.md`](../ARCHITECTURE.md) |
| What is not done yet | Each page's "Known gaps" |
| Which HTTP status an error becomes | [api.md](api.md), "Interfaces" |
| What `pl-verify` reports and why | [verify.md](verify.md), "Interfaces" (finding names) and "Security and failure behaviour" |
| What a witness checks, and what happens when witnesses are down or lie | [witness.md](witness.md), "How it works" and "Security and failure behaviour" |
| How to run demo beat 5 (rogue operator) | [witness.md](witness.md), "How to run and test" |
| How a TSA token is checked, and what stops backdating | [anchor.md](anchor.md), "Interfaces" and "Formulas and formats" |
| Why a tool call was allowed, denied or held, and what proves it | [gate.md](gate.md), "How it works" and "Data" |
| How to run demo beat 2 (payment held until approval) | [gate.md](gate.md), "How to run and test" |
| Which model, mode, price and parameters each call uses | [`../MODELS.md`](../MODELS.md) |
| What stops a brief from inventing facts or advising a verdict | [assist.md](assist.md), "Security and failure behaviour" |

## Signing contexts in use

One table for every hashed or signed byte string, so a new one never collides with an old one (skill §4).

| Object | Key | Signed or hashed bytes | Defined in |
| --- | --- | --- | --- |
| Record (producer signature) | Producer | `"PL-record-v1\x00" ‖ JCS(record − seq, prev_hash, producer.sig)` | `ledger/format.py` |
| Chain hash | | `SHA-256("PL-chain-v1" ‖ h_(n−1) ‖ JCS(record))` | `ledger/canonical.py` |
| Merkle leaf, node | | `SHA-256(0x00 ‖ JCS(record))`, `SHA-256(0x01 ‖ left ‖ right)` | `merkle/hashing.py` |
| Receipt | Log | `"PL-receipt-v1\x00" ‖ JCS(receipt − sig)` | `ledger/format.py` |
| Checkpoint | Log | Note text (C2SP signed-note); contains no NUL, so it never matches the two above | `ledger/checkpoint.py` |
| RFC 3161 / OpenTimestamps digest | TSA / calendar | `SHA-256(checkpoint body)`; signed by the TSA inside CMS, not by any of our keys | `anchor/rfc3161.py` |
| Cosignature | Witness | `"cosignature/v1\ntime " ‖ t ‖ "\n" ‖ checkpoint body` (C2SP tlog-cosignature); key type 0x04, so its key hash never equals a log key's (0x01) | `ledger/cosignature.py` |
| Tool-call arguments | | `"sha256:" ‖ hex(SHA-256("PL-args-v1\x00" ‖ JCS(arguments)))`, in `gate.decision` | `gate/normalizer.py` |
| Tool result | | `"sha256:" ‖ hex(SHA-256("PL-result-v1\x00" ‖ JCS(result)))`, in `gate.result` | `gate/records.py` |
| Policy set | | `"sha256:" ‖ hex(SHA-256(policy text))`, in `gate.decision` | `gate/decide.py` |
