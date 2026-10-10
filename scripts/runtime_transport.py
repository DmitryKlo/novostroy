#!/usr/bin/env python3
"""Safely import a SHA-pinned full-compat runtime tar.zst release.

The transport archive is intentionally separate from the deployment release.
Import verifies the compressed bytes, accepts only the declared release root,
extracts regular files/directories into a private staging directory, runs the
deployment release verifier, and publishes the immutable directory atomically.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, BinaryIO


ROOT = Path(__file__).resolve().parents[1]
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MAX_ARCHIVE_BYTES = 32 * 1024**3
MAX_TAR_MEMBERS = 200_000
CHUNK_SIZE = 1024 * 1024


class TransportError(ValueError):
    """Raised when a transport manifest or archive is invalid."""


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_SIZE):
            size += len(block)
            if size > MAX_ARCHIVE_BYTES:
                raise TransportError("Archive exceeds the configured byte limit")
            digest.update(block)
    return digest.hexdigest(), size


def _load_transport_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TransportError("Transport manifest must be a regular file")
    if path.stat().st_size > 100_000:
        raise TransportError("Transport manifest exceeds the size limit")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransportError("Transport manifest is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "novostroy-runtime-transport/1":
        raise TransportError("Unsupported transport manifest schema")
    archive = document.get("archive")
    release = document.get("release")
    source = document.get("source")
    if not isinstance(archive, dict) or not isinstance(release, dict) or not isinstance(source, dict):
        raise TransportError("Transport manifest lacks archive, release, or source pins")
    archive_name = archive.get("name")
    if (not isinstance(archive_name, str) or Path(archive_name).name != archive_name
            or "\\" in archive_name):
        raise TransportError("Invalid archive basename")
    if (archive.get("format") != "tar.zst"
            or not isinstance(archive.get("sha256"), str)
            or HASH_RE.fullmatch(archive["sha256"]) is None
            or type(archive.get("bytes")) is not int
            or not 1 <= archive["bytes"] <= MAX_ARCHIVE_BYTES
            or type(archive.get("uncompressed_bytes")) is not int
            or not 1 <= archive["uncompressed_bytes"] <= MAX_ARCHIVE_BYTES):
        raise TransportError("Invalid archive integrity record")
    release_id = release.get("release_id")
    if (not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None
            or release.get("profile") != "full-compat"
            or release.get("root_name") != release_id
            or not isinstance(release.get("manifest_sha256"), str)
            or HASH_RE.fullmatch(release["manifest_sha256"]) is None
            or type(release.get("file_count")) is not int
            or not 1 <= release["file_count"] <= MAX_TAR_MEMBERS
            or type(release.get("bytes_total")) is not int
            or not 1 <= release["bytes_total"] <= MAX_ARCHIVE_BYTES):
        raise TransportError("Invalid release identity or integrity record")
    if (not isinstance(source.get("commit"), str)
            or COMMIT_RE.fullmatch(source["commit"]) is None
            or not isinstance(source.get("bundle_sha256"), str)
            or HASH_RE.fullmatch(source["bundle_sha256"]) is None
            or type(source.get("bundle_bytes")) is not int
            or not 1 <= source["bundle_bytes"] <= MAX_ARCHIVE_BYTES
            or not isinstance(source.get("bundle_name"), str)
            or Path(source["bundle_name"]).name != source["bundle_name"]
            or "\\" in source["bundle_name"]):
        raise TransportError("Invalid source bundle pin")
    if document.get("extract_layout") != f"<release-root>/{release_id}/":
        raise TransportError("Unexpected extraction layout")
    return document


def _safe_member_name(name: str, release_id: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        raise TransportError("Archive has an absolute or malformed member path")
    raw_parts = name.split("/")
    if raw_parts and raw_parts[-1] == "":
        raw_parts.pop()
    if (not raw_parts or any(part in {"", ".", ".."} for part in raw_parts)
            or raw_parts[0] != release_id):
        raise TransportError("Archive member is outside the declared release root")
    return tuple(raw_parts)


class _BoundedReader:
    """Count decompressed tar bytes and reject expansion beyond the pin."""

    def __init__(self, stream: BinaryIO, maximum: int):
        self.stream = stream
        self.maximum = maximum
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        block = self.stream.read(size)
        self.bytes_read += len(block)
        if self.bytes_read > self.maximum:
            raise TransportError("Decompressed archive exceeds its byte limit")
        return block


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("atomic no-replace directory rename is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _extract_archive(archive_path: Path, stage: Path, manifest: dict[str, Any]) -> int:
    archive_pin = manifest["archive"]
    release_pin = manifest["release"]
    release_id = release_pin["release_id"]
    zstd = shutil.which("zstd")
    if zstd is None:
        raise TransportError("zstd executable is required")
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c", "--", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    bounded = _BoundedReader(process.stdout, archive_pin["uncompressed_bytes"])
    seen: set[str] = set()
    regular_files = 0
    regular_bytes = 0
    try:
        with tarfile.open(fileobj=bounded, mode="r|") as tar:
            for member in tar:
                if len(seen) >= MAX_TAR_MEMBERS:
                    raise TransportError("Archive has too many entries")
                parts = _safe_member_name(member.name, release_id)
                key = "/".join(parts)
                if key in seen:
                    raise TransportError(f"Duplicate archive member: {key}")
                seen.add(key)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise TransportError(f"Links and special files are forbidden: {key}")
                if getattr(member, "sparse", None):
                    raise TransportError(f"Sparse archive members are forbidden: {key}")
                if not (member.isdir() or member.isreg()):
                    raise TransportError(f"Unsupported archive member type: {key}")
                if len(parts) == 1 and not member.isdir():
                    raise TransportError("The archive root must be a directory")
                relative = parts[1:]
                if not relative:
                    destination = stage
                else:
                    destination = stage.joinpath(*relative)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    destination.chmod(0o755)
                    continue
                if member.size < 0:
                    raise TransportError(f"Negative file size: {key}")
                regular_files += 1
                regular_bytes += member.size
                if (regular_files > release_pin["file_count"] + 1
                        or regular_bytes > release_pin["bytes_total"] + 100_000_000):
                    raise TransportError("Archive file inventory exceeds the release pin")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise TransportError(f"Could not read archive member: {key}")
                written = 0
                with destination.open("xb") as output:
                    while written < member.size:
                        block = source.read(min(CHUNK_SIZE, member.size - written))
                        if not block:
                            raise TransportError(f"Truncated archive member: {key}")
                        output.write(block)
                        written += len(block)
                    if source.read(1):
                        raise TransportError(f"Archive member exceeds declared size: {key}")
                destination.chmod(0o644)
        while bounded.read(CHUNK_SIZE):
            pass
        stderr = process.stderr.read() if process.stderr is not None else b""
        code = process.wait()
        if code != 0:
            raise TransportError(f"zstd decompression failed ({code}): {stderr[:500]!r}")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    if regular_files != release_pin["file_count"] + 1:
        raise TransportError("Archive file count differs from the release manifest pin")
    if regular_bytes < release_pin["bytes_total"]:
        raise TransportError("Archive payload is smaller than the release manifest pin")
    return bounded.bytes_read


def import_release(archive_path: Path, manifest_path: Path, release_root: Path) -> Path:
    """Verify and install one immutable runtime release from a local archive."""
    if archive_path.is_symlink() or not archive_path.is_file():
        raise TransportError("Archive must be an existing regular file, not a symlink")
    archive_path = archive_path.resolve(strict=True)
    manifest = _load_transport_manifest(manifest_path)
    archive_pin = manifest["archive"]
    digest, size = _sha256_file(archive_path)
    if (archive_path.name != archive_pin["name"] or size != archive_pin["bytes"]
            or digest != archive_pin["sha256"]):
        raise TransportError("Compressed archive does not match its transport manifest")
    if not release_root.is_absolute() or release_root.is_symlink() or not release_root.is_dir():
        raise TransportError("Release root must be an existing absolute real directory")
    release_root = release_root.resolve(strict=True)
    release_id = manifest["release"]["release_id"]
    target = release_root / release_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Release already exists: {target}")
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{release_id}-", dir=release_root))
    try:
        _extract_archive(archive_path, stage, manifest)
        release_manifest = stage / "release-manifest.json"
        if (not release_manifest.is_file() or release_manifest.is_symlink()
                or hashlib.sha256(release_manifest.read_bytes()).hexdigest()
                != manifest["release"]["manifest_sha256"]):
            raise TransportError("Extracted release manifest hash differs from its pin")
        sys.path.insert(0, str(ROOT))
        from deploy.apply_release import verify_release

        verified = verify_release(stage)
        if (verified.get("profile") != "full-compat"
                or verified.get("file_count") != manifest["release"]["file_count"]
                or verified.get("bytes_total") != manifest["release"]["bytes_total"]):
            raise TransportError("Deployment verifier result differs from transport pins")
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        stage.chmod(0o755)
        _rename_noreplace(stage, target)
        parent_fd = os.open(release_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return target
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import", help="verify and atomically install a runtime tar.zst")
    importer.add_argument("--archive", type=Path, required=True,
                          help="local archive path; stdin is intentionally unsupported")
    importer.add_argument("--manifest", type=Path, required=True,
                          help="sidecar transport manifest")
    importer.add_argument("--release-root", type=Path, required=True,
                          help="existing absolute release directory")
    args = parser.parse_args(argv)
    try:
        target = import_release(args.archive, args.manifest, args.release_root)
    except (TransportError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"runtime transport failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"release": str(target), "verified": True,
                      "release_manifest_sha256": hashlib.sha256(
                          (target / "release-manifest.json").read_bytes()).hexdigest()},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
