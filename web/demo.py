"""The two attacks the demo shows, run only with ``DEMO_MODE=true`` (skill §7).

- Tamper (beat 4): a database administrator edits one recorded payment amount in place,
  through the clearly separate ``ADMIN_DATABASE_URL`` superuser connection with triggers
  switched off for that session. The app role cannot do this; the edit is real and permanent
  for the stream, which is why it needs a typed confirmation. The console then re-verifies
  the export and names the first broken record.
- Rogue operator (beat 5): ``scripts/demo_rogue_operator.py`` in a separate process, with
  throwaway keys; nothing touches the live ledger.
"""

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from web.backend import WebError

ROOT = Path(__file__).resolve().parent.parent
ROGUE_SCRIPT = ROOT / "scripts" / "demo_rogue_operator.py"
ROGUE_TIMEOUT_SECONDS = 60
# Multiply one recorded amount by ten, as a careless or dishonest admin might.
TAMPER_SQL = (
    "UPDATE ledger_record SET body = jsonb_set(body, '{resource,amount_cents}',"
    " to_jsonb((body #>> '{resource,amount_cents}')::bigint * 10))"
    " WHERE stream = %s AND seq = %s AND type = 'gate.decision'"
    " AND body #> '{resource,amount_cents}' IS NOT NULL"
)


async def tamper(admin_url: str, stream: str, seq: int) -> int:
    """Edit record ``seq``'s amount as the database superuser. Returns rows changed."""
    import psycopg  # only the demo button needs a database connection

    try:
        async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as conn:
            await conn.execute("SET session_replication_role = replica")  # skips the triggers
            cursor = await conn.execute(TAMPER_SQL, (stream, seq))
            return cursor.rowcount
    except psycopg.Error as exc:
        raise WebError(f"the admin connection failed ({type(exc).__name__})",
                       code="web.admin", status=503) from exc  # fmt: skip


@dataclass(frozen=True)
class RogueRun:
    exit_code: int
    lines: tuple[str, ...]

    @property
    def caught(self) -> bool:
        return self.exit_code == 0


async def rogue_operator(script: Path = ROGUE_SCRIPT) -> RogueRun:
    with tempfile.TemporaryDirectory(prefix="pl-rogue-") as out:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), out,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), ROGUE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            process.kill()
            raise WebError("the scenario took too long", code="web.timeout", status=504) from exc
    text = stdout.decode("utf-8", "replace")
    lines = tuple(line for line in text.splitlines()[1:] if line.strip())  # skip "files in"
    return RogueRun(process.returncode or 0, lines)
