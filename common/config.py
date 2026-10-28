"""Settings from environment variables. ``.env.example`` documents every one."""

from pathlib import Path

from pydantic import Field, SecretStr
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
    # Witnesses: "name=url" pairs, comma-separated; empty means checkpoints are not witnessed.
    # Their cosigner keys are read from KEYS_DIR/witnesses.txt (the pl-verify --witness-keys file).
    witnesses: str = ""
    witness_quorum: int = 2  # D5: 2 of 3
    witness_timeout_seconds: float = 5.0
    # Anchors: RFC 3161 TSAs as "name=url" pairs, comma-separated; empty means no timestamps.
    tsas: str = ""
    tsa_roots_file: Path = Path("deploy/tsa/roots.pem")
    # OpenTimestamps: calendars to submit to, and the calendars proofs may be upgraded from.
    ots_calendars: str = ""
    ots_upgrade_calendars: str = (
        "https://alice.btc.calendar.opentimestamps.org,https://bob.btc.calendar.opentimestamps.org"
    )
    ots_interval_seconds: float = 3600.0
    anchor_timeout_seconds: float = 10.0
    # Models (assist/llm.py, docs/MODELS.md). One setting switches every model call's profile.
    model_profile: str = "cloud"  # cloud = Meta Model API; onprem = an OpenAI-compatible endpoint
    meta_api_key: SecretStr | None = None  # Standard mode only: never a -contributor model ID
    meta_api_base: str = "https://api.meta.ai/v1"
    onprem_api_base: str = ""
    onprem_model: str = ""
    model_timeout_seconds: float = 30.0
    model_call_log: Path = Path("logs/model_calls.csv")
    log_level: str = "INFO"
    demo_mode: bool = False


def load_settings() -> Settings:
    # Required fields come from the environment; pydantic raises if they are missing.
    return Settings()  # type: ignore[call-arg]  # values are read from env, not arguments
