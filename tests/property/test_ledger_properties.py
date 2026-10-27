"""Property tests (skill §6): canonicalization is stable; any single-byte edit fails at its seq."""

import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from common.errors import FormatError
from ledger.chain import verify_chain
from ledger.format import MAX_SAFE_INT, canonical_bytes, parse_canonical, parse_json_strict
from ledger.signing import KeyRing
from tests.helpers import STREAM, build_chain, clock, signer_for

GATE = signer_for("gate-1")
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])
CHAIN = build_chain(6, GATE, clock())
LINES = [c.record.canonical() for c in CHAIN]

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-MAX_SAFE_INT, max_value=MAX_SAFE_INT)
    | st.text(alphabet=st.characters(exclude_categories=("Cs",)))
)
json_values = st.recursive(
    json_scalars,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=20,
)


def shuffled(value: Any, rng: random.Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: shuffled(v, rng) for k, v in items}
    if isinstance(value, list):
        return [shuffled(v, rng) for v in value]
    return value


@given(json_values)
def test_canonical_round_trip_is_a_fixed_point(value: Any) -> None:
    data = canonical_bytes(value)
    assert parse_canonical(data) == value
    assert canonical_bytes(parse_json_strict(data)) == data


@given(json_values, st.randoms(use_true_random=False))
def test_canonical_bytes_ignore_key_order(value: Any, rng: random.Random) -> None:
    assert canonical_bytes(shuffled(value, rng)) == canonical_bytes(value)


def tagged(value: Any) -> Any:
    """Python says True == 1; JSON does not. Tag bools so equality matches JSON."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, dict):
        return {k: tagged(v) for k, v in value.items()}
    if isinstance(value, list):
        return [tagged(v) for v in value]
    return value


@given(json_values, json_values)
def test_equal_bytes_only_for_equal_values(a: Any, b: Any) -> None:
    assert (canonical_bytes(a) == canonical_bytes(b)) == (tagged(a) == tagged(b))


@settings(max_examples=500)
@given(
    index=st.integers(min_value=0, max_value=len(LINES) - 1),
    position=st.data(),
    new_byte=st.integers(min_value=0, max_value=255),
)
def test_any_single_byte_edit_fails_at_its_seq(
    index: int, position: st.DataObject, new_byte: int
) -> None:
    line = LINES[index]
    offset = position.draw(st.integers(min_value=0, max_value=len(line) - 1))
    if line[offset] == new_byte:
        return
    edited = list(LINES)
    edited[index] = line[:offset] + bytes([new_byte]) + line[offset + 1 :]
    report = verify_chain(edited, PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    assert report.first_break.seq == index + 1


@settings(max_examples=200)
@given(index=st.integers(min_value=0, max_value=len(LINES) - 1), data=st.data())
def test_deleting_or_inserting_a_byte_fails_at_its_seq(index: int, data: st.DataObject) -> None:
    line = LINES[index]
    offset = data.draw(st.integers(min_value=0, max_value=len(line) - 1))
    edited = list(LINES)
    if data.draw(st.booleans()):
        edited[index] = line[:offset] + line[offset + 1 :]
    else:
        edited[index] = line[:offset] + bytes([data.draw(st.integers(0, 255))]) + line[offset:]
    report = verify_chain(edited, PRODUCERS, stream=STREAM)
    assert report.first_break is not None
    assert report.first_break.seq == index + 1


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_floats_are_always_rejected(value: float) -> None:
    try:
        canonical_bytes({"amount": value})
    except FormatError:
        return
    raise AssertionError("float accepted")
