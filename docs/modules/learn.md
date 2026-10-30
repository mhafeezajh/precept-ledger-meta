# Policy Learner (`learn/`): rule changes proposed from the ledger's own history, decided by people

(Demo beat 6 in ARCHITECTURE, "The ledger learns"; beat 5 in the kickoff list.)

Guarantees that a proposed rule change rests only on proven records and SQL counts, cannot loosen anything a person ever refused, cannot change anything outside the pattern it came from, and reaches Gate only after the required approvals and a normal policy approval. The learner proposes; it never activates, edits or deletes a rule (D8).

Status: done (demo beat 6, `make demo-learn` and the console's Learner page). Muse Spark drafts both demo proposals live, each passing every check, in 38 s for a run at `medium` reasoning effort (D90). · Last updated: 2026-10-29 · Tests: 19 unit in `tests/unit/test_learn.py` (18 run by default, 1 live), plus 6 learner import-boundary checks. Coverage: `patterns.py` 100%, `api.py` 99%, `replay.py` 98%, `service.py` 97%, `draft.py` 96%.

The prompt in force is the file's, or the latest version two officers approved through the improver, read back from the ledger and verified on every run (`learn/prompt.py`, [`improve.md`](improve.md), D95).

Related pages: [`policy.md`](policy.md) (rule format, checks, policy approval), [`claims.md`](claims.md) (claim checker, proven log), [`assist.md`](assist.md) (model entry point, mock), [`../MODELS.md`](../MODELS.md), [`../DECISIONS.md`](../DECISIONS.md) D80–D83.

## Responsibilities

Owns:

| Part | File |
| --- | --- |
| Loading proven records into SQLite; the pattern SQL; the fatigue filter; candidates and their scope | `learn/patterns.py` |
| The one model call: `LearnerProposal` schema, sources set by code, count, claim and reason checks | `learn/draft.py`, `assist/prompts/learn_proposal.md` |
| Replay of the window under the proposed set; what blocks a proposal | `learn/replay.py` |
| Runs, `learn.proposal` and `learn.approve` records, review rules, the policy draft an approved proposal becomes | `learn/service.py` |
| HTTP API (`/learn/v1`), served by `pl-gate`; the verified export view; `LEARN_*` settings | `learn/api.py` |
| The beat 6 demo | `scripts/demo_learn.py`, `make demo-learn` |

Does not own:

- **Deciding or activating.** Gate decides with Cedar; a proposal becomes a `policy.draft` that a compliance officer approves through `policy/` (`POST /policy/v1/drafts/{id}/approve`), like any policy.
- **Verifying the log.** `claims.log.prove_log` verifies the export (the same checks as `pl-verify`); the learner only counts what it returns.
- **Checking claims.** `claims.checker`, as for the auditor.
- **Rule format and checks.** `policy.rules` and `policy.checks`.

## How it works

### Beat 6 end to end

1. A compliance officer runs the learner: `POST /learn/v1/run?window=7d`. The learner reads the stream's export from the ledger API and verifies it up to the latest final (witnessed) checkpoint. If it does not verify, nothing is counted (`learn.log_unproven`).
2. The proven records go into an in-memory SQLite database as two tables, `decision` and `verdict`, values copied as recorded. Fixed SQL in `learn/patterns.py` joins escalations with their verdicts in the window, applies the fatigue filter and groups what is left.
3. For each candidate, Muse Spark gets the scope (as rule conditions and in words), the SQL counts, up to 12 rows and the active rules for the action. Each row is shown as its two records, `decision_record` and `verdict_record`, with every field under the path a claim asserts and the value that record holds; each active rule lists `fixtures_to_move`, the fixtures code found inside the scope that the change would flip (D90), and an answer that leaves such a rule out gets the one retry with the rule named. `policies_matched` lists the active escalate rules that hold the recorded requests now (`held_by`, re-evaluated under the active set), not the ids in the history, which a newer policy may have replaced (D91). It returns rule changes, a reason, the counts restated and claims about rows. Candidates are drafted concurrently and recorded in pattern order.
4. Code sets every changed rule's source, then checks: restated counts against SQL, claims against the proven log, the reason's numbers and names (rule ids, the scope's agent and action, and the learner's own words such as `known-payee` or `fast_dropped` need no claim; payee ids and approvers do), the merged set with every policy check, and replay of the whole window.
5. The proposal is appended as `learn.proposal`, signed by Gate, as `review`, `rejected` or `blocked`, with every reason.
6. Officers approve: one for a tightening, two distinct ones for a loosening, never the agent's owner. Each approval is a `learn.approve` record.
7. The last approval appends a `policy.draft`: the active rules with the change, checked again, citing the proposal and the approvals. `a.chen` approves it through the policy API and Gate enforces it from then on.

```mermaid
sequenceDiagram
    participant O as Compliance officers (a.chen, r.patel)
    participant L as pl-gate /learn/v1
    participant X as Ledger export (verified)
    participant M as Muse Spark (assist/llm.py)
    participant P as /policy/v1
    O->>L: run, window 7d
    L->>X: export, prove_log (final checkpoint)
    L->>L: SQLite: SQL patterns, fatigue filter
    L->>M: scope, SQL counts, rows, active rules
    M-->>L: rule changes, reason, counts, claims
    L->>L: sources by code, counts = SQL, claim checker, policy checks, replay
    L->>X: learn.proposal (review / rejected / blocked)
    O->>L: approve (a.chen), approve (r.patel)
    L->>X: learn.approve x2, then policy.draft (active rules + change)
    O->>P: approve the policy draft
    P->>X: policy.approve; Gate enforces the new set
```

### Patterns and the fatigue filter

| Step | SQL (in `learn/patterns.py`) | Seeded week |
| --- | --- | --- |
| Reviewed | `gate.decision` with `outcome = 'escalate'` and `ts_ms >= :since`, joined with its `gate.verdict` | 77 escalations with verdicts |
| Approver stats | verdicts and approvals per approver | j.ortiz 27 of 35, m.lee 36 of 42: neither at 100% |
| Fast | an approval with `verdict_ms < 5000` | 6, all known-payee routine payments |
| Rubber stamp | an approval by an approver with at least 20 verdicts, all approvals | none |
| Group | `agent, action, payee_new, business_hours` (Monday to Friday, 09:00–16:59 local) over what is left | 5 groups |
| Amount bands | each amount in (0, $5k], ($5k, $15k], ($15k, $50k], ($50k, $250k]; none above the cap | loosening scope "over $5,000 and up to $15,000" |

Rejections are never dropped. A group becomes:

| Candidate | Rule (`MIN_SAMPLE` 20, `TIGHTEN_SHARE` 0.5) | Seeded week |
| --- | --- | --- |
| Loosen | at least 20 decisions, all approved, every amount in a band | known payees, business hours: 38 approved, 6 fast approvals set aside |
| Tighten | at least 20 decisions, half or more rejected or expired | new payees, after hours: 20, 14 rejected |
| Skipped | fewer than 20; mixed; an amount above $250,000; or the active set already does it (D83) | email 5, known after hours 2, new payees in business hours 6 |

### What the model may and may not do

- It writes rules, fixtures, a title, a reason and claims. It cannot set a rule's `source`: code sets it to the scope statement (`doc: learner`, `clause: learned`), or, for a replaced rule, keeps the clause and appends `Learned exception: <scope>`. So `policy.checks`' clause-number check flags any amount, hour or weekday the scope does not state.
- It may add rules or replace active ones by id. It cannot remove a rule.
- Restated counts must equal SQL exactly. Claims are checked by `claims.checker` against the proven log. The reason may state only SQL counts, the scope's numbers, values that accepted claims assert, and the ids of the rules, agent and action involved. Any failure means `rejected`, with no retry.
- Structure (rule shape, an action other than the scope's, record ids that are not the pattern's, a count stated twice) gets one retry with the problems.

### Replay

Every decision in the window recorded the Cedar context it was decided on (`body.resource`), so replay evaluates it under the active set and under the proposed set and lists each change, with its verdict and whether it is inside the scope. The scope is compiled to a one-rule Cedar set, so "inside" means exactly what Cedar would match. The active rules' own fixtures are replayed too.

| Blocked when | Seeded week |
| --- | --- |
| A loosening would allow a past request someone rejected or let expire | loosening: 0 |
| A loosening makes anything stricter, or a tightening anything looser | 0 |
| Nothing in the window changes | |
| A changed decision, or an active fixture whose outcome changes, is outside the scope | 0 |

Seeded week results: the loosening removes 44 escalations (the 38 counted plus the 6 fast approvals, which were still escalations). The tightening denies 20 requests, 6 of which a supervisor had approved.

## Interfaces

| Function / type | Input | Output | Errors |
| --- | --- | --- | --- |
| `patterns.load(records)` | proven record dicts | SQLite connection | |
| `patterns.find_patterns(db, Window(since, min_sample, fast_seconds))` | | `list[Pattern]` (scope, direction or skip reason, counts, rows, dropped, rubber-stamp approvers) | |
| `patterns.approver_stats(db, window)` | | verdicts and approvals per approver | |
| `Scope.when()`, `.unless()`, `.statement()`, `.numbers()`, `.key()` | | rule conditions, words, stated numbers | |
| `LearnDrafter(llm).draft(pattern, base_rules, log, inject_fault=)` | | `Drafted`: answer, changed and merged rules, stated counts, problems, claim results | `ModelOutputError` |
| `draft.merge_rules(base, changed)` | | the base with replacements and additions | |
| `replay.replay(records, since, base_rules=, before=, after=, scope=, direction=)` | | `Replay`: changes, fixture drift, `counts()`, `blocked` | |
| `LearnService.run(officer, window_days=, inject_fault=)` | | `LearnRun`: proposals (receipted), skipped, failed | `learn.forbidden`, `learn.window`, `learn.no_model`, `learn.no_active_policy`, `learn.base_mismatch`, `learn.log_unproven`, `learn.ledger_unavailable` |
| `LearnService.approve(proposal_id, officer)` | | `Approved(approval, policy_draft or None)` | `learn.forbidden`, `learn.not_reviewable`, `learn.owner`, `learn.already_approved`, Gate's read errors |
| `LearnService.approvals(proposal_id)` | | verified `learn.approve` records | |

HTTP (approver token with the `compliance-officer` role):

| Method | Path | Success | Errors |
| --- | --- | --- | --- |
| POST | `/learn/v1/run?window=7d&inject_fault=false` | 201 `{window_days, log, proposals, skipped, failed}` | 400 `learn.window`; 403 role, or `inject_fault` without `DEMO_MODE=true`; 409 `learn.no_active_policy`; 503 `learn.no_model`, `learn.log_unproven` |
| GET | `/learn/v1/proposals/{id}` | 200 `{proposal, approvals}` | 404 |
| POST | `/learn/v1/proposals/{id}/approve` | 201 `{approval, policy_draft}` (`policy_draft` set by the last approval) | 403 `learn.forbidden`, `learn.owner`; 409 `learn.not_reviewable`, `learn.already_approved` |

## Data

No tables (D81). Every step is a Gate-signed ledger record.

`learn.proposal`: `agent`, `direction`, `title`, `reason`, `pattern{key, statement, scope, counts, decision_record_ids, fatigue{fast_seconds, min_sample, dropped_decision_record_ids, rubber_stamp_approvers}}`, `window{days, since, log_size, log_root}`, `stated_counts`, `claims[{text, record_ids, accepted, reasons}]`, `rules_yaml` (changed rules only), `policy_set` (of the merged set), `replay{replayed, not_replayable, escalations_removed, new_escalations, new_denials, denials_lifted, rejections_newly_allowed, approvals_now_denied, outside_scope, changed[], fixture_drift[], blocked[]}`, `state` (`review`, `rejected`, `blocked`), `reasons`, `approvals_needed`, `model`, `prompt_version`, `injected`, `base{policy_record_id, draft_record_id, policy_set}`.

`learn.approve`: `proposal_record_id`, `approver`, `direction`, `approval` (n), `approvals_needed`, `earlier` (ids of the approvals before it).

The `policy.draft` an approved proposal becomes has the usual fields (see [`policy.md`](policy.md)) plus `proposal_record_id`, `learn_approvals` and `base_policy_record`. `author` is `learner`.

## Security and failure behaviour

| Attack or failure | Result | Test |
| --- | --- | --- |
| The model inflates a count (demo: `--inject-fault`, 38 stated as 48) | `rejected`: "count decisions: stated 48, SQL counted 38"; cannot be approved | `test_an_inflated_count_rejects_the_proposal` |
| A claim misstates a record (wrong approver) | `rejected` with the checker's reason | `test_a_wider_rule_or_a_wrong_claim_is_caught` |
| A rule wider than the pattern ($50,000 for a $15,000 scope) | `blocked`: clause-number finding against the scope statement | same |
| Claims citing records outside the pattern | One retry, then no proposal | `test_structure_gets_one_retry_and_a_model_failure_gives_no_proposal` |
| Model down or refusing | No proposal for that pattern; listed in `failed` | same |
| Rubber-stamp approvals teach a loosening | Under 5 s, or an approver at 100% over 20 or more verdicts, set aside; fewer than 20 decisions left means no candidate | `test_sql_finds_the_demo_patterns_and_sets_rubber_stamps_aside`, `test_an_approver_who_approves_everything_is_set_aside` |
| A loosening that would allow a past rejection | `blocked` | `test_replay_blocks_what_a_loosening_or_tightening_must_not_do` |
| A change outside the pattern, or one that moves the wrong way | `blocked` | same |
| Payments above the $250,000 cap | Never a loosening candidate | `test_amounts_above_the_cap_are_never_loosened` |
| One approver loosens; the same approver twice; the agent's owner; a supervisor | Refused (`learn.already_approved`, `learn.owner`, `learn.forbidden`) | `test_beat_6_proposals_replayed_reviewed_and_enforced`, `test_beat_6_over_http` |
| Approving a rejected or blocked proposal, or one already approved | `learn.not_reviewable` | same |
| The learner activates a rule | Not possible: approval produces only a `policy.draft`; Gate enforces it after a policy approval | same |
| Two learner drafts approved one after the other, the second built on the old set | `policy.stale_base`: refused, never silently reverting the first | same |
| A pattern the active set already settles | Skipped, never re-proposed | same (rerun) |
| An export that does not verify (wrong origin, bad signature, below quorum) | `learn.log_unproven`; nothing counted | `test_the_export_view_counts_only_a_proven_log` |
| Ledger unreachable | `learn.ledger_unavailable` | same |
| A forged `learn.approve` record in the store | Each approval is re-read through Gate and checked against Gate's key and its receipt | `LearnService.approvals` |
| The learner reaching a model in counting or replay, or the ledger except through Gate | Import tests | `test_the_learner_counts_and_replays_without_a_model` |

What it does not protect against:

- **A pattern that is real but unwise.** All approved and well sampled still does not mean safe; that is why two officers review it, and a policy approval follows.
- **Records still waiting for a final checkpoint.** They are not counted (`log.newer` reports how many), so a run right after a burst of decisions sees slightly less.
- **History under an older policy.** Replay compares the active set with the proposed set, not with what was decided then. Changes are relative to today's policy.

## How to run and test

```bash
make serve                    # with WITNESSES=..., so checkpoints become final
make gate DEMO_MODE=true      # MODEL_PROFILE=mock (scripted) or cloud (Muse Spark)
make demo-policy              # an approved policy must be active
make seed-week                # one simulated week
make demo-learn               # ARGS="--no-approve" or ARGS="--inject-fault"
uv run pytest tests/unit/test_learn.py
uv run pytest -m live -s -k muse_spark tests/unit/test_learn.py   # Muse Spark, about 2 calls
```

Settings (`LEARN_*`, see `.env.example`): `AGENT_OWNERS` (`payments-ops=j.ortiz`), `MIN_SAMPLE` (20), `FAST_SECONDS` (5), `EXPORT_TIMEOUT_SECONDS` (30), `WITNESSED` (true: only checkpoints cosigned by the quorum count).

Run against the dev stack on 2026-10-28, mock profile, abridged:

```
The learner verified the export: 308 records under the final checkpoint (root thdJOJXaS72K…); 0 newer not counted.
  skipped payments-ops/value.transfer/new/after-hours: 15 decisions after the fatigue filter; at least 20 needed
[1] LOOSEN: Allow routine requests in this pattern without approval  (ready for review)
    pattern: Payments to known payees over $5,000 and up to $15,000 by payments-ops, Monday to Friday from 9:00 to 17:00.
    SQL: 38 decisions, 38 approved, 0 rejected; fatigue filter set aside 6 approvals (under 5 s or a 100% approver)
    ✅ A payment of $6,265.34 to keystone-it was approved by m.lee.
    rule pay-known-business-allow-01    allow     (source: learned)
    rule pay-over-5k-01                 escalate  (source: 3.2)
    replay of 189 past decisions: 44 escalations no longer needed
Review [loosen]: needs 2 approver(s)
    a.chen approves (1 of 2)
    a.chen again: 409 learn.already_approved
    r.patel approves (2 of 2)
    policy draft 01M62RD8E68PFTM2EMC9FYXVKG: 0 findings
    a.chen approves it as policy: Gate now enforces sha256:5f19092a36a2… (9 rules)
```

That dev stream held the earlier 14-row seed, so the tightening pattern had 15 decisions there and was skipped. After the clean reset in the runbook and `make seed-week`, it has 20 and is proposed, as in the unit tests.

## Decisions

D8 (learner proposes, humans approve), D73 (policy records in the ledger), D76 (seeded week), D80 (SQL over the proven export), D81 (proposals as ledger records; the policy draft and the stale-base check), D82 (scope from code, sources set by code, what blocks), D83 (settled patterns and the seed's 20 after-hours requests), D90 (live: the row view, fixtures to move, the reason's vocabulary, concurrent drafting, reasoning effort), D91 (live after the clean reset: rules that hold the requests now, every rule with fixtures to move replaced).

## Known gaps

- One pattern grouping (agent, action, payee status, business hours, amount bands). Per-payee or per-amount-band patterns are not looked for.
- The window is counted from now; a run long after `make seed-week` sees less of the seeded week.
