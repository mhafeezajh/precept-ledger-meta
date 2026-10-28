"""Dependency direction (skill §2) and "code decides" (R2), checked on the source text."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ledger never depends on the layers above it, and never on a model client.
LEDGER_FORBIDDEN = {"gate", "policy", "assist", "agent", "web", "learn", "litellm", "openai"}
# Packages the offline verifier side may import from this repo (skill §2).
VERIFY_ALLOWED_INTERNAL = {
    "ledger.format",
    "ledger.canonical",
    "ledger.checkpoint",
    "ledger.cosignature",
    "merkle",
    "merkle.hashing",
    "merkle.proofs",
    "common.errors",
    "common.clock",
    "common.ids",
}
NETWORK_MODULES = {"socket", "http", "urllib", "requests", "httpx", "aiohttp", "ssl", "psycopg"}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def top(name: str) -> str:
    return name.split(".")[0]


@pytest.mark.parametrize("path", sorted((ROOT / "ledger").glob("*.py")), ids=lambda p: p.name)
def test_ledger_does_not_import_upper_layers_or_models(path: Path) -> None:
    assert not {top(m) for m in imports_of(path)} & LEDGER_FORBIDDEN


@pytest.mark.parametrize("module", ["format", "canonical", "checkpoint", "cosignature"])
def test_verifier_facing_ledger_modules_are_pure(module: str) -> None:
    # verify/ and witness/ import these; they must pull in no I/O or network code (D28).
    names = imports_of(ROOT / "ledger" / f"{module}.py")
    assert not {top(n) for n in names} & (NETWORK_MODULES | {"asyncio", "pathlib", "os"})
    internal = {n for n in names if top(n) in {"ledger", "common"}}
    assert internal <= VERIFY_ALLOWED_INTERNAL


@pytest.mark.parametrize("package", ["verify", "claims", "witness"], ids=lambda p: p)
def test_verifier_side_imports_only_allowed_ledger_modules(package: str) -> None:
    for path in (ROOT / package).rglob("*.py"):
        for name in imports_of(path):
            if top(name) == "ledger":
                assert name in VERIFY_ALLOWED_INTERNAL, f"{path}: {name}"
            if package == "verify":
                assert top(name) not in NETWORK_MODULES, f"{path}: {name}"


@pytest.mark.parametrize("path", sorted((ROOT / "merkle").glob("*.py")), ids=lambda p: p.name)
def test_merkle_knows_nothing_about_the_ledger_or_the_network(path: Path) -> None:
    # D25: merkle is a pure core plus a port; adapters for real storage live with their owner.
    names = imports_of(path)
    tops = {top(n) for n in names}
    assert not tops & (NETWORK_MODULES | LEDGER_FORBIDDEN | {"ledger"})
    assert {n for n in names if top(n) == "common"} <= {"common.errors"}
