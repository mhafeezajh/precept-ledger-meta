"""``pl-verify BUNDLE --keys KEYS.json [--trusted-checkpoint NOTE]
[--witness-keys WITNESSES.txt [--quorum N]] [--tsa-roots ROOTS.pem [--max-timestamp-lag S]]
[--json]``

Exit codes: 0 verified, 1 verification failed (findings printed), 2 could not verify (bad
input or usage). Offline: this package imports no networking code (import test), and a test
runs it with an audit hook that fails on any socket use.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from anchor.rfc3161 import load_roots
from common.errors import AnchorError, VerificationError
from ledger.format import b64encode
from verify.anchors import AnchorPolicy, AnchorReport
from verify.bundle import MAX_BUNDLE_BYTES, read_bundle
from verify.checks import Finding, Report, verify_bundle
from verify.keys import TrustedKeys, WitnessPolicy

MAX_SMALL_FILE_BYTES = 1024 * 1024


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise VerificationError(
            f"{path.name} is larger than {limit} bytes", code="verify.too_large"
        )
    return data


def _finding_line(finding: Finding) -> str:
    where = f"seq {finding.seq}" if finding.seq is not None else "-"
    record = f"  record {finding.record_id}" if finding.record_id else ""
    return f"  {where:<9} {finding.check}{record}: {finding.detail}"


def _anchor_lines(anchors: AnchorReport, records: int) -> list[str]:
    lines = []
    latest = anchors.latest
    if not anchors.checked:
        lines.append(
            f"timestamps   {anchors.tokens:>6}  RFC 3161 tokens, not checked (no --tsa-roots)"
        )
    elif latest is None:
        lines.append(f"timestamped  {records:>6}  records with no trusted timestamp")
    else:
        lines.append(
            f"timestamped  {records - latest.size:>6}  records after the latest timestamped "
            f"checkpoint; latest: size {latest.size} at {latest.gen_time.isoformat()}"
            f" by {latest.tsa}"
        )
    for claim in anchors.bitcoin:
        if claim.status == "bitcoin":
            lines.append(
                f"bitcoin           size {claim.size}: block {claim.height}, merkle root "
                f"{claim.commitment[::-1].hex()} (check against a Bitcoin node)"
            )
        else:
            lines.append(f"bitcoin           size {claim.size}: {claim.status} at {claim.calendar}")
    return lines


def render_text(report: Report) -> str:
    latest = report.latest
    lines = [
        f"pl-verify  stream {report.stream}  (origin {report.origin})",
        f"records      {report.records:>6}  chain and producer signatures verified through "
        f"seq {report.chain_verified_through}",
        f"receipts     {report.receipts:>6}",
        f"checkpoints  {report.checkpoints:>6}  "
        + (f"latest: size {latest.size}, root {b64encode(latest.root)}" if latest else "none"),
        f"unanchored   {report.unanchored:>6}  records after the latest checkpoint",
    ]
    if report.quorum is not None:
        final = report.final
        lines.append(
            f"witnessed    {report.unwitnessed:>6}  records after the latest final checkpoint; "
            + (
                f"final: size {final.size}, {len(report.final_cosigners)} of quorum"
                f" {report.quorum} ({', '.join(report.final_cosigners)})"
                if final
                else f"no checkpoint has {report.quorum} witness cosignatures"
            )
        )
    if report.anchors is not None:
        lines.extend(_anchor_lines(report.anchors, report.records))
    first = report.first_break
    if first is None:
        lines.append("RESULT  OK")
        return "\n".join(lines)
    where = f"seq {first.seq}" if first.seq is not None else "a checkpoint"
    named = f" (record {first.record_id})" if first.record_id else ""
    lines.append(f"RESULT  FAILED: first break at {where}{named}: {first.check}")
    ordered = sorted(report.findings, key=lambda f: (f.seq is None, f.seq or 0))
    lines.extend(_finding_line(f) for f in ordered)
    return "\n".join(lines)


def _anchors_json(anchors: AnchorReport) -> dict[str, object]:
    return {
        "checked": anchors.checked,
        "tokens": anchors.tokens,
        "timestamps": [
            {"size": t.size, "gen_time": t.gen_time.isoformat(), "tsa": t.tsa}
            for t in anchors.timestamps
        ],
        "bitcoin": [
            {
                "size": c.size,
                "status": c.status,
                "height": c.height,
                "calendar": c.calendar,
                "merkle_root": c.commitment[::-1].hex() if c.status == "bitcoin" else None,
            }
            for c in anchors.bitcoin
        ],
    }


def render_json(report: Report) -> str:
    def finding(f: Finding | None) -> dict[str, object] | None:
        return None if f is None else vars(f).copy()

    latest = report.latest
    return json.dumps(
        {
            "ok": report.ok,
            "stream": report.stream,
            "origin": report.origin,
            "records": report.records,
            "chain_verified_through": report.chain_verified_through,
            "receipts": report.receipts,
            "checkpoints": report.checkpoints,
            "latest": None
            if latest is None
            else {"size": latest.size, "root": b64encode(latest.root)},
            "unanchored": report.unanchored,
            "quorum": report.quorum,
            "final": None
            if report.final is None
            else {
                "size": report.final.size,
                "root": b64encode(report.final.root),
                "cosigners": list(report.final_cosigners),
            },
            "unwitnessed": report.unwitnessed,
            "anchors": None if report.anchors is None else _anchors_json(report.anchors),
            "first_break": finding(report.first_break),
            "findings": [finding(f) for f in report.findings],
        },
        indent=2,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pl-verify", description="Verify a Precept Ledger export offline."
    )
    parser.add_argument("bundle", type=Path, help="export bundle (.tar)")
    parser.add_argument("--keys", type=Path, required=True, help="trusted keys JSON")
    parser.add_argument(
        "--trusted-checkpoint", type=Path, help="a signed checkpoint you kept from earlier"
    )
    parser.add_argument(
        "--witness-keys", type=Path, help="cosigner verifier keys of trusted witnesses"
    )
    parser.add_argument(
        "--quorum", type=int, help="witness cosignatures a checkpoint needs (default 2)"
    )
    parser.add_argument("--tsa-roots", type=Path, help="PEM roots of trusted timestamp authorities")
    parser.add_argument(
        "--max-timestamp-lag",
        type=int,
        default=3600,
        help="seconds a record's ts may precede its first trusted timestamp (default 3600)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.quorum is not None and args.witness_keys is None:
        print("pl-verify: --quorum needs --witness-keys", file=sys.stderr)
        return 2
    try:
        bundle = read_bundle(_read(args.bundle, MAX_BUNDLE_BYTES))
        keys = TrustedKeys.from_json(_read(args.keys, MAX_SMALL_FILE_BYTES))
        trusted = None
        if args.trusted_checkpoint is not None:
            trusted = _read(args.trusted_checkpoint, MAX_SMALL_FILE_BYTES).decode("utf-8")
        witnesses = None
        if args.witness_keys is not None:
            quorum = 2 if args.quorum is None else args.quorum  # D5: 2 of 3
            witnesses = WitnessPolicy.from_text(
                _read(args.witness_keys, MAX_SMALL_FILE_BYTES), quorum
            )
        anchors = None
        if args.tsa_roots is not None:
            roots = load_roots(_read(args.tsa_roots, MAX_SMALL_FILE_BYTES))
            anchors = AnchorPolicy(tuple(roots), max_lag_seconds=args.max_timestamp_lag)
        report = verify_bundle(
            bundle, keys, trusted_checkpoint=trusted, witnesses=witnesses, anchors=anchors
        )
    except (VerificationError, AnchorError, OSError, UnicodeDecodeError) as exc:
        # Could not verify is never reported as verified (R4): its own exit code.
        print(f"pl-verify: cannot verify: {exc}", file=sys.stderr)
        return 2
    print(render_json(report) if args.json else render_text(report))
    return 0 if report.ok else 1
