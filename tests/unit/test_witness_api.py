"""Witness HTTP API (C2SP tlog-witness statuses), key loading and settings."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from common.errors import ConfigError
from ledger.checkpoint import NoteKey, open_checkpoint
from ledger.cosignature import add_cosignatures, format_add_checkpoint, open_cosignatures
from merkle.proofs import consistency_proof
from tests.helpers import clock, signer_for
from tests.unit.test_witness import LOG_KEY, ORIGIN, W1, W1_KEY, leaves, note_for
from witness.api import create_witness_app
from witness.core import Witness
from witness.keys import load_log_keys, load_signer
from witness.state import FileWitnessState, MemoryWitnessState

COSIGN = "/witness/v1/cosign"


@pytest.fixture
def witness() -> Witness:
    return Witness("witness-1", W1, [LOG_KEY], MemoryWitnessState(), clock(step_ms=1000))


@pytest.fixture
async def client(witness: Witness) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_witness_app(witness), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://witness") as http:
        yield http


async def post(http: httpx.AsyncClient, old: int, proof: list[bytes], note: str) -> httpx.Response:
    return await http.post(COSIGN, content=format_add_checkpoint(old, proof, note))


def code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def test_cosign_returns_a_signature_line(client: httpx.AsyncClient) -> None:
    history = leaves(3)
    response = await post(client, 0, [], note_for(history))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    note = add_cosignatures(note_for(history), [response.text])
    assert list(open_cosignatures(note, [W1_KEY])) == ["witness-1"]
    assert len(response.headers["x-trace-id"]) == 32


async def test_c2sp_path_is_served_too(client: httpx.AsyncClient) -> None:
    body = format_add_checkpoint(0, [], note_for(leaves(2)))
    response = await client.post("/witness/v1/add-checkpoint", content=body)
    assert response.status_code == 200


async def test_conflict_is_409_with_the_latest_size(client: httpx.AsyncClient) -> None:
    history = leaves(5)
    await post(client, 0, [], note_for(history[:3]))
    response = await post(client, 0, [], note_for(history))
    assert response.status_code == 409
    assert response.headers["content-type"] == "text/x.tlog.size"
    assert response.text == "3\n"
    retry = await post(client, 3, consistency_proof(3, history), note_for(history))
    assert retry.status_code == 200


async def test_refusal_statuses(client: httpx.AsyncClient) -> None:
    history = leaves(4)
    await post(client, 0, [], note_for(history[:2]))
    forged = leaves(4, tag=b"forged")
    cases = [
        (await post(client, 2, consistency_proof(2, forged), note_for(forged)), 422,
         "witness.inconsistent"),
        (await post(client, 2, [], note_for(history, signer=signer_for("rogue"))), 403,
         "witness.bad_signature"),
        (await post(client, 0, [], note_for(history, origin="elsewhere.example/x")), 404,
         "witness.unknown_log"),
        (await post(client, 9, [], note_for(history)), 400, "witness.bad_old_size"),
        (await client.post(COSIGN, content=b"old 0\n"), 400, "witness.bad_request"),
        (await client.post(COSIGN, content=b"x" * 80_000), 413, "format.too_large"),
    ]  # fmt: skip
    for response, status, expected in cases:
        assert (response.status_code, code(response)) == (status, expected), response.text


async def test_latest_serves_the_cosigned_note(client: httpx.AsyncClient) -> None:
    missing = await client.get("/witness/v1/latest")
    assert (missing.status_code, code(missing)) == (404, "witness.not_found")
    await post(client, 0, [], note_for(leaves(3)))
    response = await client.get("/witness/v1/latest", params={"origin": ORIGIN})
    assert response.status_code == 200
    assert open_checkpoint(response.text, LOG_KEY).size == 3
    assert list(open_cosignatures(response.text, [W1_KEY])) == ["witness-1"]
    unknown = await client.get("/witness/v1/latest", params={"origin": "nope"})
    assert (unknown.status_code, code(unknown)) == (404, "witness.unknown_log")


async def test_latest_needs_an_origin_when_several_logs_are_witnessed() -> None:
    other = NoteKey("elsewhere.example/x", W1.public_key_raw)
    witness = Witness("witness-1", W1, [LOG_KEY, other], MemoryWitnessState(), clock())
    transport = httpx.ASGITransport(app=create_witness_app(witness))
    async with httpx.AsyncClient(transport=transport, base_url="http://w") as http:
        response = await http.get("/witness/v1/latest")
    assert (response.status_code, code(response)) == (400, "request.origin_required")


async def test_health_reports_identity_and_fails_on_bad_state(tmp_path: Path) -> None:
    state = FileWitnessState(tmp_path)
    witness = Witness("witness-1", W1, [LOG_KEY], state, clock())
    transport = httpx.ASGITransport(app=create_witness_app(witness))
    async with httpx.AsyncClient(transport=transport, base_url="http://w") as http:
        healthy = await http.get("/witness/v1/health")
        assert healthy.status_code == 200
        assert healthy.json() == {
            "status": "ok",
            "name": "witness-1",
            "cosigner_key": W1_KEY.verifier(),
            "logs": [{"origin": ORIGIN, "size": None}],
        }
        await post(http, 0, [], note_for(leaves(2)))
        assert (await http.get("/witness/v1/health")).json()["logs"][0]["size"] == 2
        (path,) = (tmp_path / "state").iterdir()
        path.write_text("{}")
        failing = await http.get("/witness/v1/health")
    assert failing.status_code == 503
    assert failing.json()["status"] == "failing"


# --- keys -----------------------------------------------------------------------------------


def write_key(path: Path, mode: int = 0o600) -> None:
    path.write_bytes(W1.private_pem())
    path.chmod(mode)


def test_load_signer_reads_a_private_key(tmp_path: Path) -> None:
    write_key(tmp_path / "w.pem")
    signer = load_signer(tmp_path / "w.pem")
    assert signer.public_key_raw == W1.public_key_raw
    assert signer.sign(b"m") == W1.sign(b"m")
    assert "PRIVATE" not in repr(signer)


def test_load_signer_refuses_readable_or_missing_keys(tmp_path: Path) -> None:
    write_key(tmp_path / "open.pem", 0o644)
    with pytest.raises(ConfigError, match="readable"):
        load_signer(tmp_path / "open.pem")
    with pytest.raises(ConfigError):
        load_signer(tmp_path / "missing.pem")
    (tmp_path / "junk.pem").write_bytes(b"junk")
    os.chmod(tmp_path / "junk.pem", 0o600)
    with pytest.raises(ConfigError):
        load_signer(tmp_path / "junk.pem")


def test_load_log_keys(tmp_path: Path) -> None:
    path = tmp_path / "logs.txt"
    path.write_text(f"# logs this witness follows\n\n{LOG_KEY.verifier()}\n")
    assert load_log_keys(path) == [LOG_KEY]
    path.write_text("# nothing\n")
    with pytest.raises(ConfigError):
        load_log_keys(path)
    path.write_text("garbage\n")
    with pytest.raises(ConfigError):
        load_log_keys(path)
