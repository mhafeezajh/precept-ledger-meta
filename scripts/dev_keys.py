"""Create local development keys in KEYS_DIR.

- ``gate-1.pem``, ``log.pem`` and ``trusted.json`` (producer and log public keys);
- ``witnesses/witness-{1,2,3}.pem``, one key per witness, and ``witnesses.txt``, their
  cosigner verifier keys (read by ``pl-ledger`` and ``pl-verify --witness-keys``);
- ``log-verifiers.txt``: the log's signed-note verifier key for the stream's origin, which
  the witnesses trust (``WITNESS_LOGS_FILE``);
- ``tokens/<name>.token``, one bearer token per Gate caller (the demo agent and two
  supervisors), and ``gate-callers.txt``, their hashes and roles, read by ``pl-gate``.

Refuses to write inside the repository, never overwrites an existing key, writes private keys
with mode 0600 and prints only key ids. These are development keys, not production keys.
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gate.auth import token_hash  # noqa: E402  # path set up above for a plain script
from ledger.checkpoint import NoteKey  # noqa: E402
from ledger.checkpointer import origin_for  # noqa: E402
from ledger.cosignature import CosignerKey  # noqa: E402
from ledger.format import b64encode  # noqa: E402
from ledger.signing import Signer  # noqa: E402

PRODUCERS = ("gate-1",)
WITNESSES = ("witness-1", "witness-2", "witness-3")
# Gate callers: (kind, name, roles). Two supervisors, so a loosening can get two approvals.
GATE_CALLERS = (
    ("agent", "payments-ops", "-"),
    ("approver", "j.ortiz", "payments-supervisor"),
    ("approver", "m.lee", "payments-supervisor"),
)


def _write_private(path: Path, signer: Signer) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(signer.private_pem())


def _ensure_signer(path: Path) -> Signer:
    if not path.exists():
        _write_private(path, Signer.generate())
    return Signer.from_pem_file(path)


def _write_public(path: Path, text: str) -> None:
    if not path.exists():
        path.write_text(text)


def _ensure_token(path: Path) -> str:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def ensure_gate_callers(keys_dir: Path) -> None:
    (keys_dir / "tokens").mkdir(mode=0o700, exist_ok=True)
    lines = ["# kind name roles sha256(token); tokens are in tokens/<name>.token\n"]
    for kind, name, roles in GATE_CALLERS:
        token = _ensure_token(keys_dir / "tokens" / f"{name}.token")
        lines.append(f"{kind} {name} {roles} {token_hash(token)}\n")
    _write_public(keys_dir / "gate-callers.txt", "".join(lines))


def ensure_keys(keys_dir: Path, origin: str | None = None) -> dict[str, Signer]:
    keys_dir = keys_dir.resolve()
    if keys_dir == ROOT or ROOT in keys_dir.parents:
        raise SystemExit(f"refusing to write keys inside the repository: {keys_dir}")
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    signers = {}
    for name in (*PRODUCERS, "log"):
        signers[name] = _ensure_signer(keys_dir / f"{name}.pem")
    # Separate files per witness; in a real deployment each lives on its own host.
    (keys_dir / "witnesses").mkdir(mode=0o700, exist_ok=True)
    for name in WITNESSES:
        signers[name] = _ensure_signer(keys_dir / "witnesses" / f"{name}.pem")
    trusted = {
        "v": 1,
        "producers": {n: [b64encode(signers[n].public_key_raw)] for n in PRODUCERS},
        "log": [b64encode(signers["log"].public_key_raw)],
    }
    _write_public(keys_dir / "trusted.json", json.dumps(trusted, indent=2) + "\n")
    cosigners = [CosignerKey(n, signers[n].public_key_raw).verifier() for n in WITNESSES]
    _write_public(keys_dir / "witnesses.txt", "".join(f"{v}\n" for v in cosigners))
    if origin is None:
        stream = os.environ.get("LEDGER_STREAM", "harbor-federal/decisions")
        origin = origin_for(os.environ.get("LEDGER_ORIGIN", "precept-ledger.local"), stream)
    log_verifier = NoteKey(origin, signers["log"].public_key_raw).verifier()
    _write_public(keys_dir / "log-verifiers.txt", f"{log_verifier}\n")
    ensure_gate_callers(keys_dir)
    return signers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys_dir", nargs="?", default=os.environ.get("KEYS_DIR"))
    args = parser.parse_args(argv)
    if not args.keys_dir:
        parser.error("set KEYS_DIR or pass a directory")
    for name, signer in ensure_keys(Path(args.keys_dir)).items():
        print(f"{name:<10} {signer.key_id}")
    print(f"public keys: {Path(args.keys_dir) / 'trusted.json'}")
    print(f"gate callers: {', '.join(name for _, name, _ in GATE_CALLERS)} (tokens/<name>.token)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
