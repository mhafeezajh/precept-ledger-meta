"""``pl-verify BUNDLE --keys KEYS.json [--trusted-checkpoint NOTE] [--json]``

Exit codes: 0 verified, 1 verification failed (findings printed), 2 could not verify (bad
input or usage). Offline: this package imports no networking code (import test), and a test
runs it with an audit hook that fails on any socket use.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from common.errors import VerificationError
from ledger.format import b64encode
from verify.bundle import MAX_BUNDLE_BYTES, read_bundle
from verify.checks import Finding, Report, verify_bundle
from verify.keys import TrustedKeys

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
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    try:
        bundle = read_bundle(_read(args.bundle, MAX_BUNDLE_BYTES))
        keys = TrustedKeys.from_json(_read(args.keys, MAX_SMALL_FILE_BYTES))
        trusted = None
        if args.trusted_checkpoint is not None:
            trusted = _read(args.trusted_checkpoint, MAX_SMALL_FILE_BYTES).decode("utf-8")
        report = verify_bundle(bundle, keys, trusted_checkpoint=trusted)
    except (VerificationError, OSError, UnicodeDecodeError) as exc:
        # Could not verify is never reported as verified (R4): its own exit code.
        print(f"pl-verify: cannot verify: {exc}", file=sys.stderr)
        return 2
    print(render_json(report) if args.json else render_text(report))
    return 0 if report.ok else 1
