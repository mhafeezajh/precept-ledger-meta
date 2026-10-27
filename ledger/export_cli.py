"""``pl-export --out FILE [--stream STREAM]``: write a stream's export bundle from Postgres.

Uses ``DATABASE_URL`` (the app role; read-only use here) and ``LEDGER_ORIGIN`` from settings.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from common.config import load_settings
from common.errors import PreceptError
from ledger.checkpointer import origin_for
from ledger.export import export_bundle
from ledger.pg_store import PostgresStore


async def _export(url: str, stream: str, origin: str) -> bytes:
    store = await PostgresStore.open(url)
    try:
        return await export_bundle(store, store.checkpoint_sink(stream), stream, origin)
    finally:
        await store.close()


def main(argv: Sequence[str] | None = None) -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="pl-export", description="Export a ledger stream.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stream", default=settings.ledger_stream)
    args = parser.parse_args(argv)
    origin = origin_for(settings.ledger_origin, args.stream)
    try:
        bundle = asyncio.run(_export(settings.database_url, args.stream, origin))
    except PreceptError as exc:
        print(f"pl-export: {exc}", file=sys.stderr)
        return 2
    args.out.write_bytes(bundle)
    print(f"wrote {args.out} ({len(bundle)} bytes)")
    return 0
