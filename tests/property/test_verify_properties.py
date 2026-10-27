"""Differential test (D32): pl-verify and ledger.chain are separate implementations of the same
chain rules. On any tampered record list they must name the same first seq and reason."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.chain import verify_chain
from ledger.signing import KeyRing
from tests.bundles import GATE, trusted_keys_json
from tests.helpers import STREAM, build_chain, clock, signer_for
from verify.bundle import Bundle
from verify.checks import verify_bundle
from verify.keys import TrustedKeys

LINES = [item.record.canonical() for item in build_chain(6, GATE, clock())]
ROGUE_LINE = build_chain(1, signer_for("rogue"), clock())[0].record.canonical()
PRODUCERS = KeyRing([("gate-1", GATE.public_key_raw)])
KEYS = TrustedKeys.from_json(trusted_keys_json())

tamper = st.one_of(
    st.tuples(st.just("flip"), st.integers(0, 5), st.integers(0, 10_000), st.integers(1, 255)),
    st.tuples(st.just("drop"), st.integers(0, 5)),
    st.tuples(st.just("dup"), st.integers(0, 5), st.integers(0, 6)),
    st.tuples(st.just("swap"), st.integers(0, 5), st.integers(0, 5)),
    st.tuples(st.just("rogue"), st.integers(0, 6)),
)


def apply(lines: list[bytes], op: tuple[Any, ...]) -> None:
    kind, i = op[0], op[1] % len(lines) if lines else 0
    if not lines:
        return
    if kind == "flip":
        line = bytearray(lines[i])
        line[op[2] % len(line)] ^= op[3]
        lines[i] = bytes(line)
    elif kind == "drop":
        lines.pop(i)
    elif kind == "dup":
        lines.insert(op[2] % (len(lines) + 1), lines[i])
    elif kind == "swap":
        j = op[2] % len(lines)
        lines[i], lines[j] = lines[j], lines[i]
    else:
        lines.insert(i, ROGUE_LINE)


@settings(max_examples=400)
@given(st.lists(tamper, max_size=3))
def test_verifier_agrees_with_the_ledger_on_the_first_break(ops: list[tuple[Any, ...]]) -> None:
    lines = list(LINES)
    for op in ops:
        apply(lines, op)
    ledger_report = verify_chain(lines, PRODUCERS, stream=STREAM)
    bundle = Bundle(STREAM, f"precept-ledger.local/{STREAM}", tuple(lines), (), ())
    walk_findings = [
        f for f in verify_bundle(bundle, KEYS).findings if f.check != "missing_receipt"
    ]
    if ledger_report.first_break is None:
        assert walk_findings == []
    else:
        found = walk_findings[0]
        assert (found.seq, found.check) == (
            ledger_report.first_break.seq,
            str(ledger_report.first_break.reason),
        )
