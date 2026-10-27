"""The real app over Postgres with the checkpoint loop running (make test-integration)."""

import asyncio
import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from common.config import Settings
from ledger.api import create_app
from ledger.checkpoint import NoteKey, open_checkpoint
from ledger.checkpointer import origin_for
from ledger.server import postgres_services
from ledger.signing import Signer
from merkle.proofs import verify_inclusion
from scripts.dev_keys import ensure_keys
from tests.helpers import clock, make_submission
from tests.integration.test_pg_store import env
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys

pytestmark = pytest.mark.integration


def settings_for(tmp_path: Path, stream: str, database_url: str | None = None) -> Settings:
    ensure_keys(tmp_path / "keys")
    return Settings(
        database_url=database_url or env("DATABASE_URL"),
        keys_dir=tmp_path / "keys",
        checkpoint_dir=tmp_path / "checkpoints",
        ledger_stream=stream,
    )


async def client(settings: Settings, interval: float) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(lambda: postgres_services(settings), checkpoint_interval=interval)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        yield http


async def test_append_checkpoint_proof_export(tmp_path: Path) -> None:
    stream = f"it-{os.urandom(6).hex()}/decisions"
    settings = settings_for(tmp_path, stream)
    gate = Signer.from_pem_file(tmp_path / "keys" / "gate-1.pem")
    clk = clock()
    async for http in client(settings, interval=0.05):
        receipts = []
        for _ in range(3):
            sub = make_submission(gate, clk, stream=stream)
            response = await http.post(f"/ledger/v1/streams/{stream}/records", json=sub.to_json())
            assert response.status_code == 201, response.text
            receipts.append(response.json())
        for _ in range(100):  # the loop checkpoints every 50 ms
            latest = await http.get("/ledger/v1/checkpoints/latest")
            if latest.status_code == 200 and latest.json()["size"] == 3:
                break
            await asyncio.sleep(0.05)
        key = NoteKey(origin_for(settings.ledger_origin, stream), gate.public_key_raw)
        log_key = NoteKey(
            key.name, Signer.from_pem_file(tmp_path / "keys" / "log.pem").public_key_raw
        )
        checkpoint = open_checkpoint(latest.json()["note"], log_key)
        proof = (await http.get(f"/ledger/v1/records/{receipts[1]['record_id']}/proof")).json()
        assert verify_inclusion(
            base64.b64decode(receipts[1]["leaf_hash"]), 1, 3,
            [base64.b64decode(h) for h in proof["hashes"]], checkpoint.root,
        )  # fmt: skip
        bundle = (await http.get("/ledger/v1/export")).content
        trusted = TrustedKeys.from_json((tmp_path / "keys" / "trusted.json").read_bytes())
        assert verify_bundle(read_bundle(bundle), trusted).ok
        health = (await http.get("/ledger/v1/health")).json()
        assert (health["status"], health["checkpoint_loop"]) == ("ok", "running")
    assert (tmp_path / "checkpoints" / stream / "00000000000000000003.note").exists()


async def test_database_down_means_no_receipt(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "s/decisions", "postgresql://nobody:x@127.0.0.1:1/none")
    gate = Signer.from_pem_file(tmp_path / "keys" / "gate-1.pem")
    async for http in client(settings, interval=0.05):
        sub = make_submission(gate, clock(), stream="s/decisions")
        response = await http.post("/ledger/v1/streams/s/decisions/records", json=sub.to_json())
        assert response.status_code == 503
        await asyncio.sleep(0.1)
        health = (await http.get("/ledger/v1/health")).json()
        assert health["checkpoint_errors"] == {"s/decisions": "store.unavailable"}
