"""Settings from environment variables. ``.env.example`` documents every one."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    database_url: str = Field(description="Postgres URL for the app role (INSERT + SELECT only)")
    keys_dir: Path = Field(description="Directory outside the repo holding private keys")
    ledger_stream: str = "harbor-federal/decisions"
    # Checkpoint origin is f"{ledger_origin}/{stream}" and doubles as the signed-note key name.
    ledger_origin: str = "precept-ledger.local"
    checkpoint_dir: Path = Path("var/checkpoints")
    checkpoint_interval_seconds: float = 10.0
    api_host: str = "127.0.0.1"  # localhost only: reads are unauthenticated in the demo
    api_port: int = 8080
    log_level: str = "INFO"
    demo_mode: bool = False


def load_settings() -> Settings:
    # Required fields come from the environment; pydantic raises if they are missing.
    return Settings()  # type: ignore[call-arg]  # values are read from env, not arguments
