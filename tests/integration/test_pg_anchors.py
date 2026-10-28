"""Anchors over Postgres (make test-integration): the anchor table, export, offline checks,
and the insert-only rules."""

from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from ledger.anchoring import Anchorer
from ledger.checkpointer import origin_for
from ledger.export import export_bundle
from ledger.pg_store import PostgresStore
from tests.bundles import trusted_keys_json
from tests.helpers import T0
from tests.integration.test_pg_checkpoints import append, checkpointer, store, stream
from tests.integration.test_pg_store import env
from tests.unit.test_anchoring import LocalCalendar
from tests.unit.test_verify_anchors import ROOTS, ClockedTSA, FixedClock
from verify.anchors import AnchorPolicy
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys

pytestmark = pytest.mark.integration
__all__ = ["store", "stream"]  # fixtures


async def test_anchors_are_stored_exported_and_verified(
    store: PostgresStore, stream: str, tmp_path: Path
) -> None:
    calendar = LocalCalendar()
    anchors = store.anchors(stream)
    tsa = ClockedTSA(T0 + timedelta(seconds=5))
    anchorer = Anchorer(anchors, [tsa], [calendar], {calendar.url: calendar},
                        clock=FixedClock(), ots_interval_seconds=0)  # fmt: skip
    publisher = checkpointer(store, stream, tmp_path)
    await append(store, stream, 2)
    await anchorer.anchor(await publisher.publish())
    calendar.confirmed = True
    await append(store, stream, 1)
    await anchorer.anchor(await publisher.publish())
    rows = await anchors.rows()
    assert sorted((r.size, r.kind, r.status) for r in rows) == [
        (2, "ots", "confirmed"), (2, "ots", "pending"), (2, "rfc3161", "verified"),
        (3, "ots", "confirmed"), (3, "ots", "pending"), (3, "rfc3161", "verified"),
    ]  # fmt: skip
    await anchors.add(rows[0])  # first row wins: a repeat is ignored
    assert len(await anchors.rows()) == 6

    origin = origin_for("precept-ledger.local", stream)
    sink = store.checkpoint_sink(stream)
    bundle = read_bundle(await export_bundle(store, sink, stream, origin, None, anchors))
    report = verify_bundle(
        bundle, TrustedKeys.from_json(trusted_keys_json()), anchors=AnchorPolicy(ROOTS)
    )
    assert report.ok, report.findings
    assert report.anchors is not None
    assert {c.status for c in report.anchors.bitcoin} == {"pending", "bitcoin"}

    async with await psycopg.AsyncConnection.connect(env("DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM anchor WHERE stream = %s", (stream,))
    async with await psycopg.AsyncConnection.connect(env("ADMIN_DATABASE_URL")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            await conn.execute(
                "UPDATE anchor SET status = 'confirmed' WHERE stream = %s", (stream,)
            )
