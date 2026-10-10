"""Conservative, revalidated retention for refresh-owned generations and cache.

Historical source evidence, prior public roots without tracked publication dates,
and deployment exports are protected. ``apply`` accepts only an exact saved plan;
the caller must review that plan before invoking the destructive command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from pipeline.maintenance.refresh import ROOT, exclusive_lock, read_json, sha256, write_json

GRACE_SECONDS = 7 * 86400
PUBLIC_SECONDS = 365 * 86400
CACHE_MAX_BYTES = 512 * 1024 * 1024
PLAN_MAX_AGE_SECONDS = 24 * 3600


def _files(path: Path) -> Iterator[Path]:
    if path.is_file() and not path.is_symlink():
        yield path
    elif path.is_dir() and not path.is_symlink():
        for parent, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
            for name in files:
                item = Path(parent) / name
                if not item.is_symlink():
                    yield item


def _has_symlink_ancestor(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _tree_identity(path: Path) -> tuple[str, int, list[tuple[tuple[int, int], int, int]]]:
    digest = hashlib.sha256()
    logical = 0
    inodes = []
    for item in sorted(_files(path)):
        stat = item.stat()
        name = item.relative_to(path).as_posix() if path.is_dir() else item.name
        digest.update(f"{name}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ino}\n".encode())
        digest.update(bytes.fromhex(sha256(item)))
        logical += stat.st_size
        inodes.append(((stat.st_dev, stat.st_ino), stat.st_size, stat.st_nlink))
    return digest.hexdigest(), logical, inodes


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in _files(path))


def _asset_roots(pointer: dict[str, Any]) -> set[str]:
    roots = set(pointer.get("previous_public_roots", []))
    for key, value in pointer.items():
        if isinstance(value, str) and value.startswith("public-"):
            roots.add(value)
        if isinstance(value, str) and "/assets/public-" in value:
            roots.add(value.split("/assets/", 1)[1].split("/", 1)[0])
    return roots


def _pin(protected: dict[str, list[str]], relative: str, reason: str) -> None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid retention reference")
    if reason not in protected[relative]:
        protected[relative].append(reason)


def _generation_bundle(root: Path, protected: dict[str, list[str]],
                       pointer: dict[str, Any], reason: str) -> None:
    digest = pointer.get("bundle_sha256")
    generation_id = pointer.get("generation_id")
    if (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or generation_id != f"generation-{digest[:16]}"):
        raise ValueError("invalid pinned generation pointer")
    relative = f"data/property/estateline/normalized/generations/{generation_id}"
    bundle_path = root / relative / "bundle.json"
    if _has_symlink_ancestor(bundle_path, root) or not bundle_path.is_file() or sha256(bundle_path) != digest:
        raise ValueError("pinned generation bundle missing or changed")
    bundle = read_json(bundle_path)
    source = bundle["normalized_snapshot"]["path"]
    visibility = f"data/property/estateline/visibility/runs/{bundle['visibility']['run_id']}"
    model = f"data/models/{bundle['model']['run_id']}"
    public = f"assets/{bundle['model']['public_root']}"
    for ref in (relative, source, visibility, model, public):
        _pin(protected, ref, reason)


def _pointer_references(root: Path, protected: dict[str, list[str]],
                        relative: str, pointer: dict[str, Any], reason: str) -> None:
    if relative == "assets/manifest.json":
        for name in _asset_roots(pointer):
            _pin(protected, f"assets/{name}", reason)
    elif relative == "data/models/active.json":
        _pin(protected, f"data/models/{pointer['run_id']}", reason)
    elif relative == "data/property/estateline/visibility/active.json":
        _pin(protected, f"data/property/estateline/visibility/runs/{pointer['run_id']}", reason)
    elif relative == "data/property/estateline/normalized/active.json":
        _generation_bundle(root, protected, pointer, reason)


def references(root: Path) -> dict[str, list[str]]:
    """Resolve active, compatible, local-release, and explicit operator pins."""
    protected: dict[str, list[str]] = defaultdict(list)
    for relative, reason in (("assets/manifest.json", "active_or_compat_asset_pointer"),
                             ("data/models/active.json", "active_model_pointer"),
                             ("data/property/estateline/visibility/active.json", "active_visibility_pointer"),
                             ("data/property/estateline/normalized/active.json", "active_generation")):
        path = root / relative
        if path.is_file():
            if _has_symlink_ancestor(path, root):
                raise ValueError("retention reference traverses symlink")
            _pointer_references(root, protected, relative, read_json(path), reason)
    active_path = root / "data/property/estateline/normalized/active.json"
    if active_path.is_file():
        active = read_json(active_path)
        transition = (root / "data/runtime/refresh/generation-transitions"
                      / f"{active['bundle_sha256']}.json")
        if transition.is_file():
            if _has_symlink_ancestor(transition, root):
                raise ValueError("retention transition traverses symlink")
            record = read_json(transition)
            if record.get("to_pointer") != active:
                raise ValueError("generation transition differs from active pointer")
            _pin(protected, transition.relative_to(root).as_posix(), "active_generation_rollback")
            if record.get("from_pointer") is not None:
                _generation_bundle(root, protected, record["from_pointer"], "previous_generation_rollback")
    pending_root = root / "data/runtime/refresh/pending"
    for path in pending_root.glob("*.json"):
        if _has_symlink_ancestor(path, root):
            raise ValueError("retention pending reference traverses symlink")
        pending = read_json(path)
        _pin(protected, path.relative_to(root).as_posix(), "pending_generation")
        _generation_bundle(root, protected, pending, "pending_generation")
        previous_hash = pending.get("expected_previous_bundle_sha256")
        if previous_hash is not None:
            if not isinstance(previous_hash, str) or re.fullmatch(r"[a-f0-9]{64}", previous_hash) is None:
                raise ValueError("invalid pending predecessor hash")
            _generation_bundle(root, protected, {"bundle_sha256": previous_hash,
                                                  "generation_id": f"generation-{previous_hash[:16]}"},
                               "pending_generation_predecessor")
    exports = root / ".runtime/deploy-release"
    for path in exports.glob("*/assets/manifest.json"):
        if _has_symlink_ancestor(path, root):
            raise ValueError("retention release reference traverses symlink")
        for name in _asset_roots(read_json(path)):
            protected[f"assets/{name}"].append(f"release_export:{path.parent.parent.name}")
        protected[path.parent.parent.relative_to(root).as_posix()].append("release_export_state_unknown")
    pin_file = root / "data/runtime/refresh/release_pins.json"
    if pin_file.is_file():
        for path in read_json(pin_file).get("paths", []):
            _pin(protected, path, "operator_release_pin")
            match = re.fullmatch(r"data/property/estateline/normalized/generations/(generation-[a-f0-9]{16})(?:/bundle\.json)?", path)
            if match:
                bundle_path = root / "data/property/estateline/normalized/generations" / match[1] / "bundle.json"
                if _has_symlink_ancestor(bundle_path, root) or not bundle_path.is_file():
                    raise ValueError("operator pinned generation bundle unavailable")
                _generation_bundle(root, protected, {"generation_id": match[1],
                                                     "bundle_sha256": sha256(bundle_path)}, "operator_pinned_generation")
    journal_root = root / "data/runtime/refresh"
    for path in journal_root.glob("journal-*"):
        if _has_symlink_ancestor(path, root):
            raise ValueError("retention journal traverses symlink")
        state = path / "state.json"
        if state.is_file() and read_json(state).get("status") == "applying":
            _pin(protected, path.relative_to(root).as_posix(), "in_flight_transaction")
    committed = sorted((path for path in journal_root.glob("journal-*")
                        if (path / "state.json").is_file()
                        and read_json(path / "state.json").get("status") == "committed"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    if committed:
        _pin(protected, committed[0].relative_to(root).as_posix(), "latest_rollback_journal")
    retained_journals = [path for path in journal_root.glob("journal-*")
                         if path.relative_to(root).as_posix() in protected]
    for journal in retained_journals:
        if _has_symlink_ancestor(journal, root):
            raise ValueError("retention journal traverses symlink")
        metadata = read_json(journal / "journal.json")
        for item in metadata["files"]:
            relative = item["path"]
            _pin(protected, relative, "rollback_target")
            if item["existed"] and relative in {"assets/manifest.json", "data/models/active.json",
                                                 "data/property/estateline/visibility/active.json",
                                                 "data/property/estateline/normalized/active.json"}:
                backup = journal / item["backup"]
                if backup.parent != journal or _has_symlink_ancestor(backup, root):
                    raise ValueError("invalid rollback backup path")
                _pointer_references(root, protected, relative, read_json(backup), "rollback_backup")
    return dict(protected)


def _published_ledger(root: Path) -> dict[str, float]:
    path = root / "data/runtime/refresh/published.jsonl"
    if not path.is_file():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            result[row["path"]] = float(row["published_epoch"])
    return result


def _candidates(root: Path, now: float, pins: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runtime = root / "data/runtime/refresh"
    published = _published_ledger(root)
    candidates: list[dict[str, Any]] = []
    protected_unknown: list[dict[str, Any]] = []

    def consider(path: Path, reason: str, min_age: float, *, recorded_at: float | None = None) -> None:
        if not path.exists() or _has_symlink_ancestor(path, root):
            return
        rel = path.relative_to(root).as_posix()
        if any(rel == pin or pin.startswith(rel + "/") or rel.startswith(pin + "/") for pin in pins):
            return
        age_ref = recorded_at if recorded_at is not None else path.stat().st_mtime
        if now - age_ref < min_age:
            return
        fingerprint, logical, inodes = _tree_identity(path)
        candidates.append({"path": rel, "reason": reason, "age_days": int((now - age_ref) / 86400),
                           "fingerprint": fingerprint, "logical_bytes": logical,
                           "_inodes": inodes})

    # Only the new orchestrator owns these transient folders. Prior runs and
    # release exports remain protected unless this ledger recorded publication.
    for pattern in ("estate-*", "oms-*", "journal-*"):
        for path in runtime.glob(pattern):
            if path.is_dir() and not path.is_symlink():
                consider(path, "expired_refresh_staging", GRACE_SECONDS)
    for rel, published_at in published.items():
        if not re.fullmatch(r"(?:assets/public-[a-z0-9-]+|data/models/models-v1-[a-f0-9]{16})", rel):
            continue
        consider(root / rel, "published_generation_after_cache_ttl", PUBLIC_SECONDS,
                 recorded_at=published_at)

    cache = root / "data/models/cache/geometry"
    cache_files = sorted((p for p in cache.glob("model_*.glb") if p.is_file() and not p.is_symlink()),
                         key=lambda p: p.stat().st_mtime)
    excess = max(0, sum(path.stat().st_size for path in cache_files) - CACHE_MAX_BYTES)
    for path in cache_files:
        if excess <= 0:
            break
        if now - path.stat().st_mtime >= GRACE_SECONDS:
            consider(path, "cache_over_512_mib_and_older_than_7d", GRACE_SECONDS)
            excess -= path.stat().st_size

    # Important inventory: old URL roots may look orphaned, but their age and
    # client/release usage are not proven by this orchestrator.
    for path in (root / "assets").glob("public-*"):
        rel = path.relative_to(root).as_posix()
        if path.is_dir() and not any(rel == pin or pin.startswith(rel + "/") or rel.startswith(pin + "/")
                                     for pin in pins) and rel not in published:
            logical = _tree_size(path)
            protected_unknown.append({"path": rel, "reason": "legacy_untracked_public_root",
                                      "logical_bytes": logical})
    return candidates, protected_unknown


def make_plan(root: Path = ROOT, *, now: float | None = None) -> dict[str, Any]:
    root = root.resolve()
    now = time.time() if now is None else now
    pins = references(root)
    entries, unknown = _candidates(root, now, pins)
    candidate_links: dict[tuple[int, int], int] = defaultdict(int)
    inode_info: dict[tuple[int, int], tuple[int, int]] = {}
    for entry in entries:
        for key, size, links in entry.pop("_inodes"):
            candidate_links[key] += 1
            inode_info[key] = size, links
    physical = sum(size for key, (size, links) in inode_info.items() if candidate_links[key] == links)
    inventory = {
        "schema_version": 1, "root": str(root), "created_epoch": now,
        "policy": {"transient_grace_days": 7, "published_min_days": 365,
                   "cache_max_bytes": CACHE_MAX_BYTES, "plan_max_age_hours": 24},
        "candidates": entries,
        "logical_reclaimable_bytes": sum(entry["logical_bytes"] for entry in entries),
        "physical_reclaimable_upper_bound_bytes": physical,
        "protected_references": pins,
        "protected_unknown_public_roots": unknown,
        "note": "Never delete raw source snapshots, active/compatible assets, releases, or untracked public roots.",
    }
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    inventory["plan_sha256"] = hashlib.sha256(encoded).hexdigest()
    return inventory


def save_plan(root: Path = ROOT) -> Path:
    with exclusive_lock(root):
        plan = make_plan(root)
        path = root / "data/runtime/refresh/plans" / f"{plan['plan_sha256']}.json"
        write_json(path, plan)
        return path


def apply_plan(path: Path, root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    path = path.resolve()
    plans = (root / "data/runtime/refresh/plans").resolve()
    if path.parent != plans or not re.fullmatch(r"[a-f0-9]{64}\.json", path.name):
        raise ValueError("plan must be a saved refresh retention plan")
    with exclusive_lock(root):
        stored = read_json(path)
        identity = stored.pop("plan_sha256")
        encoded = json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != identity or path.stem != identity:
            raise ValueError("plan changed after review")
        if time.time() - stored["created_epoch"] > PLAN_MAX_AGE_SECONDS:
            raise ValueError("plan expired; generate and review a new plan")
        current = make_plan(root)
        current_by_path = {entry["path"]: entry for entry in current["candidates"]}
        if current["protected_references"] != stored["protected_references"]:
            raise ValueError("reference graph changed since plan review")
        for entry in stored["candidates"]:
            if current_by_path.get(entry["path"], {}).get("fingerprint") != entry["fingerprint"]:
                raise ValueError(f"candidate changed or became protected: {entry['path']}")
        removed = []
        for entry in stored["candidates"]:
            target = root / entry["path"]
            if _has_symlink_ancestor(target, root) or not target.exists():
                raise ValueError(f"candidate unavailable: {entry['path']}")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(entry["path"])
        return {"removed": removed, "logical_bytes": sum(e["logical_bytes"] for e in stored["candidates"]),
                "physical_upper_bound_bytes": stored["physical_reclaimable_upper_bound_bytes"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.command == "plan":
        path = save_plan()
        print(json.dumps({"plan_path": str(path), **read_json(path)}, ensure_ascii=False, indent=2))
    else:
        if args.plan is None:
            parser.error("--plan is required for apply")
        print(json.dumps(apply_plan(args.plan), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
