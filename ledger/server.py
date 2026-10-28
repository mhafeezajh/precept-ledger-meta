"""``pl-ledger``: serve the ledger API from Postgres, with keys from ``KEYS_DIR``.

``KEYS_DIR`` holds ``log.pem`` (the log's private key) and ``trusted.json`` (public keys of
producers and the log, the same file ``pl-verify --keys`` reads). ``scripts/dev_keys.py``
creates both for local runs. The service refuses to start if they disagree (R4).

With ``WITNESSES`` set, every checkpoint is sent to those witnesses and their cosignatures
are checked against ``KEYS_DIR/witnesses.txt`` (the ``pl-verify --witness-keys`` file).
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from common.clock import SystemClock
from common.config import Settings, load_settings
from common.errors import ConfigError, PreceptError
from common.logging import configure_logging
from ledger.api import Services, StreamLog, create_app
from ledger.checkpointer import CheckpointSink, DirectorySink, origin_for
from ledger.pg_store import PostgresStore
from ledger.service import Ledger
from ledger.signing import KeyRing, Signer
from ledger.witness_client import HttpWitnessClient
from ledger.witnessing import CosignatureStore, WitnessClient, WitnessSet
from verify.keys import TrustedKeys, WitnessPolicy

LOG_KEY_FILE = "log.pem"
TRUSTED_KEYS_FILE = "trusted.json"
WITNESS_KEYS_FILE = "witnesses.txt"


def load_keys(keys_dir: Path) -> tuple[Signer, KeyRing]:
    """The log signer and the producer key ring; ``ConfigError`` if anything is off."""
    try:
        log = Signer.from_pem_file(keys_dir / LOG_KEY_FILE)
        trusted = TrustedKeys.from_json((keys_dir / TRUSTED_KEYS_FILE).read_bytes())
    except (OSError, PreceptError) as exc:
        raise ConfigError(f"cannot load keys from KEYS_DIR: {exc}", code="config.keys") from exc
    if log.public_key_raw not in trusted.log_keys:
        raise ConfigError(
            f"{LOG_KEY_FILE} is not a log key in {TRUSTED_KEYS_FILE}", code="config.keys"
        )
    return log, KeyRing(trusted.producers.values())


def parse_witnesses(value: str) -> list[tuple[str, str]]:
    """``"witness-1=http://host:8091,witness-2=..."`` -> ``[(name, url), ...]``."""
    pairs = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, sep, url = item.partition("=")
        if not sep or not name or not url.startswith(("http://", "https://")):
            raise ConfigError(f"WITNESSES entry {item!r} is not name=url", code="config.witnesses")
        pairs.append((name, url))
    if len({name for name, _ in pairs}) != len(pairs):
        raise ConfigError("WITNESSES names a witness twice", code="config.witnesses")
    return pairs


def load_witness_set(
    keys_dir: Path, names: Sequence[str], quorum: int, store: CosignatureStore
) -> WitnessSet:
    """The witnesses' trusted keys (all of witnesses.txt) and the quorum, checked up front."""
    try:
        policy = WitnessPolicy.from_text((keys_dir / WITNESS_KEYS_FILE).read_bytes(), quorum)
        witness_set = WitnessSet(policy.keys, quorum, store)
        for name in names:
            witness_set.key_for(name)
    except (OSError, PreceptError) as exc:
        raise ConfigError(f"cannot set up witnesses: {exc}", code="config.witnesses") from exc
    return witness_set


@asynccontextmanager
async def postgres_services(settings: Settings) -> AsyncIterator[Services]:
    log_signer, producers = load_keys(settings.keys_dir)
    pairs = parse_witnesses(settings.witnesses)
    stream = settings.ledger_stream
    store = await PostgresStore.open(settings.database_url)
    http = httpx.AsyncClient(timeout=settings.witness_timeout_seconds)
    try:
        witness_set = None
        if pairs:
            witness_set = load_witness_set(
                settings.keys_dir,
                [name for name, _ in pairs],
                settings.witness_quorum,
                store.cosignatures(stream),
            )
        clients: list[WitnessClient] = [HttpWitnessClient(n, url, http) for n, url in pairs]
        sinks: list[CheckpointSink] = [
            store.checkpoint_sink(stream),
            DirectorySink(settings.checkpoint_dir / stream),
        ]
        origin = origin_for(settings.ledger_origin, stream)
        log = StreamLog.create(
            stream,
            origin,
            store.tree(stream),
            sinks,
            log_signer,
            witnesses=witness_set,
            clients=clients,
            witness_timeout_seconds=settings.witness_timeout_seconds,
        )
        ledger = Ledger(store, producers, log_signer, SystemClock())
        yield Services(ledger=ledger, store=store, logs={stream: log})
    finally:
        await http.aclose()
        await store.close()


def app_from_settings() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level)
    return create_app(
        lambda: postgres_services(settings),
        checkpoint_interval=settings.checkpoint_interval_seconds,
    )


def main() -> None:
    settings = load_settings()
    # Our middleware writes one JSON line per request; uvicorn's access log would repeat it
    # with the raw path and query.
    uvicorn.run(
        "ledger.server:app_from_settings",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )
