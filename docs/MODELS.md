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
| `spark` | `muse-spark-1.3` | Standard | 1,048,576 | $1.25 / $0.15 / $4.25 | 3,000 RPM, 4,000,000 TPM per team | Approval briefs (now); auditor Q&A, learner proposals, demo agent (later) |
| `wonder` | `muse-wonder-1.0` | Standard (preview; Contributor not offered) | 1,048,576 | $4.00 / $0.20 / $20.00 | 3,000 RPM, 4,000,000 TPM per team | Policy PDF → rules (`policy/draft.py`); not called live yet |

Sources: [models](https://dev.meta.ai/docs/models.md), [pricing and rate limits](https://dev.meta.ai/docs/pricing-rate-limits.md). LiteLLM 1.105.0's bundled price table carries the same `muse-spark-1.3` prices; `assist/llm.py` sets `LITELLM_LOCAL_MODEL_COST_MAP=True` so LiteLLM never fetches a price table from the network at import.

## Standard mode only (R6)

Meta selects the mode by model ID: `muse-spark-1.3` is Standard, `muse-spark-1.3-contributor` is Contributor (prompts and outputs may train Meta's models). There is no header or request flag. `ModelConfig` refuses any model string containing `contributor`, case-insensitively, at startup (`config.contributor_mode`; `test_contributor_mode_models_are_refused`). Muse Code subscriptions default to Contributor mode; if Muse Code is used for a slice of the build, turn it off in its settings first.

## Request parameters

| Parameter | Value | Why |
| --- | --- | --- |
| `response_format` | `{"type": "json_schema", "json_schema": {"name": <schema>, "strict": true, "schema": <Pydantic schema>}}` | Meta structured output ([guide](https://dev.meta.ai/docs/structured-output.md)); `strict` makes the server check the schema against its strict subset |
| `temperature` | `0` | Briefs should be repeatable for the same context |
| `timeout` | `MODEL_TIMEOUT_SECONDS` (60) | The first live brief took 23 s |
| Retries | 3 attempts on 429, 500, 502, 503, connection errors and timeouts: 0.5 s, then 1 s, each plus up to the same again in jitter | Meta's 429 guidance |
| Validation | Pydantic parse, then the caller's `check`; one retry with the problems fed back, then `ModelOutputError` | Skill §5 |

Strict-subset rules our schemas follow (checked by `strict_json_schema`): every object has `additionalProperties: false` and lists all its properties in `required`, so Pydantic models use `extra="forbid"` and no defaults. Limits: nesting 10, 5,000 properties, recursive schemas rejected (`HTTP 400`).

## Prompts

| Prompt | Version | Model | Output schema | Checked by |
| --- | --- | --- | --- | --- |
| `assist/prompts/approval_brief.md` | `approval_brief/2` | `spark` | `assist.brief.Brief` | `check_brief`: cited ids in the context, a citation per point, no numbers outside the context, no verdict advice |
| `assist/prompts/auditor_query.md` | `auditor_query/1` | `spark` | `assist.auditor.RecordQuery` | Schema only; code runs it (`claims/query.py`) over the proven log |
| `assist/prompts/policy_draft.md` | `policy_draft/1` | `wonder` | `policy.draft.DraftRules` | Quote must be inside the cited clause and rules must parse (retry); then `policy.checks` for the reviewer; approval re-checks |
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
| `DraftRules` | For each clause it is given, the seed rule (`deploy/policies/harbor-federal.yaml`) written for that clause, if the seed's quote is in it |

Any other schema is refused (`model.refused`). Development runs on `mock`; switch to `cloud` for live runs and the video.

## On-prem profile (stretch)

`MODEL_PROFILE=onprem` routes `spark` to `openai/<ONPREM_MODEL>` at `ONPREM_API_BASE`, any OpenAI-compatible server (for example Muse Glimmer or Muse Spark Open behind vLLM, Baseten or Modal). Not tried yet; no on-prem model IDs are recorded until one runs.

## Verified live (2026-10-28)

First runs against the Meta Model API with `muse-spark-1.3` in Standard mode. All structured outputs parsed and passed their checks on the first attempt. The two `live` tests passed (`META_API_KEY=... uv run pytest -m live -s`), then `make demo-gate` and `make demo-audit` ran against the real services, from `logs/model_calls.csv`:

| Call | Input tokens | Output tokens | Latency |
| --- | --- | --- | --- |
| `approval_brief` | 1,035; 1,097 | 5,169; 5,780 | 23.3 s; 28.6 s |
| `auditor_query/1` | 625; 625 | 3,312; 1,707 | 15.9 s; 8.7 s |
| `auditor_answer/2` | 1,149; 1,149 | 5,040; 5,079 | 18.9 s; 18.6 s |

At the prices above, those six calls cost about $0.12 in total; output tokens are most of it. Output counts far exceed the size of the JSON returned, so they likely include the model's reasoning; this is not yet confirmed or tuned. Because one brief took 28.6 s, `MODEL_TIMEOUT_SECONDS` now defaults to 60, and `make demo-gate` waits up to 60 s for the brief.
