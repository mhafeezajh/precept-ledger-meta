"""Checkpoint text (C2SP tlog-checkpoint) and signed notes (C2SP signed-note)."""

import base64
import json
from pathlib import Path

import pytest

from common.errors import FormatError, SignatureError
from ledger.checkpoint import (
    Checkpoint,
    NoteKey,
    open_checkpoint,
    open_note,
    parse_checkpoint,
    sign_checkpoint,
    sign_note,
)
from ledger.format import RECEIPT_SIG_CONTEXT, RECORD_SIG_CONTEXT
from merkle.hashing import EMPTY_ROOT
from tests.helpers import signer_for

VECTOR_DIR = Path(__file__).parents[1] / "vectors"
VECTORS = json.loads((VECTOR_DIR / "checkpoint-v1.json").read_text())
PUBLISHED = json.loads((VECTOR_DIR / "signed-note-published.json").read_text())
LOG = signer_for("log")
ORIGIN = "precept-ledger.local/harbor-federal/decisions"
LOG_KEY = NoteKey(ORIGIN, LOG.public_key_raw)
ROOT = bytes(range(32))


def checkpoint(size: int = 7, root: bytes = ROOT) -> Checkpoint:
    return Checkpoint(ORIGIN, size, root)


# --- checkpoint body ------------------------------------------------------------------------


def test_body_format() -> None:
    root_b64 = base64.b64encode(ROOT).decode()
    assert checkpoint().body() == f"{ORIGIN}\n7\n{root_b64}\n"
    with_ext = Checkpoint(ORIGIN, 0, EMPTY_ROOT, ("ext one",))
    assert with_ext.body().endswith("\next one\n")


def test_parse_round_trip() -> None:
    cp = Checkpoint(ORIGIN, 12345, ROOT, ("a", "b c"))
    assert parse_checkpoint(cp.body()) == cp


@pytest.mark.parametrize(
    "body",
    [
        "",
        f"{ORIGIN}\n7\n",  # no root
        f"{ORIGIN}\n7\n{base64.b64encode(ROOT).decode()}",  # no final newline
        f"\n7\n{base64.b64encode(ROOT).decode()}\n",  # empty origin
        f"{ORIGIN}\n07\n{base64.b64encode(ROOT).decode()}\n",  # leading zero
        f"{ORIGIN}\n-1\n{base64.b64encode(ROOT).decode()}\n",
        f"{ORIGIN}\n+7\n{base64.b64encode(ROOT).decode()}\n",
        f"{ORIGIN}\n7 \n{base64.b64encode(ROOT).decode()}\n",
        f"{ORIGIN}\n{2**63}\n{base64.b64encode(ROOT).decode()}\n",
        f"{ORIGIN}\n7\n{base64.b64encode(ROOT[:31]).decode()}\n",  # short root
        f"{ORIGIN}\n7\n{base64.b64encode(ROOT).decode().rstrip('=')}\n",  # non-canonical
        f"{ORIGIN}\n7\n{base64.b64encode(ROOT).decode()}\n\n",  # empty extension line
        f"{ORIGIN}\x07\n7\n{base64.b64encode(ROOT).decode()}\n",  # control character
        f"{ORIGIN}\n7\n{base64.b64encode(ROOT).decode()}\n\x85\n",  # C1 control character
    ],
)
def test_parse_rejects_malformed(body: str) -> None:
    with pytest.raises(FormatError) as err:
        parse_checkpoint(body)
    assert err.value.code == "checkpoint.malformed"


@pytest.mark.parametrize(
    "bad",
    [
        Checkpoint("", 1, ROOT),
        Checkpoint(ORIGIN, -1, ROOT),
        Checkpoint(ORIGIN, 1, ROOT[:31]),
        Checkpoint(ORIGIN, 1, ROOT, ("",)),
        Checkpoint("a\nb", 1, ROOT),
    ],
)
def test_body_refuses_to_render_invalid_checkpoints(bad: Checkpoint) -> None:
    with pytest.raises(FormatError):
        bad.body()


# --- note keys ------------------------------------------------------------------------------


def test_published_verifier_key_and_note() -> None:
    # Example published in the Go golang.org/x/mod/sumdb/note documentation (see D29).
    vec = PUBLISHED
    key = NoteKey.from_verifier(vec["verifier_key"])
    assert key.name == "PeterNeumann"
    assert key.key_hash.hex() == "c74f20a3"
    assert key.verifier() == vec["verifier_key"]
    text, names = open_note(vec["note"], [key])
    assert text == vec["text"]
    assert names == frozenset({"PeterNeumann"})


@pytest.mark.parametrize(
    "verifier",
    [
        "PeterNeumann+c74f20a3",
        "PeterNeumann+c74f20a4+ARpc2QcUPDhMQegwxbzhKqiBfsVkmqq/LDE4izWy10TW",  # wrong hash
        "PeterNeumann+c74f20a3+AhpczQcUPDhMQegwxbzhKqiBfsVkmqq/LDE4izWy10TW",  # wrong type
        "Peter Neumann+c74f20a3+ARpc2QcUPDhMQegwxbzhKqiBfsVkmqq/LDE4izWy10TW",
        "+c74f20a3+ARpc2QcUPDhMQegwxbzhKqiBfsVkmqq/LDE4izWy10TW",
        "PeterNeumann+C74F20A3+ARpc2QcUPDhMQegwxbzhKqiBfsVkmqq/LDE4izWy10TW",
    ],
)
def test_from_verifier_rejects_malformed(verifier: str) -> None:
    with pytest.raises(FormatError):
        NoteKey.from_verifier(verifier)


def test_note_key_rejects_bad_name_or_key() -> None:
    for name in ("", "a b", "a+b", "a\nb"):
        with pytest.raises(FormatError):
            NoteKey(name, LOG.public_key_raw)
    with pytest.raises(FormatError):
        NoteKey("ok", b"\x00" * 31)


# --- signing and opening --------------------------------------------------------------------


def test_sign_and_open_round_trip() -> None:
    text = "hello\nworld\n"
    note = sign_note(text, LOG, "pl-log")
    assert note.startswith(text + "\n— pl-log ")
    assert note.endswith("\n")
    opened, names = open_note(note, [NoteKey("pl-log", LOG.public_key_raw)])
    assert (opened, names) == (text, frozenset({"pl-log"}))


def test_our_checkpoint_vector() -> None:
    vec = VECTORS["checkpoint"]
    cp = Checkpoint(vec["origin"], vec["size"], base64.b64decode(vec["root_hash"]))
    assert cp.body() == vec["body"]
    assert sign_checkpoint(cp, LOG) == vec["note"]
    assert open_checkpoint(vec["note"], NoteKey.from_verifier(vec["verifier_key"])) == cp


def test_signatures_from_unknown_keys_are_ignored() -> None:
    rogue = signer_for("rogue")
    note = sign_note("t\n", LOG, "pl-log")
    extra = sign_note("t\n", rogue, "someone-else").split("\n\n", 1)[1]
    _, names = open_note(note + extra, [NoteKey("pl-log", LOG.public_key_raw)])
    assert names == frozenset({"pl-log"})


def test_no_known_signature_is_rejected() -> None:
    note = sign_note("t\n", signer_for("rogue"), "pl-log")
    with pytest.raises(SignatureError) as err:
        open_note(note, [NoteKey("pl-log", LOG.public_key_raw)])
    assert err.value.code == "note.unverified"


def test_bad_signature_from_a_known_key_is_rejected() -> None:
    # Same name and key hash as the trusted key, signature over different text.
    good = sign_note("t\n", LOG, "pl-log")
    forged = "u\n" + good[len("t\n") :]
    with pytest.raises(SignatureError) as err:
        open_note(forged, [NoteKey("pl-log", LOG.public_key_raw)])
    assert err.value.code == "note.bad_signature"


def test_duplicate_signature_from_a_known_key_is_rejected() -> None:
    note = sign_note("t\n", LOG, "pl-log")
    line = note.split("\n\n", 1)[1]
    with pytest.raises(FormatError):
        open_note(note + line, [NoteKey("pl-log", LOG.public_key_raw)])


@pytest.mark.parametrize(
    "note",
    [
        "t\n",  # no signature block
        "t\n\n",  # empty signature block
        "t\n\n— pl-log AAAA",  # no final newline
        "t\n\n- pl-log AAAAAAA=\n",  # hyphen instead of em dash
        "t\n\n— pl-log\n",  # no signature
        "t\n\n— pl-log AAAA\n",  # too short for key hash + signature
        "t\n\n— pl-log !!!!!!!!\n",  # not base64
        "t\n\n—  pl-log AAAAAAA=\n",  # empty name
        "t\x00\n\n— pl-log AAAAAAA=\n",  # control character in text
    ],
)
def test_open_rejects_malformed_notes(note: str) -> None:
    with pytest.raises(FormatError) as err:
        open_note(note, [NoteKey("pl-log", LOG.public_key_raw)])
    assert err.value.code == "note.malformed"


def test_sign_note_refuses_bad_text() -> None:
    for text in ("no newline", "a\n\nb\n", "nul\x00\n", ""):
        with pytest.raises(FormatError):
            sign_note(text, LOG, "pl-log")


def test_other_signed_payloads_can_never_be_note_text() -> None:
    # D28: the log key also signs receipts. Their signing inputs contain NUL, which note text
    # may not, so a receipt signature can never be presented as a checkpoint signature.
    assert b"\x00" in RECEIPT_SIG_CONTEXT
    assert b"\x00" in RECORD_SIG_CONTEXT
    with pytest.raises(FormatError):
        sign_note(RECEIPT_SIG_CONTEXT.decode() + "x\n", LOG, "pl-log")


# --- checkpoints as signed notes ------------------------------------------------------------


def test_open_checkpoint_requires_the_origin_to_match_the_key_name() -> None:
    other = Checkpoint("elsewhere.example/log", 1, ROOT)
    note = sign_note(other.body(), LOG, ORIGIN)  # signed under the right key name
    with pytest.raises(SignatureError) as err:
        open_checkpoint(note, LOG_KEY)
    assert err.value.code == "checkpoint.wrong_origin"


def test_open_checkpoint_rejects_another_signer() -> None:
    note = sign_checkpoint(checkpoint(), signer_for("rogue"))
    with pytest.raises(SignatureError):
        open_checkpoint(note, LOG_KEY)


def test_open_checkpoint_rejects_a_non_checkpoint_note() -> None:
    note = sign_note("just text\n", LOG, ORIGIN)
    with pytest.raises(FormatError):
        open_checkpoint(note, LOG_KEY)


def test_limits_and_encoding_edge_cases() -> None:
    key = NoteKey("pl-log", LOG.public_key_raw)
    root_b64 = base64.b64encode(ROOT).decode()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    # Same bytes, non-zero padding bits: decodes fine but is not the canonical encoding (D19).
    loose = root_b64[:-2] + alphabet[alphabet.index(root_b64[-2]) | 1] + "="
    with pytest.raises(FormatError):
        parse_checkpoint(f"{ORIGIN}\n7\n{loose}\n")
    with pytest.raises(FormatError):
        sign_note("lone surrogate \ud800\n", LOG, "pl-log")
    with pytest.raises(FormatError, match="too large"):
        open_note("x" * 70_000 + "\n" + sign_note("t\n", LOG, "pl-log"), [key])
    unknown = "— other AAAAAAA=\n" * 33
    with pytest.raises(FormatError, match="too many"):
        open_note(sign_note("t\n", LOG, "pl-log") + unknown, [key])
