"""Ledger side of witnessing: collecting, verifying and storing cosignatures; 2-of-3 finality;
the rogue operator; witnesses down, slow, lying or buggy; the HTTP client."""

import asyncio
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from common.errors import LedgerError, WitnessError
from ledger.checkpointer import Checkpointer, DirectorySink
from ledger.cosignature import CosignerKey, cosign
from ledger.witness_client import HttpWitnessClient
from ledger.witnessing import (
    MemoryCosignatureStore,
    WitnessedPublisher,
    Witnessing,
    WitnessSet,
)
from merkle.log import LogTree
from merkle.store import MemoryTreeStore
from tests.helpers import clock, signer_for
from tests.unit.test_witness import LOG, LOG_KEY, ORIGIN, leaves
from witness.api import create_witness_app
from witness.core import Witness
from witness.state import MemoryWitnessState

NAMES = ("witness-1", "witness-2", "witness-3")
SIGNERS = {name: signer_for(name) for name in NAMES}
KEYS = tuple(CosignerKey(n, s.public_key_raw) for n, s in SIGNERS.items())


def witnesses() -> list[Witness]:
    return [
        Witness(n, s, [LOG_KEY], MemoryWitnessState(), clock(step_ms=1000))
        for n, s in SIGNERS.items()
    ]


class Log:
    """A tree, a checkpointer and witnessing over the given witness clients."""

    def __init__(self, directory: Path, clients: Sequence[object], quorum: int = 2) -> None:
        self.tree_store = MemoryTreeStore()
        self.tree = LogTree(self.tree_store)
        self.store = MemoryCosignatureStore()
        self.set = WitnessSet(KEYS, quorum, self.store)
        self.checkpointer = Checkpointer(self.tree, LOG, ORIGIN, [DirectorySink(directory)])
        self.witnessing = Witnessing(self.tree, self.set, clients, timeout_seconds=0.5)  # type: ignore[arg-type]
        self.publisher = WitnessedPublisher(self.checkpointer, self.witnessing)

    def grow(self, items: list[bytes]) -> None:
        for item in items:
            self.tree_store.append(item)


class Down:
    def __init__(self, name: str) -> None:
        self.name, self.calls = name, 0

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        self.calls += 1
        raise WitnessError("down", code="witness.unreachable")


class Slow(Down):
    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        await asyncio.sleep(5)
        return ""


class Buggy(Down):
    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        raise RuntimeError("bug")


class Liar(Down):
    """Signs, but with someone else's key."""

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        from ledger.checkpoint import open_checkpoint

        return cosign(open_checkpoint(note, LOG_KEY), signer_for("rogue"), self.name, 1)


class Counting:
    def __init__(self, inner: Witness) -> None:
        self.inner, self.name, self.calls = inner, inner.name, 0

    async def add_checkpoint(self, old_size: int, proof: Sequence[bytes], note: str) -> str:
        self.calls += 1
        return await self.inner.add_checkpoint(old_size, proof, note)


async def test_three_witnesses_cosign_and_the_checkpoint_is_final(tmp_path: Path) -> None:
    log = Log(tmp_path, witnesses())
    log.grow(leaves(3))
    signed = await log.publisher.publish()
    note, cosigners = await log.set.cosigned(signed.note, 3)
    assert sorted(cosigners) == list(NAMES)
    assert note.startswith(signed.note)
    log.grow(leaves(2, tag=b"more"))
    await log.publisher.publish()  # consistency proof 3 -> 5 accepted by all three
    assert sorted(await log.store.lines(5)) == list(NAMES)


async def test_one_witness_down_is_still_final(tmp_path: Path) -> None:
    w = witnesses()
    log = Log(tmp_path, [w[0], w[1], Down("witness-3")])
    log.grow(leaves(2))
    signed = await log.checkpointer.publish()
    result = await log.witnessing.collect(signed)
    assert result.final
    assert result.cosigners == ("witness-1", "witness-2")
    assert result.refused == {"witness-3": "witness.unreachable"}


@pytest.mark.parametrize("bad", [Down, Slow, Buggy, Liar])
async def test_two_bad_witnesses_mean_no_quorum(tmp_path: Path, bad: type[Down]) -> None:
    log = Log(tmp_path, [witnesses()[0], bad("witness-2"), bad("witness-3")])
    log.grow(leaves(2))
    with pytest.raises(WitnessError) as exc:
        await log.publisher.publish()
    assert exc.value.code == "witness.no_quorum"
    assert list(await log.store.lines(2)) == ["witness-1"]
    expected = {
        Down: "witness.unreachable",
        Slow: "witness.timeout",
        Buggy: "witness.error",
        Liar: "witness.bad_cosignature",
    }[bad]
    signed = await log.checkpointer.publish()
    result = await log.witnessing.collect(signed)
    assert result.refused == {"witness-2": expected, "witness-3": expected}
    assert (tmp_path / f"{2:020d}.note").exists()  # the checkpoint itself is still stored


async def test_rogue_operator_rewrite_gets_no_cosignature(tmp_path: Path) -> None:
    # Beat 5: the operator keeps the log key, rebuilds the tree with a different history and
    # signs it. The witnesses remember what they cosigned and refuse.
    w = witnesses()
    honest = Log(tmp_path / "honest", w)
    honest.grow(leaves(3))
    await honest.publisher.publish()

    rogue = Log(tmp_path / "rogue", w)  # same witnesses, fresh storage, same log key
    rogue.grow(leaves(2) + leaves(3, tag=b"forged"))
    signed = await rogue.checkpointer.publish()
    result = await rogue.witnessing.collect(signed)
    assert result.cosigners == ()
    assert set(result.refused.values()) == {"witness.inconsistent"}
    for witness in w:
        latest = await witness.latest(ORIGIN)
        assert latest is not None
        assert latest.size == 3  # still the honest checkpoint


async def test_rogue_rollback_to_a_smaller_tree_is_refused(tmp_path: Path) -> None:
    w = witnesses()
    honest = Log(tmp_path / "honest", w)
    honest.grow(leaves(4))
    await honest.publisher.publish()
    rogue = Log(tmp_path / "rogue", w)
    rogue.grow(leaves(2))
    result = await rogue.witnessing.collect(await rogue.checkpointer.publish())
    assert result.cosigners == ()
    assert set(result.refused.values()) == {"witness.ahead"}


async def test_a_restarted_ledger_learns_each_witness_size_from_409(tmp_path: Path) -> None:
    w = witnesses()
    first = Log(tmp_path / "a", w)
    history = leaves(5)
    first.grow(history[:3])
    await first.publisher.publish()
    second = Log(tmp_path / "b", w)  # same history, no memory of witness sizes
    second.grow(history)
    result = await second.witnessing.collect(await second.checkpointer.publish())
    assert result.cosigners == NAMES


async def test_witnesses_that_already_cosigned_a_size_are_not_asked_again(tmp_path: Path) -> None:
    counting = [Counting(witness) for witness in witnesses()]
    log = Log(tmp_path, counting)
    log.grow(leaves(2))
    await log.publisher.publish()
    await log.publisher.publish()  # same size: nothing to ask
    assert [c.calls for c in counting] == [1, 1, 1]


async def test_a_tampered_stored_cosignature_is_never_served(tmp_path: Path) -> None:
    log = Log(tmp_path, witnesses())
    log.grow(leaves(2))
    signed = await log.publisher.publish()
    line = log.store.rows[2]["witness-1"]
    log.store.rows[2]["witness-1"] = line[:-6] + ("A" if line[-6] != "A" else "B") + line[-5:]
    with pytest.raises(LedgerError) as exc:
        await log.set.cosigned(signed.note, 2)
    assert exc.value.code == "cosignature.unverifiable"


def test_witness_set_validation() -> None:
    store = MemoryCosignatureStore()
    with pytest.raises(WitnessError):
        WitnessSet(KEYS, 0, store)
    with pytest.raises(WitnessError):
        WitnessSet(KEYS, 4, store)
    with pytest.raises(WitnessError):
        WitnessSet((KEYS[0], KEYS[0]), 1, store)
    with pytest.raises(WitnessError):
        Witnessing(LogTree(MemoryTreeStore()), WitnessSet(KEYS[:1], 1, store), [Down("other")])


async def test_first_stored_line_wins() -> None:
    store = MemoryCosignatureStore()
    await store.add(3, "witness-1", "first")
    await store.add(3, "witness-1", "second")
    assert await store.lines(3) == {"witness-1": "first"}
    assert await store.lines(4) == {}


# --- HTTP client ----------------------------------------------------------------------------


def http_client_for(witness: Witness) -> HttpWitnessClient:
    transport = httpx.ASGITransport(app=create_witness_app(witness))
    return HttpWitnessClient(witness.name, "http://w/", httpx.AsyncClient(transport=transport))


async def test_http_client_through_the_real_witness_app(tmp_path: Path) -> None:
    log = Log(tmp_path, [http_client_for(w) for w in witnesses()])
    log.grow(leaves(3))
    await log.publisher.publish()
    log.grow(leaves(3, tag=b"more"))
    await log.publisher.publish()
    assert sorted(await log.store.lines(6)) == list(NAMES)


async def test_http_client_maps_conflict_and_refusals(tmp_path: Path) -> None:
    witness = witnesses()[0]
    client = http_client_for(witness)
    log = Log(tmp_path, [])
    log.grow(leaves(3))
    signed = await log.checkpointer.publish()
    await client.add_checkpoint(0, [], signed.note)
    with pytest.raises(WitnessError) as exc:
        await client.add_checkpoint(0, [], signed.note)
    assert (exc.value.code, exc.value.latest_size) == ("witness.conflict", 3)
    with pytest.raises(WitnessError) as exc:
        await client.add_checkpoint(3, [bytes(32)], signed.note)
    assert exc.value.code == "witness.inconsistent"


def mock_client(handler: httpx.MockTransport) -> HttpWitnessClient:
    return HttpWitnessClient("witness-1", "http://w", httpx.AsyncClient(transport=handler))


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500, text="boom"), "witness.http_500"),
        (httpx.Response(403, json={"error": {"code": "internal; drop table"}}), "witness.http_403"),
        (httpx.Response(409, text="3\n"), "witness.bad_response"),  # wrong content type
        (
            httpx.Response(409, text="x\n", headers={"content-type": "text/x.tlog.size"}),
            "witness.bad_response",
        ),
        (httpx.Response(200, text="x" * 20_000), "witness.bad_response"),
    ],
)
async def test_http_client_distrusts_odd_responses(response: httpx.Response, code: str) -> None:
    client = mock_client(httpx.MockTransport(lambda request: response))
    with pytest.raises(WitnessError) as exc:
        await client.add_checkpoint(0, [], "note\n")
    assert exc.value.code == code


async def test_http_client_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WitnessError) as exc:
        await mock_client(httpx.MockTransport(refuse)).add_checkpoint(0, [], "note\n")
    assert exc.value.code == "witness.unreachable"
