# Runbook: running the demo locally

How to start the services, fill the ledger with a demo week, run each beat, and reset to a clean stream for recording. Commands are from the repository root; settings come from `.env` (see `.env.example`).

## Start

| Order | Command | What it is | Port |
| --- | --- | --- | --- |
| 1 | `make db-up` | Postgres (docker compose) | 5432 |
| 2 | `make dev-keys` | TEST-grade keys and tokens in `KEYS_DIR`, outside the repo; adds missing producers and callers, never changes existing ones | |
| 3 | `make witnesses` | Three witnesses, each with its own key and state in `var/witness-N` | 8091–8093 |
| 4 | `make serve` | The ledger, checkpointing every 10 s; set `WITNESSES` in `.env` (see `.env.example`) or nothing becomes final | 8080 |
| 5 | `make gate` | Gate, the approval queue and the policy API; `make gate DEMO_MODE=true` for the planted policy fault (a make argument, because the Makefile reads `.env` and its value wins over the environment) | 8070 |

Restart `make serve` after `make dev-keys` adds a producer (it reads `trusted.json` at start), and `make gate` after it adds a caller (`gate-callers.txt`).

## Models

`MODEL_PROFILE=mock` (development): briefs, audit answers and policy drafts come from `assist/mock.py`, offline and free, labelled `mock/scripted` in every record. `MODEL_PROFILE=cloud` with `META_API_KEY`: Muse Spark and Muse Wonder through the Meta Model API. Restart `make gate` after switching. Set it in `.env`, or pass it as a make argument (`make gate MODEL_PROFILE=cloud`, `make demo-agent MODEL_PROFILE=cloud`): the Makefile loads `.env`, and its values win over a variable set in front of `make`. `MODEL_REASONING_EFFORT` (default `medium`) sets how long the model thinks; at `medium` a learner run takes 40 s to 2 minutes (one call took 119 s; `MODEL_TIMEOUT_SECONDS` is 150, never retried) and beat 2 about 35 s. See [`MODELS.md`](MODELS.md).

## The console

`DEMO_MODE=true make web` and `make approvals`, then open the Console at http://127.0.0.1:8090 and the Approvals app at http://127.0.0.1:8094 side by side (D94). The Console drafts and operates: Rules in force (beat 1), Run the agent (beat 2), Ask the record (beat 3), Ledger and Attacks (beats 4 and 5), Suggested changes and Improve the learner (beat 6). Every signature happens in the Approvals app's **Waiting for you** inbox: the held $48,000 payment (beat 2), the fixed policy draft (beat 1), the learner's suggestions and changes to the learner's prompt (beat 6). Each app links to the other from the foot of its menu. Each app asks you to sign in (D96); the passwords `make dev-keys` made are in `$KEYS_DIR/web-passwords.txt`. Sign in as j.ortiz or m.lee to run the agent and approve payments, a.chen or r.patel for rules, suggested changes, the learner's prompt and Ask the record, `examiner` to ask the record and read the ledger, and `demo-admin` for the Attacks page. Each person sees only their own screens; to change person, use Sign out at the top of the page. The tamper button needs `ADMIN_DATABASE_URL`, edits the live stream and cannot be undone without the clean reset below. Start `make web` and `make approvals` after `make gate`.

## Delegation (who authorized the agent)

`make dev-keys` creates the principal `treasury.harbor-federal`. Start Gate with `GATE_REQUIRE_DELEGATION=true make gate`, then `make delegate`: the principal signs a credential for payments-ops (the six demo vendors, up to $250,000, 30 days), a.chen submits it, and Gate records `gate.delegation.issue`. The agent picks the credential up from `KEYS_DIR/credentials/payments-ops.json`. `make delegate ARGS=revoke` takes it back (every later call is denied `delegation.revoked`, also after a Gate restart); `ARGS=status` shows it. Without `GATE_REQUIRE_DELEGATION=true`, Gate ignores credentials and the beats run as before.

## Demo week and beats

| Step | Command | Result |
| --- | --- | --- |
| Seed | `make seed-week` | 169 decisions and 77 verdicts dated across the past week, through the real Gate code (D76) |
| Beat 1 | `make demo-policy` | The policy PDF drafted into eight rules; the planted $50,000 rule flagged; approval refused; fixed; approved; Gate switches to the approved set |
| Beat 2 | `make demo-agent` | The payments-ops agent (LangGraph) looks up Northwind Supply (allowed), pays $48,000 (held; brief; j.ortiz approves; paid), emails accounts payable (allowed). `ARGS="--manual"` to approve yourself; `uv run python scripts/demo_agent.py --task 'Pay Acme Paper $300,000 for invoice INV-9.'` shows a denial |
| Beat 2 (scripted) | `make demo-gate` | Without the agent: $120 runs; $48,000 to a new payee waits for the brief and j.ortiz's approval; $300,000 denied |
| Beat 3 | `make demo-audit`, then `ARGS="--inject-fault"` | On a clean seeded stream, five proven payments over $10,000 approved outside business hours; with the fault, one claim rejected with its reason |
| Beat 4 | `uv run pl-export --out export.tar` then `uv run pl-verify export.tar --keys "$KEYS_DIR/trusted.json" --witness-keys "$KEYS_DIR/witnesses.txt" --quorum 2` | `RESULT OK`; after an admin edit of one row, the first broken record is named |
| Beat 5 | `make demo-rogue` | The operator rewrites history; witnesses refuse; `pl-verify` catches it |
| Beat 6 | `make demo-learn`, `ARGS="--inject-fault"` | After beat 1 and the seed: the learner proposes loosening 38 routine known-payee escalations (6 rubber-stamp approvals set aside; replay: 44 escalations removed, 0 past rejections allowed) and denying after-hours new-payee payments (20, 14 rejected); a.chen and r.patel approve the loosening, a.chen the tightening, and a.chen approves each policy draft. With the fault, an inflated count is rejected. Needs `make dev-keys` once for r.patel |
| Beat 6, the learner learns (D95) | Console: Improve the learner, as a.chen; Approvals app: Waiting for you | **Run it after the learner proposes and before its changes are approved**: once the rules handle both patterns there is nothing to test a prompt on (`improve.no_cases`). Propose a change (about 2 minutes live, so run it before recording); then propose again with "Plant a shortcut" ticked: it accepts as many drafts but asserts far fewer facts, the wrong amount and approver have "Nothing to check", and code blocks it. a.chen and r.patel approve the honest change in the Approvals app; the next learner run uses it |

Seeded records are backdated by design: their receipts carry the real arrival time. With `--tsa-roots` and timestamps on, `pl-verify` reports them as `late_timestamp`, which is the backdating check working. Keep `TSAS` empty while seeding if the recording should show `RESULT OK` with timestamps.

## One command

`make demo` does everything below in one go: the clean reset, the witnesses, the ledger (timestamps off, see above), Gate in demo mode, the seeded week and beats 1 to 6 headless, then it stops the services. `make video-seed` stops after beat 1 and leaves the Console and the Approvals app running for recording. Both refuse to start while any demo port (8070, 8080, 8090–8094) is in use, and ask before wiping (`YES=1` skips the question). Models are the offline mock unless `DEMO_PROFILE=cloud`. Service logs go to `var/demo-logs/`.

## A clean stream for recording

The dev stream collects every test run, so counts in beat 3 and in briefs include them. To start clean, stop every service first, then:

```sh
make db-down            # drops the Postgres volume: every ledger record and escalation
rm -rf var/checkpoints var/witness-1 var/witness-2 var/witness-3
make db-up && make db-schema
```

Witness state must go with the database: a witness that remembers a larger tree would see the fresh log as a rollback and refuse to cosign, which is the rogue-operator defence doing its job. Keys in `KEYS_DIR` can stay. Then start the services, `make seed-week`, and run the beats in order (beat 1 first, so Gate enforces the approved set).

These commands delete data and cannot be undone; they are for a development machine only.
