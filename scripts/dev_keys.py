"""Create local development keys in KEYS_DIR.

- ``gate-1.pem``, ``log.pem`` and ``trusted.json`` (producer and log public keys);
- ``witnesses/witness-{1,2,3}.pem``, one key per witness, and ``witnesses.txt``, their
  cosigner verifier keys (read by ``pl-ledger`` and ``pl-verify --witness-keys``);
- ``log-verifiers.txt``: the log's signed-note verifier key for the stream's origin, which
  the witnesses trust (``WITNESS_LOGS_FILE``);
- ``principals/<principal>.pem`` and ``principals.json``: the key of each principal an agent
  acts for, which signs its delegation credentials (D88, D89), and the public keys Gate trusts;
- ``tokens/<name>.token``, one bearer token per Gate caller (the demo agent, two
  supervisors and a compliance officer), and ``gate-callers.txt``, their hashes and roles,
  read by ``pl-gate``.
- ``web-logins.txt``: who may sign in to the web apps, with their roles and a scrypt hash of
  their password (D96): the Gate approvers, an examiner and a demo admin; and
  ``web-passwords.txt`` (mode 0600), the plain demo passwords, for the person running the demo.

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
from web.logins import hash_password  # noqa: E402

PRODUCERS = ("gate-1", "assist-1")  # assist-1 signs assist.qa audit records
WITNESSES = ("witness-1", "witness-2", "witness-3")
PRINCIPALS = ("treasury.harbor-federal",)  # the team payments-ops acts for (D89)
# Gate callers: (kind, name, roles). Two compliance officers: a learned loosening needs two.
GATE_CALLERS = (
    ("agent", "payments-ops", "-"),
    ("approver", "j.ortiz", "payments-supervisor"),
    ("approver", "m.lee", "payments-supervisor"),
    ("approver", "a.chen", "compliance-officer"),  # reviews and approves policy rules
    ("approver", "r.patel", "compliance-officer"),  # the second approver a loosening needs
)

# Web sign-in (D96): (name, roles). Gate approvers keep Gate's roles; the last two have no token.
WEB_LOGINS = (
    *((name, roles) for kind, name, roles in GATE_CALLERS if kind == "approver"),
    ("examiner", "examiner"),  # reads and asks the record; never acts through Gate
    ("demo-admin", "demo-admin"),  # the Attacks page, in demo mode only
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


def _ensure_trusted(path: Path, signers: dict[str, Signer]) -> None:
    """Write trusted.json, or add producers missing from an existing one. Never removes or
    replaces a key: a key already listed stays exactly as it is."""
    if not path.exists():
        trusted = {
            "v": 1,
            "producers": {n: [b64encode(signers[n].public_key_raw)] for n in PRODUCERS},
            "log": [b64encode(signers["log"].public_key_raw)],
        }
        path.write_text(json.dumps(trusted, indent=2) + "\n")
        return
    trusted = json.loads(path.read_text())
    missing = [n for n in PRODUCERS if n not in trusted["producers"]]
    for name in missing:
        trusted["producers"][name] = [b64encode(signers[name].public_key_raw)]
        print(f"added producer {name} to {path.name}; restart pl-ledger to trust it")
    if missing:
        path.write_text(json.dumps(trusted, indent=2) + "\n")


def _ensure_token(path: Path) -> str:
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def ensure_gate_callers(keys_dir: Path) -> None:
    (keys_dir / "tokens").mkdir(mode=0o700, exist_ok=True)
    path = keys_dir / "gate-callers.txt"
    # Add callers missing from an existing file; never change or remove a line already there.
    text = (
        path.read_text()
        if path.exists()
        else ("# kind name roles sha256(token); tokens are in tokens/<name>.token\n")
    )
    listed = {line.split()[1] for line in text.splitlines() if line and not line.startswith("#")}
    for kind, name, roles in GATE_CALLERS:
        token = _ensure_token(keys_dir / "tokens" / f"{name}.token")
        if name not in listed:
            text += f"{kind} {name} {roles} {token_hash(token)}\n"
    path.write_text(text)


def _names(text: str) -> dict[str, str]:
    """The first two columns of each line: name to the rest of its first field."""
    return {line.split()[0]: line.split()[1] for line in text.splitlines()
            if line.strip() and not line.startswith("#")}  # fmt: skip


def ensure_web_logins(keys_dir: Path) -> None:
    """Add people missing from ``web-logins.txt``; never change a line already there. A
    password already in ``web-passwords.txt`` is reused, so the two files stay in step."""
    logins_path, passwords_path = keys_dir / "web-logins.txt", keys_dir / "web-passwords.txt"
    if not passwords_path.exists():
        fd = os.open(passwords_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write("# name password: demo sign-in passwords (D96). Development only.\n")
    logins = (
        logins_path.read_text()
        if logins_path.exists()
        else "# name roles scrypt-hash; the demo passwords are in web-passwords.txt\n"
    )
    listed, passwords = _names(logins), _names(passwords_path.read_text())
    for name, roles in WEB_LOGINS:
        if name in listed:
            continue
        password = passwords.get(name)
        if password is None:
            password = secrets.token_urlsafe(9)  # 12 characters, 72 bits
            with passwords_path.open("a") as file:
                file.write(f"{name} {password}\n")
        logins += f"{name} {roles} {hash_password(password)}\n"
    logins_path.write_text(logins)


def ensure_principals(keys_dir: Path) -> dict[str, Signer]:
    """Each principal's signing key, and ``principals.json`` with every public key Gate trusts.
    Adds missing principals; never replaces or removes a listed key."""
    (keys_dir / "principals").mkdir(mode=0o700, exist_ok=True)
    signers = {n: _ensure_signer(keys_dir / "principals" / f"{n}.pem") for n in PRINCIPALS}
    path = keys_dir / "principals.json"
    data = json.loads(path.read_text()) if path.exists() else {"v": 1, "principals": {}}
    for name, signer in signers.items():
        data["principals"].setdefault(name, [b64encode(signer.public_key_raw)])
    path.write_text(json.dumps(data, indent=2) + "\n")
    return signers


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
    _ensure_trusted(keys_dir / "trusted.json", signers)
    cosigners = [CosignerKey(n, signers[n].public_key_raw).verifier() for n in WITNESSES]
    _write_public(keys_dir / "witnesses.txt", "".join(f"{v}\n" for v in cosigners))
    if origin is None:
        stream = os.environ.get("LEDGER_STREAM", "harbor-federal/decisions")
        origin = origin_for(os.environ.get("LEDGER_ORIGIN", "precept-ledger.local"), stream)
    log_verifier = NoteKey(origin, signers["log"].public_key_raw).verifier()
    _write_public(keys_dir / "log-verifiers.txt", f"{log_verifier}\n")
    ensure_gate_callers(keys_dir)
    ensure_web_logins(keys_dir)
    signers.update({f"principal {n}": s for n, s in ensure_principals(keys_dir).items()})
    return signers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys_dir", nargs="?", default=os.environ.get("KEYS_DIR"))
    args = parser.parse_args(argv)
    if not args.keys_dir:
        parser.error("set KEYS_DIR or pass a directory")
    for name, signer in ensure_keys(Path(args.keys_dir)).items():
        print(f"{name:<10} {signer.key_id}")
    print(f"principals: {', '.join(PRINCIPALS)} (principals.json; delegate with make delegate)")
    print(f"public keys: {Path(args.keys_dir) / 'trusted.json'}")
    print(f"gate callers: {', '.join(name for _, name, _ in GATE_CALLERS)} (tokens/<name>.token)")
    print(f"web sign-in: {', '.join(name for name, _ in WEB_LOGINS)} "
          f"(passwords in {Path(args.keys_dir) / 'web-passwords.txt'})")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
