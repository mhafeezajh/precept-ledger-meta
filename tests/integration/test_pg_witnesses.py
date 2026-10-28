"""Full path over Postgres and real HTTP: ledger -> three witness servers -> 2-of-3 final ->
export -> pl-verify with the witness quorum (skill §6 integration row)."""

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn

from common.clock import SystemClock
from common.config import Settings
from ledger.api import create_app
from ledger.checkpointer import origin_for
from ledger.cosignature import open_cosignatures
from ledger.server import postgres_services
from ledger.signing import Signer
from scripts.dev_keys import ensure_keys
from tests.helpers import clock, make_submission
from tests.integration.test_pg_store import env
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys, WitnessPolicy
from witness.api import create_witness_app
from witness.core import Witness
from witness.keys import load_log_keys, load_signer
from witness.state import FileWitnessState

pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def witness_servers(keys: Path, state: Path, count: int) -> AsyncIterator[dict[str, str]]:
    servers, tasks, urls = [], [], {}
    for n in range(1, count + 1):
        name = f"witness-{n}"
        witness = Witness(
            name,
            load_signer(keys / "witnesses" / f"{name}.pem"),
            load_log_keys(keys / "log-verifiers.txt"),
            FileWitnessState(state / name),
            SystemClock(),
        )
        port = free_port()
        config = uvicorn.Config(
            create_witness_app(witness), host="127.0.0.1", port=port, log_config=None
        )
        server = uvicorn.Server(config)
        servers.append(server)
        tasks.append(asyncio.create_task(server.serve()))
        urls[name] = f"http://127.0.0.1:{port}"
    while not all(s.started for s in servers):
        await asyncio.sleep(0.01)
    try:
        yield urls
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks)


async def test_checkpoints_become_final_and_the_export_verifies_with_quorum(
    tmp_path: Path,
) -> None:
    stream = f"it-{os.urandom(6).hex()}/decisions"
    keys = tmp_path / "keys"
    ensure_keys(keys, origin_for("precept-ledger.local", stream))
    gate = Signer.from_pem_file(keys / "gate-1.pem")
    # Only two witnesses run: the third is down, and 2 of 3 is still final (D5).
    async with witness_servers(keys, tmp_path / "witness-state", 2) as urls:
        urls["witness-3"] = f"http://127.0.0.1:{free_port()}"  # nothing listens here
        settings = Settings(
            database_url=env("DATABASE_URL"),
            keys_dir=keys,
            checkpoint_dir=tmp_path / "checkpoints",
            ledger_stream=stream,
            witnesses=",".join(f"{n}={u}" for n, u in urls.items()),
            witness_timeout_seconds=1.0,
        )
        app = create_app(lambda: postgres_services(settings), checkpoint_interval=0.1)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        ):
            clk = clock()
            for _ in range(3):
                sub = make_submission(gate, clk, stream=stream)
                response = await http.post(
                    f"/ledger/v1/streams/{stream}/records", json=sub.to_json()
                )
                assert response.status_code == 201, response.text
            body: dict[str, object] = {}
            for _ in range(100):
                response = await http.get("/ledger/v1/checkpoints/latest?final=true")
                body = response.json()
                if response.status_code == 200 and body["size"] == 3:
                    break
                await asyncio.sleep(0.05)
            assert body["size"] == 3, body
            assert [c["witness"] for c in body["cosignatures"]] == ["witness-1", "witness-2"]  # type: ignore[union-attr]
            bundle = (await http.get("/ledger/v1/export")).content

    policy = WitnessPolicy.from_text((keys / "witnesses.txt").read_bytes(), 2)
    trusted = TrustedKeys.from_json((keys / "trusted.json").read_bytes())
    report = verify_bundle(read_bundle(bundle), trusted, witnesses=policy)
    assert report.ok, report.findings
    assert report.final is not None
    assert (report.final.size, report.final_cosigners) == (3, ("witness-1", "witness-2"))
    assert len(open_cosignatures(str(body["note"]), policy.keys)) == 2

    async with await psycopg.AsyncConnection.connect(env("DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM cosignature WHERE stream = %s", (stream,))
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute("UPDATE cosignature SET line = '' WHERE stream = %s", (stream,))
