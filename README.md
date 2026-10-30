# Precept Ledger

Gate and tamper-evident ledger for AI agent actions in regulated finance. Entry for the Meta Global AI Developer Hackathon (Oct 27 – Nov 4, 2026), Indie Developer track. Licence: Apache-2.0.

Work in progress. Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Knowledge base (one page per module): [`docs/modules/`](docs/modules/README.md). Plan: [`docs/HACKATHON_KICKOFF.md`](docs/HACKATHON_KICKOFF.md). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Module status

| Module | Status | Docs |
| --- | --- | --- |
| `ledger/` | Partial. Canonical records, producer signatures, hash chain, receipts, memory and Postgres stores, C2SP signed checkpoints, the Postgres tree adapter, the HTTP API (`pl-ledger`), the 10 s checkpoint loop and witness collection with 2-of-3 finality done. Batch append and read auth later | [`docs/modules/ledger.md`](docs/modules/ledger.md), [`docs/modules/checkpoints.md`](docs/modules/checkpoints.md), [`docs/modules/api.md`](docs/modules/api.md) |
| `merkle/` | Done for now. RFC 9162 core, inclusion and consistency proofs, `TreeStore` port, memory adapter | [`docs/modules/merkle.md`](docs/modules/merkle.md) |
| `verify/` (`pl-verify`) | Done for now. Export bundle v2, `pl-export`, offline `pl-verify` naming the first broken seq and record, witness quorum (`--witness-keys`, `--quorum`), RFC 3161 timestamps and backdating (`--tsa-roots`), OpenTimestamps claims | [`docs/modules/verify.md`](docs/modules/verify.md) |
| `witness/` (`pl-witness`) | Done for this step. C2SP tlog-cosignature and tlog-witness, three witnesses with their own keys and state, 2-of-3 finality, demo beat 5 (`make demo-rogue`); compose services later | [`docs/modules/witness.md`](docs/modules/witness.md) |
| `anchor/` | Done for this step. RFC 3161 timestamps from DigiCert and FreeTSA on every final checkpoint, verified offline; OpenTimestamps (Bitcoin) submit and upgrade; confirmed Bitcoin vector after O5 | [`docs/modules/anchor.md`](docs/modules/anchor.md) |
| `gate/` (`pl-gate`) | Done for this step. Normalizer, Cedar allow / deny / escalate (`cedarpy`), signed decision, verdict and result records, write-before-effect, approval queue with expiry, agent and approver tokens, `@governed` wrapper that verifies receipts itself, demo beat 2 (`make demo-gate`); MCP proxy deferred, briefs with `assist/` | [`docs/modules/gate.md`](docs/modules/gate.md) |
| `policy/` | Done for this step (demo beat 1, `make demo-policy`). Policy PDF to clauses with spans (pypdf), Muse Wonder drafting (run on the mock so far), YAML rules compiled to Cedar and checked (schema, fixtures, allows everything, numbers not in the clause), review as signed ledger records, approval that re-checks and refuses findings, Gate activation and restore at startup | [`docs/modules/policy.md`](docs/modules/policy.md) |
| `assist/` | Done for this step. `assist/llm.py`, the only model entry point (Muse Spark via LiteLLM, Standard mode only, structured output, one checked retry, call log); approval briefs recorded as `assist.brief`; auditor Q&A (beat 3, `make demo-audit`) recorded as `assist.qa`; run live against the Meta Model API on Oct 28 (beats 2 and 3) | [`docs/modules/assist.md`](docs/modules/assist.md), [`docs/MODELS.md`](docs/MODELS.md) |
| `claims/` | Done for this step. Claim checker: producer signature, witnessed checkpoint and inclusion proof per cited record, or the whole export proven up to the final checkpoint; the auditor's query run in code over that proven prefix; exact assertions; numbers and names in claims and the summary backed by assertions | [`docs/modules/claims.md`](docs/modules/claims.md) |
| `learn/` | Done for this step (demo beat 6, `make demo-learn`). Policy Learner served by `pl-gate` at `/learn/v1`: SQL patterns over the verified export, fatigue filter, Muse Spark proposals (run on the mock so far) checked against SQL and the claim checker, replay that blocks past rejections and out-of-pattern changes, one or two compliance officers, then a normal policy approval | [`docs/modules/learn.md`](docs/modules/learn.md) |
| `agent/` (`pl-agent`) | Done for this step (demo beat 2 with the agent, `make demo-agent`). payments-ops on LangGraph: Muse Spark picks each step as structured output through `assist/llm.py`, every tool call goes through `@governed` and Gate, repeated calls skipped, 8-step limit, in-memory demo bank. Run on the mock so far | [`docs/modules/agent.md`](docs/modules/agent.md) |
| `web/` (`pl-web`, `pl-approvals`) | Done for this step (every beat in the browser, `make web` + `make approvals`). Two apps from one codebase (D94): the Approvals inbox where people sign, and the Console. Server-rendered with htmx vendored, strict CSP, CSRF, persona menu (demo stand-in for sign-in), the proof seal from its own verification, live agent runs, the tamper and rogue-operator buttons in demo mode | [`docs/modules/web.md`](docs/modules/web.md) |

## How to run what works now

The full start order, the demo week and a clean reset for recording are in [`docs/runbook.md`](docs/runbook.md).

Requires Python 3.12, [`uv`](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
make check              # lint, strict types, 1055 unit + property tests (coverage ≥ 90%), secret scan
cp .env.example .env    # set the passwords
make test-integration   # Postgres 16 via docker compose; 27 tests incl. demo beat 4 end to end
                        # (edit a row, export, pl-verify names the record), 2-of-3 witnessing, anchors,
                        # Gate escalations
make vectors            # regenerate tests/vectors/{ledger,checkpoint,cosignature}-v1.json
make demo-rogue         # demo beat 5: operator rewrites history, witnesses refuse, pl-verify catches it
make dev-keys           # keys in KEYS_DIR (outside the repo): producer, log, three witnesses,
                        # Gate caller tokens (payments-ops, j.ortiz, m.lee)
make witnesses          # three witnesses on 127.0.0.1:8091-8093 (own keys, own state)
make serve              # ledger API on 127.0.0.1:8080, checkpoint every 10 s, cosigned by WITNESSES,
                        # timestamped by TSAS, OpenTimestamps hourly (see .env.example)
uv run pl-export --out export.tar
uv run pl-verify export.tar --keys "$KEYS_DIR/trusted.json" --witness-keys "$KEYS_DIR/witnesses.txt" \
  --tsa-roots deploy/tsa/roots.pem
make gate               # Gate on 127.0.0.1:8070 in front of the ledger (policies: deploy/policies/)
DEMO_MODE=true make web # the Console on http://127.0.0.1:8090: agent, rules, learner, audit, ledger, attacks
make approvals          # the Approvals app on http://127.0.0.1:8094: everything waiting for a signature
make demo-gate          # demo beat 2, scripted: $48,000 to a new payee is held until j.ortiz approves;
                        # with META_API_KEY set, Muse Spark's brief is shown first
make demo-agent         # demo beat 2 with the LangGraph agent: lookup, $48,000 held, brief, j.ortiz
                        # approves, paid, accounts payable emailed; ARGS="--manual" to approve yourself
uv run pl-agent 'Pay Keystone IT $120 for invoice INV-2.'   # one task for the agent
make demo-audit         # demo beat 3: examiner's question, each claim proven or rejected (needs
                        # META_API_KEY); ARGS="--inject-fault" shows a wrong claim caught
make seed-week            # one simulated week of decisions (169 decisions, 77 verdicts) through
                          # Gate, for beats 3 and 5; records are backdated, receipts keep the real time
make demo-policy          # beat 1 against make serve + make gate DEMO_MODE=true: a bad
                          # drafted rule caught, fixed, approved, enforced
GATE_REQUIRE_DELEGATION=true make gate; make delegate   # the principal delegates to payments-ops (ARGS=revoke)
make demo-learn           # beat 6 after demo-policy and seed-week: the learner proposes, two officers
                          # approve the loosening, one the tightening, then each policy draft
uv run pytest -m live -s  # real Muse Spark brief, audit answer and agent run (needs META_API_KEY)
# MODEL_PROFILE=mock in .env: briefs, audit answers, policy drafts, agent steps and learner proposals come from assist/mock.py, offline and
# free, labelled mock/scripted; MODEL_PROFILE=cloud for the live Meta Model API (D72)
make ots-upgrade        # hours after submission: record the confirmed Bitcoin proof
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
| Oct 27 | `ledger/cosignature.py`: C2SP `cosignature/v1` cosignatures and the tlog-witness request format; vectors in `tests/vectors/cosignature-v1.json` |
| Oct 27 | `witness/` (`pl-witness`): witness core, own file state with refusal evidence, key loading, HTTP API; `common/http.py` shared by both APIs |
| Oct 27 | `ledger/witnessing.py`, `ledger/witness_client.py`, `cosignature` table: collecting and verifying cosignatures, 2-of-3 finality, cosigned checkpoints in the API and export; `pl-verify --witness-keys --quorum`; `scripts/demo_rogue_operator.py` (demo beat 5) |
| Oct 28 | `anchor/rfc3161.py`, `anchor/ots.py`, `anchor/clients.py`: RFC 3161 requests and offline token verification, OpenTimestamps proof reading and upgrading, TSA and calendar clients; real TSA tokens and calendar proofs as vectors (`tests/vectors/rfc3161-published.json`, `ots-published.json`) |
| Oct 28 | `ledger/anchoring.py`, `anchor` table, export bundle v2, `verify/anchors.py`: anchoring final checkpoints, anchors in the API, `pl-verify --tsa-roots` with backdating checks; `deploy/tsa/` roots; `scripts/upgrade_ots_vectors.py` (`make ots-upgrade`) |
| Oct 28 | `gate/normalizer.py`, `gate/decide.py`, `gate/schema.cedarschema`, `deploy/policies/harbor-federal.cedar`: tool calls to action classes and facts, Cedar decisions with `@escalate`, seed demo policies |
| Oct 28 | `gate/core.py`, `gate/records.py`, `gate/escalations.py`, `gate/ledger_client.py`, `gate/governed.py`: write-before-effect decisions, verdicts, expiry and result records; the `@governed` wrapper |
| Oct 28 | `gate/api.py`, `gate/auth.py`, `gate/client.py`, `gate/server.py` (`pl-gate`), `gate/pg_escalations.py`, `gate/schema.sql`, `deploy/demo/payees.json`, `scripts/demo_gate.py` (`make demo-gate`, demo beat 2); Gate tokens in `scripts/dev_keys.py` |
| Oct 28 | `assist/llm.py`: the model entry point (LiteLLM, Meta Model API, Standard mode, structured output, retries, redaction, call log); `docs/MODELS.md` |
| Oct 28 | `assist/brief.py`, `assist/prompts/approval_brief.md`: approval briefs grounded in ledger history and checked; Gate's `BriefWriter` port and `assist.brief` records |
| Oct 28 | `claims/checker.py`: the claim checker |
| Oct 28 | `assist/auditor.py`, `assist/evidence.py`, auditor prompts, `scripts/demo_audit.py` (`make demo-audit`, demo beat 3), `assist-1` producer key in `scripts/dev_keys.py` |
| Oct 28 | `claims/log.py` (whole export proven up to the final checkpoint), `claims/query.py` (the auditor's query, run over proven records), names and the summary checked in `claims/checker.py` |
| Oct 28 | `policy/rules.py`, `policy/compiler.py`, `policy/checks.py`, `deploy/policies/harbor-federal.yaml`, `deploy/demo/payments_policy.txt` (policy step 1 of 3, demo beat 1) |
| Oct 28 | First live Meta Model API runs (beats 2 and 3); brief history fix; `assist/mock.py`, the `mock` model profile for offline development |
| Oct 28 | `policy/pdf.py`, `policy/draft.py`, `policy/service.py`, `policy/api.py`, `assist/prompts/policy_draft.md`, `deploy/demo/payments_policy.pdf`, `scripts/make_policy_pdf.py`, `scripts/demo_policy.py` (`make demo-policy`, demo beat 1); compliance officer `a.chen` in `scripts/dev_keys.py` |
| Oct 28 | `scripts/seed_week.py` (`make seed-week`): one simulated week of decisions through the real Gate code; `pl-export` now includes cosignatures and anchors |
| Oct 28 | `agent/` (`pl-agent`): the payments-ops agent on LangGraph (`graph.py`), Muse Spark steps as structured output (`steps.py`, `assist/prompts/agent_step.md`), the demo bank (`bank.py`, `deploy/demo/vendors.json`), wiring and CLI (`cli.py`); `Governor.run_cleared` in `gate/governed.py`; the mock's agent script; `scripts/demo_agent.py` (`make demo-agent`, demo beat 2 with the agent) |
| Oct 28 | `learn/`: the Policy Learner (`patterns.py` SQL over the verified export and the fatigue filter, `draft.py` and `assist/prompts/learn_proposal.md`, `replay.py`, `service.py`, `api.py` at `/learn/v1` in `pl-gate`); `check_summary` names in `claims/checker.py`; `active_draft` and the stale-base check in `policy/service.py`; the mock's learner script; `scripts/demo_learn.py` (`make demo-learn`, beat 6); seed week 20 after-hours new-payee requests; `r.patel` in dev keys |
| Oct 28 | `web/` (`pl-web`): the console (`app.py`, `nav.py`, `security.py`, `backend.py`, `views.py`, `agent_runs.py`, `demo.py`, `routes/`, `templates/`, `static/css/app.css`, `static/js/`); htmx 2.0.4 vendored; `make web` |
| Oct 29 | `gate/delegation.py`, `gate/delegation_records.py`: principal-signed delegation credentials checked before Cedar (missing, bad signature, not issued, revoked, expired, out of scope all deny), issue and revoke as ledger records, revocation derived from the ledger; `tests/vectors/delegation-v1.json`; off by default (`GATE_REQUIRE_DELEGATION`) |
| Oct 29 | Delegation switched on: the principal `treasury.harbor-federal` in `make dev-keys`, `scripts/delegate.py` (`make delegate`, issue, revoke, status), the agent presenting its saved credential, Gate health and the console showing delegation |
| Oct 29 | The live model switch: `MODEL_REASONING_EFFORT` in `assist/llm.py`; the learner's rows as their two records, `fixtures_to_move`, its own vocabulary in the reason check and concurrent drafting (`learn/draft.py`, `learn/service.py`, prompt `learn_proposal/2`); a non-string rule action refused in `policy/rules.py`. Live suite and beat 2 run on Muse Spark; beat 1's first live draft on Muse Wonder (`test_beat_1_on_muse_wonder`) |
| Oct 29 | Clean reset and the demo run live on Muse Spark and Muse Wonder (beats 1, 2, 3, 4, 6, D91): timeouts never retried and the SDK's hidden retries off (`assist/llm.py`), `learn.draft.held_by` (the active rules that hold the requests now), prompt `learn_proposal/3` and its structure check |
| Oct 30 | Faster audits: a per-call `reasoning_effort` in `assist/llm.py`, the auditor at `minimal` (21 s instead of 40–47 s, every claim still checked; D92) |
| Oct 30 | The web UI split into two apps (D94): the Approvals app (`pl-approvals`, `make approvals`), an inbox of held payments, policy drafts and learner suggestions to sign, plus what you decided; the Console keeps operating and proving and can no longer approve. Each app mounts only its own routes and has its own session cookie |
| Oct 30 | The console streamlined (D93): the menu in story order with plain page names, who you are acting as at the top of every page with a one-click switch, rules in force shown to everyone from the verified ledger, current state before compact history, out-of-date suggestions marked, example audit questions |

Meta tooling used so far: Muse Spark 1.3 (`muse-spark-1.3`, Standard mode) writes approval briefs and auditor answers, picks the payments agent's steps and drafts the learner's proposals, through `assist/llm.py`; see [`docs/MODELS.md`](docs/MODELS.md). Gate itself makes no model calls (R2). Muse Code will be used for at least one slice and recorded here.
