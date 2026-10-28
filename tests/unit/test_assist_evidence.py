"""assist/evidence.py failure paths, and model_config: every ledger problem is an error, never
a proof."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from assist.evidence import HttpLedgerReader
from assist.llm import model_config
from common.config import Settings
from common.errors import ModelOutputError


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
async def test_the_export_fails_closed(r: HttpLedgerReader) -> None:
    with pytest.raises(ModelOutputError):
        await r.export()


def settings(tmp_path: Path, **values: object) -> Settings:
    # Never the developer's .env or environment (the Makefile exports .env): with a real key or
    # MODEL_PROFILE=mock there, "no key" would not be.
    values.setdefault("meta_api_key", None)
    values.setdefault("model_profile", "cloud")
    return Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql://x", keys_dir=tmp_path, **values
    )


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
