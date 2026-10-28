"""Ledger API with witnesses and anchors: anchors in checkpoints/latest, re-verification,
the export, and a TSA outage in health."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx

from common.errors import PreceptError
from ledger.anchoring import Anchorer, AnchorView, MemoryAnchorStore
from ledger.api import Services, StreamLog
from ledger.checkpointer import DirectorySink
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import MemoryStore
from ledger.witnessing import MemoryCosignatureStore, WitnessSet
from tests.bundles import GATE, LOG, ORIGIN, trusted_keys_json
from tests.helpers import STREAM, T0, clock
from tests.unit.test_anchoring import LocalCalendar, LocalTSA
from tests.unit.test_api_witnessed import append, open_client
from tests.unit.test_verify_anchors import ROOTS, ClockedTSA, FixedClock
from tests.unit.test_witnessing import KEYS, witnesses
from verify.anchors import AnchorPolicy
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys, WitnessPolicy


def services(directory: Path, tsa: object, store: MemoryAnchorStore | None = None) -> Services:
    memory = MemoryStore()
    ledger = Ledger(memory, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    calendar = LocalCalendar()
    anchorer = Anchorer(store or MemoryAnchorStore(), [tsa], [calendar],  # type: ignore[list-item]
                        {calendar.url: calendar}, clock=FixedClock())  # fmt: skip
    log = StreamLog.create(
        STREAM,
        ORIGIN,
        memory.tree(STREAM),
        [DirectorySink(directory)],
        LOG,
        witnesses=WitnessSet(KEYS, 2, MemoryCosignatureStore()),
        clients=witnesses(),
        anchoring=AnchorView(anchorer, ROOTS),
    )
    return Services(ledger=ledger, store=memory, logs={STREAM: log})


async def publish(svc: Services) -> None:
    with contextlib.suppress(PreceptError):
        await svc.logs[STREAM].publisher.publish()


async def client(svc: Services, interval: float | None = None) -> AsyncIterator[httpx.AsyncClient]:
    async for http in open_client(svc, interval):
        yield http


async def test_latest_checkpoint_lists_verified_anchors(tmp_path: Path) -> None:
    svc = services(tmp_path, ClockedTSA(T0 + timedelta(seconds=5)))
    async for http in client(svc):
        await append(http, 3)
        await publish(svc)
        body = (await http.get("/ledger/v1/checkpoints/latest?final=true")).json()
    kinds = {a["kind"]: a for a in body["anchors"]}
    assert kinds["rfc3161"]["gen_time"] == (T0 + timedelta(seconds=5)).isoformat()
    assert kinds["rfc3161"]["tsa"] == "Test TSA"
    assert kinds["ots"]["status"] == "pending"
    assert kinds["ots"]["attestations"][0]["kind"] == "pending"


async def test_an_edited_anchor_row_is_never_served(tmp_path: Path) -> None:
    store = MemoryAnchorStore()
    svc = services(tmp_path, ClockedTSA(T0 + timedelta(seconds=5)), store)
    async for http in client(svc):
        await append(http, 1)
        await publish(svc)
        key = next(k for k in store.stored if k[1] == "rfc3161")
        row = store.stored[key]
        store.stored[key] = type(row)(**{**vars(row), "token": row.token[:-1] + b"\x00"})
        response = await http.get("/ledger/v1/checkpoints/latest")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "anchor.unverifiable"


async def test_export_carries_anchors_that_verify_offline(tmp_path: Path) -> None:
    svc = services(tmp_path, ClockedTSA(T0 + timedelta(seconds=5)))
    async for http in client(svc):
        await append(http, 4)
        await publish(svc)
        bundle = read_bundle((await http.get("/ledger/v1/export")).content)
    report = verify_bundle(
        bundle,
        TrustedKeys.from_json(trusted_keys_json()),
        witnesses=WitnessPolicy(KEYS, 2),
        anchors=AnchorPolicy(ROOTS),
    )
    assert report.ok, report.findings
    assert report.anchors is not None
    latest = report.anchors.latest
    assert latest is not None
    assert latest.size == 4


async def test_tsa_outage_shows_in_health(tmp_path: Path) -> None:
    svc = services(tmp_path, LocalTSA(fail=True))
    async for http in client(svc, interval=0.05):
        await append(http, 1)
        for _ in range(100):
            health = (await http.get("/ledger/v1/health")).json()
            if health["checkpoint_errors"]:
                break
            await asyncio.sleep(0.02)
    assert health["checkpoint_errors"] == {STREAM: "anchor.no_timestamp"}
