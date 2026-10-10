"""Inventory and package private Novostroy data outside Git.

Bundles use content-addressed tar.zst chunks. The deployment subtree contains
only a checked deploy release; source captures and historical assets remain in
the evidence subtree and are never installed on the application VM.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "novostroy-agent-data-bundle/1"
INVENTORY_SCHEMA = "novostroy-agent-inventory/1"
DEFAULT_CHUNK_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_FILES = 300_000
MAX_BUNDLE_BYTES = 128 * 1024 * 1024 * 1024
MAX_BLOB_BYTES = 16 * 1024 * 1024 * 1024
MAX_TRAILING_ZERO_BYTES = 1024 * 1024
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
BUNDLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,99}\Z")
SECRET_NAME_PARTS = ("secret", "credential", "password", "passwd", "token", "cookie")
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx", ".gpg", ".asc"}
EXCLUDED_NAMES = {
    ".env", ".env.local", ".pgpass", "pgpass", "pg_service.conf",
    "credentials.json", "tokens.json", "secrets.json", "cookies.txt",
    "id_rsa", "id_ed25519", "authorized_keys",
}
EXCLUDED_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "browser-profile",
    "browser-profiles", "browser_profiles", "playwright-profile", ".cache",
}
PRESERVED_HISTORICAL_ASSET_FILES = frozenset({
    "assets/.citywide-v9-sample/manifest.json",
    "assets/.citywide-v9-sample/massing_region_752_1115.glb",
    "assets/.citywide-v9-sample-5m/manifest.json",
    "assets/.citywide-v9-sample-5m/massing_region_752_1115.glb",
})


class BundleError(ValueError):
    """Raised for invalid or unsafe inventory/bundle input."""


@dataclass(frozen=True)
class SourceFile:
    logical_path: str
    source: Path
    sha256: str
    size: int
    mtime_ns: int
    category: str
    role: str


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, body: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish a directory without replacing an existing target."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BundleError("Atomic no-replace directory rename is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    raise OSError(error, os.strerror(error), str(target))


def _safe_relative(value: str, *, allow_runtime_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BundleError("Invalid bundle path")
    if "\n" in value or "\r" in value:
        raise BundleError("Control character in bundle path")
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise BundleError("Unsafe bundle path")
    candidate_parts = parts[1:] if parts[0] == "evidence" else parts
    preserved_historical_asset = (
        allow_runtime_root
        and PurePosixPath(*candidate_parts).as_posix() in PRESERVED_HISTORICAL_ASSET_FILES
    )
    for index, part in enumerate(parts):
        allowed_runtime = allow_runtime_root and (
            (parts[0] == ".runtime" and index == 0)
            or (len(parts) > 1 and parts[:2] == ("evidence", ".runtime") and index == 1)
        )
        allowed_historical_asset = preserved_historical_asset and part in {
            ".citywide-v9-sample", ".citywide-v9-sample-5m"
        }
        if part.startswith(".") and not (allowed_runtime or allowed_historical_asset):
            raise BundleError("Hidden bundle path component")
    return path.as_posix()


def _excluded_path(path: Path, *, root_kind: str) -> str | None:
    parts = path.parts
    if any(part.lower() in EXCLUDED_DIRS for part in parts):
        return "rebuildable_or_browser_directory"
    name = path.name.lower()
    if name in EXCLUDED_NAMES or any(part in name for part in SECRET_NAME_PARTS):
        return "credential_or_secret_name"
    if path.suffix.lower() in SECRET_SUFFIXES:
        return "key_or_credential_file"
    if path.suffix.lower() in {".log", ".lock", ".tmp", ".swp", ".sqlite-wal", ".sqlite-shm"}:
        return "process_or_temporary_state"
    if name.endswith(".headers"):
        # Headers may contain Set-Cookie; capture manifests retain public metadata.
        return "response_headers_may_contain_cookies"
    if name.startswith("dev-process") or name in {"api.log", "web.log", "postgis-server.log"}:
        return "runtime_process_state"
    if root_kind == "assets" and path.suffix.lower() in {".cache", ".tmp"}:
        return "rebuildable_asset_cache"
    return None


def _category(relative: str) -> str:
    if relative.startswith(".runtime/research/developer-harvest/"):
        return "catalogs_units_and_geocode_ledgers"
    if relative.startswith(".runtime/research/fr-mos-"):
        return "fr_mos_registry_captures"
    if relative.startswith(".runtime/research/facades/") or relative.startswith("data/research/facades/"):
        return "source_photos_and_facade_research"
    if relative.startswith(".runtime/research/"):
        return "other_research_evidence"
    if relative.startswith("data/property/estateline/normalized/"):
        return "normalized_registry_units"
    if relative.startswith("data/property/developer_map/"):
        return "developer_building_registry"
    if relative.startswith("data/property/"):
        return "property_sources_and_ledgers"
    if relative.startswith("data/models/"):
        return "model_runs_and_active_pointers"
    if relative.startswith("data/city/"):
        return "city_geometry_sources"
    if relative.startswith("assets/"):
        return "active_and_historical_geometry_assets"
    if relative.startswith("data/social/"):
        return "nearby_and_social_sources"
    if relative.startswith("data/research/"):
        return "source_media_and_research"
    if relative.startswith("data/"):
        return "other_data"
    return "unknown"


def _hash_file(path: Path, *, expected: os.stat_result | None = None) -> tuple[str, int, int]:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise BundleError(f"Not a regular file: {path.name}")
    if expected is not None and (before.st_size, before.st_mtime_ns, before.st_ino) != (
        expected.st_size, expected.st_mtime_ns, expected.st_ino
    ):
        raise BundleError(f"Source changed before read: {path.name}")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ):
            raise BundleError(f"Source changed while opening: {path.name}")
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
        after_open = os.fstat(stream.fileno())
    after = path.stat(follow_symlinks=False)
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if signature(before) != signature(after_open) or signature(before) != signature(after):
        raise BundleError(f"Source changed during read: {path.name}")
    if size != before.st_size:
        raise BundleError(f"Source size changed during read: {path.name}")
    return digest.hexdigest(), size, before.st_mtime_ns


def _scan_roots(source_root: Path) -> tuple[list[SourceFile], dict[str, int]]:
    if source_root.is_symlink():
        raise BundleError("Source root cannot be a symlink")
    source_root = source_root.resolve(strict=True)
    files: list[SourceFile] = []
    excluded: Counter[str] = Counter()
    roots = (("data", "data"), ("assets", "assets"), (".runtime/research", ".runtime/research"))
    for rel_root, root_kind in roots:
        base = source_root / rel_root
        if base.is_symlink():
            raise BundleError(f"Source root cannot be a symlink: {rel_root}")
        if not base.exists():
            excluded["missing_root"] += 1
            continue
        if base.is_symlink() or not base.is_dir():
            raise BundleError(f"Source root must be a real directory: {rel_root}")
        stack = [base]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
            except OSError as exc:
                raise BundleError(f"Could not inventory {rel_root}: {exc.__class__.__name__}") from exc
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    excluded["symlink"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    reason = _excluded_path(path, root_kind=root_kind)
                    if reason:
                        excluded[reason] += 1
                    else:
                        stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    excluded["non_regular_file"] += 1
                    continue
                reason = _excluded_path(path, root_kind=root_kind)
                if reason:
                    excluded[reason] += 1
                    continue
                relative = path.relative_to(source_root).as_posix()
                try:
                    safe_relative = _safe_relative(relative, allow_runtime_root=True)
                except BundleError:
                    excluded["unsafe_path"] += 1
                    continue
                digest, size, mtime_ns = _hash_file(path)
                files.append(SourceFile(
                    logical_path=safe_relative,
                    source=path,
                    sha256=digest,
                    size=size,
                    mtime_ns=mtime_ns,
                    category=_category(safe_relative),
                    role="evidence",
                ))
    files.sort(key=lambda item: item.logical_path)
    if len(files) > MAX_FILES or sum(item.size for item in files) > MAX_BUNDLE_BYTES:
        raise BundleError("Selected source data exceeds the configured inventory limits")
    return files, dict(sorted(excluded.items()))


def _coverage(files: list[SourceFile], excluded: dict[str, int]) -> dict[str, Any]:
    categories: dict[str, dict[str, int]] = {}
    roots: dict[str, dict[str, int]] = {}
    by_hash: dict[str, int] = {}
    for item in files:
        categories.setdefault(item.category, {"files": 0, "bytes": 0})
        categories[item.category]["files"] += 1
        categories[item.category]["bytes"] += item.size
        logical = item.logical_path
        if logical.startswith("evidence/"):
            logical = logical.removeprefix("evidence/")
        elif logical.startswith("deployment/"):
            logical = "deployment"
        root = logical.split("/", 1)[0]
        roots.setdefault(root, {"files": 0, "bytes": 0})
        roots[root]["files"] += 1
        roots[root]["bytes"] += item.size
        by_hash[item.sha256] = item.size
    logical_bytes = sum(row["bytes"] for row in roots.values())
    unique_bytes = sum(by_hash.values())
    return {
        "schema_version": "novostroy-agent-data-coverage/1",
        "scope": ["data/", "assets/", ".runtime/research/"],
        "included_files": len(files),
        "included_logical_bytes": logical_bytes,
        "unique_content_blobs": len(by_hash),
        "unique_content_bytes": unique_bytes,
        "duplicate_content_bytes_avoided": logical_bytes - unique_bytes,
        "by_category": dict(sorted(categories.items())),
        "by_root": dict(sorted(roots.items())),
        "excluded_counts_by_reason": excluded,
        "rights_status": "preserved_as_recorded; unknown remains unknown",
        "source_capture_dates": "preserved in source manifests and raw records; not normalized by this exporter",
    }


def create_inventory(source_root: Path, output_root: Path) -> Path:
    files, excluded = _scan_roots(source_root)
    coverage = _coverage(files, excluded)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    records = [{
        "path": item.logical_path,
        "sha256": item.sha256,
        "bytes": item.size,
        "mtime_ns": item.mtime_ns,
        "category": item.category,
    } for item in files]
    inventory_hash = hashlib.sha256(_canonical(records)).hexdigest()[:12]
    inventory_id = now.replace(":", "").replace("-", "") + f"-{inventory_hash}"
    output_root.mkdir(parents=True, exist_ok=True)
    out = output_root / inventory_id
    if out.exists():
        raise FileExistsError(f"Inventory already exists: {out}")
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{inventory_id}-", dir=output_root))
    try:
        coverage_bytes = json.dumps(coverage, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        _atomic_write(stage / "coverage.json", coverage_bytes)
        manifest = {
            "schema_version": INVENTORY_SCHEMA,
            "inventory_id": inventory_id,
            "created_at": now,
            "source_root_label": "Novostroy checkout",
            "scope": ["data/", "assets/", ".runtime/research/"],
            "file_count": len(records),
            "bytes_total": sum(row["bytes"] for row in records),
            "coverage_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
            "files": records,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        _atomic_write(stage / "manifest.json", manifest_bytes)
        _atomic_write(stage / "manifest.sha256", (hashlib.sha256(manifest_bytes).hexdigest() + "  manifest.json\n").encode())
        _rename_directory_noreplace(stage, out)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return out


class _HashingReader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        value = self.stream.read(size)
        self.digest.update(value)
        self.bytes_read += len(value)
        return value


def _zstd_path(explicit: str | None = None) -> str:
    candidate = explicit or shutil.which("zstd")
    if not candidate or not Path(candidate).is_file():
        raise BundleError("zstd executable is required to create or read tar.zst bundles")
    return candidate


def _runtime_release_files(release_dir: Path) -> list[SourceFile]:
    sys.path.insert(0, str(ROOT))
    from deploy.apply_release import verify_release

    if release_dir.is_symlink():
        raise BundleError("Runtime release cannot be a symlink")
    release_dir = release_dir.resolve(strict=True)
    manifest = verify_release(release_dir)
    if manifest.get("profile") != "full-compat":
        raise BundleError("External updates require a full-compat data release")
    records: list[SourceFile] = []
    seen: set[str] = set()
    release_manifest_path = release_dir / "release-manifest.json"
    digest, size, mtime_ns = _hash_file(release_manifest_path)
    records.append(SourceFile("deployment/release-manifest.json", release_manifest_path,
                              digest, size, mtime_ns, "runtime_release_manifest", "deployment"))
    for row in manifest["files"]:
        relative = _safe_relative(row["path"])
        path = release_dir / relative
        digest, size, mtime_ns = _hash_file(path)
        if digest != row["sha256"] or size != row["bytes"]:
            raise BundleError(f"Inner release changed during bundle planning: {relative}")
        logical = _safe_relative(f"deployment/{relative}")
        if logical in seen:
            raise BundleError("Duplicate deployment file path")
        seen.add(logical)
        records.append(SourceFile(logical, path, digest, size, mtime_ns,
                                  f"runtime:{row['group']}", "deployment"))
    return records


def _write_chunk(
    output: Path,
    blobs: list[dict[str, Any]],
    source_by_hash: dict[str, Path],
    *,
    zstd: str,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists() or output.exists():
        raise FileExistsError(f"Chunk output exists: {output.name}")
    process = subprocess.Popen(
        [zstd, "-q", "-3", "-T1", "-o", str(temporary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        with tarfile.open(fileobj=process.stdin, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for blob in blobs:
                digest = blob["sha256"]
                source = source_by_hash[digest]
                before = source.stat(follow_symlinks=False)
                if (before.st_size, before.st_mtime_ns) != (blob["bytes"], blob["source_mtime_ns"]):
                    raise BundleError(f"Source changed before archive: {source.name}")
                info = tarfile.TarInfo(f"blobs/{digest}")
                info.size = blob["bytes"]
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with source.open("rb") as stream:
                    reader = _HashingReader(stream)
                    archive.addfile(info, reader)
                if reader.bytes_read != blob["bytes"] or reader.digest.hexdigest() != digest:
                    raise BundleError(f"Source changed during archive: {source.name}")
                after = source.stat(follow_symlinks=False)
                if (after.st_size, after.st_mtime_ns) != (blob["bytes"], blob["source_mtime_ns"]):
                    raise BundleError(f"Source changed during archive: {source.name}")
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        stderr = process.stderr.read() if process.stderr else b""
        code = process.wait()
        if code != 0:
            raise BundleError(f"zstd failed while creating chunk (exit {code})")
        if not temporary.is_file():
            raise BundleError("zstd did not produce a chunk")
        os.replace(temporary, output)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        temporary.unlink(missing_ok=True)
        raise
    digest, size, _ = _hash_file(output)
    return {
        "path": f"chunks/{output.name}",
        "sha256": digest,
        "bytes": size,
        "uncompressed_bytes": sum(512 + ((blob["bytes"] + 511) // 512) * 512 for blob in blobs) + 10240,
        "file_count": len(blobs),
        "role": "deployment" if any(blob["contains_deployment"] for blob in blobs) else "evidence",
        "blob_ids": [blob["sha256"] for blob in blobs],
    }


def create_bundle(
    *,
    source_root: Path,
    output_root: Path,
    bundle_id: str | None,
    release_dir: Path | None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    zstd_bin: str | None = None,
) -> Path:
    if chunk_bytes < 1 or chunk_bytes > 8 * 1024 * 1024 * 1024:
        raise BundleError("Chunk target must be between 1 byte and 8 GiB")
    zstd = _zstd_path(zstd_bin)
    if source_root.is_symlink():
        raise BundleError("Source root cannot be a symlink")
    source_root = source_root.resolve(strict=True)
    release_files = _runtime_release_files(release_dir) if release_dir else []
    evidence_files, excluded = _scan_roots(source_root)
    evidence_files = [
        SourceFile(
            _safe_relative(f"evidence/{item.logical_path}", allow_runtime_root=True),
            item.source, item.sha256, item.size, item.mtime_ns, item.category, "evidence"
        )
        for item in evidence_files
    ]
    logical_files = [*release_files, *evidence_files]
    if not logical_files:
        raise BundleError("No files selected for bundle")
    if len(logical_files) > MAX_FILES:
        raise BundleError("Bundle file count exceeds limit")
    if len({item.logical_path for item in logical_files}) != len(logical_files):
        raise BundleError("Duplicate logical path in bundle")
    if bundle_id is None:
        bundle_id = datetime.now(timezone.utc).strftime("data-%Y%m%dT%H%M%SZ")
    if BUNDLE_ID_RE.fullmatch(bundle_id) is None:
        raise BundleError("Invalid bundle ID")
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / bundle_id
    if final.exists() or final.is_symlink():
        raise FileExistsError(f"Bundle already exists: {final}")
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{bundle_id}-", dir=output_root))
    chunks_dir = stage / "chunks"
    chunks_dir.mkdir()
    try:
        by_hash: dict[str, dict[str, Any]] = {}
        source_by_hash: dict[str, Path] = {}
        file_rows: list[dict[str, Any]] = []
        for item in logical_files:
            row = {
                "path": item.logical_path,
                "sha256": item.sha256,
                "bytes": item.size,
                "source_mtime_ns": item.mtime_ns,
                "category": item.category,
                "role": item.role,
            }
            file_rows.append(row)
            blob = by_hash.get(item.sha256)
            if blob is None:
                by_hash[item.sha256] = {
                    "sha256": item.sha256,
                    "bytes": item.size,
                    "source_mtime_ns": item.mtime_ns,
                    "contains_deployment": item.role == "deployment",
                    "contains_evidence": item.role == "evidence",
                }
                source_by_hash[item.sha256] = item.source
            else:
                if blob["bytes"] != item.size:
                    raise BundleError("Same SHA256 has inconsistent sizes")
                blob["contains_deployment"] |= item.role == "deployment"
                blob["contains_evidence"] |= item.role == "evidence"
                if str(item.source) < str(source_by_hash[item.sha256]):
                    source_by_hash[item.sha256] = item.source
                    blob["source_mtime_ns"] = item.mtime_ns

        buckets: dict[str, list[dict[str, Any]]] = {"deployment": [], "evidence": []}
        for blob in sorted(by_hash.values(), key=lambda row: row["sha256"]):
            role = "deployment" if blob["contains_deployment"] else "evidence"
            buckets[role].append(blob)
        blob_rows: list[dict[str, Any]] = []
        chunk_rows: list[dict[str, Any]] = []
        for role in ("deployment", "evidence"):
            pending: list[dict[str, Any]] = []
            pending_bytes = 0
            sequence = 0
            for blob in buckets[role]:
                if pending and pending_bytes + blob["bytes"] > chunk_bytes:
                    sequence += 1
                    name = f"{role}-{sequence:05d}.tar.zst"
                    chunk = _write_chunk(chunks_dir / name, pending, source_by_hash, zstd=zstd)
                    chunk["chunk_id"] = name.removesuffix(".tar.zst")
                    chunk_rows.append(chunk)
                    for pending_blob in pending:
                        blob_rows.append({
                            "sha256": pending_blob["sha256"],
                            "bytes": pending_blob["bytes"],
                            "chunk_id": chunk["chunk_id"],
                            "role": role,
                        })
                    pending, pending_bytes = [], 0
                pending.append(blob)
                pending_bytes += blob["bytes"]
            if pending:
                sequence += 1
                name = f"{role}-{sequence:05d}.tar.zst"
                chunk = _write_chunk(chunks_dir / name, pending, source_by_hash, zstd=zstd)
                chunk["chunk_id"] = name.removesuffix(".tar.zst")
                chunk_rows.append(chunk)
                for pending_blob in pending:
                    blob_rows.append({
                        "sha256": pending_blob["sha256"],
                        "bytes": pending_blob["bytes"],
                        "chunk_id": chunk["chunk_id"],
                        "role": role,
                    })

        blob_lookup = {row["sha256"]: row for row in blob_rows}
        for row in file_rows:
            row["blob"] = row["sha256"]
            row["chunk_id"] = blob_lookup[row["sha256"]]["chunk_id"]
        file_rows.sort(key=lambda row: row["path"])
        blob_rows.sort(key=lambda row: row["sha256"])
        chunk_rows.sort(key=lambda row: row["chunk_id"])
        coverage = _coverage(logical_files, excluded)
        coverage.update({
            "bundle_id": bundle_id,
            "bundle_type": "deployment_and_evidence" if release_dir else "evidence_snapshot",
            "deployment_files": sum(item.role == "deployment" for item in logical_files),
            "evidence_files": sum(item.role == "evidence" for item in logical_files),
            "archive_unique_bytes": sum(row["bytes"] for row in blob_rows),
            "archive_chunks": len(chunk_rows),
            "all_historical_assets_included_as_evidence": True,
            "private_research_evidence_included": True,
            "excluded_paths_are_not_listed": True,
        })
        coverage_bytes = json.dumps(coverage, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        _atomic_write(stage / "coverage.json", coverage_bytes)
        release_manifest_record = next((row for row in file_rows if row["path"] == "deployment/release-manifest.json"), None)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        manifest = {
            "schema_version": SCHEMA,
            "bundle_id": bundle_id,
            "bundle_type": "deployment_and_evidence" if release_dir else "evidence_snapshot",
            "created_at": now,
            "runtime_profile": "full-compat" if release_dir else None,
        "zstd": {"level": 3, "threads": 1, "executable": Path(zstd).name,
                 "archive_format": "PAX tar with normalized uid/gid/mode/mtime"},
            "chunk_target_bytes": chunk_bytes,
            "coverage_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
            "release_manifest_sha256": release_manifest_record["sha256"] if release_manifest_record else None,
            "chunks": chunk_rows,
            "blobs": blob_rows,
            "files": file_rows,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BundleError("Outer bundle manifest exceeds size limit")
        _atomic_write(stage / "manifest.json", manifest_bytes)
        _atomic_write(stage / "manifest.sha256", (hashlib.sha256(manifest_bytes).hexdigest() + "  manifest.json\n").encode())
        stage.chmod(0o750)
        _rename_directory_noreplace(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return final


def _load_manifest(bundle_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if bundle_dir.is_symlink():
        raise BundleError("Bundle path must not be a symlink")
    if bundle_dir.is_symlink():
        raise BundleError("Bundle path must not be a symlink")
    bundle_dir = bundle_dir.resolve(strict=True)
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise BundleError("Bundle path must be a real directory")
    manifest_path = bundle_dir / "manifest.json"
    coverage_path = bundle_dir / "coverage.json"
    if manifest_path.is_symlink() or coverage_path.is_symlink():
        raise BundleError("Bundle metadata cannot be symlinks")
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BundleError("Bundle manifest is missing or oversized")
    manifest_bytes = manifest_path.read_bytes()
    sha_path = bundle_dir / "manifest.sha256"
    if not sha_path.is_file() or sha_path.is_symlink():
        raise BundleError("Manifest checksum sidecar is missing")
    checksum_parts = sha_path.read_text(encoding="ascii").strip().split()
    expected_manifest_hash = checksum_parts[0] if checksum_parts else ""
    if not HASH_RE.fullmatch(expected_manifest_hash) or hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_hash:
        raise BundleError("Outer manifest checksum mismatch")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA:
        raise BundleError("Unsupported bundle schema")
    bundle_id = manifest.get("bundle_id")
    if not isinstance(bundle_id, str) or BUNDLE_ID_RE.fullmatch(bundle_id) is None:
        raise BundleError("Invalid bundle ID")
    if manifest.get("bundle_type") not in {"deployment_and_evidence", "evidence_snapshot"}:
        raise BundleError("Invalid bundle type")
    file_table = manifest.get("files")
    blob_table = manifest.get("blobs")
    if not isinstance(file_table, list) or not isinstance(blob_table, list):
        raise BundleError("Bundle file tables are invalid")
    if len(file_table) > MAX_FILES or len(blob_table) > MAX_FILES:
        raise BundleError("Bundle inventory exceeds configured file count")
    if not coverage_path.is_file() or coverage_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BundleError("Coverage index is missing or oversized")
    coverage_bytes = coverage_path.read_bytes()
    if hashlib.sha256(coverage_bytes).hexdigest() != manifest.get("coverage_sha256"):
        raise BundleError("Coverage checksum mismatch")
    coverage = json.loads(coverage_bytes)
    if not isinstance(coverage, dict):
        raise BundleError("Coverage index is invalid")
    return manifest, coverage


def _validate_bundle_records(manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    files = manifest.get("files")
    blobs = manifest.get("blobs")
    chunks = manifest.get("chunks")
    if not isinstance(files, list) or not isinstance(blobs, list) or not isinstance(chunks, list):
        raise BundleError("Bundle inventory tables are invalid")
    if not 1 <= len(chunks) <= MAX_FILES:
        raise BundleError("Bundle chunk inventory is invalid")
    if not files:
        raise BundleError("Bundle inventory is empty")
    file_by_path: dict[str, dict[str, Any]] = {}
    file_bytes = 0
    for row in files:
        if not isinstance(row, dict) or set(row) != {
            "path", "sha256", "bytes", "source_mtime_ns", "category", "role", "blob", "chunk_id"
        }:
            raise BundleError("Invalid bundle file record")
        path = _safe_relative(row["path"], allow_runtime_root=True)
        if (path in file_by_path or not isinstance(row["sha256"], str)
                or not HASH_RE.fullmatch(row["sha256"])):
            raise BundleError("Duplicate path or invalid file hash")
        size = row["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_BLOB_BYTES:
            raise BundleError("Invalid bundle file size")
        if (row["role"] not in {"deployment", "evidence"} or not isinstance(row["category"], str)
                or not isinstance(row["source_mtime_ns"], int) or isinstance(row["source_mtime_ns"], bool)):
            raise BundleError("Invalid bundle file classification")
        if row["blob"] != row["sha256"] or not isinstance(row["chunk_id"], str):
            raise BundleError("Invalid content address")
        is_deployment = path.startswith("deployment/")
        is_evidence = path.startswith("evidence/")
        if (is_deployment != (row["role"] == "deployment") or is_evidence != (row["role"] == "evidence")):
            raise BundleError("Bundle role does not match path root")
        file_by_path[path] = row
        file_bytes += size
    if file_bytes > MAX_BUNDLE_BYTES:
        raise BundleError("Bundle logical size exceeds configured limit")
    blob_by_hash: dict[str, dict[str, Any]] = {}
    blob_bytes = 0
    chunk_ids: set[str] = set()
    compressed_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {
            "path", "sha256", "bytes", "uncompressed_bytes", "file_count", "role", "blob_ids", "chunk_id"
        }:
            raise BundleError("Invalid chunk record")
        if (not isinstance(chunk["chunk_id"], str) or not isinstance(chunk["path"], str)
                or chunk["chunk_id"] in chunk_ids):
            raise BundleError("Duplicate chunk ID")
        chunk_ids.add(chunk["chunk_id"])
        expected_path = f"chunks/{chunk['chunk_id']}.tar.zst"
        if _safe_relative(chunk["path"]) != expected_path:
            raise BundleError("Chunk path does not match its ID")
        if (not isinstance(chunk["sha256"], str) or not HASH_RE.fullmatch(chunk["sha256"])
                or not isinstance(chunk["bytes"], int) or isinstance(chunk["bytes"], bool)
                or not 1 <= chunk["bytes"] <= MAX_BUNDLE_BYTES
                or not isinstance(chunk["uncompressed_bytes"], int)
                or not 10240 <= chunk["uncompressed_bytes"] <= MAX_BUNDLE_BYTES
                or not isinstance(chunk["file_count"], int) or isinstance(chunk["file_count"], bool)):
            raise BundleError("Invalid chunk hash or size")
        compressed_bytes += chunk["bytes"]
        if compressed_bytes > MAX_BUNDLE_BYTES:
            raise BundleError("Compressed bundle exceeds configured size limit")
        if chunk["role"] not in {"deployment", "evidence"}:
            raise BundleError("Invalid chunk role")
        if not isinstance(chunk["blob_ids"], list) or len(chunk["blob_ids"]) != chunk["file_count"]:
            raise BundleError("Invalid chunk blob list")
        if any(not isinstance(blob_id, str) or not HASH_RE.fullmatch(blob_id) for blob_id in chunk["blob_ids"]):
            raise BundleError("Invalid chunk blob reference")
    for row in blobs:
        if not isinstance(row, dict) or set(row) != {"sha256", "bytes", "chunk_id", "role"}:
            raise BundleError("Invalid blob record")
        digest = row["sha256"]
        size = row["bytes"]
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest) or digest in blob_by_hash:
            raise BundleError("Duplicate or invalid blob hash")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_BLOB_BYTES:
            raise BundleError("Invalid blob size")
        if (not isinstance(row["chunk_id"], str) or row["chunk_id"] not in chunk_ids
                or row["role"] not in {"deployment", "evidence"}):
            raise BundleError("Invalid blob chunk assignment")
        blob_by_hash[digest] = row
        blob_bytes += size
    if blob_bytes > MAX_BUNDLE_BYTES:
        raise BundleError("Unique blob size exceeds configured limit")
    for path, row in file_by_path.items():
        blob = blob_by_hash.get(row["blob"])
        if not blob or blob["sha256"] != row["sha256"] or blob["bytes"] != row["bytes"] or blob["chunk_id"] != row["chunk_id"]:
            raise BundleError(f"File references missing or mismatched blob: {path}")
    for chunk in chunks:
        assigned = {digest for digest, blob in blob_by_hash.items() if blob["chunk_id"] == chunk["chunk_id"]}
        if set(chunk["blob_ids"]) != assigned or len(chunk["blob_ids"]) != len(assigned):
            raise BundleError("Chunk and blob inventories differ")
        if len(assigned) != chunk["file_count"]:
            raise BundleError("Chunk file count mismatch")
    if set(blob_by_hash) != {row["sha256"] for row in file_by_path.values()}:
        raise BundleError("Orphan or missing content blob")
    return file_by_path, blob_by_hash


def _verify_chunk(
    chunk_path: Path,
    chunk: dict[str, Any],
    expected_blobs: dict[str, dict[str, Any]],
    *,
    zstd: str,
    on_blob: Callable[[str, BinaryIO, int], None] | None = None,
) -> None:
    if chunk_path.is_symlink() or not chunk_path.is_file() or chunk_path.stat().st_size != chunk["bytes"]:
        raise BundleError(f"Missing or wrong-sized chunk: {chunk_path.name}")
    digest, size, _ = _hash_file(chunk_path)
    if digest != chunk["sha256"] or size != chunk["bytes"]:
        raise BundleError(f"Chunk checksum mismatch: {chunk_path.name}")
    process = subprocess.Popen(
        [zstd, "-q", "-d", "-c", "--", str(chunk_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    actual: set[str] = set()
    uncompressed = 0
    try:
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if not member.isfile() or member.issym() or member.islnk() or member.size < 0:
                    raise BundleError("Archive contains a non-regular file")
                expected = re.fullmatch(r"blobs/([0-9a-f]{64})", member.name)
                if not expected:
                    raise BundleError("Archive member path is unsafe")
                blob_id = expected.group(1)
                record = expected_blobs.get(blob_id)
                if record is None or blob_id in actual or member.size != record["bytes"]:
                    raise BundleError("Archive member differs from its blob inventory")
                source = archive.extractfile(member)
                if source is None:
                    raise BundleError("Archive member could not be read")
                actual.add(blob_id)
                uncompressed += member.size
                if uncompressed > MAX_BUNDLE_BYTES:
                    raise BundleError("Archive expansion exceeds configured limit")
                if on_blob:
                    on_blob(blob_id, source, member.size)
                else:
                    _consume_blob(source, blob_id, member.size)
        tail = process.stdout.read(MAX_TRAILING_ZERO_BYTES + 1)
        if len(tail) > MAX_TRAILING_ZERO_BYTES or any(byte != 0 for byte in tail):
            raise BundleError("Archive contains excessive data after the end marker")
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr else b""
        code = process.wait()
        if code != 0:
            raise BundleError(f"zstd decompression failed for {chunk_path.name}")
        expected_ids = set(expected_blobs)
        if actual != expected_ids:
            raise BundleError(f"Archive member list mismatch: {chunk_path.name}")
        if uncompressed > chunk["uncompressed_bytes"]:
            raise BundleError("Archive expansion exceeded its declared size")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise


def _consume_blob(stream: BinaryIO, expected_hash: str, expected_size: int) -> None:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        size += len(block)
        if size > expected_size:
            raise BundleError("Archive member exceeds its declared size")
        digest.update(block)
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise BundleError("Archive content does not match its SHA256 record")


def verify_bundle(bundle_dir: Path, *, zstd_bin: str | None = None) -> dict[str, Any]:
    zstd = _zstd_path(zstd_bin)
    if bundle_dir.is_symlink():
        raise BundleError("Bundle path must not be a symlink")
    bundle_dir = bundle_dir.resolve(strict=True)
    manifest, coverage = _load_manifest(bundle_dir)
    file_by_path, blob_by_hash = _validate_bundle_records(manifest)
    expected_paths = {"manifest.json", "manifest.sha256", "coverage.json"}
    expected_paths.update(chunk["path"] for chunk in manifest["chunks"])
    seen_paths: set[str] = set()
    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            raise BundleError("Bundle contains a symlink")
        relative = path.relative_to(bundle_dir).as_posix()
        if path.is_file():
            seen_paths.add(relative)
    if seen_paths != expected_paths:
        raise BundleError("Bundle contains missing or unlisted files")
    for chunk in manifest["chunks"]:
        expected = {blob_id: blob_by_hash[blob_id] for blob_id in chunk["blob_ids"]}
        _verify_chunk(bundle_dir / chunk["path"], chunk, expected, zstd=zstd)
    release_manifest = file_by_path.get("deployment/release-manifest.json")
    if manifest["bundle_type"] == "deployment_and_evidence":
        if not release_manifest or release_manifest["sha256"] != manifest.get("release_manifest_sha256"):
            raise BundleError("Outer and inner release manifest hashes differ")
        if manifest.get("runtime_profile") != "full-compat":
            raise BundleError("Runtime bundle must use full-compat profile")
    elif release_manifest or manifest.get("release_manifest_sha256") is not None:
        raise BundleError("Evidence-only snapshot must not claim a deploy release")
    return {
        "verified": True,
        "bundle_id": manifest["bundle_id"],
        "bundle_type": manifest["bundle_type"],
        "files": len(file_by_path),
        "blobs": len(blob_by_hash),
        "chunks": len(manifest["chunks"]),
        "logical_bytes": sum(row["bytes"] for row in file_by_path.values()),
        "unique_bytes": sum(row["bytes"] for row in blob_by_hash.values()),
        "coverage": coverage,
    }


def _write_verified_blob(target: Path, stream: BinaryIO, digest_expected: str, size_expected: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with target.open("xb") as output:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > size_expected:
                raise BundleError("Archive member exceeds its declared size")
            digest.update(block)
            output.write(block)
    if size != size_expected or digest.hexdigest() != digest_expected:
        target.unlink(missing_ok=True)
        raise BundleError("Archive content does not match its SHA256 record")
    target.chmod(0o600)


def import_deployment(
    bundle_dir: Path,
    release_root: Path,
    *,
    zstd_bin: str | None = None,
) -> Path:
    """Verify and atomically install only the deployment subtree.

    Evidence blobs are verified and discarded. The caller activates the
    resulting immutable release with deploy/apply_release.py, which owns health
    checks and rollback.
    """
    zstd = _zstd_path(zstd_bin)
    if bundle_dir.is_symlink():
        raise BundleError("Bundle path must not be a symlink")
    bundle_dir = bundle_dir.resolve(strict=True)
    manifest, _coverage_doc = _load_manifest(bundle_dir)
    if manifest.get("bundle_type") != "deployment_and_evidence":
        raise BundleError("Evidence-only snapshots cannot be imported as runtime releases")
    file_by_path, blob_by_hash = _validate_bundle_records(manifest)
    if manifest.get("runtime_profile") != "full-compat":
        raise BundleError("Only full-compat runtime bundles can be imported")
    if release_root.is_symlink():
        raise BundleError("Release root must not be a symlink")
    release_root = release_root.resolve(strict=True)
    if release_root.is_symlink() or not release_root.is_dir():
        raise BundleError("Release root must be an existing real directory")
    bundle_id = manifest["bundle_id"]
    target = release_root / bundle_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Release already exists: {target}")
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{bundle_id}-", dir=release_root))
    objects = Path(tempfile.mkdtemp(prefix=f".objects-{bundle_id}-", dir=release_root))
    try:
        expected_paths = {"manifest.json", "manifest.sha256", "coverage.json"}
        expected_paths.update(chunk["path"] for chunk in manifest["chunks"])
        seen_paths: set[str] = set()
        for path in bundle_dir.rglob("*"):
            if path.is_symlink():
                raise BundleError("Bundle contains a symlink")
            if path.is_file():
                seen_paths.add(path.relative_to(bundle_dir).as_posix())
        if seen_paths != expected_paths:
            raise BundleError("Bundle contains missing or unlisted files")
        deployment_blobs = {
            row["blob"] for row in file_by_path.values() if row["role"] == "deployment"
        }
        for chunk in manifest["chunks"]:
            expected = {blob_id: blob_by_hash[blob_id] for blob_id in chunk["blob_ids"]}

            def maybe_store(blob_id: str, stream: BinaryIO, size: int) -> None:
                if blob_id in deployment_blobs:
                    _write_verified_blob(objects / blob_id, stream, blob_id, size)
                else:
                    _consume_blob(stream, blob_id, size)

            _verify_chunk(bundle_dir / chunk["path"], chunk, expected, zstd=zstd, on_blob=maybe_store)
        for logical, row in sorted(file_by_path.items()):
            if row["role"] != "deployment":
                continue
            relative = logical.removeprefix("deployment/")
            if relative != "release-manifest.json" and not (
                relative.startswith("data/") or relative.startswith("assets/")
            ):
                raise BundleError("Deployment path is outside data/assets")
            safe_relative = relative if relative == "release-manifest.json" else _safe_relative(relative)
            source = objects / row["blob"]
            if not source.is_file() or source.is_symlink():
                raise BundleError("Verified deployment blob is missing")
            destination = stage / safe_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)
            destination.chmod(0o644)
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        objects_names = {path.name for path in objects.iterdir() if path.is_file()}
        required = {row["blob"] for row in file_by_path.values() if row["role"] == "deployment"}
        if objects_names != required:
            raise BundleError("Extracted deployment object set differs from manifest")
        shutil.rmtree(objects)
        sys.path.insert(0, str(ROOT))
        from deploy.apply_release import verify_release

        inner = verify_release(stage)
        if inner.get("profile") != "full-compat":
            raise BundleError("Imported inner release is not full-compat")
        stage.chmod(0o750)
        _rename_directory_noreplace(stage, target)
        return target
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(objects, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inventory_parser = sub.add_parser("inventory", help="hash selected data/assets/research without copying")
    inventory_parser.add_argument("--source-root", type=Path, default=ROOT)
    inventory_parser.add_argument("--output-root", type=Path, default=ROOT / ".runtime/exports/inventories")
    pack_parser = sub.add_parser("pack", help="write immutable content-addressed tar.zst chunks")
    pack_parser.add_argument("--source-root", type=Path, default=ROOT)
    pack_parser.add_argument("--release-dir", type=Path, help="checked full-compat runtime release; omit for evidence snapshot")
    pack_parser.add_argument("--output-root", type=Path, default=ROOT / ".runtime/exports")
    pack_parser.add_argument("--bundle-id")
    pack_parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    pack_parser.add_argument("--zstd-bin")
    verify_parser = sub.add_parser("verify", help="verify outer manifests, chunk SHA and every blob")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.add_argument("--zstd-bin")
    import_parser = sub.add_parser("import", help="install verified deployment into an immutable release directory")
    import_parser.add_argument("--bundle-dir", type=Path, required=True)
    import_parser.add_argument("--release-root", type=Path, required=True)
    import_parser.add_argument("--zstd-bin")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            output = create_inventory(args.source_root, args.output_root)
            print(json.dumps({"inventory": str(output), "files": json.loads((output / "manifest.json").read_text())["file_count"]}))
        elif args.command == "pack":
            output = create_bundle(source_root=args.source_root, output_root=args.output_root,
                                   bundle_id=args.bundle_id, release_dir=args.release_dir,
                                   chunk_bytes=args.chunk_bytes, zstd_bin=args.zstd_bin)
            print(json.dumps({"bundle": str(output), "manifest_sha256": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()}))
        elif args.command == "verify":
            result = verify_bundle(args.bundle_dir, zstd_bin=args.zstd_bin)
            result.pop("coverage", None)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            output = import_deployment(args.bundle_dir, args.release_root, zstd_bin=args.zstd_bin)
            print(json.dumps({"release": str(output), "verified": True}))
    except (BundleError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"bundle operation failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
