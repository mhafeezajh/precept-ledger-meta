"""Witness core: check-then-cosign (C2SP tlog-witness) over memory and file state."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from common.errors import WitnessError
from ledger.checkpoint import Checkpoint, NoteKey, open_checkpoint, sign_checkpoint
from ledger.cosignature import CosignerKey, add_cosignatures, open_cosignatures
from merkle.hashing import EMPTY_ROOT, leaf_hash
from merkle.proofs import consistency_proof, root_hash
from tests.helpers import clock, signer_for
from witness.core import Witness
from witness.state import FileWitnessState, MemoryWitnessState, WitnessState

LOG, ROGUE, W1 = signer_for("log"), signer_for("rogue"), signer_for("witness-1")
ORIGIN = "precept-ledger.local/harbor-federal/decisions"
LOG_KEY = NoteKey(ORIGIN, LOG.public_key_raw)
W1_KEY = CosignerKey("witness-1", W1.public_key_raw)


def leaves(count: int, tag: bytes = b"r") -> list[bytes]:
    return [leaf_hash(tag + str(i).encode()) for i in range(count)]


def note_for(items: list[bytes], signer: object = LOG, origin: str = ORIGIN) -> str:
    return sign_checkpoint(Checkpoint(origin, len(items), root_hash(items)), signer)  # type: ignore[arg-type]


def make_witness(state: WitnessState | None = None, step_s: int = 1) -> Witness:
    return Witness(
        "witness-1",
        W1,
        [LOG_KEY],
        state if state is not None else MemoryWitnessState(),
        clock(step_ms=step_s * 1000),
    )


async def cosign_through(witness: Witness, history: list[bytes], sizes: list[int]) -> list[str]:
    old, lines = 0, []
    for size in sizes:
        proof = consistency_proof(old, history[:size]) if old else []
        lines.append(await witness.add_checkpoint(old, proof, note_for(history[:size])))
        old = size
    return lines


def assert_refused(exc: pytest.ExceptionInfo[WitnessError], code: str) -> None:
    assert exc.value.code == code, exc.value


async def test_first_checkpoint_is_cosigned_and_remembered() -> None:
    witness, history = make_witness(), leaves(3)
    line = await witness.add_checkpoint(0, [], note_for(history))
    note = add_cosignatures(note_for(history), [line])
    assert list(open_cosignatures(note, [W1_KEY])) == ["witness-1"]
    latest = await witness.latest(ORIGIN)
    assert latest is not None
    assert (latest.size, latest.root) == (3, root_hash(history))


async def test_growth_with_a_valid_consistency_proof_is_cosigned() -> None:
    witness, history = make_witness(), leaves(7)
    await cosign_through(witness, history, [3, 5, 7])
    latest = await witness.latest(ORIGIN)
    assert latest is not None
    assert latest.size == 7


async def test_wrong_old_size_is_a_conflict_carrying_the_latest_size() -> None:
    witness, history = make_witness(), leaves(5)
    await cosign_through(witness, history, [3])
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(0, [], note_for(history))
    assert_refused(exc, "witness.conflict")
    assert exc.value.latest_size == 3


async def test_old_size_beyond_the_checkpoint_is_a_bad_request() -> None:
    witness, history = make_witness(), leaves(3)
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(4, [], note_for(history))
    assert_refused(exc, "witness.bad_old_size")


async def test_unknown_origin_is_refused() -> None:
    with pytest.raises(WitnessError) as exc:
        await make_witness().add_checkpoint(0, [], note_for(leaves(1), origin="other.example/log"))
    assert_refused(exc, "witness.unknown_log")


async def test_checkpoint_not_signed_by_the_log_key_is_refused() -> None:
    witness = make_witness()
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(0, [], note_for(leaves(3), signer=ROGUE))
    assert_refused(exc, "witness.bad_signature")
    assert await witness.latest(ORIGIN) is None


async def test_garbage_note_is_a_bad_request() -> None:
    with pytest.raises(WitnessError) as exc:
        await make_witness().add_checkpoint(0, [], "not a note\n")
    assert_refused(exc, "witness.bad_request")


async def test_rewritten_history_is_refused_and_kept_as_evidence() -> None:
    # The rogue operator beat: same log key, different history.
    state = MemoryWitnessState()
    witness, honest = make_witness(state), leaves(5)
    await cosign_through(witness, honest, [3])
    forged = honest[:2] + leaves(3, tag=b"forged")
    proof = consistency_proof(3, forged)  # a proof from the forged tree, not the cosigned one
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(3, proof, note_for(forged))
    assert_refused(exc, "witness.inconsistent")
    latest = await witness.latest(ORIGIN)
    assert latest is not None
    assert latest.size == 3  # state unchanged
    assert state.refusals == [(ORIGIN, note_for(forged), "witness.inconsistent")]


async def test_old_size_zero_with_a_proof_is_inconsistent() -> None:
    history = leaves(4)
    with pytest.raises(WitnessError) as exc:
        await make_witness().add_checkpoint(0, consistency_proof(2, history), note_for(history))
    assert_refused(exc, "witness.inconsistent")


async def test_same_size_must_have_the_same_root() -> None:
    witness, history = make_witness(), leaves(3)
    await cosign_through(witness, history, [3])
    with pytest.raises(WitnessError) as exc:  # split view: two roots for one size
        await witness.add_checkpoint(3, [], note_for(leaves(3, tag=b"other")))
    assert_refused(exc, "witness.inconsistent")
    again = await witness.add_checkpoint(3, [], note_for(history))  # same root: fine
    assert open_cosignatures(add_cosignatures(note_for(history), [again]), [W1_KEY])


async def test_empty_tree_must_have_the_empty_root() -> None:
    witness = make_witness()
    bad = sign_checkpoint(Checkpoint(ORIGIN, 0, bytes(32)), LOG)
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(0, [], bad)
    assert_refused(exc, "witness.inconsistent")
    good = sign_checkpoint(Checkpoint(ORIGIN, 0, EMPTY_ROOT), LOG)
    assert await witness.add_checkpoint(0, [], good)


async def test_rollback_to_a_smaller_tree_is_never_cosigned() -> None:
    witness, history = make_witness(), leaves(5)
    await cosign_through(witness, history, [3, 5])
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(3, [], note_for(history[:3]))
    assert_refused(exc, "witness.conflict")
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(5, [], note_for(history[:3]))
    assert_refused(exc, "witness.bad_old_size")


async def test_bad_proof_hashes_are_inconsistent() -> None:
    witness, history = make_witness(), leaves(5)
    await cosign_through(witness, history, [3])
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(3, [bytes(32)], note_for(history))
    assert_refused(exc, "witness.inconsistent")


async def test_timestamps_never_go_backwards() -> None:
    witness, history = make_witness(), leaves(5)
    first = await witness.add_checkpoint(0, [], note_for(history[:3]))
    witness._clock.current -= timedelta(days=1)  # type: ignore[attr-defined]
    second = await witness.add_checkpoint(3, consistency_proof(3, history), note_for(history))
    t1 = open_cosignatures(add_cosignatures(note_for(history[:3]), [first]), [W1_KEY])
    t2 = open_cosignatures(add_cosignatures(note_for(history), [second]), [W1_KEY])
    assert t2["witness-1"] >= t1["witness-1"]


async def test_concurrent_requests_from_one_old_size_cosign_once() -> None:
    witness, history = make_witness(), leaves(6)
    await cosign_through(witness, history, [2])
    results = await asyncio.gather(
        witness.add_checkpoint(2, consistency_proof(2, history[:4]), note_for(history[:4])),
        witness.add_checkpoint(2, consistency_proof(2, history), note_for(history)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, str) for r in results) == 1
    errors = [r for r in results if isinstance(r, WitnessError)]
    assert [e.code for e in errors] == ["witness.conflict"]


async def test_latest_note_carries_the_witness_cosignature() -> None:
    witness, history = make_witness(), leaves(3)
    assert await witness.latest_note(ORIGIN) is None
    await cosign_through(witness, history, [3])
    note = await witness.latest_note(ORIGIN)
    assert note is not None
    assert open_checkpoint(note, LOG_KEY).size == 3
    assert list(open_cosignatures(note, [W1_KEY])) == ["witness-1"]


async def test_tampered_state_fails_closed() -> None:
    state = MemoryWitnessState()
    witness, history = make_witness(state), leaves(5)
    await cosign_through(witness, history, [3])
    saved = state.saved[ORIGIN]
    state.saved[ORIGIN] = type(saved)(**{**vars(saved), "root": bytes(32)})
    with pytest.raises(WitnessError) as exc:
        await witness.add_checkpoint(3, consistency_proof(3, history), note_for(history))
    assert_refused(exc, "witness.state_invalid")


async def test_file_state_survives_a_restart(tmp_path: Path) -> None:
    history = leaves(5)
    first = make_witness(FileWitnessState(tmp_path))
    await cosign_through(first, history, [3])
    restarted = make_witness(FileWitnessState(tmp_path))
    with pytest.raises(WitnessError) as exc:  # it remembers size 3
        await restarted.add_checkpoint(0, [], note_for(history))
    assert exc.value.latest_size == 3
    await restarted.add_checkpoint(3, consistency_proof(3, history), note_for(history))
    files = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert len(files) == 1
    assert files[0].endswith(".json")
    saved = json.loads((tmp_path / "state" / files[0]).read_text())
    assert (saved["origin"], saved["size"]) == (ORIGIN, 5)


async def test_file_state_keeps_refused_checkpoints(tmp_path: Path) -> None:
    witness, honest = make_witness(FileWitnessState(tmp_path)), leaves(4)
    await cosign_through(witness, honest, [2])
    forged = leaves(4, tag=b"forged")
    with pytest.raises(WitnessError):
        await witness.add_checkpoint(2, consistency_proof(2, forged), note_for(forged))
    evidence = list((tmp_path / "refused").iterdir())
    assert len(evidence) == 1
    assert evidence[0].read_text() == note_for(forged)
    assert "inconsistent" in evidence[0].name


async def test_corrupt_state_file_fails_closed(tmp_path: Path) -> None:
    witness = make_witness(FileWitnessState(tmp_path))
    await cosign_through(witness, leaves(3), [3])
    (path,) = (tmp_path / "state").iterdir()
    path.write_text("{not json")
    with pytest.raises(WitnessError) as exc:
        await make_witness(FileWitnessState(tmp_path)).latest(ORIGIN)
    assert_refused(exc, "witness.state_invalid")


def test_witness_needs_at_least_one_log() -> None:
    with pytest.raises(WitnessError):
        Witness("witness-1", W1, [], MemoryWitnessState(), clock())
