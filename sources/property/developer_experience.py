"""Publish or roll back one atomic developer unit+model experience pointer.

The map, menu, and selected-only models are immutable inputs. The active JSON
pointer is the only user-facing commit marker; activation never deletes runs.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Any

from backend import developer_map as map_reader
from backend import developer_models as model_reader
from backend import developer_units as unit_reader
from sources.property.developer_map import (
    ROOT, _atomic_private_json, _canonical, _file_sha, _fsync_dir, _sha,
)


BASE = ROOT / "data/property/developer_experience"
POINTER_SCHEMA = "developer-experience-pointer/1"
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _active_bytes(base: Path) -> bytes | None:
    path = base / "active.json"
    if path.is_symlink():
        raise ValueError("Experience pointer is a symlink")
    return path.read_bytes() if path.exists() else None


def pointer_sha(base: Path = BASE) -> str:
    return _sha(_active_bytes(base) or b"")


def _verify_bundle(map_run_id: str, unit_run_id: str,
                   model_run_id: str) -> dict[str, Any]:
    map_run = map_reader.resolve_active()
    if map_run is None or map_run["manifest"]["run_id"] != map_run_id:
        raise ValueError("Experience does not match active map generation")
    unit_run = unit_reader.load_staged(unit_run_id, map_run)
    model_run = model_reader.load_staged(model_run_id, map_run)
    unit_sha = _file_sha(unit_reader.BASE / "runs" / unit_run_id / "manifest.json")
    model_sha = _file_sha(model_reader.BASE / "runs" / model_run_id / "manifest.json")
    model_manifest = model_run["manifest"]
    if (model_manifest.get("menu_run_id") != unit_run_id or
            model_manifest.get("menu_manifest_sha256") != unit_sha):
        raise ValueError("Selected models do not match the exact unit menu")
    # Reader validates JSONL memberships; publication also verifies every
    # listed asset byte before the one-pointer flip.
    for sha in unit_run["assets"]:
        unit_reader.asset_bytes(unit_run, sha)
    for sha in model_run["asset_shas"]:
        model_reader.asset_bytes(model_run, sha)
    return {"schema_version": POINTER_SCHEMA,
            "map_run_id": map_run_id,
            "map_snapshot_id": map_run["manifest"]["snapshot_id"],
            "map_manifest_sha256": _file_sha(
                map_reader.BASE / "runs" / map_run_id / "manifest.json"),
            "unit_run_id": unit_run_id, "unit_manifest_sha256": unit_sha,
            "model_run_id": model_run_id, "model_manifest_sha256": model_sha}


def publish(map_run_id: str, unit_run_id: str, model_run_id: str, *,
            expected_active_sha: str, base: Path = BASE) -> dict[str, Any]:
    if HASH_RE.fullmatch(expected_active_sha) is None:
        raise ValueError("Publish requires exact prior pointer SHA")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (base / "publish.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = _active_bytes(base)
        if _sha(previous or b"") != expected_active_sha:
            raise ValueError("Experience pointer changed before publish")
        pointer = _verify_bundle(map_run_id, unit_run_id, model_run_id)
        current = _canonical(pointer) + b"\n"
        if previous == current:
            return pointer
        # Journal is written first. A crash before the flip leaves only an
        # inert journal; a crash after the flip leaves an immediate rollback.
        history = base / "history"
        history.mkdir(exist_ok=True, mode=0o700)
        _atomic_private_json(history / (_sha(current) + ".json"),
                             {"previous": json.loads(previous) if previous else None,
                              "previous_sha256": _sha(previous or b"")})
        _atomic_private_json(base / "active.json", pointer)
        return pointer


def rollback(*, expected_active_sha: str, base: Path = BASE) -> dict[str, Any] | None:
    if HASH_RE.fullmatch(expected_active_sha) is None:
        raise ValueError("Rollback requires exact current pointer SHA")
    with (base / "publish.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = _active_bytes(base)
        if current is None or _sha(current) != expected_active_sha:
            raise ValueError("Experience pointer changed before rollback")
        journal = base / "history" / (expected_active_sha + ".json")
        if journal.is_symlink() or not journal.is_file():
            raise ValueError("No reviewed rollback journal for current pointer")
        record = json.loads(journal.read_text(encoding="utf-8"))
        prior = record.get("previous")
        prior_bytes = _canonical(prior) + b"\n" if prior is not None else None
        if _sha(prior_bytes or b"") != record.get("previous_sha256"):
            raise ValueError("Rollback journal SHA mismatch")
        if prior is None:
            (base / "active.json").unlink()
            _fsync_dir(base)
            return None
        expected = _verify_bundle(prior["map_run_id"], prior["unit_run_id"],
                                  prior["model_run_id"])
        if expected != prior:
            raise ValueError("Previous experience refs no longer validate")
        _atomic_private_json(base / "active.json", prior)
        return prior


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    publish_cmd = sub.add_parser("publish")
    publish_cmd.add_argument("--map-run-id", required=True)
    publish_cmd.add_argument("--unit-run-id", required=True)
    publish_cmd.add_argument("--model-run-id", required=True)
    publish_cmd.add_argument("--expected-active-sha", required=True)
    rollback_cmd = sub.add_parser("rollback")
    rollback_cmd.add_argument("--expected-active-sha", required=True)
    args = parser.parse_args()
    if args.command == "status":
        raw = _active_bytes(args.base)
        result: Any = {"active_pointer_sha256": _sha(raw or b""),
                       "active": json.loads(raw) if raw else None}
    elif args.command == "publish":
        result = publish(args.map_run_id, args.unit_run_id, args.model_run_id,
                         expected_active_sha=args.expected_active_sha, base=args.base)
    else:
        result = rollback(expected_active_sha=args.expected_active_sha, base=args.base)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
