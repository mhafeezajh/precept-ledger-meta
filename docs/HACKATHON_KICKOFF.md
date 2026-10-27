# Precept Ledger — Meta Global AI Developer Hackathon kickoff

Paste this whole file into Claude Code as the first message in a NEW, EMPTY repository (not the Precept Insight monorepo). Save it in the repo as `docs/HACKATHON_KICKOFF.md` and add a short `CLAUDE.md` that points to it.

---

## 0. Context and hard rules

You are building **Precept Ledger**, a hackathon entry for the Meta Global AI Developer Hackathon.

| Item | Value |
|---|---|
| Build window | **Oct 27 → Nov 4, submissions due 9:00 PM ET via the Cerebral Valley platform** (internal deadline: Nov 4, 12:00 PM ET). Nov 3 is the last day for organizer troubleshooting |
| Judging | Nov 5–8; top 5 per track demo live on Nov 9 at 12:00 PM ET; community vote announced Nov 16 |
| Track / theme | Indie Developer track · "Build for Your World" (regulated financial services) |
| Judging (third-party summary of the event waiver; confirm against Official Rules) | Impact 30% · Use of Meta AI tooling 25% · Demo (live + video) 25% · Technical depth 20% |
| Builder | Solo, senior AI architect, Python-first |

**Hard rules — follow every one:**

1. **New work only (official rule).** Every line of code in this repo is written from Oct 27 onward, and the demo may show only what was built during the event; unclear attribution can mean disqualification. Keep a "Built during the hackathon (Oct 27 – Nov 4)" section in the README listing every component, and never show Precept Insight code or screens. Do NOT copy, vendor, port or paste code from the private `precept-insight` monorepo (Go: `pcpt-ledger`, `pcpt-gate`, `pcpt-verify`, `ledgerclient`). You may follow the same *design ideas* (listed in section 2). If the Official Rules turn out to allow pre-existing code, the owner decides separately; do not assume it.
2. **Keep the commercial core out.** Organizers receive a perpetual, non-exclusive licence to submissions. Nothing from the commercial Gate runtime, bundle signing, SPIFFE/mTLS setup or Terraform goes here.
3. **Code decides, models extract.** No LLM call is ever in the allow/deny path. Models draft, explain and answer; deterministic code validates, enforces and verifies.
4. **Fail closed.** If the ledger write fails, the action does not run. If a model's output fails schema validation or proof checking, it is rejected, not repaired silently.
5. **Write before effect.** An allowed tool call reaches the tool only after its decision record has a ledger receipt.
6. **Meta models only for model calls**, through LiteLLM. Use Standard mode on the Meta Model API. **Never use Contributor mode** (Contributor-mode prompts and outputs train Meta's models).
7. Public repo, Apache-2.0 licence, from day 1. Commit small and often with clear messages; the commit history is evidence of new work.

---

## 1. What the demo must show (build backwards from this)

A three-minute video and a live demo following one storyline at a fictional bank, "Harbor Federal":

1. **Policy → rules.** A compliance officer uploads `payments_policy.pdf`. Muse Wonder drafts YAML rules, each citing its source clause. Code compiles them to Cedar, runs fixtures, and **flags one bad rule** (e.g. a threshold that contradicts the clause, or a rule that allows everything). The officer fixes it and approves. The approval is itself a ledger record.
2. **Gate holds a risky action.** A LangGraph payments-ops agent (running on Muse Spark) tries to pay $48,000 to a new vendor. Gate decides `escalate` **before** the payment tool runs. Muse Spark writes an approval brief explaining what is unusual, citing ledger record IDs. The approver approves; a signed receipt is issued; only then does the payment tool execute.
3. **Auditor question with proofs.** An examiner asks: "Show every payment over $10,000 approved outside business hours this week, and who approved it." Muse Spark answers with claims that cite record IDs. The verifier attaches a Merkle inclusion proof and checks each claim's field assertions against the record. **One claim the model got wrong is rejected on screen** (inject a hallucination test case on demand).
4. **Tamper caught offline.** Edit one row in Postgres directly. The offline `pl-verify` CLI, with networking disabled, names the exact broken record.
5. **The ledger learns.** After a week of seeded decisions, the Policy Learner proposes removing 38 routine escalations (known vendors, under $15K, business hours) and tightening after-hours new-payee payments. Each proposal cites proven records, shows a replay with zero past rejections newly allowed, and needs two approvers to loosen. About 25 seconds in the video.
6. **On-prem switch (stretch).** Flip one setting: the same auditor Q&A runs on Muse Glimmer locally (or Muse Spark Open on a Baseten/Modal endpoint). No data leaves the "bank".

If a feature does not serve one of these five beats, it is out of scope.

---

## 2. Architecture

Single language (Python 3.12), one `docker compose up`, no cloud account required.

```
                    ┌──────────────────────── Precept Ledger (hackathon) ───────────────────────┐
 Payments agent ──► │  Gate-lite (MCP proxy + Python decorator)                                  │
 (LangGraph,        │    normalize → Cedar (cedarpy) → allow / deny / escalate                   │
  Muse Spark)       │    escalate → Approval queue (Postgres state machine, timeout = deny)      │
                    │    every decision ──► Ledger-lite ──► receipt ──► only then: tool call      │
                    │                                                                             │
 Compliance UI ───► │  Policy Studio-lite: PDF → Muse Wonder → YAML (+clause cites)              │
                    │    → compile to Cedar → validate + fixtures → human approve → active set   │
                    │                                                                             │
 Approver UI ─────► │  Approval brief (Muse Spark, structured, cites record IDs)                 │
 Auditor UI ──────► │  Auditor Q&A (Muse Spark + read-only query tools)                          │
                    │    → Claim checker: inclusion proof + field assertions → accept / reject   │
                    └─────────────────────────────────────────────────────────────────────────────┘
 pl-verify (offline CLI): chain + receipts + checkpoints + inclusion proofs, no network
 Model routing (LiteLLM): cloud = Meta Model API (Muse Spark 1.3, Muse Wonder) · onprem = Muse Glimmer local / Spark Open endpoint
```

### 2.1 Components

| Component | Responsibility | Key libraries |
|---|---|---|
| `ledger/` | Append-only records: RFC 8785 canonical JSON, `hash = SHA-256(prev_hash ‖ canonical_record)`, Ed25519 signed receipts, Merkle tree (RFC 9162 leaf/node hashing) with signed checkpoints every N records or 60 s written to a write-once local directory | `rfc8785`, `cryptography`, `psycopg` |
| `witness/` | Three independent witness services, each with its own Ed25519 key and storage. On each new checkpoint: request a consistency proof from the last cosigned size, verify it, cosign (C2SP tlog-cosignature). A checkpoint is final with 2 of 3 cosignatures. Refuse and alert on any inconsistency | `cryptography`, `fastapi` |
| `anchor/` | RFC 3161 timestamp on every final checkpoint; OpenTimestamps (Bitcoin) submission hourly, upgrade proofs when confirmed | `rfc3161ng`, `opentimestamps-client` |
| Producer signatures | Gate signs every record envelope with its own Ed25519 key before append; ledger rejects bad signatures | `cryptography` |
| `verify/` → `pl-verify` CLI | Offline: recompute chain, check receipts and checkpoint signatures, check inclusion proofs, report the first broken sequence number. Zero network imports (add a test that fails if `socket` is used) | stdlib + `cryptography` |
| `gate/` | Normalizer (tool call → action class + typed facts), Cedar evaluation, decision record, write-before-effect, approval queue, MCP proxy and `@governed` decorator | `cedarpy`, `mcp` |
| `policy/` | PDF → text with page/clause spans → Muse Wonder structured output (YAML rules with `source_span`) → compiler to Cedar → `cedarpy` validation + fixtures + "allows everything" check → review state (draft / approved / active) | `pypdf`, `pydantic`, `litellm` |
| `assist/` | Approval briefs and auditor Q&A. Strict JSON schemas. Every claim carries `record_ids` and `assertions` (`field`, `op`, `value`) | `litellm`, `pydantic` |
| `claims/` | Claim checker: for each claim, fetch records, verify inclusion proof against the latest signed checkpoint, evaluate each assertion deterministically against the record fields. Accept only if all pass | stdlib |
| `learn/` | Policy Learner: SQL pattern queries over the ledger (counts only from SQL), fatigue filter (drop approvals under 5 s or approvers at 100% over a large sample; skip patterns under 20 decisions), Muse Spark drafts proposals (YAML rule + fixtures + reason citing record IDs and counts), claim checker verifies citations and counts, deterministic replay of the past window under the proposed rule, review: tighten = 1 approver, loosen = 2 approvers; a loosening that would allow any past rejection is blocked. Proposals, replays and approvals are ledger records. The learner never activates a rule itself | `psycopg`, `cedarpy`, `litellm` |
| `agent/` | Demo payments-ops agent (LangGraph) on Muse Spark, with tools `lookup_vendor`, `create_payment`, `send_email`, routed through Gate | `langgraph`, `litellm` |
| `web/` | FastAPI + HTMX pages: Policy review, Approver queue, Auditor console, Ledger viewer with a "Tamper" button (demo only) | `fastapi`, `jinja2`, `htmx` |
| `models.yaml` | LiteLLM routes: `cloud` and `onprem` profiles, one env var switches | `litellm` |

### 2.2 Data model (Postgres 16)

| Table | Key fields |
|---|---|
| `ledger_record` | `stream`, `seq` (gap-free), `record_id`, `ts`, `type` (`decision`, `approval`, `policy.approve`, `qa.answer`, …), `body` (jsonb), `prev_hash`, `hash` |
| `ledger_receipt` | `record_id`, `seq`, `hash`, `signature`, `key_id` |
| `checkpoint` | `stream`, `tree_size`, `root_hash`, `signature`, `ts`, `worm_path` |
| `policy_rule` | `id`, `version`, `status`, `yaml`, `cedar`, `source_span`, `approver` |
| `escalation` | `id`, `decision_record_id`, `state` (`open`/`approved`/`rejected`/`expired`), `approver`, `expires_at` |

Database role used by the app has INSERT and SELECT only on `ledger_record`, enforced with a trigger that rejects UPDATE and DELETE. The demo "Tamper" button uses a separate superuser connection on purpose, to show that even an admin edit is detected.

### 2.3 Action classes (subset)

`value.transfer`, `comm.external`, `data.export`, `record.modify`. Unmapped tools are denied.

### 2.4 Example policy YAML

```yaml
id: pay-new-payee-01
source: { doc: payments_policy.pdf, page: 3, clause: "4.2", span: [812, 1040] }
when: { action: value.transfer, amount: { gt: 10000 }, payee_new: true }
then: escalate
approver_role: payments-supervisor
tests:
  - { amount: 48000, payee_new: true,  expect: escalate }
  - { amount: 500,   payee_new: false, expect: allow }
```

### 2.5 Auditor answer schema

```json
{
  "answer": "string",
  "claims": [
    {
      "text": "Payment of $48,000 to Northwind Supply was approved by j.ortiz at 21:14",
      "record_ids": ["rec_..."],
      "assertions": [
        {"record_id": "rec_...", "field": "body.resource.amount", "op": "eq", "value": 48000},
        {"record_id": "rec_...", "field": "body.approver", "op": "eq", "value": "j.ortiz"}
      ]
    }
  ]
}
```

Rendering rule: each claim shows ✅ (proof and all assertions pass) or ❌ (reason). The final answer text only uses accepted claims.

---

## 3. Meta model usage (this is 25% of the score — make it visible)

| Model | Job | Why it is not decorative |
|---|---|---|
| Muse Wonder (`muse-wonder-1.0`) | Policy PDF → YAML rules with clause citations | Hard reading task; output is gated by compiler, fixtures and a human |
| Muse Spark 1.3 | Agent reasoning; approval briefs; auditor Q&A with tool calls and structured output; Policy Learner proposals | Every claim is machine-checked against cryptographic proofs |
| Muse Glimmer (open weights, Apache 2.0) or Muse Spark Open via Baseten/Modal | On-prem profile for the same flows | Regulated-customer story: data never leaves |
| Muse Code | Use for at least one real slice of the build (e.g. the HTMX pages) and say so in the README | Meta tooling counts toward the score |

Confirm exact model IDs, structured-output parameters and rate limits from the Meta Model API docs on day 1; record them in `docs/MODELS.md`. Base URL is OpenAI-compatible; call through LiteLLM only. Add retries with backoff and a fallback route to the open-weights endpoint.

Budget: about $150 of Model API credit. Use Spark for most calls, Wonder only for policy extraction. Log tokens per call to a CSV so the spend is visible.

---

## 4. Plan (Oct 26 → Nov 4)

| Day | Date | Deliver | Done when |
|---|---|---|---|
| 1 | Tue Oct 27 | Repo (public, Apache-2.0), compose, canonical records, producer signatures, chain, receipts | Tests prove any single-byte edit fails at its sequence |
| 2 | Wed Oct 28 | Merkle tree (RFC 9162 hashing), C2SP signed checkpoints, inclusion + consistency proofs, `pl-verify` | Verifier passes Merkle test vectors and names the exact edited row |
| 3 | Thu Oct 29 | Three witnesses, 2-of-3 quorum, RFC 3161 timestamps, OpenTimestamps submission | Rogue-operator test: history rewritten and re-signed with the log key is detected |
| 4 | Fri Oct 30 | Gate-lite (normalizer, `cedarpy`, write-before-effect, approvals) + demo agent on Muse Spark + approval briefs | $48K payment escalates before the tool runs and executes only after approval |
| 5 | Sat Oct 31 | Policy pipeline: PDF → Wonder → YAML → Cedar → fixtures → approve | One planted bad rule is caught |
| 6 | Sun Nov 1 | Auditor Q&A + claim checker; seed generator for one week of decisions | A hallucinated claim is rejected with a clear reason |
| 7 | Mon Nov 2 | Web UI for all beats (incl. tamper and rogue-operator buttons); deploy to Modal with managed Postgres | Core demo beats run at the live URL |
| 8 | Tue Nov 3 | Policy Learner; README with "Built during the hackathon" section; record video (≤3 min) | Learner beat works; fresh-clone `make demo` works; video recorded |
| 9 | Wed Nov 4 | Buffer; on-prem profile only if everything else is done; submit by 12:00 PM ET | Submitted |

Cut order if behind: on-prem profile → OpenTimestamps (keep RFC 3161) → MCP proxy (keep decorator) → learner tightening proposals (keep loosening with replay). Do not call the auditor Q&A "RAG" anywhere: basic RAG apps are on the organizers' anti-project list; describe it as proof-checked answers. Never cut: write-before-effect, witnesses, offline verifier, claim checker.

Demo beat added: **Rogue operator caught.** Rewrite history and re-sign it with the log key; the witnesses refuse to cosign and `pl-verify` shows the old cosigned root. This is what a plain hash chain cannot do.

The full ledger cryptography (formulas, envelope, threat model) is in the architecture doc "Precept Ledger: Hackathon Architecture"; export it to `docs/ARCHITECTURE.md` and treat it as the source of truth.

---

## 5. Engineering standards

- Python 3.12, `uv`, `ruff`, `mypy --strict` on `ledger/`, `verify/`, `claims/`, `gate/`.
- `pytest` with property-based tests (`hypothesis`) for canonicalization stability and "any single-byte edit breaks verification".
- Test vectors in `verify/testdata/` so anyone can check the verifier.
- All model outputs validated with Pydantic; one retry on schema failure, then fail closed.
- No secrets in the repo; `.env.example` only. Never log raw prompts containing account data; log hashes.
- `make demo` resets the database, seeds data and runs all five beats headlessly; `make video-seed` prepares the recorded run.
- README sections: problem, what's new, Meta models used and where, architecture diagram, how to run, how to verify offline, threat model (what the ledger proves and does not prove), limitations.

---

## 6. First actions for Claude Code

1. Scaffold the repo per section 2.1 with `pyproject.toml`, `docker-compose.yml`, `Makefile`, `LICENSE` (Apache-2.0), `CLAUDE.md`.
2. Write `docs/ARCHITECTURE.md` from section 2 and `docs/DECISIONS.md` with the hard rules from section 0.
3. Implement and test `ledger/` (append, canonicalization, chain, receipts) before anything else.
4. Stop and show me the ledger tests passing and the record format before starting the Merkle tree.
