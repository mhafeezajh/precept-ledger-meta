"""Demo beat 4 end to end on Postgres: edit one row, export, and pl-verify names the record."""

import asyncio
import json
import os
from pathlib import Path

import psycopg
import pytest

from ledger.checkpointer import Checkpointer, origin_for
from ledger.export import export_bundle
from ledger.export_cli import main as export_main
from ledger.pg_store import PostgresStore
from ledger.service import Ledger
from ledger.signing import KeyRing
from merkle.log import LogTree
from tests.bundles import trusted_keys_json
from tests.helpers import clock, make_submission, signer_for
from tests.integration.test_pg_store import env
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.cli import main as verify_main
from verify.keys import TrustedKeys

pytestmark = pytest.mark.integration

GATE, LOG = signer_for("gate-1"), signer_for("log")
KEYS = TrustedKeys.from_json(trusted_keys_json())


async def seeded(stream: str) -> tuple[PostgresStore, list[str]]:
    store = await PostgresStore.open(env("DATABASE_URL"))
    ledger, clk = Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock()), clock()
    ids = []
    for amount in (1000, 48000, 3000):
        body = {"action": "value.transfer", "amount": amount}
        receipt = await ledger.append(stream, make_submission(GATE, clk, stream=stream, body=body))
        ids.append(receipt.record_id)
    origin = origin_for("precept-ledger.local", stream)
    await Checkpointer(
        LogTree(store.tree(stream)), LOG, origin, [store.checkpoint_sink(stream)]
    ).publish()
    return store, ids


async def test_admin_edit_is_named_by_pl_verify() -> None:
    stream = f"it-{os.urandom(6).hex()}/decisions"
    store, ids = await seeded(stream)
    try:
        origin = origin_for("precept-ledger.local", stream)
        clean = await export_bundle(store, store.checkpoint_sink(stream), stream, origin)
        assert verify_bundle(read_bundle(clean), KEYS).ok
        async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
            await conn.execute("SET session_replication_role = replica")  # skips triggers
            await conn.execute(
                "UPDATE ledger_record SET body = jsonb_set(body, '{amount}', '480') "
                "WHERE stream = %s AND seq = 2",
                (stream,),
            )
        tampered = await export_bundle(store, store.checkpoint_sink(stream), stream, origin)
    finally:
        await store.close()
    report = verify_bundle(read_bundle(tampered), KEYS)
    assert report.first_break is not None
    assert (report.first_break.seq, report.first_break.check) == (2, "bad_producer_signature")
    assert report.first_break.record_id == ids[1]


async def test_export_cli_then_verify_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = f"it-{os.urandom(6).hex()}/decisions"
    store, _ = await seeded(stream)
    await store.close()
    monkeypatch.setenv("LEDGER_ORIGIN", "precept-ledger.local")
    monkeypatch.setenv("KEYS_DIR", str(tmp_path))
    out, keys = tmp_path / "export.tar", tmp_path / "keys.json"
    keys.write_bytes(trusted_keys_json())
    # pl-export calls asyncio.run, which cannot run inside this test's event loop.
    assert await asyncio.to_thread(export_main, ["--out", str(out), "--stream", stream]) == 0
    assert verify_main([str(out), "--keys", str(keys), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["records"] == 3
