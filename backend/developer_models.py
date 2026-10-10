"""Selected-only schematic building models bound to the active developer map."""

from __future__ import annotations

import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend import developer_map
from backend.store import ROOT


BASE = ROOT / "data/property/developer_models"
ACTIVE_PATH = BASE / "active.json"
RUN_RE = re.compile(r"developer-models-v1-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def _safe_file(path: Path) -> Path:
    if not path.is_relative_to(BASE):
        raise ValueError("Developer model path escapes its root")
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        if not part or part in {".", ".."} or part.startswith("."):
            raise ValueError("Unsafe developer model path")
        current /= part
        if current.is_symlink():
            raise ValueError("Developer model path contains a symlink")
    if not current.is_file():
        raise FileNotFoundError("Developer model file is missing")
    return current


def _sha(path: Path, limit: int) -> str:
    path = _safe_file(path)
    if path.stat().st_size > limit:
        raise ValueError("Developer model file exceeds byte limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, limit: int) -> dict[str, Any]:
    if _safe_file(path).stat().st_size > limit:
        raise ValueError("Developer model JSON exceeds byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Developer model JSON must be an object")
    return value


@lru_cache(maxsize=2)
def _validated(run_id: str, manifest_sha256: str,
               signatures: tuple[tuple[str, int, int, int, int], ...],
               map_run_id: str, map_snapshot_id: str, map_sha256: str) -> dict[str, Any]:
    directory = BASE / "runs" / run_id
    manifest_path = directory / "manifest.json"
    if _sha(manifest_path, 100_000) != manifest_sha256:
        raise ValueError("Developer model manifest differs from pointer")
    manifest = _json(manifest_path, 100_000)
    if (manifest.get("schema_version") != "developer-models-snapshot/1" or
            manifest.get("run_id") != run_id or
            manifest.get("map_run_id") != map_run_id or
            manifest.get("map_snapshot_id") != map_snapshot_id or
            manifest.get("map_manifest_sha256") != map_sha256):
        raise ValueError("Developer model run does not match active map")
    if (not isinstance(manifest.get("menu_run_id"), str) or
            not re.fullmatch(r"developer-units-v1-[0-9a-f]{16}", manifest["menu_run_id"]) or
            not isinstance(manifest.get("menu_manifest_sha256"), str) or
            SHA_RE.fullmatch(manifest["menu_manifest_sha256"]) is None):
        raise ValueError("Selected model has no pinned unit menu")
    expected = manifest.get("models_sha256")
    total = manifest.get("models_total")
    if (not isinstance(expected, str) or SHA_RE.fullmatch(expected) is None or
            type(total) is not int or total < 0):
        raise ValueError("Invalid developer model file descriptor")
    path = directory / "models.jsonl"
    if _sha(path, 30_000_000) != expected:
        raise ValueError("Developer model rows differ from manifest")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Invalid developer model row")
            rows.append(value)
    if len(rows) != total:
        raise ValueError("Developer model count differs from manifest")
    return {"manifest": manifest, "rows": rows, "run_directory": directory}


def load_staged(run_id: str, map_run: dict[str, Any], *,
                expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    if not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None:
        raise ValueError("Invalid developer model run ID")
    directory = BASE / "runs" / run_id
    manifest_path = _safe_file(directory / "manifest.json")
    digest = _sha(manifest_path, 100_000)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise ValueError("Developer model pointer SHA mismatch")
    signatures = tuple((name, path.stat().st_mtime_ns, path.stat().st_size,
                        path.stat().st_ctime_ns, path.stat().st_ino)
                       for name in ("manifest.json", "models.jsonl")
                       for path in (_safe_file(directory / name),))
    map_run_id = map_run["manifest"]["run_id"]
    map_sha = hashlib.sha256(
        (developer_map.BASE / "runs" / map_run_id / "manifest.json").read_bytes()).hexdigest()
    loaded = _validated(run_id, digest, signatures, map_run_id,
                        map_run["manifest"]["snapshot_id"], map_sha)
    by_registry: dict[str, dict[str, Any]] = {}
    asset_shas: set[str] = set()
    for row in loaded["rows"]:
        map_row = map_run["by_registry"].get(row.get("registry_id"))
        asset_sha = row.get("asset_sha256")
        height = row.get("height_m")
        transform = row.get("model_transform")
        geometry_basis = row.get("source_geometry_basis")
        lake_osm = geometry_basis == "osm_exact_building_footprint_way_1317814900_v2"
        if (map_row is None or not map_row["map_eligible"] or
                not isinstance(asset_sha, str) or SHA_RE.fullmatch(asset_sha) is None or
                type(row.get("asset_bytes")) is not int or row["asset_bytes"] <= 0 or
                row["asset_bytes"] > 20_000_000 or
                type(height) not in {int, float} or not math.isfinite(height) or
                not 0 < height <= 1500 or
                row.get("depiction") != "schematic_massing" or
                row.get("label") != "реконструкция" or
                row.get("height_basis") != (
                    "schematic_min_section_floors_times_3m_estimate" if lake_osm else
                    "source_total_floors_times_3m_estimate") or
                geometry_basis not in {"aggregator_unverified",
                                       "osm_exact_building_footprint_way_1317814900_v2"} or
                (lake_osm and (row.get("attribution") != "© OpenStreetMap contributors" or
                               row.get("rights_license") != "ODbL-1.0" or
                               row.get("photo_facade_reconstruction") is not True or
                               row.get("texture_basis") !=
                               "generated_photo_informed_repeat_unoriented")) or
                (not lake_osm and (row.get("attribution") not in {None, ""} or
                                   row.get("photo_facade_reconstruction") is True or
                                   row.get("texture_basis") not in {None, ""})) or
                not isinstance(transform, list) or len(transform) != 16 or
                any(type(value) not in {int, float} or not math.isfinite(value)
                    for value in transform) or
                row["registry_id"] in by_registry or asset_sha in asset_shas):
            raise ValueError("Invalid selected developer model")
        by_registry[row["registry_id"]] = row
        asset_shas.add(asset_sha)
    return {"manifest": loaded["manifest"], "by_registry": by_registry,
            "asset_shas": asset_shas, "run_directory": directory}


def resolve_active(map_run: dict[str, Any]) -> dict[str, Any] | None:
    if not ACTIVE_PATH.exists() and not ACTIVE_PATH.is_symlink():
        return None
    pointer = _json(ACTIVE_PATH, 4096)
    if (set(pointer) != {"schema_version", "run_id", "manifest_sha256"} or
            pointer["schema_version"] != "developer-models-pointer/1" or
            not isinstance(pointer["manifest_sha256"], str) or
            SHA_RE.fullmatch(pointer["manifest_sha256"]) is None):
        raise ValueError("Invalid developer model pointer")
    return load_staged(pointer["run_id"], map_run,
                       expected_manifest_sha256=pointer["manifest_sha256"])


def public_model(map_run: dict[str, Any], registry_id: str,
                 snapshot: dict[str, Any] | None,
                 menu_run_id: str) -> dict[str, Any]:
    identity = {"registry_snapshot_id": map_run["manifest"]["snapshot_id"],
                "map_run_id": map_run["manifest"]["run_id"],
                "unit_run_id": menu_run_id,
                "model_run_id": snapshot["manifest"]["run_id"] if snapshot else None,
                "registry_id": registry_id}
    row = snapshot["by_registry"].get(registry_id) if snapshot else None
    if row is None:
        return {**identity, "status": "unavailable", "depiction": None,
                "label": None, "height_m": None, "height_basis": None,
                "geometry_basis": None, "model_transform": None,
                "asset_url": None, "asset_sha256": None,
                "geometry_attribution": None, "photo_facade_reconstruction": False,
                "texture_basis": None}
    lake_osm = row["source_geometry_basis"] == "osm_exact_building_footprint_way_1317814900_v2"
    return {**identity, "status": "ready", "depiction": "schematic_massing",
            "label": "реконструкция", "height_m": row["height_m"],
            "height_basis": ("schematic_min_section_floors_times_3m_estimate" if lake_osm else
                             "source_floor_count_times_3m_estimate"),
            "geometry_basis": "source_building_footprint",
            "model_transform": row["model_transform"],
            "asset_url": f"/v1/developer-model-assets/{row['asset_sha256']}",
            "asset_sha256": row["asset_sha256"],
            "geometry_attribution": "© OpenStreetMap contributors" if lake_osm else None,
            "photo_facade_reconstruction": lake_osm,
            "texture_basis": "generated_photo_informed_unoriented" if lake_osm else None}


def asset_bytes(snapshot: dict[str, Any], sha256: str) -> bytes:
    if SHA_RE.fullmatch(sha256) is None or sha256 not in snapshot["asset_shas"]:
        raise FileNotFoundError("Model asset is not in the active allowlist")
    path = _safe_file(snapshot["run_directory"] / "assets" / f"{sha256}.glb")
    row = next(row for row in snapshot["by_registry"].values()
               if row["asset_sha256"] == sha256)
    if path.stat().st_size != row["asset_bytes"] or path.stat().st_size > 20_000_000:
        raise ValueError("Selected model asset exceeds byte limit")
    content = path.read_bytes()
    if (hashlib.sha256(content).hexdigest() != sha256 or
            not content.startswith(b"glTF") or len(content) < 12 or
            int.from_bytes(content[8:12], "little") != len(content)):
        raise ValueError("Selected model asset hash or format mismatch")
    return content
