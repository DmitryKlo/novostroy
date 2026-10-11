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
STYLE_TEXTURE_RE = re.compile(r"styles/textures/([0-9a-f]{64})\.png\Z")
STYLE_LEVELS = frozenset({"near", "closer", "close"})


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


def _jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if _safe_file(path).stat().st_size > limit:
        raise ValueError("Developer style rows exceed byte limit")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid developer style row")
            rows.append(row)
    return rows


def _styles(directory: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = ("style_batch_manifest_sha256", "styles_materials_sha256",
            "styles_materials_total", "styles_texture_inventory_sha256",
            "styles_textures_total", "styled_models_total")
    if not any(key in manifest for key in keys):
        return {}
    if (any(key not in manifest for key in keys) or
            any(not isinstance(manifest[key], str) or SHA_RE.fullmatch(manifest[key]) is None
                for key in ("style_batch_manifest_sha256", "styles_materials_sha256",
                            "styles_texture_inventory_sha256")) or
            any(type(manifest[key]) is not int or manifest[key] < 0
                for key in ("styles_materials_total", "styles_textures_total",
                            "styled_models_total"))):
        raise ValueError("Invalid developer style manifest")
    material_path = directory / "styles/materials.jsonl"
    inventory_path = directory / "styles/texture-inventory.jsonl"
    if (_sha(material_path, 10_000_000) != manifest["styles_materials_sha256"] or
            _sha(inventory_path, 10_000_000) != manifest["styles_texture_inventory_sha256"]):
        raise ValueError("Developer style rows differ from manifest")
    materials = _jsonl(material_path, 10_000_000)
    inventory = _jsonl(inventory_path, 10_000_000)
    if (len(materials) != manifest["styles_materials_total"] or
            len(inventory) != manifest["styles_textures_total"]):
        raise ValueError("Developer style row count differs from manifest")
    texture_by_style_level: dict[tuple[str, str], dict[str, Any]] = {}
    for item in inventory:
        path = item.get("path")
        match = STYLE_TEXTURE_RE.fullmatch(path) if isinstance(path, str) else None
        style_id = item.get("style_id")
        level = item.get("level")
        if (match is None or item.get("sha256") != match.group(1) or
                type(item.get("bytes")) is not int or not 0 < item["bytes"] <= 8_000_000 or
                not isinstance(style_id, str) or not style_id or len(style_id) > 100 or
                not isinstance(level, str) or level not in STYLE_LEVELS):
            raise ValueError("Invalid developer style texture descriptor")
        key = (style_id, level)
        if key in texture_by_style_level:
            raise ValueError("Duplicate developer style texture descriptor")
        file = directory / path
        if file.stat().st_size != item["bytes"] or _sha(file, 8_000_000) != item["sha256"]:
            raise ValueError("Developer style texture bytes differ")
        texture_by_style_level[key] = item
    by_style: dict[str, dict[str, Any]] = {}
    used_texture_keys: set[tuple[str, str]] = set()
    for material in materials:
        style_id = material.get("style_id")
        project_id = material.get("publisher_project_id")
        lods = material.get("lods")
        if (not isinstance(style_id, str) or not style_id or len(style_id) > 100 or
                style_id in by_style or not isinstance(project_id, str) or not project_id or
                material.get("basis") != "project_style_inference" or
                material.get("label") != "Реконструкция" or
                material.get("geometry_scope") != "appearance_only_no_geometry_change" or
                not isinstance(material.get("source_evidence_sha256"), str) or
                SHA_RE.fullmatch(material["source_evidence_sha256"]) is None or
                not isinstance(lods, list) or len(lods) != 3):
            raise ValueError("Invalid developer project style material")
        levels: dict[str, dict[str, Any]] = {}
        for lod in lods:
            level = lod.get("level") if isinstance(lod, dict) else None
            if not isinstance(level, str) or level not in STYLE_LEVELS or level in levels:
                raise ValueError("Invalid developer style LOD")
            item = texture_by_style_level.get((style_id, level))
            if (item is None or lod.get("sha256") != item["sha256"] or
                    lod.get("asset_path") != item["path"].removeprefix("styles/") or
                    lod.get("asset_uri") != f"facade-style://sha256/{item['sha256']}" or
                    lod.get("mime_type") != "image/png" or
                    type(lod.get("width")) is not int or lod["width"] <= 0 or
                    type(lod.get("height")) is not int or lod["height"] <= 0):
                raise ValueError("Developer style LOD does not match its texture")
            levels[level] = lod
            used_texture_keys.add((style_id, level))
        by_style[style_id] = {**material, "by_level": levels}
    if used_texture_keys != set(texture_by_style_level):
        raise ValueError("Developer style texture inventory has unbound rows")
    return by_style


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
    return {"manifest": manifest, "rows": rows, "styles": _styles(directory, manifest),
            "run_directory": directory}


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
    candidate_manifest = _json(manifest_path, 100_000)
    if "styles_materials_sha256" in candidate_manifest:
        for name in ("styles/materials.jsonl", "styles/texture-inventory.jsonl"):
            path = _safe_file(directory / name)
            signatures += ((name, path.stat().st_mtime_ns, path.stat().st_size,
                            path.stat().st_ctime_ns, path.stat().st_ino),)
        for item in _jsonl(directory / "styles/texture-inventory.jsonl", 10_000_000):
            name = item.get("path")
            if not isinstance(name, str) or STYLE_TEXTURE_RE.fullmatch(name) is None:
                raise ValueError("Invalid developer style texture path")
            path = _safe_file(directory / name)
            signatures += ((name, path.stat().st_mtime_ns, path.stat().st_size,
                            path.stat().st_ctime_ns, path.stat().st_ino),)
    map_run_id = map_run["manifest"]["run_id"]
    map_sha = hashlib.sha256(
        (developer_map.BASE / "runs" / map_run_id / "manifest.json").read_bytes()).hexdigest()
    loaded = _validated(run_id, digest, signatures, map_run_id,
                        map_run["manifest"]["snapshot_id"], map_sha)
    by_registry: dict[str, dict[str, Any]] = {}
    asset_shas: set[str] = set()
    styled_count = 0
    for row in loaded["rows"]:
        map_row = map_run["by_registry"].get(row.get("registry_id"))
        asset_sha = row.get("asset_sha256")
        height = row.get("height_m")
        transform = row.get("model_transform")
        geometry_basis = row.get("source_geometry_basis")
        lake_osm = geometry_basis == "osm_exact_building_footprint_way_1317814900_v2"
        styled = row.get("texture_basis") == "generated_project_style_repeat_unoriented"
        style_id = row.get("style_id")
        material = (loaded["styles"].get(style_id)
                    if styled and isinstance(style_id, str) else None)
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
                                   row.get("texture_basis") not in
                                   {None, "", "generated_project_style_repeat_unoriented"})) or
                (styled and (material is None or map_row.get("source_group") != "proplanirovki" or
                             row.get("source_project_id") != map_row.get("publisher_project_id") or
                             material["publisher_project_id"] != row.get("source_project_id") or
                             row.get("facade_style_basis") != "project_style_inference" or
                             row.get("facade_style_evidence_sha256") !=
                             material["source_evidence_sha256"] or
                             row.get("texture_sha256") !=
                             material["by_level"]["closer"]["sha256"] or
                             row.get("model_quality") !=
                             "project_style_inferred_schematic_reconstruction")) or
                not isinstance(transform, list) or len(transform) != 16 or
                any(type(value) not in {int, float} or not math.isfinite(value)
                    for value in transform) or
                row["registry_id"] in by_registry or asset_sha in asset_shas):
            raise ValueError("Invalid selected developer model")
        by_registry[row["registry_id"]] = row
        asset_shas.add(asset_sha)
        styled_count += int(styled)
    if styled_count != loaded["manifest"].get("styled_models_total", 0):
        raise ValueError("Styled selected-model count differs from manifest")
    if styled_count:
        baseline_id = loaded["manifest"].get("base_model_run_id")
        baseline_sha = loaded["manifest"].get("base_model_manifest_sha256")
        if (not isinstance(baseline_id, str) or RUN_RE.fullmatch(baseline_id) is None or
                baseline_id == run_id or not isinstance(baseline_sha, str) or
                SHA_RE.fullmatch(baseline_sha) is None):
            raise ValueError("Styled models lack an immutable baseline")
        baseline_manifest_path = BASE / "runs" / baseline_id / "manifest.json"
        if _sha(baseline_manifest_path, 100_000) != baseline_sha:
            raise ValueError("Styled model baseline manifest differs")
        if _json(baseline_manifest_path, 100_000).get("styled_models_total", 0) != 0:
            raise ValueError("Styled model baseline must be unstyled")
        baseline = load_staged(baseline_id, map_run, expected_manifest_sha256=baseline_sha)
        if set(baseline["by_registry"]) != set(by_registry):
            raise ValueError("Styled model changes the selected building set")
        unchanged = ("model_transform", "height_m", "height_basis",
                     "source_total_floors", "source_section_total_floors",
                     "source_geometry_basis", "source_geometry_evidence_sha256",
                     "source_observed_at", "depiction", "render_policy",
                     "triangles", "courtyards")
        for registry_id, row in by_registry.items():
            prior = baseline["by_registry"][registry_id]
            if any(row.get(name) != prior.get(name) for name in unchanged):
                raise ValueError("Styled model changes source geometry or height")
    return {"manifest": loaded["manifest"], "by_registry": by_registry,
            "styles": loaded["styles"],
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
                "texture_basis": None, "facade_style_basis": None}
    lake_osm = row["source_geometry_basis"] == "osm_exact_building_footprint_way_1317814900_v2"
    return {**identity, "status": "ready", "depiction": "schematic_massing",
            "label": "реконструкция", "height_m": row["height_m"],
            "height_basis": row["height_basis"],
            "geometry_basis": "source_building_footprint",
            "model_transform": row["model_transform"],
            "asset_url": f"/v1/developer-model-assets/{row['asset_sha256']}",
            "asset_sha256": row["asset_sha256"],
            "geometry_attribution": row.get("attribution") if lake_osm else None,
            "photo_facade_reconstruction": row.get("photo_facade_reconstruction") is True,
            "texture_basis": row.get("texture_basis"),
            "facade_style_basis": row.get("facade_style_basis")}


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
