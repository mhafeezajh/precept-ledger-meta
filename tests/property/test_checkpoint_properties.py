"""Checkpoint properties: text round-trips, and any single-byte edit of a signed note fails."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from common.errors import FormatError, SignatureError
from ledger.checkpoint import (
    Checkpoint,
    NoteKey,
    open_checkpoint,
    parse_checkpoint,
    sign_checkpoint,
)
from tests.helpers import signer_for

LOG = signer_for("log")
origins = st.text(st.characters(min_codepoint=0x21, max_codepoint=0x7E, exclude_characters="+"),
                  min_size=1, max_size=40)  # fmt: skip
extensions = st.lists(
    st.text(
        st.characters(max_codepoint=0x2FF, exclude_categories=("Cc", "Cs")), min_size=1, max_size=20
    ),
    max_size=3,
)
checkpoints = st.builds(
    Checkpoint,
    origins,
    st.integers(0, 2**63 - 1),
    st.binary(min_size=32, max_size=32),
    extensions.map(tuple),
)


@given(checkpoints)
def test_body_round_trips(checkpoint: Checkpoint) -> None:
    assert parse_checkpoint(checkpoint.body()) == checkpoint


@settings(max_examples=300)
@given(checkpoints, st.data())
def test_any_single_byte_edit_of_a_signed_checkpoint_fails(
    checkpoint: Checkpoint, data: st.DataObject
) -> None:
    note = sign_checkpoint(checkpoint, LOG).encode("utf-8")
    at = data.draw(st.integers(0, len(note) - 1))
    value = data.draw(st.integers(0, 255).filter(lambda b: b != note[at]))
    edited = note[:at] + bytes([value]) + note[at + 1 :]
    try:
        text = edited.decode("utf-8")
    except UnicodeDecodeError:
        return  # not even text: rejected before any parsing
    with pytest.raises((FormatError, SignatureError)):
        open_checkpoint(text, NoteKey(checkpoint.origin, LOG.public_key_raw))
