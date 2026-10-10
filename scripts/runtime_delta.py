#!/usr/bin/env python3
"""Export/import a checked runtime delta against one SHA-pinned base release.

Only changed regular files and the complete new release manifest travel. Import
hardlinks unchanged verified files into private staging, never writes/chmods a
base inode, verifies the complete assembled release, then renames atomically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.runtime_transport import (
    HASH_RE, COMMIT_RE, RELEASE_ID_RE, MAX_ARCHIVE_BYTES, MAX_TAR_MEMBERS,
    TransportError, _extract_archive, _load_transport_manifest, _rename_noreplace,
    _sha256_file,
)

SCHEMA = "novostroy-runtime-delta/1"


def _real_directory(path: Path) -> Path:
    path = path.absolute()
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise TransportError(f"Directory contains a symlink: {parent}")
    if not path.is_dir():
        raise TransportError(f"Missing real directory: {path}")
    return path


def _manifest_sha(release: Path) -> str:
    path = release / "release-manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000_000:
        raise TransportError("Release manifest is missing, linked or oversized")
    return _sha256_file(path)[0]


def checked_base(base: Path, expected_sha: str) -> tuple[Path, dict[str, Any]]:
    """Validate an immutable base, including historical connector omissions."""
    if not isinstance(expected_sha, str) or HASH_RE.fullmatch(expected_sha) is None:
        raise TransportError("A full expected base manifest SHA is required")
    base = _real_directory(base)
    if _manifest_sha(base) != expected_sha:
        raise TransportError("Base release manifest differs from its expected SHA")
    from deploy.apply_release import verify_release
    manifest = verify_release(base, allow_legacy_missing_route_inputs=True)
    if manifest["profile"] != "full-compat":
        raise TransportError("Delta base must be a full-compat release")
    if _manifest_sha(base) != expected_sha:
        raise TransportError("Base release manifest changed during verification")
    return base, manifest


def _inventory(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = document.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TAR_MEMBERS:
        raise TransportError("Invalid full release inventory")
    result = {}
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        if (not isinstance(path, str) or "\\" in path or "\0" in path
                or path.split("/")[0] not in {"data", "assets"}
                or any(p in {"", ".", ".."} or p.startswith(".") for p in path.split("/"))
                or path in result or type(row.get("bytes")) is not int or row["bytes"] < 0
                or not isinstance(row.get("sha256"), str) or HASH_RE.fullmatch(row["sha256"]) is None):
            raise TransportError("Unsafe, duplicate or invalid full release file")
        result[path] = row
    if document.get("file_count") != len(result) or document.get("bytes_total") != sum(r["bytes"] for r in result.values()):
        raise TransportError("Full release inventory totals differ")
    return result


def _changed(base: dict[str, dict], target: dict[str, dict]) -> set[str]:
    return {name for name, row in target.items() if name not in base
            or any(row[key] != base[name][key] for key in ("sha256", "bytes"))}


def export_delta(release: Path, base: Path, base_sha: str, output: Path, *,
                 source_commit: str, source_bundle: Path) -> Path:
    from deploy.apply_release import verify_release
    release = _real_directory(release)
    base, base_doc = checked_base(base, base_sha)
    new_doc = verify_release(release)
    if (new_doc["profile"] != "full-compat" or release == base
            or RELEASE_ID_RE.fullmatch(release.name) is None
            or RELEASE_ID_RE.fullmatch(base.name) is None):
        raise TransportError("Delta requires distinct named full-compat releases")
    if (COMMIT_RE.fullmatch(source_commit) is None or source_bundle.is_symlink()
            or not source_bundle.is_file()):
        raise TransportError("Reviewed source commit and regular bundle are required")
    bundle_sha, bundle_size = _sha256_file(source_bundle)
    bundle_head = subprocess.run(
        ["git", "bundle", "list-heads", str(source_bundle.absolute()), "refs/heads/branch1"],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if (bundle_head.returncode != 0
            or bundle_head.stdout.strip() != f"{source_commit} refs/heads/branch1"
            or _sha256_file(source_bundle) != (bundle_sha, bundle_size)):
        raise TransportError("Source bundle branch1 differs from the reviewed source commit")
    before_sha = _manifest_sha(release)
    changed = sorted(_changed(_inventory(base_doc), _inventory(new_doc)))
    new_rows = _inventory(new_doc)
    changed_bytes = sum(new_rows[name]["bytes"] for name in changed)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Delta output exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    stage = Path(tempfile.mkdtemp(prefix=".delta-export-", dir=output.parent))
    try:
        tar_path = stage / "payload.tar"
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for name in ["release-manifest.json", *changed]:
                source = release / name
                if source.is_symlink() or not source.is_file():
                    raise TransportError("Release changed during delta export")
                before = source.stat()
                if name != "release-manifest.json" and _sha256_file(source) != (new_rows[name]["sha256"], new_rows[name]["bytes"]):
                    raise TransportError("Changed file differs from the verified release")
                info = archive.gettarinfo(str(source), arcname=f"{release.name}/{name}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                info.mtime = 0
                # gettarinfo can infer a hardlink from an earlier inode. Force
                # a regular payload; imported archives never accept links.
                info.type = tarfile.REGTYPE
                info.linkname = ""
                info.size = source.stat().st_size
                with source.open("rb") as stream:
                    archive.addfile(info, stream)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
                    raise TransportError("Changed file mutated during delta export")
        uncompressed = tar_path.stat().st_size
        archive_path = stage / f"{release.name}.delta.tar.zst"
        with archive_path.open("xb") as stream:
            subprocess.run(["zstd", "-q", "-T2", "-3", "-c", "--", str(tar_path)],
                           check=True, stdout=stream)
        digest, size = _sha256_file(archive_path)
        if uncompressed > MAX_ARCHIVE_BYTES:
            raise TransportError("Delta archive exceeds its byte bound")
        tar_path.unlink()
        if _manifest_sha(release) != before_sha or _manifest_sha(base) != base_sha:
            raise TransportError("Release manifest changed during export")
        transport = {"schema_version": SCHEMA,
            "archive": {"name": archive_path.name, "format": "tar.zst", "sha256": digest,
                        "bytes": size, "uncompressed_bytes": uncompressed},
            "base": {"release_id": base.name, "manifest_sha256": base_sha},
            "release": {"release_id": release.name, "root_name": release.name,
                        "manifest_sha256": before_sha, "profile": "full-compat",
                        "file_count": new_doc["file_count"], "bytes_total": new_doc["bytes_total"]},
            "delta": {"file_count": len(changed), "bytes_total": changed_bytes},
            "source": {"commit": source_commit, "bundle_sha256": bundle_sha,
                       "bundle_bytes": bundle_size, "bundle_name": source_bundle.name},
            "extract_layout": f"<release-root>/{release.name}/"}
        sidecar = stage / "transport.json"
        sidecar.write_text(json.dumps(transport, sort_keys=True, indent=2)+"\n", encoding="utf-8")
        _load_delta(sidecar)
        _rename_noreplace(stage, output)
        return output
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _load_delta(path: Path) -> dict[str, Any]:
    doc = _load_transport_manifest(path, schema=SCHEMA)
    base, delta = doc.get("base"), doc.get("delta")
    if (not isinstance(base, dict) or set(base) != {"release_id", "manifest_sha256"}
            or not isinstance(base["release_id"], str) or RELEASE_ID_RE.fullmatch(base["release_id"]) is None
            or base["release_id"] == doc["release"]["release_id"]
            or not isinstance(base["manifest_sha256"], str) or HASH_RE.fullmatch(base["manifest_sha256"]) is None
            or not isinstance(delta, dict) or set(delta) != {"file_count", "bytes_total"}
            or type(delta["file_count"]) is not int or not 0 <= delta["file_count"] <= doc["release"]["file_count"]
            or type(delta["bytes_total"]) is not int or not 0 <= delta["bytes_total"] <= doc["release"]["bytes_total"]):
        raise TransportError("Invalid delta base or payload inventory pins")
    return doc


def import_delta(archive: Path, sidecar: Path, release_root: Path) -> Path:
    doc = _load_delta(sidecar)
    release_root = _real_directory(release_root)
    if archive.is_symlink() or not archive.is_file():
        raise TransportError("Delta archive must be a regular file")
    digest, size = _sha256_file(archive)
    if (archive.name != doc["archive"]["name"] or digest != doc["archive"]["sha256"]
            or size != doc["archive"]["bytes"]):
        raise TransportError("Delta archive differs from its transport pin")
    target = release_root / doc["release"]["release_id"]
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Release exists: {target}")
    base, base_doc = checked_base(release_root / doc["base"]["release_id"], doc["base"]["manifest_sha256"])
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{target.name}-", dir=release_root))
    try:
        extraction = {**doc, "release": {**doc["release"], **doc["delta"]}}
        _extract_archive(archive, stage, extraction)
        if _manifest_sha(stage) != doc["release"]["manifest_sha256"]:
            raise TransportError("New full release manifest differs from delta pin")
        new_doc = json.loads((stage / "release-manifest.json").read_text(encoding="utf-8"))
        new_rows, old_rows = _inventory(new_doc), _inventory(base_doc)
        changed = _changed(old_rows, new_rows)
        actual = {str(path.relative_to(stage)) for path in stage.rglob("*") if path.is_file()}
        if (actual != changed | {"release-manifest.json"}
                or len(changed) != doc["delta"]["file_count"]
                or sum(new_rows[name]["bytes"] for name in changed) != doc["delta"]["bytes_total"]):
            raise TransportError("Delta payload does not contain exactly the changed files")
        for name in sorted(new_rows.keys() - changed):
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            # open('wb'), chmod and chown must never touch these linked inodes.
            os.link(base / name, destination, follow_symlinks=False)
        (stage / "data/runtime").mkdir(parents=True, exist_ok=True)
        from deploy.apply_release import verify_release
        verified = verify_release(stage)
        if any(verified.get(key) != doc["release"][key] for key in ("profile", "file_count", "bytes_total")):
            raise TransportError("Assembled full release differs from transport pins")
        if _manifest_sha(base) != doc["base"]["manifest_sha256"]:
            raise TransportError("Base manifest changed during import")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export")
    exporter.add_argument("--release", type=Path, required=True)
    exporter.add_argument("--base-release", type=Path, required=True)
    exporter.add_argument("--base-manifest-sha256", required=True)
    exporter.add_argument("--output", type=Path, required=True)
    exporter.add_argument("--source-commit", required=True)
    exporter.add_argument("--source-bundle", type=Path, required=True)
    importer = commands.add_parser("import")
    importer.add_argument("--archive", type=Path, required=True)
    importer.add_argument("--manifest", type=Path, required=True)
    importer.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            target = export_delta(args.release, args.base_release, args.base_manifest_sha256,
                                  args.output, source_commit=args.source_commit, source_bundle=args.source_bundle)
        else:
            target = import_delta(args.archive, args.manifest, args.release_root)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Runtime delta failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"verified": True, "output": str(target)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
