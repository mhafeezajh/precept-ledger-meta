"""Ledger side of anchoring: RFC 3161 on each final checkpoint, hourly OpenTimestamps with
upgrades, failures, and the HTTP clients against fake servers."""

import hashlib
from datetime import timedelta

import httpx
import pytest
from asn1crypto import tsp

from anchor.clients import HttpCalendar, HttpTimestampAuthority
from anchor.ots import read_timestamp
from anchor.rfc3161 import anchor_digest, load_roots, verify_token
from common.errors import AnchorError, WitnessError
from ledger.anchoring import (
    CONFIRMED,
    PENDING,
    AnchoredPublisher,
    Anchorer,
    AnchorRow,
    MemoryAnchorStore,
)
from ledger.checkpoint import Checkpoint, sign_checkpoint
from ledger.checkpointer import SignedCheckpoint
from tests.helpers import clock, signer_for
from tests.tsa import FakeTSA
from tests.unit.test_ots import SHA256, append, bitcoin, pending

LOG = signer_for("log")
ORIGIN = "precept-ledger.local/harbor-federal/decisions"
FAKE = FakeTSA()
ROOTS = load_roots(FAKE.root_pem)
CAL = "https://alice.example"


def signed(size: int) -> SignedCheckpoint:
    checkpoint = Checkpoint(ORIGIN, size, hashlib.sha256(str(size).encode()).digest())
    return SignedCheckpoint(checkpoint, sign_checkpoint(checkpoint, LOG))


class LocalTSA:
    def __init__(self, name: str = "test-tsa", fail: bool = False) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    async def timestamp(self, digest: bytes) -> bytes:
        self.calls += 1
        if self.fail:
            raise AnchorError("down", code="anchor.unreachable")
        token = FAKE.token(digest)
        verify_token(token, digest, ROOTS)
        return token


class LocalCalendar:
    """Answers like a Bitcoin calendar: pending, then confirmed once ``confirm()`` is called."""

    def __init__(self, url: str = CAL) -> None:
        self.url, self.confirmed, self.submits, self.upgrades = url, False, 0, 0

    async def submit(self, digest: bytes) -> bytes:
        self.submits += 1
        return append(b"n") + SHA256 + pending(self.url)

    async def upgrade(self, commitment: bytes) -> bytes | None:
        self.upgrades += 1
        return append(b"tx") + SHA256 + bitcoin(912_345) if self.confirmed else None


def anchorer(
    store: MemoryAnchorStore,
    tsas: list[LocalTSA],
    calendars: list[LocalCalendar] | None = None,
    clk: object | None = None,
) -> Anchorer:
    calendars = calendars or []
    return Anchorer(
        store,
        tsas,
        calendars,
        {c.url: c for c in calendars},
        clock=clk or clock(step_ms=0),  # type: ignore[arg-type]
        ots_interval_seconds=3600,
    )


async def test_each_final_checkpoint_gets_a_verified_timestamp_once() -> None:
    store, tsa = MemoryAnchorStore(), LocalTSA()
    a = anchorer(store, [tsa])
    result = await a.anchor(signed(3))
    assert result.timestamped_by == ("test-tsa",)
    await a.anchor(signed(3))  # same size: not asked again
    assert tsa.calls == 1
    (row,) = await store.rows()
    assert (row.size, row.kind, row.status) == (3, "rfc3161", "verified")
    assert verify_token(row.token, anchor_digest(signed(3).checkpoint), ROOTS)


async def test_tsa_down_means_no_timestamp_and_the_publisher_reports_it() -> None:
    store = MemoryAnchorStore()
    a = anchorer(store, [LocalTSA(fail=True)])

    class Inner:
        async def publish(self) -> SignedCheckpoint:
            return signed(2)

    with pytest.raises(AnchorError) as exc:
        await AnchoredPublisher(Inner(), a).publish()
    assert exc.value.code == "anchor.no_timestamp"
    assert await store.rows() == []


async def test_one_of_two_tsas_is_enough() -> None:
    a = anchorer(MemoryAnchorStore(), [LocalTSA("a", fail=True), LocalTSA("b")])
    result = await a.anchor(signed(2))
    assert (result.timestamped_by, result.failures) == (("b",), {"a": "anchor.unreachable"})


async def test_checkpoints_without_quorum_are_not_anchored() -> None:
    tsa = LocalTSA()

    class NoQuorum:
        async def publish(self) -> SignedCheckpoint:
            raise WitnessError("no", code="witness.no_quorum")

    with pytest.raises(WitnessError):
        await AnchoredPublisher(NoQuorum(), anchorer(MemoryAnchorStore(), [tsa])).publish()
    assert tsa.calls == 0


async def test_ots_is_submitted_hourly_and_upgraded_when_confirmed() -> None:
    store, calendar, clk = MemoryAnchorStore(), LocalCalendar(), clock(step_ms=0)
    a = anchorer(store, [LocalTSA()], [calendar], clk)
    await a.anchor(signed(2))
    await a.anchor(signed(3))  # within the hour: no new submission
    assert calendar.submits == 1
    clk.current += timedelta(hours=1)
    await a.anchor(signed(4))  # due: submits size 4, tries to upgrade sizes 2 and 4
    # upgrades: 1 in the first tick (size 2, right after submitting) + 2 now
    assert (calendar.submits, calendar.upgrades) == (2, 3)
    calendar.confirmed = True
    clk.current += timedelta(hours=1)
    await a.anchor(signed(4))
    rows = [r for r in await store.rows() if r.kind == "ots"]
    statuses = sorted((r.size, r.status) for r in rows)
    assert statuses == [(2, CONFIRMED), (2, PENDING), (4, CONFIRMED), (4, PENDING)]
    confirmed = next(r for r in rows if r.status == CONFIRMED)
    (attestation,) = read_timestamp(confirmed.token, confirmed.digest)
    assert (attestation.kind, attestation.height) == ("bitcoin", 912_345)


async def test_upgrades_go_only_to_configured_calendars() -> None:
    store, calendar = MemoryAnchorStore(), LocalCalendar()
    elsewhere = LocalCalendar("https://evil.example")
    a = Anchorer(store, [], [elsewhere], {calendar.url: calendar}, clock=clock(step_ms=0),
                 ots_interval_seconds=0)  # fmt: skip
    await a.anchor(signed(2))  # pending row names https://evil.example
    await a.anchor(signed(2))
    assert elsewhere.upgrades == 0
    assert calendar.upgrades == 0


async def test_calendar_failures_are_reported_not_fatal() -> None:
    class Broken(LocalCalendar):
        async def submit(self, digest: bytes) -> bytes:
            raise AnchorError("down", code="anchor.unreachable")

    a = anchorer(MemoryAnchorStore(), [LocalTSA()], [Broken()])
    result = await a.anchor(signed(2))
    assert result.timestamped_by == ("test-tsa",)
    assert result.failures == {CAL: "anchor.unreachable"}


async def test_bad_upgrade_is_a_failure_not_a_confirmation() -> None:
    class Liar(LocalCalendar):
        async def upgrade(self, commitment: bytes) -> bytes | None:
            return b"\x99garbage"

    store, calendar = MemoryAnchorStore(), Liar()
    a = Anchorer(store, [], [calendar], {calendar.url: calendar}, clock=clock(step_ms=0),
                 ots_interval_seconds=0)  # fmt: skip
    await a.anchor(signed(2))
    result = await a.anchor(signed(2))
    assert result.failures == {CAL: "anchor.bad_ots"}
    assert all(r.status == PENDING for r in await store.rows())


async def test_first_row_wins() -> None:
    store = MemoryAnchorStore()
    await store.add(AnchorRow(1, "ots", CAL, PENDING, b"first", bytes(32)))
    await store.add(AnchorRow(1, "ots", CAL, PENDING, b"second", bytes(32)))
    assert [r.token for r in await store.rows()] == [b"first"]


# --- HTTP clients ---------------------------------------------------------------------------


def http(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_http_tsa_verifies_what_it_gets() -> None:
    digest = hashlib.sha256(b"d").digest()

    def answer(request: httpx.Request) -> httpx.Response:
        nonce = tsp.TimeStampReq.load(request.content)["nonce"].native
        assert request.headers["content-type"] == "application/timestamp-query"
        return httpx.Response(200, content=FAKE.response(digest, nonce=nonce).dump())

    token = await HttpTimestampAuthority("t", "https://tsa", http(answer), ROOTS).timestamp(digest)
    assert verify_token(token, digest, ROOTS)
    other_roots = load_roots(FakeTSA().root_pem)
    with pytest.raises(AnchorError) as exc:  # a token from a TSA we do not trust is refused
        await HttpTimestampAuthority("t", "https://tsa", http(answer), other_roots).timestamp(
            digest
        )
    assert exc.value.code == "anchor.untrusted"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500), "anchor.http_500"),
        (httpx.Response(200, content=b"x" * 70_000), "anchor.bad_response"),
        (httpx.Response(200, content=b"junk"), "anchor.bad_timestamp"),
    ],
)
async def test_http_tsa_errors(response: httpx.Response, code: str) -> None:
    client = HttpTimestampAuthority("t", "https://tsa", http(lambda r: response), ROOTS)
    with pytest.raises(AnchorError) as exc:
        await client.timestamp(bytes(32))
    assert exc.value.code == code


async def test_unreachable_servers() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no")

    with pytest.raises(AnchorError) as exc:
        await HttpTimestampAuthority("t", "https://tsa", http(refuse), ROOTS).timestamp(bytes(32))
    assert exc.value.code == "anchor.unreachable"
    with pytest.raises(AnchorError) as exc:
        await HttpCalendar(CAL, http(refuse)).submit(bytes(32))
    assert exc.value.code == "anchor.unreachable"


async def test_http_calendar_submit_and_upgrade() -> None:
    digest = bytes(32)
    proof = SHA256 + pending(CAL)
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            assert request.content == digest
            return httpx.Response(200, content=proof)
        if request.url.path.endswith("00"):
            return httpx.Response(404)
        return httpx.Response(200, content=bitcoin(1))

    calendar = HttpCalendar(CAL + "/", http(answer))
    assert await calendar.submit(digest) == proof
    assert await calendar.upgrade(bytes(32)) is None
    assert await calendar.upgrade(b"\x01" * 32) == bitcoin(1)
    assert seen[0] == "POST /digest"
    assert seen[1] == f"GET /timestamp/{'00' * 32}"


@pytest.mark.parametrize(
    ("response", "method", "code"),
    [
        (httpx.Response(503), "submit", "anchor.http_503"),
        (httpx.Response(200, content=SHA256 + bitcoin(1)), "submit", "anchor.bad_ots"),
        (httpx.Response(200, content=b"\x99"), "submit", "anchor.bad_ots"),
        (httpx.Response(500), "upgrade", "anchor.http_500"),
    ],
)
async def test_http_calendar_errors(response: httpx.Response, method: str, code: str) -> None:
    calendar = HttpCalendar(CAL, http(lambda r: response))
    with pytest.raises(AnchorError) as exc:
        await getattr(calendar, method)(bytes(32))
    assert exc.value.code == code
