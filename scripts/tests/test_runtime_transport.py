from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

import pytest

from scripts import runtime_transport


def _make_archive(tmp_path: Path, release_id: str,
                  extra_member: Callable[[tarfile.TarFile], None] | None = None) -> tuple[Path, bytes]:
    tar_path = tmp_path / "payload.tar"
    release_manifest = json.dumps({
        "schema_version": "novostroy-deploy-data/1",
        "profile": "full-compat",
        "file_count": 1,
        "bytes_total": 3,
    }, sort_keys=True).encode()
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        root = tarfile.TarInfo(f"{release_id}/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        for name, body in (("release-manifest.json", release_manifest), ("assets/test.txt", b"abc")):
            info = tarfile.TarInfo(f"{release_id}/{name}")
            info.size = len(body)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(body))
        if extra_member is not None:
            extra_member(tar)
    archive_path = tmp_path / f"{release_id}.tar.zst"
    with archive_path.open("wb") as output:
        subprocess.run(["zstd", "-q", "-3", "-c", str(tar_path)],
                       check=True, stdout=output)
    return archive_path, release_manifest


def _transport_manifest(archive: Path, release_id: str,
                        release_manifest: bytes) -> dict[str, object]:
    archive_sha, archive_bytes = runtime_transport._sha256_file(archive)
    uncompressed = subprocess.run(["zstd", "-q", "-d", "-c", str(archive)],
                                  check=True, stdout=subprocess.PIPE).stdout
    return {
        "schema_version": "novostroy-runtime-transport/1",
        "archive": {
            "name": archive.name,
            "format": "tar.zst",
            "sha256": archive_sha,
            "bytes": archive_bytes,
            "uncompressed_bytes": len(uncompressed),
        },
        "release": {
            "release_id": release_id,
            "root_name": release_id,
            "manifest_sha256": hashlib.sha256(release_manifest).hexdigest(),
            "profile": "full-compat",
            "file_count": 1,
            "bytes_total": 3,
        },
        "source": {
            "commit": "a" * 40,
            "bundle_sha256": "b" * 64,
            "bundle_bytes": 1,
            "bundle_name": "source.bundle",
        },
        "extract_layout": f"<release-root>/{release_id}/",
    }


def _write_manifest(path: Path, doc: dict[str, object]) -> None:
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")


def test_import_streams_checked_release_and_sets_readable_modes(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    release_id = "runtime-test-01"
    archive, release_manifest = _make_archive(tmp_path, release_id)
    sidecar = tmp_path / "transport.json"
    _write_manifest(sidecar, _transport_manifest(archive, release_id, release_manifest))
    release_root = tmp_path / "releases"
    release_root.mkdir()

    import deploy.apply_release

    monkeypatch.setattr(deploy.apply_release, "verify_release", lambda _path: {
        "profile": "full-compat", "file_count": 1, "bytes_total": 3,
    })
    target = runtime_transport.import_release(archive, sidecar, release_root)

    assert target == release_root / release_id
    assert (target / "assets/test.txt").read_bytes() == b"abc"
    assert (target / "assets/test.txt").stat().st_mode & 0o777 == 0o644
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("member_name", ["runtime-test-02/../outside", "/absolute/file"])
def test_import_rejects_unsafe_paths_without_publishing(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        member_name: str) -> None:
    release_id = "runtime-test-02"

    def add_bad_member(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(member_name)
        body = b"bad"
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))

    archive, release_manifest = _make_archive(tmp_path, release_id, add_bad_member)
    sidecar = tmp_path / "transport.json"
    _write_manifest(sidecar, _transport_manifest(archive, release_id, release_manifest))
    release_root = tmp_path / "releases"
    release_root.mkdir()

    with pytest.raises(runtime_transport.TransportError):
        runtime_transport.import_release(archive, sidecar, release_root)
    assert not (release_root / release_id).exists()
    assert not (tmp_path / "outside").exists()
    assert list(release_root.iterdir()) == []


def test_import_rejects_links_and_archive_hash_mismatch(tmp_path: Path) -> None:
    release_id = "runtime-test-link"

    def add_link(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(f"{release_id}/assets/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tar.addfile(info)

    archive, release_manifest = _make_archive(tmp_path, release_id, add_link)
    sidecar = tmp_path / "transport.json"
    doc = _transport_manifest(archive, release_id, release_manifest)
    _write_manifest(sidecar, doc)
    release_root = tmp_path / "releases"
    release_root.mkdir()
    with pytest.raises(runtime_transport.TransportError, match="Links and special"):
        runtime_transport.import_release(archive, sidecar, release_root)
    assert list(release_root.iterdir()) == []

    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(runtime_transport.TransportError, match="does not match"):
        runtime_transport.import_release(archive, sidecar, release_root)


def test_verifier_failure_removes_private_stage(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    release_id = "runtime-test-fail"
    archive, release_manifest = _make_archive(tmp_path, release_id)
    sidecar = tmp_path / "transport.json"
    _write_manifest(sidecar, _transport_manifest(archive, release_id, release_manifest))
    release_root = tmp_path / "releases"
    release_root.mkdir()

    import deploy.apply_release

    def fail(_path: Path) -> dict[str, object]:
        raise ValueError("release rejected")

    monkeypatch.setattr(deploy.apply_release, "verify_release", fail)
    with pytest.raises(ValueError, match="release rejected"):
        runtime_transport.import_release(archive, sidecar, release_root)
    assert list(release_root.iterdir()) == []
