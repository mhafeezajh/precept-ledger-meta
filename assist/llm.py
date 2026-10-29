"""The only model entry point (skill §5, R6): ``LlmClient.complete_structured``.

Every call goes through LiteLLM to a route of the active profile (``cloud``: the Meta Model API;
``onprem``: an OpenAI-compatible endpoint; ``mock``: ``assist.mock``'s scripted answers, no
network, for development), asks for JSON matching a Pydantic schema with
Meta's ``json_schema`` structured output, and parses the answer into that schema. A schema or
check failure gets one retry with the problems fed back; a second failure raises
``ModelOutputError`` (fail closed). Rate limits and outages are retried with backoff (Meta's
429 guidance). Contributor-mode model IDs are refused at configuration time: their prompts and
outputs train Meta's models.

Before sending, long digit runs (account and routing numbers) are masked. Each call appends
one row to the call log with a hash of the prompt, never the prompt itself.
"""

import asyncio
import copy
import csv
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

# Before importing litellm: use its bundled price table instead of fetching one from GitHub.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # the environment variable above must be set first
from pydantic import BaseModel, ValidationError

from assist.mock import MOCK_MODEL, scripted_completion
from common.config import Settings
from common.errors import ConfigError, ModelOutputError

litellm.telemetry = False
litellm.drop_params = False  # an unsupported parameter is an error, never silently dropped

M = TypeVar("M", bound=BaseModel)
Check = Callable[[Any], list[str]]
PROFILES = ("cloud", "onprem", "mock")
# Meta's levels (docs/MODELS.md); "none" is a 400 on Muse Spark and Wonder, so it is not here.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
CALL_LOG_FIELDS = (
    "ts", "profile", "model", "prompt_version", "prompt_sha256", "input_tokens",
    "output_tokens", "latency_ms", "outcome",
)  # fmt: skip
MAX_ATTEMPTS = 3  # transport attempts per model call
_TRANSIENT = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.BadGatewayError,
    litellm.exceptions.APIConnectionError,
)
# 10+ digits with optional single separators, or 8+ in a row; dates (8 digits) and amounts
# with thousands commas survive, account and routing numbers do not.
_LONG_DIGITS = re.compile(r"\b\d(?:[ -]?\d){9,}\b|\b\d{8,}\b")


@dataclass(frozen=True)
class Route:
    model: str  # LiteLLM model string, e.g. meta/muse-spark-1.3
    api_base: str | None
    api_key: str | None
    reasoning_effort: str | None = None  # Meta's top-level parameter; None lets the model pick

    def __repr__(self) -> str:
        return f"Route({self.model!r}, {self.api_base!r})"  # never the key


@dataclass(frozen=True)
class ModelConfig:
    profile: str
    routes: Mapping[str, Mapping[str, Route]]  # profile -> role (spark, wonder) -> route
    timeout_seconds: float
    call_log: Path

    def __post_init__(self) -> None:
        if self.profile not in PROFILES or self.profile not in self.routes:
            raise ConfigError(
                f"unknown model profile {self.profile!r}", code="config.model_profile"
            )
        for profile, roles in self.routes.items():
            for route in roles.values():
                if "contributor" in route.model.lower():
                    raise ConfigError(
                        "Contributor-mode models are never used (R6)",
                        code="config.contributor_mode",
                    )
                if route.reasoning_effort is not None and (
                    route.reasoning_effort not in REASONING_EFFORTS
                ):
                    raise ConfigError(
                        f"unknown reasoning effort {route.reasoning_effort!r}",
                        code="config.reasoning_effort",
                    )
                if profile == "cloud" and not route.model.startswith("meta/"):
                    raise ConfigError(
                        "cloud routes go to the Meta Model API only", code="config.model_route"
                    )

    def route(self, role: str) -> Route:
        route = self.routes[self.profile].get(role)
        if route is None:
            raise ConfigError(
                f"no {role!r} model in profile {self.profile}", code="config.model_route"
            )
        return route


def _effort(route: Route) -> dict[str, Any]:
    if not route.reasoning_effort:
        return {}
    # LiteLLM 1.105.0's model table has no muse-wonder-1.0 and refuses the parameter for it
    # before sending; Meta documents it for both models (docs/MODELS.md, D90).
    return {
        "reasoning_effort": route.reasoning_effort,
        "allowed_openai_params": ["reasoning_effort"],
    }


def redact(text: str) -> str:
    return _LONG_DIGITS.sub("[redacted]", text)


def prompt_hash(messages: Sequence[Mapping[str, str]]) -> str:
    data = json.dumps(list(messages), sort_keys=True, ensure_ascii=False)  # a log key, not signed
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema, checked against Meta's strict subset."""
    schema = copy.deepcopy(model.model_json_schema())
    for node in [schema, *schema.get("$defs", {}).values()]:
        properties = node.get("properties", {})
        if node.get("type") == "object" and (
            node.get("additionalProperties") is not False
            or set(node.get("required", [])) != set(properties)
        ):
            name = node.get("title", model.__name__)
            raise ConfigError(
                f"{name}: strict schemas need extra='forbid' and no defaults",
                code="config.model_schema",
            )
    return schema


class LlmClient:
    def __init__(
        self,
        config: ModelConfig,
        *,
        completion: Callable[..., Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        if completion is None:
            completion = scripted_completion if config.profile == "mock" else litellm.acompletion
        self._completion = completion
        self._sleep = sleep

    async def complete_structured(
        self,
        model_role: str,
        schema: type[M],
        messages: Sequence[Mapping[str, str]],
        *,
        prompt_version: str,
        check: Check | None = None,
    ) -> M:
        """``schema`` parsed from the model's answer, after ``check`` finds no problems."""
        route = self.config.route(model_role)
        conversation = [{"role": m["role"], "content": redact(m["content"])} for m in messages]
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": strict_json_schema(schema),
            },
        }
        for attempt in (1, 2):
            started = time.perf_counter()
            try:
                content, usage = await self._call(route, conversation, response_format)
            except ModelOutputError as exc:
                self._log(
                    route, prompt_version, conversation, (0, 0), started, exc.code.split(".")[1]
                )
                raise
            problems, parsed = _parse(schema, content, check)
            outcome = "ok" if not problems else "invalid"
            self._log(route, prompt_version, conversation, usage, started, outcome)
            if not problems and parsed is not None:
                return parsed
            if attempt == 2:
                break
            feedback = "Your answer was rejected: " + "; ".join(problems)
            conversation = [
                *conversation,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": feedback + ". Answer again with JSON that fixes every problem.",
                },
            ]
        raise ModelOutputError("model output failed validation twice", code="model.bad_output")

    async def _call(
        self, route: Route, conversation: list[dict[str, str]], response_format: dict[str, Any]
    ) -> tuple[str, tuple[int, int]]:
        """The answer text and (input, output) tokens. Transient failures are retried."""
        delay = 0.5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._completion(
                    model=route.model, messages=conversation, response_format=response_format,
                    temperature=0, api_base=route.api_base, api_key=route.api_key,
                    timeout=self.config.timeout_seconds, max_retries=0, **_effort(route),
                )  # fmt: skip  # max_retries: the SDK's own retries hid under ours (D90)
                break
            except litellm.exceptions.Timeout as exc:  # out of time: again would be too (D90)
                raise ModelOutputError("model timed out", code="model.unavailable") from exc
            except _TRANSIENT as exc:
                if attempt == MAX_ATTEMPTS:
                    raise ModelOutputError("model unavailable", code="model.unavailable") from exc
                await self._sleep(delay + random.uniform(0, delay))  # noqa: S311  # jitter, not crypto
                delay *= 2
            # Bad request, auth, content policy, anything else: refused, never retried (R4).
            # LiteLLM's exceptions share no base of their own short of the provider SDK's.
            except Exception as exc:
                name = type(exc).__name__
                raise ModelOutputError(
                    f"model call refused ({name})", code="model.refused"
                ) from exc
        usage = getattr(response, "usage", None)
        tokens = (
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise ModelOutputError("model returned no message", code="model.bad_output") from exc
        return str(content or ""), tokens

    def _log(
        self,
        route: Route,
        prompt_version: str,
        conversation: Sequence[Mapping[str, str]],
        usage: tuple[int, int],
        started: float,
        outcome: str,
    ) -> None:
        path = self.config.call_log
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CALL_LOG_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow({
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "profile": self.config.profile,
                "model": route.model,
                "prompt_version": prompt_version,
                "prompt_sha256": prompt_hash(conversation),
                "input_tokens": usage[0],
                "output_tokens": usage[1],
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "outcome": outcome,
            })  # fmt: skip


def _parse[P: BaseModel](
    schema: type[P], content: str, check: Check | None
) -> tuple[list[str], P | None]:
    try:
        parsed = schema.model_validate_json(content)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or '$'}: {e['msg']}" for e in exc.errors()
        ]
        return problems[:10], None
    problems = check(parsed) if check is not None else []
    return problems[:10], parsed


def model_config(settings: Settings) -> ModelConfig | None:
    """The model routes for the profiles, or None when the active one has no credentials."""
    if settings.model_profile == "mock":  # D72: no route, no key, nothing leaves the machine
        mock = Route(MOCK_MODEL, None, None)
        return ModelConfig(
            "mock", {"mock": {"spark": mock, "wonder": mock}},
            settings.model_timeout_seconds, settings.model_call_log,
        )  # fmt: skip
    key = settings.meta_api_key.get_secret_value() if settings.meta_api_key else None
    effort = settings.model_reasoning_effort or None
    onprem = Route(f"openai/{settings.onprem_model}", settings.onprem_api_base or None, None)
    routes = {
        "cloud": {
            "spark": Route("meta/muse-spark-1.3", settings.meta_api_base, key, effort),
            "wonder": Route("meta/muse-wonder-1.0", settings.meta_api_base, key, effort),
        },
        "onprem": {"spark": onprem},
    }
    active = key if settings.model_profile == "cloud" else settings.onprem_api_base
    if not active:
        return None
    return ModelConfig(
        settings.model_profile, routes, settings.model_timeout_seconds, settings.model_call_log
    )
