"""Read one immutable publisher-building map snapshot per API request.

The active pointer is the sole authority for the new catalogue.  A malformed
published pointer fails closed; it must never fall back to OSM candidates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextvars import ContextVar, Token
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.store import ROOT


BASE = ROOT / "data/property/developer_map"
ACTIVE_PATH = BASE / "active.json"
RUN_RE = re.compile(r"developer-map-v1-[0-9a-f]{16}\Z")
SNAPSHOT_RE = re.compile(r"developer-map-snapshot-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
REGISTRY_RE = re.compile(r"registry-[0-9a-f]{24}\Z")
POINT_RE = re.compile(r"mappt_[0-9a-f]{24}\Z")
GROUP_RE = re.compile(r"group_[0-9a-f]{24}\Z")
_UNSET = object()
_PINNED: ContextVar[object] = ContextVar("novostroy_developer_map", default=_UNSET)


def _safe_file(path: Path) -> Path:
    if not path.is_relative_to(BASE):
        raise ValueError("Developer map path escapes its root")
    relative = path.relative_to(ROOT)
    current = ROOT
    for part in relative.parts:
        if part in {"", ".", ".."} or part.startswith("."):
            raise ValueError("Unsafe developer map path")
        current /= part
        if current.is_symlink():
            raise ValueError("Developer map path contains a symlink")
    if not current.is_file():
        raise FileNotFoundError("Developer map file is missing")
    return current


def _sha(path: Path, limit: int) -> str:
    path = _safe_file(path)
    if path.stat().st_size > limit:
        raise ValueError("Developer map file exceeds size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, limit: int) -> dict[str, Any]:
    if _safe_file(path).stat().st_size > limit:
        raise ValueError("Developer map JSON exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Developer map JSON must be an object")
    return value


def _rows(path: Path, limit: int) -> list[dict[str, Any]]:
    path = _safe_file(path)
    if path.stat().st_size > limit:
        raise ValueError("Developer map rows exceed size limit")
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                raise ValueError("Blank developer map row")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid developer map row")
            rows.append(row)
    return rows


def _string(value: Any, *, nullable: bool = False, max_length: int = 400) -> bool:
    return ((nullable and value is None) or
            (isinstance(value, str) and 0 < len(value) <= max_length and value == value.strip()))


def _location(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 2 and
            all(type(item) in {int, float} and math.isfinite(item) for item in value) and
            -180 <= value[0] <= 180 and -90 <= value[1] <= 90)


def _timestamp(value: Any) -> bool:
    if not _string(value, max_length=40):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _point(row: dict[str, Any]) -> None:
    if row.get("schema_version") != "developer-map-building/1":
        raise ValueError("Invalid developer building schema")
    if (not isinstance(row.get("registry_id"), str) or REGISTRY_RE.fullmatch(row["registry_id"]) is None or
            not isinstance(row.get("point_id"), str) or POINT_RE.fullmatch(row["point_id"]) is None or
            row.get("point_type") != "developer_building"):
        raise ValueError("Invalid developer building identity")
    for key in ("name", "source_group", "source_evidence_sha256"):
        if not _string(row.get(key), max_length=400):
            raise ValueError(f"Invalid developer building {key}")
    for key in ("project_name", "complex_label", "publisher_project_id",
                "publisher_building_id", "source_corpus_label"):
        if not _string(row.get(key), nullable=True, max_length=400):
            raise ValueError(f"Invalid developer building {key}")
    identity_basis = row.get("identity_basis")
    if identity_basis == "publisher_building_id":
        if row.get("publisher_building_id") is None:
            raise ValueError("Publisher building identity is missing")
    elif identity_basis == "source_project_corpus_label":
        if (row.get("publisher_building_id") is not None or
                row.get("publisher_project_id") is None or
                row.get("source_corpus_label") is None or row.get("map_eligible") is True):
            raise ValueError("Project corpus label is only a list identity")
    else:
        raise ValueError("Unknown developer building identity basis")
    if SHA_RE.fullmatch(row["source_evidence_sha256"]) is None:
        raise ValueError("Invalid developer building evidence hash")
    for key in ("developer_name", "address", "address_basis", "location_basis"):
        if not _string(row.get(key), nullable=True, max_length=500):
            raise ValueError(f"Invalid developer building {key}")
    if type(row.get("address_complete")) is not bool:
        raise ValueError("Invalid developer address completeness")
    if row["address_complete"] and row.get("address") is None:
        raise ValueError("Complete address is missing")
    if (type(row.get("unit_count")) is not int or row["unit_count"] < 1 or
            not _timestamp(row.get("observed_at"))):
        raise ValueError("Developer building must be backed by observed units")
    mapped = row.get("map_eligible")
    if type(mapped) is not bool:
        raise ValueError("Invalid developer map eligibility")
    if mapped:
        if (not _location(row.get("location")) or row.get("location_quality") != "verified" or
                not _string(row.get("location_basis"))):
            raise ValueError("Mapped developer building lacks verified geometry")
        basis = row["location_basis"].lower()
        if any(token in basis for token in ("project_center", "project_marker", "approximate")):
            raise ValueError("Project marker cannot be a building point")
    elif row.get("location") is not None or row.get("location_quality") is not None:
        raise ValueError("Unmapped developer building has a coordinate")


def _group(row: dict[str, Any], by_registry: dict[str, dict[str, Any]]) -> None:
    if (row.get("schema_version") != "developer-map-group/1" or
            not isinstance(row.get("group_id"), str) or GROUP_RE.fullmatch(row["group_id"]) is None or
            row.get("proof_basis") != "same_publisher_project_id" or
            not _string(row.get("group_label")) or not _string(row.get("source_group")) or
            not _string(row.get("publisher_project_id")) or
            not isinstance(row.get("evidence_sha256"), str) or
            SHA_RE.fullmatch(row["evidence_sha256"]) is None):
        raise ValueError("Invalid developer project group")
    member_ids = row.get("member_registry_ids")
    point_ids = row.get("member_point_ids")
    if (not isinstance(member_ids, list) or len(member_ids) < 2 or
            len(member_ids) != len(set(member_ids)) or not isinstance(point_ids, list) or
            len(point_ids) != len(set(point_ids))):
        raise ValueError("Invalid developer project membership")
    if any(member not in by_registry or
           by_registry[member]["source_group"] != row["source_group"] or
           by_registry[member]["publisher_project_id"] != row["publisher_project_id"]
           for member in member_ids):
        raise ValueError("Developer group crosses publisher projects")
    mapped = {by_registry[member]["point_id"] for member in member_ids
              if by_registry[member]["map_eligible"]}
    if set(point_ids) != mapped:
        raise ValueError("Developer group map membership differs from buildings")


@lru_cache(maxsize=2)
def _validated(run_id: str, manifest_hash: str,
               signatures: tuple[tuple[str, int, int, int, int], ...]) -> dict[str, Any]:
    directory = BASE / "runs" / run_id
    manifest_path = directory / "manifest.json"
    if _sha(manifest_path, 100_000) != manifest_hash:
        raise ValueError("Developer map manifest differs from pointer")
    manifest = _json(manifest_path, 100_000)
    if (manifest.get("schema_version") != "developer-map-snapshot/1" or
            manifest.get("run_id") != run_id or
            not isinstance(manifest.get("snapshot_id"), str) or
            SNAPSHOT_RE.fullmatch(manifest["snapshot_id"]) is None):
        raise ValueError("Invalid developer map manifest")
    files: dict[str, list[dict[str, Any]]] = {}
    for label, limit in (("points", 100_000_000), ("groups", 20_000_000),
                         ("rejections", 50_000_000)):
        expected = manifest.get(f"{label}_sha256")
        total = manifest.get(f"{label}_total")
        if (not isinstance(expected, str) or SHA_RE.fullmatch(expected) is None or
                type(total) is not int or total < 0):
            raise ValueError("Invalid developer map file descriptor")
        path = directory / f"{label}.jsonl"
        if _sha(path, limit) != expected:
            raise ValueError("Developer map file hash mismatch")
        files[label] = _rows(path, limit)
        if len(files[label]) != total:
            raise ValueError("Developer map file count mismatch")
    rows = files["points"]
    by_registry: dict[str, dict[str, Any]] = {}
    point_ids: set[str] = set()
    publisher_buildings: set[tuple[str, ...]] = set()
    for row in rows:
        _point(row)
        publisher_key = ((row["source_group"], "building", row["publisher_building_id"])
                         if row["identity_basis"] == "publisher_building_id" else
                         (row["source_group"], "corpus", row["publisher_project_id"],
                          row["source_corpus_label"]))
        if (row["registry_id"] in by_registry or row["point_id"] in point_ids or
                publisher_key in publisher_buildings):
            raise ValueError("Duplicate developer building identity")
        by_registry[row["registry_id"]] = row
        point_ids.add(row["point_id"])
        publisher_buildings.add(publisher_key)
    if (manifest.get("buildings_total") != len(rows) or
            manifest.get("map_points_total") != sum(row["map_eligible"] for row in rows)):
        raise ValueError("Developer map totals differ from rows")
    group_ids: set[str] = set()
    publisher_groups: set[tuple[str, str]] = set()
    claimed_members: set[str] = set()
    for group in files["groups"]:
        _group(group, by_registry)
        publisher_key = group["source_group"], group["publisher_project_id"]
        if (group["group_id"] in group_ids or publisher_key in publisher_groups or
                claimed_members.intersection(group["member_registry_ids"])):
            raise ValueError("Duplicate developer project group membership")
        group_ids.add(group["group_id"])
        publisher_groups.add(publisher_key)
        claimed_members.update(group["member_registry_ids"])
    return {"manifest": manifest, "rows": rows, "groups": files["groups"],
            "by_registry": by_registry,
            "by_point": {row["point_id"]: row for row in rows if row["map_eligible"]}}


def load_staged(run_id: str, *, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Validate one unpublished immutable run without changing the active pointer."""
    if not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None:
        raise ValueError("Invalid developer map run identity")
    directory = BASE / "runs" / run_id
    manifest_path = _safe_file(directory / "manifest.json")
    digest = _sha(manifest_path, 100_000)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise ValueError("Developer map pointer manifest hash mismatch")
    signatures = tuple((name, path.stat().st_mtime_ns, path.stat().st_size,
                        path.stat().st_ctime_ns, path.stat().st_ino)
                       for name in ("manifest.json", "points.jsonl", "groups.jsonl", "rejections.jsonl")
                       for path in (_safe_file(directory / name),))
    return _validated(run_id, digest, signatures)


def resolve_active() -> dict[str, Any] | None:
    """Resolve an active snapshot, or None only before first publication."""
    if not ACTIVE_PATH.exists() and not ACTIVE_PATH.is_symlink():
        return None
    pointer = _json(ACTIVE_PATH, 4096)
    if (set(pointer) != {"schema_version", "run_id", "manifest_sha256"} or
            pointer["schema_version"] != "developer-map-pointer/1"):
        raise ValueError("Invalid developer map pointer")
    run_id = pointer["run_id"]
    digest = pointer["manifest_sha256"]
    if (not isinstance(digest, str) or SHA_RE.fullmatch(digest) is None):
        raise ValueError("Invalid developer map pointer identity")
    return load_staged(run_id, expected_manifest_sha256=digest)


def current() -> dict[str, Any] | None:
    pinned = _PINNED.get()
    return resolve_active() if pinned is _UNSET else pinned  # type: ignore[return-value]


def pin(snapshot: dict[str, Any] | None) -> Token[object]:
    return _PINNED.set(snapshot)


def reset(token: Token[object]) -> None:
    _PINNED.reset(token)


def public_building(row: dict[str, Any]) -> dict[str, Any]:
    """Explicit allowlist: publisher keys and evidence never leave this module."""
    return {
        "registry_id": row["registry_id"], "point_id": row["point_id"],
        "point_type": "developer_building", "name": row["name"],
        "record_kind": "developer_building",
        "complex_label": row["complex_label"], "developer_name": row["developer_name"],
        "address": row["address"], "address_complete": row["address_complete"],
        "address_basis": row["address_basis"], "location": row["location"],
        "location_quality": row["location_quality"], "location_basis": row["location_basis"],
        "map_eligible": row["map_eligible"], "unit_count": row["unit_count"],
        "observed_at": row["observed_at"], "city": "Москва",
        "latest_known_date": None, "status": None, "handover": None,
        "handover_year": None, "handover_quarter": None, "handover_precision": None,
        "floors": None, "building_type": None,
        "has_apartment_data": True, "offer_count": 0, "price": None,
    }


def public_point(row: dict[str, Any]) -> dict[str, Any]:
    if not row["map_eligible"]:
        raise ValueError("Unmapped building cannot be projected as a map point")
    return {key: value for key, value in public_building(row).items()
            if key not in {"has_apartment_data", "offer_count", "price", "map_eligible",
                           "status", "handover", "handover_year", "handover_quarter",
                           "handover_precision", "floors", "building_type"}}


def selected(snapshot: dict[str, Any], query: str | None) -> list[dict[str, Any]]:
    needle = query.strip().casefold() if query else ""
    if not needle:
        return snapshot["rows"]
    return [row for row in snapshot["rows"] if any(
        needle in value.casefold() for value in
        (row["name"], row["complex_label"] or "", row["developer_name"] or "",
         row["address"] or ""))]


def public_groups(snapshot: dict[str, Any], selected_points: set[str]) -> list[dict[str, Any]]:
    return [{"group_id": group["group_id"], "group_scope": "residential_complex",
             "identity_basis": "source_complex_id", "group_label": group["group_label"],
             "point_ids": group["member_point_ids"]}
            for group in snapshot["groups"]
            if len(group["member_point_ids"]) >= 2 and
            set(group["member_point_ids"]) <= selected_points]
