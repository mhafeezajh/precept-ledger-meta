"""assist/evidence.py failure paths, and model_config: every ledger problem is an error or no
evidence, never a proof."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from assist.evidence import HttpLedgerReader
from assist.llm import model_config
from common.config import Settings
from common.errors import ModelOutputError

ULID = "01JB6X7Q2M9ZK3T8V4N5R6S7W0"


def reader(status: int = 200, content: bytes = b"{}") -> HttpLedgerReader:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    return HttpLedgerReader("http://ledger", httpx.AsyncClient(transport=transport), "s/d")


def down() -> HttpLedgerReader:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    return HttpLedgerReader(
        "http://ledger", httpx.AsyncClient(transport=httpx.MockTransport(fail)), "s/d"
    )


@pytest.mark.parametrize("r", [down(), reader(503), reader(200, b"not a tar")])
async def test_records_fail_closed(r: HttpLedgerReader) -> None:
    with pytest.raises(ModelOutputError):
        await r.records()


async def test_final_size_answers() -> None:
    assert await reader(404).final_size() is None
    assert await reader(200, b'{"size": 7}').final_size() == 7
    with pytest.raises(ModelOutputError):
        await reader(500).final_size()


async def test_missing_or_odd_evidence_is_none() -> None:
    assert await reader().evidence("../export", 3) is None
    assert await reader(404).evidence(ULID, 3) is None
    assert await reader(200, b'{"record": {}}').evidence(ULID, 3) is None
    with pytest.raises(ModelOutputError):
        await down().evidence(ULID, 3)


def settings(tmp_path: Path, **values: object) -> Settings:
    return Settings(database_url="postgresql://x", keys_dir=tmp_path, **values)  # type: ignore[arg-type]


def test_model_config_needs_credentials_for_the_active_profile(tmp_path: Path) -> None:
    assert model_config(settings(tmp_path)) is None
    cloud = model_config(settings(tmp_path, meta_api_key=SecretStr("test-only-key-7781")))
    assert cloud is not None
    assert cloud.route("spark").model == "meta/muse-spark-1.3"
    assert cloud.route("wonder").model == "meta/muse-wonder-1.0"
    assert "test-only-key-7781" not in repr(cloud.route("spark"))
    onprem = model_config(
        settings(
            tmp_path, model_profile="onprem", onprem_api_base="http://h/v1", onprem_model="glimmer"
        )
    )
    assert onprem is not None
    assert onprem.route("spark").model == "openai/glimmer"
