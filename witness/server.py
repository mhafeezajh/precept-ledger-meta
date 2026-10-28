"""``pl-witness``: run one witness. Each witness has its own key, log list and state directory.

Settings come from ``WITNESS_*`` environment variables (see ``.env.example``). Three witnesses
are three processes with different settings (``make witnesses``); none of them can reach the
ledger's database.
"""

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.clock import SystemClock
from common.logging import configure_logging
from witness.api import create_witness_app
from witness.core import Witness
from witness.keys import load_log_keys, load_signer
from witness.state import FileWitnessState


class WitnessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WITNESS_", extra="ignore", frozen=True)

    name: str = Field(description="Cosigner name, e.g. witness-1; the key name in its lines")
    key_file: Path = Field(description="Ed25519 private key (PEM, mode 0600), outside the repo")
    logs_file: Path = Field(description="Signed-note verifier keys of the logs it witnesses")
    state_dir: Path = Field(description="This witness's own storage; never the ledger's")
    host: str = "127.0.0.1"
    port: int = 8091
    log_level: str = "INFO"


def witness_from_settings(settings: WitnessSettings) -> Witness:
    return Witness(
        settings.name,
        load_signer(settings.key_file),
        load_log_keys(settings.logs_file),
        FileWitnessState(settings.state_dir),
        SystemClock(),
    )


def app_from_settings() -> FastAPI:
    settings = WitnessSettings()  # type: ignore[call-arg]  # values are read from env
    configure_logging(settings.log_level)
    return create_witness_app(witness_from_settings(settings))


def main() -> None:
    settings = WitnessSettings()  # type: ignore[call-arg]  # values are read from env
    uvicorn.run(
        "witness.server:app_from_settings",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,  # our middleware logs each request without the raw path
    )
