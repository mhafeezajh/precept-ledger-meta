# Models

Every model the project calls, with the exact IDs, parameters and limits, checked against Meta's Model API documentation on 2026-10-28. All calls go through `assist/llm.py` (R6), and a test fails if any other file imports `litellm`, `openai` or `anthropic`.

## Meta Model API

| Item | Value | Source |
| --- | --- | --- |
| Base URL | `https://api.meta.ai/v1` (OpenAI-compatible: Chat Completions, Responses) | [Meta Model API docs](https://ai.developer.meta.com/docs/) |
| Auth | Bearer token; ours is `META_API_KEY` in `.env` (git-ignored) | same |
| LiteLLM provider | `meta/` prefix, e.g. `meta/muse-spark-1.3`; `api_base` and `api_key` passed explicitly | [LiteLLM: Meta Model API](https://docs.litellm.ai/docs/providers/meta) |
| LiteLLM version | `litellm==1.105.0` (released 2026-10-11, MIT). 1.82.7 and 1.82.8 were the compromised releases of March 2026; nothing near them is used | PyPI; D59 |

## Models in use

| Role | Model ID | Mode | Context | Price per 1M tokens (input / cached / output) | Rate limit | Used for |
| --- | --- | --- | --- | --- | --- | --- |
| `spark` | `muse-spark-1.3` | Standard | 1,048,576 | $1.25 / $0.15 / $4.25 | 3,000 RPM, 4,000,000 TPM per team | Approval briefs, auditor Q&A, the payments agent's steps (`agent/steps.py`), learner proposals (`learn/draft.py`); all run live on Oct 29 |
| `wonder` | `muse-wonder-1.0` | Standard (preview; Contributor not offered) | 1,048,576 | $4.00 / $0.20 / $20.00 | 3,000 RPM, 4,000,000 TPM per team | Policy PDF → rules (`policy/draft.py`); run live on Oct 29 |

Sources: [models](https://dev.meta.ai/docs/models.md), [pricing and rate limits](https://dev.meta.ai/docs/pricing-rate-limits.md). LiteLLM 1.105.0's bundled price table carries the same `muse-spark-1.3` prices; `assist/llm.py` sets `LITELLM_LOCAL_MODEL_COST_MAP=True` so LiteLLM never fetches a price table from the network at import.

## Standard mode only (R6)

Meta selects the mode by model ID: `muse-spark-1.3` is Standard, `muse-spark-1.3-contributor` is Contributor (prompts and outputs may train Meta's models). There is no header or request flag. `ModelConfig` refuses any model string containing `contributor`, case-insensitively, at startup (`config.contributor_mode`; `test_contributor_mode_models_are_refused`). Muse Code subscriptions default to Contributor mode; if Muse Code is used for a slice of the build, turn it off in its settings first.

## Request parameters

| Parameter | Value | Why |
| --- | --- | --- |
| `response_format` | `{"type": "json_schema", "json_schema": {"name": <schema>, "strict": true, "schema": <Pydantic schema>}}` | Meta structured output ([guide](https://dev.meta.ai/docs/structured-output.md)); `strict` makes the server check the schema against its strict subset |
| `temperature` | `0` | Briefs should be repeatable for the same context |
| `reasoning_effort` | `MODEL_REASONING_EFFORT` (default `medium`), cloud routes only; the auditor's two calls ask for `minimal` per call (D92); [reasoning guide](https://dev.meta.ai/docs/reasoning.md). Levels `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; `none` is a 400 on Muse Spark and Wonder and is refused at startup | Left to the model, a learner proposal took 167–425 s and 9–13k output tokens. Live learner, two runs per level (D90): `low` ~40 s but proposals failed the claim checker or the rule parser; `medium` ~40 s per proposal, 4 of 4 passed every check; the model's own level slower and missed a fixture |
| `timeout` | `MODEL_TIMEOUT_SECONDS` (150), per attempt | The first live brief took 23 s; a live learner call took 119 s at `medium` (D91) |
| Retries | 3 attempts on 429, 500, 502, 503 and connection errors: 0.5 s, then 1 s, each plus up to the same again in jitter. A timeout is not retried (`model.unavailable` at once). `max_retries=0` turns off the OpenAI SDK's own retries under LiteLLM | Meta's 429 guidance. Live, the SDK's two hidden retries under our three turned one 60 s timeout into a 9-minute learner run (D91) |
| Validation | Pydantic parse, then the caller's `check`; one retry with the problems fed back, then `ModelOutputError` | Skill §5 |

Strict-subset rules our schemas follow (checked by `strict_json_schema`): every object has `additionalProperties: false` and lists all its properties in `required`, so Pydantic models use `extra="forbid"` and no defaults. Limits: nesting 10, 5,000 properties, recursive schemas rejected (`HTTP 400`).

## Prompts

| Prompt | Version | Model | Output schema | Checked by |
| --- | --- | --- | --- | --- |
| `assist/prompts/approval_brief.md` | `approval_brief/2` | `spark` | `assist.brief.Brief` | `check_brief`: cited ids in the context, a citation per point, no numbers outside the context, no verdict advice |
| `assist/prompts/auditor_query.md` | `auditor_query/1` | `spark` | `assist.auditor.RecordQuery` | Schema only; code runs it (`claims/query.py`) over the proven log |
| `assist/prompts/policy_draft.md` | `policy_draft/1` | `wonder` | `policy.draft.DraftRules` | Quote must be inside the cited clause and rules must parse (retry); then `policy.checks` for the reviewer; approval re-checks |
| `assist/prompts/agent_step.md` | `agent_step/1` | `spark` | `agent.steps.AgentStep` | `check_step`: exactly the chosen tool's arguments, a reply on finish (retry); then Gate decides every call and the governor verifies its records (D78) |
| `assist/prompts/learn_proposal.md` | `learn_proposal/3` | `spark` | `learn.draft.LearnerProposal` | Rule shape, the scope's action, record ids in the pattern, counts once (retry); then sources set by code, counts = SQL, `claims.checker`, reason text, policy checks, replay (no retry; D82) |
| `assist/prompts/auditor_answer.md` | `auditor_answer/2` | `spark` | `assist.auditor.AuditorAnswer` | Ids must be in the rows (retry); then `claims.checker` checks every claim against the proven log, and the summary against the accepted claims |

## Call log

`MODEL_CALL_LOG` (default `logs/model_calls.csv`, git-ignored), one row per model response or failure:

`ts, profile, model, prompt_version, prompt_sha256, input_tokens, output_tokens, latency_ms, outcome`

`outcome` is `ok`, `invalid` (failed the schema or the check), `unavailable` or `refused`. Only a SHA-256 of the messages is logged, never the prompt. Spend is input × price + output × price from the table above.

## Mock profile (development)

`MODEL_PROFILE=mock` replaces the LiteLLM call with `assist/mock.py`'s `scripted_completion`: no network, no key, no cost, deterministic. It is not a model. Its route is `mock/scripted`, so every record it feeds (`assist.brief`, `assist.qa`) and every call-log row (0 tokens) names it, and it can never pass for Muse output. It answers from the same messages a model receives, and `LlmClient` puts every answer through the same schema validation, checks, retry and claim checking.

| Schema | What the script does |
| --- | --- |
| `Brief` | Points from the context: payee history, amount against the largest cleared payment and median, after hours, matched policies |
| `RecordQuery` | Keywords: payment or email, approved or rejected, "over $X", "under $X", "approved by a.b", "to payee-id", "outside business hours", "today", "this week", "last N days" |
| `AuditorAnswer` | One claim per row (at most 12) with assertions on amount (first, so `--inject-fault` changes it), payee, decision, verdict and approver |
| `AgentStep` | payments-ops: look up the vendor named in "pay <vendor> $<amount>", pay it once with the invoice as memo, email accounts payable, finish; finishes early, saying why, on no or several vendors, or on a denied, rejected or failed call |
| `LearnerProposal` | Loosen: an allow rule for the scope, the scope added to the `unless` of each escalate rule that held these requests, and any active fixture the change would affect moved just outside the scope. Tighten: a deny rule for the scope. Claims on the first three rows; counts restated from the input |
| `DraftRules` | For each clause it is given, the seed rule (`deploy/policies/harbor-federal.yaml`) written for that clause, if the seed's quote is in it |

Any other schema is refused (`model.refused`). Development runs on `mock`; switch to `cloud` for live runs and the video.

## On-prem profile (stretch)

`MODEL_PROFILE=onprem` routes `spark` to `openai/<ONPREM_MODEL>` at `ONPREM_API_BASE`, any OpenAI-compatible server (for example Muse Glimmer or Muse Spark Open behind vLLM, Baseten or Modal). Not tried yet; no on-prem model IDs are recorded until one runs.

## Verified live (2026-10-29): the switch to the live model

Every model role now runs live. `MODEL_PROFILE=cloud uv run pytest -m live` passes all four live tests (agent, brief, auditor, learner) in 1 min 45 s at `reasoning_effort` `medium`; it took 7 min with the model choosing its own effort. Then `make demo-agent MODEL_PROFILE=cloud` ran beat 2 on the running stack with delegation required: three agent steps, a held $48,000 payment, a live brief, j.ortiz's approval, 35 s in all. From `logs/model_calls.csv`:

| Call | Effort | Input tokens | Output tokens | Latency |
| --- | --- | --- | --- | --- |
| `learn_proposal/1` | model's own | 4,412–4,434 | 8,753–13,122 | 47–425 s |
| `learn_proposal/2` | `medium` | 4,991–5,090 | 3,374–13,687 | 12–110 s, most 20–60 s |
| `learn_proposal/3` (beat 6 after the clean reset) | `medium` | 5,469 | 10,527 | 119 s |
| `agent_step/1` | `medium` | 870–1,438 | 303–849 | 2.3–3.8 s |
| `approval_brief/2` | `medium` | 1,356 | 5,255 | 20.5 s |
| `policy_draft/1` (Muse Wonder) | `medium` | 915 | 4,121 | 27.9 s |
| `auditor_query/1` + `auditor_answer/2` (one audit) | `minimal` (D92) | 486–1,858 | — | 18–21 s for both, every claim proven; 38–47 s at `medium` |

Muse Wonder's first live draft turned every clause of `deploy/demo/payments_policy.pdf` into rules whose quotes code found in their clauses, with no findings from the policy checks (`test_beat_1_on_muse_wonder`). Its first attempt never left the machine: LiteLLM 1.105.0's model table has no `muse-wonder-1.0`, so it refused `reasoning_effort` locally (`UnsupportedParamsError`, logged `refused`, 0 tokens). `assist/llm.py` now names the parameter in LiteLLM's `allowed_openai_params` whenever it sends it.

What changed to get the learner's proposals through their checks live, and the measurements per level, are in D90.

## Verified live (2026-10-28)

First runs against the Meta Model API with `muse-spark-1.3` in Standard mode. All structured outputs parsed and passed their checks on the first attempt. The two `live` tests passed (`META_API_KEY=... uv run pytest -m live -s`), then `make demo-gate` and `make demo-audit` ran against the real services, from `logs/model_calls.csv`:

| Call | Input tokens | Output tokens | Latency |
| --- | --- | --- | --- |
| `approval_brief` | 1,035; 1,097 | 5,169; 5,780 | 23.3 s; 28.6 s |
| `auditor_query/1` | 625; 625 | 3,312; 1,707 | 15.9 s; 8.7 s |
| `auditor_answer/2` | 1,149; 1,149 | 5,040; 5,079 | 18.9 s; 18.6 s |

At the prices above, those six calls cost about $0.12 in total; output tokens are most of it. Output counts far exceed the size of the JSON returned, so they likely include the model's reasoning; this is not yet confirmed or tuned. Because one brief took 28.6 s, `MODEL_TIMEOUT_SECONDS` now defaults to 60, and `make demo-gate` waits up to 60 s for the brief.
