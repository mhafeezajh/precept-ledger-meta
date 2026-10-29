"""assist/llm.py: the only model entry point. Every call is mocked here; no network."""

import csv
import json
from pathlib import Path
from typing import Any

import litellm
import pytest
from pydantic import BaseModel, ConfigDict, Field

from assist.llm import (
    LlmClient,
    ModelConfig,
    Route,
    prompt_hash,
    redact,
    strict_json_schema,
)
from common.errors import ConfigError, ModelOutputError


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=50)
    count: int


class FakeCompletion:
    """Stands in for ``litellm.acompletion``: replays queued outputs, records every call."""

    def __init__(self, *outputs: object) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": output}}],
            usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        )


async def no_sleep(seconds: float) -> None:
    return None


def client(tmp_path: Path, completion: FakeCompletion, profile: str = "cloud") -> LlmClient:
    config = ModelConfig(
        profile=profile,
        routes={
            "cloud": {"spark": Route("meta/muse-spark-1.3", "https://api.meta.ai/v1", "test-key")},
            "onprem": {"spark": Route("openai/muse-glimmer", "http://127.0.0.1:9000/v1", None)},
        },
        timeout_seconds=5,
        call_log=tmp_path / "logs" / "model_calls.csv",
    )
    return LlmClient(config, completion=completion, sleep=no_sleep)


MESSAGES = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Count."}]


def log_rows(tmp_path: Path) -> list[dict[str, str]]:
    with (tmp_path / "logs" / "model_calls.csv").open() as file:
        return list(csv.DictReader(file))


async def test_a_valid_answer_is_parsed_and_logged(tmp_path: Path) -> None:
    fake = FakeCompletion('{"text": "three", "count": 3}')
    answer = await client(tmp_path, fake).complete_structured(
        "spark", Answer, MESSAGES, prompt_version="test/1"
    )
    assert answer == Answer(text="three", count=3)
    call = fake.calls[0]
    assert call["model"] == "meta/muse-spark-1.3"
    assert call["api_base"] == "https://api.meta.ai/v1"
    assert call["temperature"] == 0
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    (row,) = log_rows(tmp_path)
    assert (row["profile"], row["model"], row["prompt_version"]) == (
        "cloud",
        "meta/muse-spark-1.3",
        "test/1",
    )
    assert (row["input_tokens"], row["output_tokens"], row["outcome"]) == ("120", "30", "ok")
    assert row["prompt_sha256"] == prompt_hash(MESSAGES)
    assert "Count." not in (tmp_path / "logs" / "model_calls.csv").read_text()  # hash only


async def test_one_retry_with_the_validation_error_fed_back(tmp_path: Path) -> None:
    fake = FakeCompletion('{"text": "x"}', '{"text": "three", "count": 3}')
    answer = await client(tmp_path, fake).complete_structured(
        "spark", Answer, MESSAGES, prompt_version="t/1"
    )
    assert answer.count == 3
    retry_messages = fake.calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "count" in retry_messages[-1]["content"]
    assert [r["outcome"] for r in log_rows(tmp_path)] == ["invalid", "ok"]


async def test_a_second_invalid_answer_fails_closed(tmp_path: Path) -> None:
    fake = FakeCompletion("not json", '{"text": "x"}')
    with pytest.raises(ModelOutputError) as caught:
        await client(tmp_path, fake).complete_structured(
            "spark", Answer, MESSAGES, prompt_version="t/1"
        )
    assert caught.value.code == "model.bad_output"
    assert len(fake.calls) == 2


async def test_semantic_checks_get_the_same_single_retry(tmp_path: Path) -> None:
    def must_be_positive(answer: Answer) -> list[str]:
        return [] if answer.count > 0 else ["count must be positive"]

    fake = FakeCompletion('{"text": "a", "count": 0}', '{"text": "a", "count": 2}')
    answer = await client(tmp_path, fake).complete_structured(
        "spark", Answer, MESSAGES, prompt_version="t/1", check=must_be_positive
    )
    assert answer.count == 2
    assert "count must be positive" in fake.calls[1]["messages"][-1]["content"]
    fake = FakeCompletion('{"text": "a", "count": 0}', '{"text": "a", "count": -1}')
    with pytest.raises(ModelOutputError):
        await client(tmp_path, fake).complete_structured(
            "spark", Answer, MESSAGES, prompt_version="t/1", check=must_be_positive
        )


def rate_limited() -> Exception:
    return litellm.RateLimitError("slow down", llm_provider="meta", model="muse-spark-1.3")


async def test_rate_limits_and_outages_are_retried_with_backoff(tmp_path: Path) -> None:
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    fake = FakeCompletion(rate_limited(), rate_limited(), '{"text": "ok", "count": 1}')
    llm = client(tmp_path, fake)
    llm._sleep = record_sleep  # type: ignore[method-assign]
    assert (
        await llm.complete_structured("spark", Answer, MESSAGES, prompt_version="t/1")
    ).count == 1
    assert len(slept) == 2
    assert 0.5 <= slept[0] < 1.0  # 500 ms plus jitter
    assert 1.0 <= slept[1] < 2.0  # doubled


async def test_a_timeout_fails_closed_at_once_and_the_sdk_never_retries(tmp_path: Path) -> None:
    # Live, the OpenAI SDK's own two retries under our three turned one 60 s timeout into
    # nine minutes. A reasoning call that ran out of time is not retried (D90).
    timeout = litellm.Timeout("took too long", model="muse-spark-1.3", llm_provider="meta")
    fake = FakeCompletion(timeout, '{"text": "ok", "count": 1}')
    with pytest.raises(ModelOutputError) as caught:
        await client(tmp_path, fake).complete_structured(
            "spark", Answer, MESSAGES, prompt_version="t/1"
        )
    assert caught.value.code == "model.unavailable"
    assert len(fake.calls) == 1
    assert fake.calls[0]["max_retries"] == 0
    assert log_rows(tmp_path)[-1]["outcome"] == "unavailable"


async def test_a_model_that_stays_down_is_a_model_error(tmp_path: Path) -> None:
    down = litellm.ServiceUnavailableError("down", llm_provider="meta", model="muse-spark-1.3")
    fake = FakeCompletion(down, down, down)
    with pytest.raises(ModelOutputError) as caught:
        await client(tmp_path, fake).complete_structured(
            "spark", Answer, MESSAGES, prompt_version="t/1"
        )
    assert caught.value.code == "model.unavailable"
    assert [r["outcome"] for r in log_rows(tmp_path)] == ["unavailable"]


async def test_an_unexpected_provider_error_is_not_retried(tmp_path: Path) -> None:
    fake = FakeCompletion(litellm.BadRequestError("bad schema", llm_provider="meta", model="m"))
    with pytest.raises(ModelOutputError) as caught:
        await client(tmp_path, fake).complete_structured(
            "spark", Answer, MESSAGES, prompt_version="t/1"
        )
    assert caught.value.code == "model.refused"
    assert len(fake.calls) == 1


async def test_the_onprem_profile_switches_every_route(tmp_path: Path) -> None:
    fake = FakeCompletion('{"text": "a", "count": 1}')
    await client(tmp_path, fake, profile="onprem").complete_structured(
        "spark", Answer, MESSAGES, prompt_version="t/1"
    )
    assert (fake.calls[0]["model"], fake.calls[0]["api_base"]) == (
        "openai/muse-glimmer",
        "http://127.0.0.1:9000/v1",
    )


async def test_reasoning_effort_is_sent_only_when_a_route_sets_it(tmp_path: Path) -> None:
    fake = FakeCompletion('{"text": "a", "count": 1}', '{"text": "a", "count": 1}')
    await client(tmp_path, fake).complete_structured(
        "spark", Answer, MESSAGES, prompt_version="t/1"
    )
    assert "reasoning_effort" not in fake.calls[0]  # unset: the model picks its own level
    config = ModelConfig(
        profile="cloud",
        routes={"cloud": {"spark": Route("meta/muse-spark-1.3", None, "k", "low")}},
        timeout_seconds=5,
        call_log=tmp_path / "x.csv",
    )
    await LlmClient(config, completion=fake, sleep=no_sleep).complete_structured(
        "spark", Answer, MESSAGES, prompt_version="t/1"
    )
    assert fake.calls[1]["reasoning_effort"] == "low"
    # LiteLLM 1.105.0's table lacks muse-wonder-1.0 and refused the parameter locally, live.
    assert fake.calls[1]["allowed_openai_params"] == ["reasoning_effort"]
    assert "allowed_openai_params" not in fake.calls[0]


@pytest.mark.parametrize("effort", ["none", "fast", "LOW", ""])
def test_unknown_reasoning_efforts_are_refused(tmp_path: Path, effort: str) -> None:
    # "none" is a 400 on Muse Spark and Wonder; anything else unknown is never guessed.
    routes = {"cloud": {"spark": Route("meta/muse-spark-1.3", None, "k", effort)}}
    with pytest.raises(ConfigError) as caught:
        ModelConfig(profile="cloud", routes=routes, timeout_seconds=5, call_log=tmp_path / "x.csv")
    assert caught.value.code == "config.reasoning_effort"


def test_the_setting_reaches_cloud_routes_only(tmp_path: Path) -> None:
    from assist.llm import model_config
    from common.config import Settings

    settings = Settings(_env_file=None, database_url="postgresql://x", keys_dir=tmp_path,  # type: ignore[call-arg]  # values, not env
                        model_profile="cloud", meta_api_key="test-key", onprem_api_base="http://h/v1",
                        onprem_model="m", model_reasoning_effort="low")  # fmt: skip
    config = model_config(settings)
    assert config is not None
    assert {r.reasoning_effort for r in config.routes["cloud"].values()} == {"low"}
    assert config.routes["onprem"]["spark"].reasoning_effort is None
    unset = model_config(settings.model_copy(update={"model_reasoning_effort": ""}))
    assert unset is not None
    assert config.routes["cloud"]["spark"] != unset.routes["cloud"]["spark"]
    assert unset.routes["cloud"]["spark"].reasoning_effort is None


async def test_digits_that_could_be_account_numbers_never_leave(tmp_path: Path) -> None:
    fake = FakeCompletion('{"text": "a", "count": 1}')
    messages = [
        {"role": "user", "content": "Pay account 004277810012 routing 021000021 for $48,000.00"}
    ]
    await client(tmp_path, fake).complete_structured(
        "spark", Answer, messages, prompt_version="t/1"
    )
    sent = fake.calls[0]["messages"][0]["content"]
    assert "004277810012" not in sent
    assert "021000021" not in sent
    assert "$48,000.00" in sent  # amounts are facts the brief needs


def test_redact_masks_long_digit_runs_only() -> None:
    assert redact("acct 0042-7781-0012 and 12345678") == "acct [redacted] and [redacted]"
    assert (
        redact("$48,000.00 at 21:14 on 2026-10-29, 3 payments")
        == "$48,000.00 at 21:14 on 2026-10-29, 3 payments"
    )


@pytest.mark.parametrize(
    "model",
    [
        "meta/muse-spark-1.3-contributor",
        "meta/muse-spark-1.2-contributor",
        "meta/MUSE-SPARK-1.3-Contributor",
    ],
)
def test_contributor_mode_models_are_refused(tmp_path: Path, model: str) -> None:
    # R6: Contributor-mode prompts and outputs train Meta's models.
    routes = {"cloud": {"spark": Route(model, None, "k")}}
    with pytest.raises(ConfigError) as caught:
        ModelConfig(profile="cloud", routes=routes, timeout_seconds=5, call_log=tmp_path / "x.csv")
    assert caught.value.code == "config.contributor_mode"


@pytest.mark.parametrize(
    ("profile", "routes", "code"),
    [
        ("lab", {"cloud": {}}, "config.model_profile"),
        ("cloud", {"cloud": {"spark": Route("anthropic/claude", None, "k")}}, "config.model_route"),
    ],
)
def test_unknown_profiles_and_non_meta_cloud_routes_are_refused(
    tmp_path: Path, profile: str, routes: dict, code: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        ModelConfig(profile=profile, routes=routes, timeout_seconds=5, call_log=tmp_path / "x.csv")
    assert caught.value.code == code


async def test_an_unknown_role_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        await client(tmp_path, FakeCompletion()).complete_structured(
            "wonder", Answer, MESSAGES, prompt_version="t/1"
        )


def test_the_schema_sent_is_strict() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ids: list[str]

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str
        items: list[Inner]

    schema = strict_json_schema(Outer)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["title", "items"]
    inner = schema["$defs"]["Inner"]
    assert (inner["additionalProperties"], inner["required"]) == (False, ["ids"])
    json.dumps(schema)


def test_models_with_optional_fields_cannot_be_strict() -> None:
    class Loose(BaseModel):
        text: str = "x"

    with pytest.raises(ConfigError):
        strict_json_schema(Loose)
