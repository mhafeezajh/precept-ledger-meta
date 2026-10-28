"""pl-verify inputs: the bundle is hostile until checked, and keys come only from the auditor."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from common.errors import VerificationError
from tests.bundles import demo_bundle, members, pack, trusted_keys_json
from verify import bundle as bundle_module
from verify.bundle import read_bundle
from verify.keys import TrustedKeys


def code_of(data: bytes) -> str:
    with pytest.raises(VerificationError) as err:
        read_bundle(data)
    return err.value.code


async def test_reads_a_real_bundle(tmp_path: Path) -> None:
    bundle = read_bundle(await demo_bundle(tmp_path))
    assert bundle.stream == "harbor-federal/decisions"
    assert (len(bundle.records), len(bundle.receipts), len(bundle.checkpoints)) == (5, 5, 2)
    assert all(not line.endswith(b"\n") for line in bundle.records)


def test_not_a_tar() -> None:
    assert code_of(b"definitely not a tar file" * 40) == "bundle.not_tar"


async def test_compressed_bundles_are_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        tar.addfile(info, io.BytesIO(b""))
    assert code_of(buffer.getvalue()) == "bundle.not_tar"


async def test_links_and_directories_are_refused(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    buffer = io.BytesIO(pack(files))
    with tarfile.open(fileobj=buffer, mode="a:") as tar:
        link = tarfile.TarInfo("checkpoints/link.note")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    assert code_of(buffer.getvalue()) == "bundle.bad_member"


@pytest.mark.parametrize(
    "name", ["../manifest.json", "/etc/passwd", "extra.txt", "checkpoints/x.txt", "checkpoints/"]
)
async def test_unexpected_member_names_are_refused(tmp_path: Path, name: str) -> None:
    files = members(await demo_bundle(tmp_path))
    files[name] = b"x"
    assert code_of(pack(files)) == "bundle.bad_member"


async def test_duplicate_members_are_refused(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    data = pack(files)
    buffer = io.BytesIO(data)
    with tarfile.open(fileobj=buffer, mode="a:") as tar:
        info = tarfile.TarInfo("records.jsonl")
        tar.addfile(info, io.BytesIO(b""))
    assert code_of(buffer.getvalue()) == "bundle.duplicate"


async def test_oversized_members_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle_module, "MAX_MEMBER_BYTES", 100)
    assert code_of(await demo_bundle(tmp_path)) == "bundle.too_large"


@pytest.mark.parametrize("missing", ["manifest.json", "records.jsonl", "receipts.jsonl"])
async def test_required_members(tmp_path: Path, missing: str) -> None:
    files = members(await demo_bundle(tmp_path))
    del files[missing]
    assert code_of(pack(files)) == "bundle.missing"


@pytest.mark.parametrize(
    "change",
    [
        {"format": "something-else"},
        {"v": 3},
        {"v": 1},  # v1 manifests have no anchors count
        {"stream": "Not A Stream"},
        {"origin": "precept-ledger.local/another/stream"},
        {"counts": {"records": 4, "receipts": 5, "checkpoints": 2}},
        {"extra": True},
    ],
)
async def test_bad_manifest(tmp_path: Path, change: dict[str, object]) -> None:
    files = members(await demo_bundle(tmp_path))
    manifest = {**json.loads(files["manifest.json"]), **change}
    files["manifest.json"] = json.dumps(manifest).encode()
    assert code_of(pack(files)) in {"bundle.bad_manifest", "bundle.count_mismatch"}


async def test_jsonl_must_end_with_a_newline(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    files["records.jsonl"] = files["records.jsonl"].rstrip(b"\n")
    assert code_of(pack(files)) == "bundle.bad_lines"


async def test_checkpoint_notes_must_be_utf8(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    files["checkpoints/00000000000000000003.note"] = b"\xff\xfe"
    assert code_of(pack(files)) == "bundle.bad_lines"


# --- trusted keys -----------------------------------------------------------------------------


def test_keys_file_round_trip() -> None:
    keys = TrustedKeys.from_json(trusted_keys_json())
    assert keys.producer_owners() == {"gate-1"}
    assert len(keys.log_keys) == 1


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        b'{"v": 1, "producers": {}}',
        b'{"v": 2, "producers": {}, "log": ["AAAA"]}',
        b'{"v": 1, "producers": {}, "log": []}',
        b'{"v": 1, "producers": {"gate-1": "notalist"}, "log": ["AAAA"]}',
        b'{"v": 1, "producers": {}, "log": ["AAAA"]}',
        b'{"v": 1, "producers": {}, "log": ["AAAA"], "extra": 1}',
        b'{"v": 1, "producers": {"gate-1": [7]}, "log": ["AAAA"]}',
    ],
)
def test_bad_keys_files(data: bytes) -> None:
    with pytest.raises(VerificationError) as err:
        TrustedKeys.from_json(data)
    assert err.value.code == "verify.bad_keys"


def test_one_key_cannot_belong_to_two_producers() -> None:
    gate = json.loads(trusted_keys_json())["producers"]["gate-1"]
    data = json.loads(trusted_keys_json())
    data["producers"]["gate-2"] = gate
    with pytest.raises(VerificationError):
        TrustedKeys.from_json(json.dumps(data).encode())


async def test_oversized_bundle_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_BYTES", 100)
    assert code_of(await demo_bundle(tmp_path)) == "bundle.too_large"


async def test_too_many_members_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_MEMBERS", 3)
    assert code_of(await demo_bundle(tmp_path)) == "bundle.too_large"


async def test_manifest_that_is_not_json(tmp_path: Path) -> None:
    files = members(await demo_bundle(tmp_path))
    files["manifest.json"] = b"{not json"
    assert code_of(pack(files)) == "bundle.bad_manifest"
