"""Create local development keys in KEYS_DIR: gate-1.pem, log.pem and trusted.json.

Refuses to write inside the repository, never overwrites an existing key, writes private keys
with mode 0600 and prints only key ids. These are development keys, not production keys.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ledger.format import b64encode  # noqa: E402  # path set up above for a plain script
from ledger.signing import Signer  # noqa: E402

PRODUCERS = ("gate-1",)


def _write_private(path: Path, signer: Signer) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(signer.private_pem())


def ensure_keys(keys_dir: Path) -> dict[str, Signer]:
    keys_dir = keys_dir.resolve()
    if keys_dir == ROOT or ROOT in keys_dir.parents:
        raise SystemExit(f"refusing to write keys inside the repository: {keys_dir}")
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    signers = {}
    for name in (*PRODUCERS, "log"):
        path = keys_dir / f"{name}.pem"
        if not path.exists():
            _write_private(path, Signer.generate())
        signers[name] = Signer.from_pem_file(path)
    trusted = {
        "v": 1,
        "producers": {n: [b64encode(signers[n].public_key_raw)] for n in PRODUCERS},
        "log": [b64encode(signers["log"].public_key_raw)],
    }
    trusted_path = keys_dir / "trusted.json"
    if not trusted_path.exists():
        trusted_path.write_text(json.dumps(trusted, indent=2) + "\n")
    return signers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys_dir", nargs="?", default=os.environ.get("KEYS_DIR"))
    args = parser.parse_args(argv)
    if not args.keys_dir:
        parser.error("set KEYS_DIR or pass a directory")
    for name, signer in ensure_keys(Path(args.keys_dir)).items():
        print(f"{name:<8} {signer.key_id}")
    print(f"public keys: {Path(args.keys_dir) / 'trusted.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
