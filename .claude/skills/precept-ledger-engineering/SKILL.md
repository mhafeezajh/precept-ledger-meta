---
name: precept-ledger-engineering
description: Engineering standards for the Precept Ledger hackathon repo (Python, Meta Global AI Developer Hackathon, Oct 27 – Nov 4, 2026). Use for ANY work in this repo — writing or changing code, tests, crypto, Gate, witnesses, anchors, policy drafting, auditor Q&A, the web UI, or docs — even if the user just says "next module", "continue", "fix this" or "the ledger". Enforces new-work-only, code-decides-models-extract, fail-closed, write-before-effect, crypto hygiene, test gates, and a mandatory documentation pass after every module.
---

# Precept Ledger engineering skill

Read this whole file before writing code in this repo. When a rule here conflicts with a convenience, the rule wins. When a rule conflicts with the user's explicit instruction, stop and ask.

Source of truth for design: `docs/ARCHITECTURE.md` (exported from "Precept Ledger: Hackathon Architecture"). Build plan and demo beats: `docs/HACKATHON_KICKOFF.md`. If code and these docs disagree, flag it; do not silently pick one.

---

## 1. Non-negotiable rules

| # | Rule | Check before you finish |
|---|---|---|
| R1 | **New work only.** Never copy, port or paste code from the private `precept-insight` monorepo or any other private source. Design ideas are fine; code is not. | No file or function was transcribed from elsewhere |
| R2 | **Code decides, models extract.** No model call in the allow / deny / escalate path, in verification, or in the claim checker. | `grep` shows no `litellm` import under `gate/decide*`, `ledger/`, `verify/`, `witness/`, `claims/` |
| R3 | **Write before effect.** A tool runs only after its decision record has a verified receipt. | Test: ledger down → tool never called |
| R4 | **Fail closed.** Errors, timeouts, missing receipts, schema failures and unverifiable claims mean deny or reject. Never `except: pass`, never default to allow. | No bare `except`; every `except` either re-raises or returns a deny/reject |
| R5 | **Verify, don't trust.** Every model claim about the ledger is checked against Merkle proofs and field values before it is shown as true. | Claim checker test covers wrong amount, unknown ID, wrong approver |
| R6 | **Meta models only, Standard mode only.** All model calls go through `assist/llm.py` (LiteLLM). Contributor mode is never enabled. | No direct provider SDK imports; no contributor model IDs |
| R7 | **Small, reviewable steps.** One module at a time, about 150–300 lines of change per step, then stop and report. | Step summary posted before starting the next module |
| R8 | **Every module ends with documentation** (section 8). A module is not done until its docs are written. | `docs/modules/<module>.md` exists and is current |

---

## 2. Repository layout

```
precept-ledger/
  ledger/      records, canonical JSON, signatures, chain, Merkle, checkpoints, receipts, proofs, API
  witness/     independent witness service (own key, own storage)
  anchor/      RFC 3161 timestamps, OpenTimestamps
  verify/      pl-verify offline CLI (no network imports)
  gate/        normalizer, Cedar, decisions, write-before-effect, approvals, MCP proxy, @governed
  policy/      PDF spans, Muse Wonder drafting, YAML→Cedar compiler, fixtures
  assist/      llm.py (only model entry point), briefs, auditor Q&A
  claims/      claim checker (deterministic)
  learn/       Policy Learner: patterns (SQL), fatigue filter, proposal drafting, replay, approvals
  agent/       demo payments agent (LangGraph, Muse Spark)
  web/         FastAPI + Jinja + HTMX pages
  common/      config, logging, ids, errors, time
  tests/       unit/, property/, integration/, vectors/
  docs/        ARCHITECTURE.md, HACKATHON_KICKOFF.md, MODELS.md, DECISIONS.md, modules/, runbook.md
```

Dependency direction (enforced by an import test): `verify` and `claims` may import only `ledger.format`, `ledger.merkle` and stdlib + `cryptography`. `ledger` never imports `gate`, `policy`, `assist`, `agent` or `web`. `witness` never imports `ledger` internals beyond `ledger.format` and `ledger.merkle`.

---

## 3. Python standards

- Python 3.12, managed with `uv`. Pin direct dependencies in `pyproject.toml`; commit `uv.lock`.
- `ruff` (lint + format), `mypy --strict` on `ledger/ verify/ witness/ claims/ gate/ anchor/`; normal `mypy` elsewhere.
- Type hints on every public function. Use `dataclasses(frozen=True)` or Pydantic v2 models for data; no untyped dicts crossing module boundaries.
- Pure functions for anything cryptographic or deterministic; I/O at the edges.
- Errors: one exception hierarchy in `common/errors.py` (`PreceptError` → `LedgerError`, `VerificationError`, `PolicyError`, `ModelOutputError`, …). Raise specific errors with a machine-readable `code`.
- No global mutable state. Configuration through `common/config.py` (Pydantic Settings) from environment variables; `.env.example` documents every variable.
- Time: UTC only, RFC 3339 with milliseconds and `Z`. Inject a clock for tests; never call `datetime.now()` directly in logic.
- IDs: ULIDs for records, escalations and drafts.
- Async: FastAPI handlers async; DB through `psycopg` 3 with a pool. No blocking calls inside async code.
- Functions under ~40 lines; modules under ~400 lines. Split when larger.
- Names say what things are (`append_record`, `verify_inclusion`), no abbreviations beyond standard ones (`seq`, `ts`, `id`).
- Comments explain *why*, not *what*. Every security-relevant decision gets a one-line comment pointing to its rule or decision ID.

---

## 4. Cryptography rules

| Topic | Rule |
|---|---|
| Libraries | `cryptography` for Ed25519 and SHA-256; `rfc8785` for canonical JSON. Never hand-roll primitives. |
| Canonicalization | Every hashed or signed object goes through `ledger.format.canonical_bytes()`. Never `json.dumps` for anything hashed. Reject non-I-JSON (NaN, ±Inf, duplicate keys, integers beyond 2^53). |
| Domain separation | Chain: prefix `PL-chain-v1`. Merkle leaf: `0x00`. Merkle node: `0x01`. Receipts, checkpoints, cosignatures: their own context strings. Never hash two kinds of thing the same way. |
| Signatures | Ed25519 only. Sign canonical bytes of the object without its signature field. Verify every signature you receive before using the signed data. |
| Comparisons | `hmac.compare_digest` for hashes and MACs. |
| Keys | Load from files outside the repo (`KEYS_DIR`). Never log, print or return private keys. Test keys live in `tests/keys/` and are marked TEST ONLY. Each key has a `key_id` = SHA-256 of its public key. |
| Formats | Checkpoints and cosignatures follow C2SP tlog-checkpoint, signed-note and tlog-cosignature. Merkle hashing follows RFC 9162. |
| Agility | Every envelope carries `alg`. Unknown `alg` is rejected, never guessed. |
| Randomness | `secrets` only. |
| Verifier | `verify/` imports no networking modules; a test fails if `socket` is touched during verification. |

Any change to hashing, signing, canonicalization or proof logic requires: new or updated test vectors in `tests/vectors/`, a property test, and an entry in `docs/DECISIONS.md`.

---

## 5. Model calls

- Single entry point: `assist/llm.py` with `complete_structured(profile, model_role, schema, messages)`. Profiles: `cloud`, `onprem`.
- Every output is parsed into a Pydantic schema. One retry on validation failure with the error fed back; then raise `ModelOutputError` (fail closed).
- Prompts live in `assist/prompts/*.md`, versioned, with a header: purpose, inputs, output schema, version.
- Never send keys, raw account numbers or full PII to a model. Redact before the call; log only a hash of the prompt.
- Every call appends a row to `logs/model_calls.csv`: ts, profile, model, prompt_version, input_tokens, output_tokens, latency_ms, outcome.
- Models never receive write-capable tools. The auditor Q&A tools are read-only queries.
- Record exact model IDs, parameters and limits in `docs/MODELS.md` the first time they are used.

### Policy Learner rules

- The learner proposes; it never activates, edits or deletes a rule. Activation goes through the same review as any policy.
- Every number in a proposal comes from SQL in `learn/patterns.py`. The model (only in `learn/draft.py`) writes rule text, fixtures and the explanation, and must restate counts as assertions the claim checker verifies against the SQL result.
- Fatigue filter before drafting: drop approvals decided in under 5 seconds and approvers at 100% approval over a large sample; skip patterns with fewer than 20 decisions. Record what was filtered in the proposal.
- Replay (`learn/replay.py`, deterministic, Cedar only) reruns the window under the proposed rule. A loosening proposal that would allow any past rejection is blocked, not just flagged.
- Approvals: tightening needs one approver; loosening needs two distinct approvers, neither the agent's owner. Proposals, replay results and every approval are ledger records.

---

## 6. Testing

| Layer | Required |
|---|---|
| Unit | Every public function; coverage ≥ 90% on `ledger/ verify/ witness/ claims/ gate/`, ≥ 75% elsewhere |
| Property (`hypothesis`) | Canonicalization stable; any single-byte edit fails verification; Merkle proofs verify for random trees and fail when tampered |
| Vectors | `tests/vectors/` with known inputs and expected hashes/proofs; the verifier must pass them |
| Tamper suite | Edit, delete, insert, reorder, truncate, fork, rogue re-sign — each rejected with the right reason and sequence |
| Integration | `docker compose` stack: append → checkpoint → 2-of-3 cosign → export → `pl-verify` passes |
| Fail-closed | Ledger down, witness down, model down, bad schema → deny/reject, never allow |
| Model tests | Model calls mocked in unit tests; a small marked `@pytest.mark.live` suite hits the real API and is skipped by default |

Write the test first for anything in `ledger/`, `verify/`, `witness/`, `claims/`. A bug fix starts with a failing test that reproduces it.

Commands (Makefile): `make check` (ruff, mypy, unit + property tests, coverage), `make test-integration`, `make demo`, `make vectors`.

---

## 7. Security and data

- No secrets in the repo, ever. `.env` is git-ignored; run `gitleaks` in `make check`.
- Database: the app role has INSERT + SELECT on ledger tables only; triggers reject UPDATE and DELETE. The demo tamper button uses a separate, clearly named admin connection, enabled only when `DEMO_MODE=true`.
- Input validation on every API boundary with Pydantic; size limits on uploads and request bodies.
- Logs are JSON, include `trace_id`, never include raw tool arguments, prompts, keys or PII.
- Dependencies: add only what is needed; note each new dependency and its licence in `docs/DECISIONS.md`.
- The repo is public and Apache-2.0 from day 1.

---

## 8. Documentation after every module (mandatory)

A module is **not done** until its documentation is written and committed. After finishing each module (ledger, witness, anchor, verify, gate, policy, assist, claims, agent, web), do all of the following before starting the next one:

1. **Write or update `docs/modules/<module>.md`** using the template below. Keep it accurate to the code as it is now, not as planned.
2. **Update `docs/ARCHITECTURE.md`** if any interface, data shape, formula or flow changed; mark what changed in the doc's change log.
3. **Add decisions** made during the module to `docs/DECISIONS.md` (ID, decision, alternatives rejected, why, date).
4. **Update `README.md`**: module status table and how to run what now works.
5. **Post a step summary** to the user (section 9) and wait for a go-ahead.

### Module doc template (`docs/modules/<module>.md`)

```markdown
# <Module name>

<One-sentence purpose: what this module guarantees.>

Status: <done | partial — what's missing> · Last updated: <YYYY-MM-DD> · Tests: <count>, coverage <n>%

## Responsibilities
- What it owns. What it explicitly does not own.

## How it works
Short explanation in plain language, then a Mermaid diagram of the main flow or structure.

## Interfaces
| Function / endpoint | Input | Output | Errors |

## Data
Tables, record types or files this module reads and writes, with field meanings.

## Security and failure behaviour
What it protects against, what happens when each dependency fails (fail-closed path), what it does not protect against.

## Formulas and formats (crypto modules only)
Exact hashing, signing and encoding, with a worked example from tests/vectors/.

## How to run and test
Commands, environment variables, sample output.

## Decisions
Links to the DECISIONS.md entries this module relies on.

## Known gaps
What is not done yet and why.
```

Documentation style: plain words, short sentences, tables for comparisons, real examples from the code and test vectors, no marketing language. Every number in a doc must come from a test, a benchmark or the code.

---

## 9. Workflow for each step

1. Restate the step's goal and the demo beat it serves in one or two lines.
2. Write tests first for deterministic and crypto code.
3. Implement in small commits. Commit message format: `<module>: <what changed>` (e.g. `ledger: add consistency proofs`).
4. Run `make check`; fix everything it reports. Never weaken a test, lower a coverage floor, or add `# type: ignore` / `noqa` without a one-line reason.
5. Write the module documentation (section 8).
6. Report:

```
Step: <name>
Built: <bullets>
Tests: <passed/total>, coverage <n>%
Docs: <files written or updated>
Decisions: <IDs or none>
Open issues: <bullets or none>
Next: <proposed next step>
```

7. Stop and wait for the user's go-ahead before the next module.

---

## 10. Definition of done (per module)

- [ ] Rules R1–R8 hold
- [ ] `make check` green; coverage floors met
- [ ] Tamper and fail-closed tests pass where relevant
- [ ] No secrets, no TODOs without an issue reference
- [ ] `docs/modules/<module>.md` written from the template and accurate
- [ ] `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `README.md` updated
- [ ] Step summary posted

---

## 11. Hackathon guardrails

- Building period Oct 27 – Nov 4. Submit by Nov 4, 12:00 PM ET (official cutoff 9:00 PM ET via the Cerebral Valley platform). Every step should move a demo beat forward.
- Official rule: the demo may show only what was built during the event, and unclear attribution can mean disqualification. Keep the README's "Built during the hackathon (Oct 27 – Nov 4)" section current after every module, and never reference Precept Insight code or screens in the repo, README or video.
- Never describe any part as "RAG" (basic RAG apps are on the organizers' anti-project list). The auditor feature is "proof-checked answers".
- Cut order if behind: OpenTimestamps → on-prem profile → MCP proxy (keep the decorator). Never cut write-before-effect, witnesses, the offline verifier or the claim checker.
- Use Muse Code for at least one real slice and record which in the README.
- Do not gold-plate: no Kubernetes, no multi-tenancy, no SPIFFE, no analytics add-on.
