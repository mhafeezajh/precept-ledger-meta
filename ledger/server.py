"""``pl-ledger``: serve the ledger API from Postgres, with keys from ``KEYS_DIR``.

``KEYS_DIR`` holds ``log.pem`` (the log's private key) and ``trusted.json`` (public keys of
producers and the log, the same file ``pl-verify --keys`` reads). ``scripts/dev_keys.py``
creates both for local runs. The service refuses to start if they disagree (R4).

With ``WITNESSES`` set, every checkpoint is sent to those witnesses and their cosignatures
are checked against ``KEYS_DIR/witnesses.txt`` (the ``pl-verify --witness-keys`` file).
With ``TSAS`` and ``OTS_CALENDARS`` set, final checkpoints are anchored (``ledger.anchoring``).
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from anchor.clients import HttpCalendar, HttpTimestampAuthority
from anchor.rfc3161 import load_roots
from common.clock import SystemClock
from common.config import Settings, load_settings
from common.errors import ConfigError, PreceptError
from common.logging import configure_logging
from ledger.anchoring import Anchorer, AnchorStore, AnchorView
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


def parse_named_urls(value: str, setting: str) -> list[tuple[str, str]]:
    """``"name-1=http://host:8091,name-2=..."`` -> ``[(name, url), ...]``."""
    code = f"config.{setting.lower()}"
    pairs = []
    for item in filter(None, (part.strip() for part in value.split(","))):
        name, sep, url = item.partition("=")
        if not sep or not name or not url.startswith(("http://", "https://")):
            raise ConfigError(f"{setting} entry {item!r} is not name=url", code=code)
        pairs.append((name, url))
    if len({name for name, _ in pairs}) != len(pairs):
        raise ConfigError(f"{setting} names one entry twice", code=code)
    return pairs


def parse_witnesses(value: str) -> list[tuple[str, str]]:
    return parse_named_urls(value, "WITNESSES")


def parse_urls(value: str, setting: str) -> list[str]:
    urls = [part.strip().rstrip("/") for part in value.split(",") if part.strip()]
    for url in urls:
        if not url.startswith("https://"):
            raise ConfigError(f"{setting}: {url!r} must be https", code="config.anchors")
    return urls


def anchor_view(
    settings: Settings, store: AnchorStore, http: httpx.AsyncClient
) -> AnchorView | None:
    """The anchorer from TSAS / OTS_CALENDARS, or ``None`` if neither is set."""
    tsas = parse_named_urls(settings.tsas, "TSAS")
    calendars = parse_urls(settings.ots_calendars, "OTS_CALENDARS")
    if not tsas and not calendars:
        return None
    try:
        roots = load_roots(settings.tsa_roots_file.read_bytes()) if tsas else []
    except (OSError, PreceptError) as exc:
        raise ConfigError(f"cannot read TSA roots: {exc}", code="config.anchors") from exc
    if tsas and not roots:
        raise ConfigError("TSA_ROOTS_FILE lists no certificates", code="config.anchors")
    upgrades = parse_urls(settings.ots_upgrade_calendars, "OTS_UPGRADE_CALENDARS")
    anchorer = Anchorer(
        store,
        [HttpTimestampAuthority(name, url, http, roots) for name, url in tsas],
        [HttpCalendar(url, http) for url in calendars],
        {url: HttpCalendar(url, http) for url in upgrades},
        clock=SystemClock(),
        ots_interval_seconds=settings.ots_interval_seconds,
    )
    return AnchorView(anchorer, tuple(roots))


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
    anchor_http = httpx.AsyncClient(timeout=settings.anchor_timeout_seconds)
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
            anchoring=anchor_view(settings, store.anchors(stream), anchor_http),
        )
        ledger = Ledger(store, producers, log_signer, SystemClock())
        yield Services(ledger=ledger, store=store, logs={stream: log})
    finally:
        await http.aclose()
        await anchor_http.aclose()
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
