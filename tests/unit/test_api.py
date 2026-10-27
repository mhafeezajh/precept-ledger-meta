"""Ledger HTTP API over the memory store: every route, every error status, fail-closed paths."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from common.errors import StoreError
from ledger.api import Services, StreamLog, create_app
from ledger.checkpoint import NoteKey, open_checkpoint
from ledger.checkpointer import DirectorySink
from ledger.format import Receipt
from ledger.receipts import LOG_KEY_OWNER, verify_receipt
from ledger.service import Ledger
from ledger.signing import KeyRing
from ledger.store import MemoryStore
from merkle.proofs import verify_consistency, verify_inclusion
from tests.bundles import GATE, LOG, ORIGIN, trusted_keys_json
from tests.helpers import STREAM, clock, make_submission, signer_for
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys

LOG_KEY = NoteKey(ORIGIN, LOG.public_key_raw)
RECORDS = f"/ledger/v1/streams/{STREAM}/records"


@dataclass
class Api:
    client: httpx.AsyncClient
    services: Services
    log: StreamLog
    clock_: Any

    async def append(self, body: dict[str, object] | None = None) -> httpx.Response:
        sub = make_submission(GATE, self.clock_, body=body)
        return await self.client.post(RECORDS, json=sub.to_json())

    async def append_many(self, count: int) -> list[dict[str, Any]]:
        out = []
        for _ in range(count):
            response = await self.append()
            assert response.status_code == 201, response.text
            out.append(response.json())
        return out


def memory_services(directory: Path, store: Any = None) -> Services:
    store = store if store is not None else MemoryStore()
    ledger = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock())
    tree_store = (
        store.tree(STREAM) if isinstance(store, MemoryStore) else MemoryStore().tree(STREAM)
    )
    log = StreamLog.create(STREAM, ORIGIN, tree_store, [DirectorySink(directory)], LOG)
    return Services(ledger=ledger, store=store, logs={STREAM: log})


def static(services: Services) -> Callable[[], Any]:
    @asynccontextmanager
    async def open_services() -> AsyncIterator[Services]:
        yield services

    return open_services


@asynccontextmanager
async def client_for(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(static(services), checkpoint_interval=None)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    services = memory_services(tmp_path / "checkpoints")
    async with client_for(services) as client:
        yield Api(client, services, services.logs[STREAM], clock())


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


# --- append ---------------------------------------------------------------------------------


async def test_append_returns_a_signed_receipt(api: Api) -> None:
    response = await api.append()
    assert response.status_code == 201
    receipt = Receipt.from_json(response.json())
    verify_receipt(receipt, KeyRing([(LOG_KEY_OWNER, LOG.public_key_raw)]))
    assert receipt.seq == 1
    assert len(response.headers["x-trace-id"]) == 32


async def test_trace_id_from_the_caller_is_kept(api: Api) -> None:
    trace = "ab" * 16
    response = await api.client.get("/ledger/v1/health", headers={"x-trace-id": trace})
    assert response.headers["x-trace-id"] == trace
    other = await api.client.get("/ledger/v1/health", headers={"x-trace-id": "not a trace id"})
    assert other.headers["x-trace-id"] != "not a trace id"


@pytest.mark.parametrize(
    "body",
    [b"{not json", b'{"a": 1, "a": 2}', b'{"amount": 1.5}', b"{}", b"[]"],
    ids=["not-json", "duplicate-key", "float", "missing-fields", "not-an-object"],
)
async def test_malformed_submissions_are_400(api: Api, body: bytes) -> None:
    response = await api.client.post(
        RECORDS, content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert error_code(response).startswith("format.")


async def test_wrong_content_type_is_415(api: Api) -> None:
    response = await api.client.post(RECORDS, content=b"{}", headers={"content-type": "text/plain"})
    assert response.status_code == 415


async def test_oversized_body_is_413(api: Api) -> None:
    response = await api.client.post(
        RECORDS, content=b" " * (65 * 1024), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert error_code(response) == "format.too_large"


async def test_unknown_producer_key_is_403(api: Api) -> None:
    sub = make_submission(signer_for("rogue"), api.clock_)
    response = await api.client.post(RECORDS, json=sub.to_json())
    assert response.status_code == 403


async def test_duplicate_is_409(api: Api) -> None:
    sub = make_submission(GATE, api.clock_)
    assert (await api.client.post(RECORDS, json=sub.to_json())).status_code == 201
    again = await api.client.post(RECORDS, json=sub.to_json())
    assert again.status_code == 409
    assert error_code(again) == "ledger.duplicate_record"


async def test_unknown_stream_is_404_and_mismatch_is_400(api: Api) -> None:
    sub = make_submission(GATE, api.clock_)
    unknown = await api.client.post(
        "/ledger/v1/streams/other/decisions/records", json=sub.to_json()
    )
    assert unknown.status_code == 404
    assert error_code(unknown) == "ledger.unknown_stream"
    other = make_submission(GATE, api.clock_, stream="other/decisions")
    mismatch = await api.client.post(RECORDS, json=other.to_json())
    assert mismatch.status_code == 400
    assert error_code(mismatch) == "ledger.stream_mismatch"


class DownStore(MemoryStore):
    async def append(self, stream: str, build: Any) -> Receipt:
        raise StoreError("database unreachable")

    async def get(self, record_id: str) -> Any:
        raise RuntimeError("secret detail that must not leak")


async def test_store_down_is_503_and_never_a_receipt(tmp_path: Path) -> None:
    async with client_for(memory_services(tmp_path, DownStore())) as client:
        sub = make_submission(GATE, clock())
        response = await client.post(RECORDS, json=sub.to_json())
        assert response.status_code == 503
        assert error_code(response) == "store.unavailable"
        crash = await client.get("/ledger/v1/records/01JB6X7Q2M9ZK3T8V4N5R6S7W0")
        assert crash.status_code == 500
        assert crash.json() == {"error": {"code": "internal", "message": "internal error"}}


# --- reads ----------------------------------------------------------------------------------


async def test_get_record(api: Api) -> None:
    (receipt,) = await api.append_many(1)
    response = await api.client.get(f"/ledger/v1/records/{receipt['record_id']}")
    assert response.status_code == 200
    assert response.json()["receipt"] == receipt
    assert response.json()["record"]["seq"] == 1
    missing = await api.client.get("/ledger/v1/records/01JB6X7Q2M9ZK3T8V4N5R6S7W0")
    assert missing.status_code == 404


async def test_latest_checkpoint(api: Api) -> None:
    none_yet = await api.client.get("/ledger/v1/checkpoints/latest")
    assert none_yet.status_code == 404
    await api.append_many(3)
    await api.log.checkpointer.publish()
    body = (await api.client.get("/ledger/v1/checkpoints/latest")).json()
    checkpoint = open_checkpoint(body["note"], LOG_KEY)
    assert (body["size"], body["stream"], checkpoint.size) == (3, STREAM, 3)
    assert body["root"] == base64.b64encode(checkpoint.root).decode()
    assert (body["cosignatures"], body["anchors"]) == ([], [])


async def test_inclusion_proof_verifies_against_the_signed_checkpoint(api: Api) -> None:
    receipts = await api.append_many(5)
    not_yet = await api.client.get(f"/ledger/v1/records/{receipts[1]['record_id']}/proof")
    assert not_yet.status_code == 404
    assert error_code(not_yet) == "checkpoint.not_found"
    await api.log.checkpointer.publish()
    for receipt in receipts:
        response = await api.client.get(f"/ledger/v1/records/{receipt['record_id']}/proof?size=5")
        assert response.status_code == 200
        proof = response.json()
        checkpoint = open_checkpoint(proof["checkpoint"], LOG_KEY)
        hashes = [base64.b64decode(h) for h in proof["hashes"]]
        leaf = base64.b64decode(receipt["leaf_hash"])
        assert proof["leaf_index"] == receipt["seq"] - 1
        assert verify_inclusion(leaf, proof["leaf_index"], 5, hashes, checkpoint.root)


async def test_proof_errors(api: Api) -> None:
    receipts = await api.append_many(3)
    await api.log.checkpointer.publish()
    await api.append_many(1)
    await api.log.checkpointer.publish()
    later = receipts[0]["record_id"]
    no_such_size = await api.client.get(f"/ledger/v1/records/{later}/proof?size=2")
    assert no_such_size.status_code == 404
    fourth = (await api.append_many(1))[0]["record_id"]
    beyond = await api.client.get(f"/ledger/v1/records/{fourth}/proof?size=3")
    assert beyond.status_code == 409
    assert error_code(beyond) == "proof.not_checkpointed"
    bad = await api.client.get(f"/ledger/v1/records/{later}/proof?size=-1")
    assert bad.status_code == 422
    assert error_code(bad) == "request.invalid"


async def test_proof_fails_closed_if_the_tree_no_longer_matches(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipts = await api.append_many(2)
    await api.log.checkpointer.publish()

    async def wrong_head(size: int | None = None) -> Any:
        from merkle.log import TreeHead

        return TreeHead(2, bytes(32))

    monkeypatch.setattr(api.log.tree, "head", wrong_head)
    response = await api.client.get(f"/ledger/v1/records/{receipts[0]['record_id']}/proof")
    assert response.status_code == 503
    assert error_code(response) == "checkpoint.inconsistent"


async def test_consistency_proof(api: Api) -> None:
    await api.append_many(2)
    old = (await api.log.checkpointer.publish()).checkpoint
    await api.append_many(3)
    new = (await api.log.checkpointer.publish()).checkpoint
    response = await api.client.get("/ledger/v1/consistency?from=2&to=5")
    assert response.status_code == 200
    hashes = [base64.b64decode(h) for h in response.json()["hashes"]]
    assert verify_consistency(2, 5, old.root, new.root, hashes)
    backwards = await api.client.get("/ledger/v1/consistency?from=5&to=2")
    assert backwards.status_code == 400
    beyond = await api.client.get("/ledger/v1/consistency?from=1&to=50")
    assert beyond.status_code == 400


async def test_export_verifies_offline(api: Api) -> None:
    await api.append_many(4)
    await api.log.checkpointer.publish()
    response = await api.client.get("/ledger/v1/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-tar"
    report = verify_bundle(
        read_bundle(response.content), TrustedKeys.from_json(trusted_keys_json())
    )
    assert report.ok
    assert report.latest is not None
    assert report.latest.size == 4
    ranged = await api.client.get("/ledger/v1/export?from=1&to=2")
    assert ranged.status_code == 400
    assert error_code(ranged) == "export.range_unsupported"


async def test_stream_query_parameter(api: Api) -> None:
    unknown = await api.client.get("/ledger/v1/checkpoints/latest?stream=nope/decisions")
    assert unknown.status_code == 404
    named = await api.client.get(f"/ledger/v1/checkpoints/latest?stream={STREAM}")
    assert error_code(named) == "checkpoint.not_found"


async def test_health_without_a_loop(api: Api) -> None:
    response = await api.client.get("/ledger/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checkpoint_loop": "disabled", "streams": [STREAM]}


async def test_error_bodies_are_json_with_a_code(api: Api) -> None:
    response = await api.client.get("/ledger/v1/records/x")
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert json.dumps(body)  # serializable, no exception objects


async def test_health_reports_a_running_then_stopped_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    services = memory_services(tmp_path)
    app = create_app(static(services), checkpoint_interval=0.01)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        body = (await client.get("/ledger/v1/health")).json()
        assert (body["checkpoint_loop"], body["checkpoint_errors"]) == ("running", {})
        await asyncio.sleep(0.03)
        assert (await client.get("/ledger/v1/checkpoints/latest")).status_code == 200

        async def crash() -> Any:
            raise RuntimeError("bug")

        monkeypatch.setattr(services.logs[STREAM].checkpointer, "publish", crash)
        await asyncio.sleep(0.05)
        response = await client.get("/ledger/v1/health")
        assert response.status_code == 503
        assert response.json()["checkpoint_loop"] == "stopped"


def test_status_for_unknown_precept_errors_is_500() -> None:
    from common.errors import PolicyError
    from ledger.api import status_for

    assert status_for(PolicyError("x")) == 500


async def test_stream_parameter_is_required_with_several_streams(tmp_path: Path) -> None:
    services = memory_services(tmp_path / "a")
    second = StreamLog.create(
        "other/decisions", "precept-ledger.local/other/decisions",
        services.store.tree("other/decisions"), [DirectorySink(tmp_path / "b")], LOG,
    )  # fmt: skip
    both = Services(services.ledger, services.store, {**services.logs, "other/decisions": second})
    async with client_for(both) as client:
        response = await client.get("/ledger/v1/checkpoints/latest")
        assert response.status_code == 400
        assert error_code(response) == "request.stream_required"


async def test_body_over_the_limit_without_content_length_is_413(api: Api) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(70):
            yield b" " * 1024

    response = await api.client.post(
        RECORDS, content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


async def test_stored_checkpoint_under_another_key_is_never_served(api: Api) -> None:
    from ledger.checkpoint import Checkpoint, sign_checkpoint
    from merkle.hashing import leaf_hash

    forged = sign_checkpoint(Checkpoint(ORIGIN, 1, leaf_hash(b"x")), signer_for("rogue"))
    directory = api.log.sink._dir  # type: ignore[attr-defined]  # test reaches into the sink
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "00000000000000000001.note").write_text(forged)
    response = await api.client.get("/ledger/v1/checkpoints/latest")
    assert response.status_code == 503
    assert error_code(response) == "checkpoint.unverifiable"
