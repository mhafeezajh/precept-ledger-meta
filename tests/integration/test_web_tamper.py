"""The console's tamper button (web/demo.py) against real Postgres: the admin edit lands, the
app role could not have made it, and verification names the edited record (demo beat 4)."""

import os

import pytest

from ledger.checkpointer import Checkpointer, origin_for
from ledger.export import export_bundle
from ledger.pg_store import PostgresStore
from ledger.service import Ledger
from ledger.signing import KeyRing
from merkle.log import LogTree
from tests.bundles import trusted_keys_json
from tests.helpers import clock, make_submission, signer_for
from tests.integration.test_pg_store import env
from verify.bundle import read_bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys
from web import demo

pytestmark = pytest.mark.integration
GATE, LOG = signer_for("gate-1"), signer_for("log")


async def test_the_tamper_button_edits_one_amount_and_verification_names_it() -> None:
    stream = f"it-{os.urandom(6).hex()}/decisions"
    store = await PostgresStore.open(env("DATABASE_URL"))
    try:
        ledger, clk = (
            Ledger(store, KeyRing([("gate-1", GATE.public_key_raw)]), LOG, clock()),
            clock(),
        )
        for cents in (120_000, 4_800_000, 95_000):
            body = {"action": "value.transfer", "resource": {"amount_cents": cents}}
            sub = make_submission(GATE, clk, stream=stream, body=body, record_type="gate.decision")
            await ledger.append(stream, sub)
        origin = origin_for("precept-ledger.local", stream)
        await Checkpointer(
            LogTree(store.tree(stream)), LOG, origin, [store.checkpoint_sink(stream)]
        ).publish()
        assert await demo.tamper(env("ADMIN_DATABASE_URL"), stream, 2) == 1
        assert await demo.tamper(env("ADMIN_DATABASE_URL"), stream, 99) == 0
        bundle = await export_bundle(store, store.checkpoint_sink(stream), stream, origin)
    finally:
        await store.close()
    report = verify_bundle(read_bundle(bundle), TrustedKeys.from_json(trusted_keys_json()))
    assert report.first_break is not None
    assert (report.first_break.seq, report.first_break.check) == (2, "bad_producer_signature")


async def test_a_bad_admin_connection_is_reported_not_raised_raw() -> None:
    with pytest.raises(demo.WebError) as caught:
        await demo.tamper("postgresql://nobody:nothing@127.0.0.1:1/none", "s", 1)
    assert caught.value.code == "web.admin"
