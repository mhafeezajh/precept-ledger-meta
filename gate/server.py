"""``pl-gate``: serve the Gate API. Gate holds its producer key; agents hold only a token.

From ``KEYS_DIR``: ``gate-1.pem`` (Gate's signing key), ``trusted.json`` (the log keys its
receipts must verify under) and ``gate-callers.txt`` (agent and approver token hashes).
Gate-specific settings are ``GATE_*`` variables (see ``.env.example``); ``DATABASE_URL``,
``KEYS_DIR`` and ``LEDGER_STREAM`` are shared with the ledger.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from assist.brief import BriefWriter
from assist.llm import LlmClient, model_config
from claims.checker import Trust
from common.clock import SystemClock
from common.config import Settings, load_settings
from common.errors import ConfigError, PreceptError
from common.logging import configure_logging, log_event
from gate.api import create_gate_app
from gate.auth import Callers
from gate.core import BriefWriter as BriefPort
from gate.core import Gate
from gate.decide import load_policy_set
from gate.ledger_client import HttpLedgerClient
from gate.normalizer import Normalizer, StaticPayees
from gate.pg_escalations import PostgresEscalationStore
from learn.api import ExportView, LearnSettings, install_learn_routes
from learn.draft import LearnDrafter
from ledger.checkpointer import origin_for
from ledger.pg_store import PostgresStore
from ledger.receipts import LOG_KEY_OWNER
from ledger.signing import KeyRing, Signer
from policy.api import install_policy_routes
from policy.draft import Drafter
from policy.service import restore_active
from verify.keys import TrustedKeys, WitnessPolicy

CALLERS_FILE = "gate-callers.txt"
logger = logging.getLogger("pl.gate.server")


class GateSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATE_", env_file=".env", extra="ignore", frozen=True
    )

    producer_id: str = "gate-1"
    ledger_url: str = "http://127.0.0.1:8080"
    policy_file: Path = Path("deploy/policies/harbor-federal.cedar")
    payees_file: Path = Path("deploy/demo/payees.json")
    internal_domains: str = "harborfederal.example"
    timezone: str = "America/New_York"  # business hours are the bank's, not UTC
    approval_timeout_seconds: float = 900.0
    sweep_interval_seconds: float = 5.0
    ledger_timeout_seconds: float = 5.0
    briefs: bool = True  # approval briefs from Muse Spark, when a model is configured
    host: str = "127.0.0.1"
    port: int = 8070


def load_gate_keys(keys_dir: Path, producer_id: str) -> tuple[Signer, KeyRing, Callers]:
    """Gate's signer, the log keys, and the callers; ``ConfigError`` if anything is off."""
    try:
        signer = Signer.from_pem_file(keys_dir / f"{producer_id}.pem")
        trusted = TrustedKeys.from_json((keys_dir / "trusted.json").read_bytes())
        callers = Callers.from_text((keys_dir / CALLERS_FILE).read_text("utf-8"))
    except (OSError, PreceptError) as exc:
        raise ConfigError(
            f"cannot load Gate keys from KEYS_DIR: {exc}", code="config.keys"
        ) from exc
    # Refuse to start with a key the auditors' trusted.json does not list for this producer.
    if trusted.producers.get(signer.key_id, ("", b""))[0] != producer_id:
        raise ConfigError(f"{producer_id}.pem is not listed in trusted.json", code="config.keys")
    log_keys = KeyRing((LOG_KEY_OWNER, raw) for raw in trusted.log_keys)
    return signer, log_keys, callers


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {name!r}", code="config.timezone") from exc


@asynccontextmanager
async def gate_from_settings(
    settings: Settings, gate: GateSettings
) -> AsyncIterator[tuple[Gate, Callers]]:
    signer, log_keys, callers = load_gate_keys(settings.keys_dir, gate.producer_id)
    clock = SystemClock()
    normalizer = Normalizer(
        StaticPayees.from_file(gate.payees_file),
        internal_domains=filter(None, (d.strip() for d in gate.internal_domains.split(","))),
        clock=clock,
        timezone=_timezone(gate.timezone),
    )
    store = await PostgresEscalationStore.open_pool(settings.database_url, settings.ledger_stream)
    history = await PostgresStore.open(settings.database_url, max_size=2)
    http = httpx.AsyncClient(timeout=gate.ledger_timeout_seconds)
    config = model_config(settings) if gate.briefs else None
    briefs: BriefPort | None = None if config is None else BriefWriter(LlmClient(config), history)
    log_event(logger, logging.INFO, "gate starting", briefs=config is not None,
              profile=settings.model_profile)  # fmt: skip
    try:
        service = Gate(
            producer_id=gate.producer_id,
            signer=signer,
            stream=settings.ledger_stream,
            ledger=HttpLedgerClient(gate.ledger_url, http),
            log_keys=log_keys,
            normalizer=normalizer,
            policies=load_policy_set(gate.policy_file),
            escalations=store,
            clock=clock,
            approval_timeout=timedelta(seconds=gate.approval_timeout_seconds),
            briefs=briefs,
        )
        # The latest approved policy set replaces the file Gate starts with (D74).
        await restore_active(service, history)
        yield service, callers
    finally:
        await http.aclose()
        await history.close()
        await store.close_pool()


def _drafter(settings: Settings) -> Drafter | None:
    config = model_config(settings)
    if config is None or "wonder" not in config.routes[config.profile]:
        return None
    return Drafter(LlmClient(config))


def _learner(settings: Settings, gate: GateSettings, learn: LearnSettings) -> ExportView:
    """How the learner reads the ledger: the export, verified with the auditors' keys."""
    try:
        trusted = TrustedKeys.from_json((settings.keys_dir / "trusted.json").read_bytes())
        witnesses = None
        if learn.witnessed:
            raw = (settings.keys_dir / "witnesses.txt").read_bytes()
            witnesses = WitnessPolicy.from_text(raw, settings.witness_quorum)
    except (OSError, PreceptError) as exc:
        raise ConfigError(f"learner keys: {exc}", code="config.keys") from exc
    trust = Trust(trusted, origin_for(settings.ledger_origin, settings.ledger_stream), witnesses)
    return ExportView(gate.ledger_url, settings.ledger_stream, trust, learn.export_timeout_seconds)


def app_from_settings() -> FastAPI:
    settings, gate_settings, learn = load_settings(), GateSettings(), LearnSettings()
    configure_logging(settings.log_level)
    drafter = _drafter(settings)
    config = model_config(settings)
    learn_drafter = None if config is None else LearnDrafter(LlmClient(config))
    view = _learner(settings, gate_settings, learn)

    def extend(app: FastAPI) -> None:
        install_policy_routes(app, drafter, demo_mode=settings.demo_mode)
        install_learn_routes(app, learn_drafter, view, learn, demo_mode=settings.demo_mode)

    return create_gate_app(
        lambda: gate_from_settings(settings, gate_settings),
        sweep_interval=gate_settings.sweep_interval_seconds,
        extend=extend,
    )


def main() -> None:
    settings = GateSettings()
    uvicorn.run(
        "gate.server:app_from_settings",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,  # the Gate app logs one JSON line per request
    )
