"""Ledger API with three witnesses: cosigned checkpoints, finality, proofs, export, health."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from common.errors import WitnessError
from ledger.api import Services, StreamLog, create_app
from ledger.checkpointer import DirectorySink
from ledger.cosignature import open_cosignatures
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import MemoryStore
from ledger.witnessing import MemoryCosignatureStore, WitnessSet
from tests.bundles import GATE, LOG, ORIGIN, trusted_keys_json
from tests.helpers import STREAM, clock, make_submission
from tests.unit.test_api import static
from tests.unit.test_witnessing import KEYS, NAMES, Down, witnesses
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys, WitnessPolicy

RECORDS = f"/ledger/v1/streams/{STREAM}/records"


def services(directory: Path, clients: list[object]) -> Services:
    store = MemoryStore()
    ledger = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    log = StreamLog.create(
        STREAM,
        ORIGIN,
        store.tree(STREAM),
        [DirectorySink(directory)],
        LOG,
        witnesses=WitnessSet(KEYS, 2, MemoryCosignatureStore()),
        clients=clients,  # type: ignore[arg-type]
        witness_timeout_seconds=0.5,
    )
    return Services(ledger=ledger, store=store, logs={STREAM: log})


async def open_client(svc: Services, interval: float | None) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(static(svc), checkpoint_interval=interval)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


async def append(client: httpx.AsyncClient, count: int) -> list[str]:
    ids, clk = [], clock()
    for _ in range(count):
        response = await client.post(RECORDS, json=make_submission(GATE, clk).to_json())
        assert response.status_code == 201, response.text
        ids.append(response.json()["record_id"])
    return ids


async def publish(svc: Services) -> None:
    # A no-quorum tick raises witness.no_quorum; some tests expect exactly that.
    with contextlib.suppress(WitnessError):
        await svc.logs[STREAM].publisher.publish()


@pytest.fixture
async def witnessed(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Services]]:
    svc = services(tmp_path, witnesses())  # type: ignore[arg-type]
    async for client in open_client(svc, None):
        yield client, svc


async def test_latest_checkpoint_is_cosigned_and_final(
    witnessed: tuple[httpx.AsyncClient, Services],
) -> None:
    client, svc = witnessed
    await append(client, 3)
    await publish(svc)
    body = (await client.get("/ledger/v1/checkpoints/latest")).json()
    assert (body["size"], body["quorum"], body["final"]) == (3, 2, True)
    assert [c["witness"] for c in body["cosignatures"]] == list(NAMES)
    assert sorted(open_cosignatures(body["note"], KEYS)) == list(NAMES)


async def test_final_filter_skips_checkpoints_without_quorum(tmp_path: Path) -> None:
    w = witnesses()
    svc = services(tmp_path, w)  # type: ignore[arg-type]
    async for client in open_client(svc, None):
        await append(client, 2)
        await publish(svc)  # final at size 2
        log = svc.logs[STREAM]
        assert log.witnessing is not None
        # Witnesses 2 and 3 become unreachable for the next checkpoint.
        log.witnessing._clients = [w[0], Down("witness-2"), Down("witness-3")]  # type: ignore[list-item]
        await append(client, 2)
        await publish(svc)  # size 4: one cosignature only
        latest = (await client.get("/ledger/v1/checkpoints/latest")).json()
        final = (await client.get("/ledger/v1/checkpoints/latest?final=true")).json()
        assert (latest["size"], latest["final"], len(latest["cosignatures"])) == (4, False, 1)
        assert (final["size"], final["final"]) == (2, True)
        health = (await client.get("/ledger/v1/health")).json()
        assert health["witnesses"] == {STREAM: {"count": 3, "quorum": 2}}


async def test_no_final_checkpoint_is_404(tmp_path: Path) -> None:
    svc = services(tmp_path, [Down(n) for n in NAMES])
    async for client in open_client(svc, None):
        await append(client, 1)
        await publish(svc)
        response = await client.get("/ledger/v1/checkpoints/latest?final=true")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "checkpoint.not_found"


async def test_proof_carries_the_cosigned_note(
    witnessed: tuple[httpx.AsyncClient, Services],
) -> None:
    client, svc = witnessed
    ids = await append(client, 3)
    await publish(svc)
    body = (await client.get(f"/ledger/v1/records/{ids[1]}/proof")).json()
    assert body["final"] is True
    assert len(open_cosignatures(body["checkpoint"], KEYS)) == 3


async def test_tampered_cosignature_is_503_not_served(
    witnessed: tuple[httpx.AsyncClient, Services],
) -> None:
    client, svc = witnessed
    await append(client, 1)
    await publish(svc)
    witnesses_ = svc.logs[STREAM].witnesses
    assert witnesses_ is not None
    store = witnesses_.store
    assert isinstance(store, MemoryCosignatureStore)
    line = store.rows[1]["witness-1"]
    # Flip a character in the signature bytes; the key hash still names witness-1.
    store.rows[1]["witness-1"] = line[:-6] + ("A" if line[-6] != "A" else "B") + line[-5:]
    response = await client.get("/ledger/v1/checkpoints/latest")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "cosignature.unverifiable"


async def test_export_verifies_with_the_witness_quorum(
    witnessed: tuple[httpx.AsyncClient, Services],
) -> None:
    client, svc = witnessed
    await append(client, 4)
    await publish(svc)
    bundle = read_bundle((await client.get("/ledger/v1/export")).content)
    report = verify_bundle(
        bundle, TrustedKeys.from_json(trusted_keys_json()), witnesses=WitnessPolicy(KEYS, 2)
    )
    assert report.ok, report.findings
    assert report.final is not None
    assert report.final.size == 4


async def test_loop_reports_no_quorum_in_health(tmp_path: Path) -> None:
    svc = services(tmp_path, [witnesses()[0], Down("witness-2"), Down("witness-3")])
    async for client in open_client(svc, 0.05):
        await append(client, 1)
        for _ in range(100):
            health = (await client.get("/ledger/v1/health")).json()
            if health["checkpoint_errors"]:
                break
            await asyncio.sleep(0.02)
    assert health["checkpoint_errors"] == {STREAM: "witness.no_quorum"}
    assert health["checkpoint_loop"] == "running"
