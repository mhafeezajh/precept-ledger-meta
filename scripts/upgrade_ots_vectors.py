"""Try to upgrade the pending proofs in tests/vectors/ots-published.json (O5).

Bitcoin confirmation takes hours. Once a calendar has it, this writes the upgraded proof as
``confirmed_b64`` next to the pending one, and prints the block height and the merkle root
to compare with any Bitcoin block explorer or node. Calls only the calendars named in the
proofs, and only if they are in OTS_UPGRADE_CALENDARS' default list. Run: ``make ots-upgrade``.
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from anchor.clients import HttpCalendar  # noqa: E402  # path set up above for a plain script
from anchor.ots import read_timestamp, upgrade_timestamp  # noqa: E402
from common.config import Settings  # noqa: E402

VECTORS = ROOT / "tests" / "vectors" / "ots-published.json"


async def upgrade(data: dict[str, object]) -> int:
    allowed = Settings.model_fields["ots_upgrade_calendars"].default.split(",")
    digest = bytes.fromhex(str(data["digest_hex"]))
    confirmed = 0
    async with httpx.AsyncClient(timeout=20) as http:
        for entry in data["calendars"]:  # type: ignore[attr-defined]
            proof = base64.b64decode(entry.get("confirmed_b64") or entry["timestamp_b64"])
            for attestation in read_timestamp(proof, digest):
                if attestation.kind != "pending" or attestation.calendar not in allowed:
                    continue
                answer = await HttpCalendar(attestation.calendar, http).upgrade(
                    attestation.commitment
                )
                if answer is None:
                    print(f"{attestation.calendar}: still pending")
                    continue
                proof = upgrade_timestamp(proof, digest, attestation.commitment, answer)
            for attestation in read_timestamp(proof, digest):
                if attestation.kind == "bitcoin":
                    root = attestation.commitment[::-1].hex()
                    print(f"{entry['url']}: Bitcoin block {attestation.height}, merkle root {root}")
                    entry["confirmed_b64"] = base64.b64encode(proof).decode()
                    confirmed += 1
                    break
    return confirmed


def main() -> int:
    data = json.loads(VECTORS.read_text("utf-8"))
    if asyncio.run(upgrade(data)):
        VECTORS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {VECTORS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
