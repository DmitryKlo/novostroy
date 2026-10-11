"""Read a hash-pinned, source-building geometry overlay for the active experience.

The tiles are visualization assets. Their apartment and project identity comes
from the already validated developer map; visual alignment never creates an
identity join or an observed building height.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.store import ROOT


BASE = ROOT / "data/property/developer_overlay"
RUN_RE = re.compile(r"developer-overlay-v1-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
TILE_RE = re.compile(r"tiles/([0-9a-f]{64})\.b3dm\Z")
PATCH_REGION_RE = re.compile(r"city-patch/regions/([0-9a-f]{64})\.json\Z")
PATCH_TILE_RE = re.compile(r"city-patch/tiles/([0-9a-f]{64})\.glb\Z")
BASE_CITY_NAME_RE = re.compile(
    r"(?:region_-?[0-9]+_-?[0-9]+\.tileset\.json|"
    r"massing_region_-?[0-9]+_-?[0-9]+\.glb|"
    r"cell_-?[0-9]+_-?[0-9]+\.glb)\Z")
BASE_CITY_ROOT = ROOT / "assets/public-citywide-v12"
MAX_MANIFEST_BYTES = 128_000
MAX_FEATURE_BYTES = 4_000_000
MAX_TILESET_BYTES = 4_000_000
MAX_INVENTORY_BYTES = 2_000_000
MAX_TILE_BYTES = 8_000_000
MAX_TILES = 4096
MAX_PATCH_BYTES = 20_000_000
MAX_PATCH_FILES = 512


def _safe_file(path: Path, limit: int) -> Path:
    if not path.is_relative_to(BASE):
        raise ValueError("Developer overlay path escapes its root")
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        if part in {"", ".", ".."} or part.startswith("."):
            raise ValueError("Unsafe developer overlay path")
        current /= part
        if current.is_symlink():
            raise ValueError("Developer overlay path contains a symlink")
    if not current.is_file() or current.stat().st_size > limit:
        raise ValueError("Developer overlay file is missing or too large")
    return current


def _sha(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    with _safe_file(path, limit).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _city_sha(name: str, limit: int) -> str:
    if BASE_CITY_NAME_RE.fullmatch(name) is None and name not in {"manifest.json", "tileset.json"}:
        raise ValueError("Invalid source city asset name")
    path = BASE_CITY_ROOT / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Pinned source city asset is missing or too large")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pin_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
        raise ValueError("Pinned experience manifest is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with _safe_file(path, limit).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid developer overlay row")
            rows.append(row)
    return rows


def _bbox(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4 and
            all(type(item) in {int, float} and math.isfinite(item) for item in value) and
            -180 <= value[0] < value[2] <= 180 and
            -90 <= value[1] < value[3] <= 90)


def _tile_uris(node: Any) -> list[str]:
    if not isinstance(node, dict):
        raise ValueError("Invalid developer overlay tileset node")
    result: list[str] = []
    content = node.get("content")
    if content is not None:
        if not isinstance(content, dict) or not isinstance(content.get("uri"), str):
            raise ValueError("Invalid developer overlay content URI")
        result.append(content["uri"])
    children = node.get("children", [])
    if not isinstance(children, list) or len(children) > MAX_TILES:
        raise ValueError("Invalid developer overlay tileset children")
    for child in children:
        result.extend(_tile_uris(child))
    return result


def _patch_refs(document: dict[str, Any], *, region: bool) -> tuple[set[str], set[str]]:
    """Resolve only same-run patch refs and the pinned public v12 city assets."""
    local: set[str] = set()
    base: set[str] = set()
    for uri in _tile_uris(document.get("root")):
        if uri.startswith("/assets/public-citywide-v12/"):
            name = uri.removeprefix("/assets/public-citywide-v12/")
            if BASE_CITY_NAME_RE.fullmatch(name) is None:
                raise ValueError("City patch refers to an unknown base asset")
            base.add(name)
        else:
            prefix = "../tiles/" if region else "tiles/"
            if uri.startswith(prefix):
                path = "city-patch/tiles/" + uri.removeprefix(prefix)
                if PATCH_TILE_RE.fullmatch(path) is None:
                    raise ValueError("City patch has an invalid tile reference")
            elif not region and uri.startswith("regions/"):
                path = "city-patch/regions/" + uri.removeprefix("regions/")
                if PATCH_REGION_RE.fullmatch(path) is None:
                    raise ValueError("City patch has an invalid region reference")
            else:
                raise ValueError("City patch contains an unpinned asset reference")
            local.add(path)
    return local, base


def _validated_city_patch(run: Path, manifest: dict[str, Any],
                          features: dict[str, dict[str, Any]],
                          model_assets: dict[str, str]) -> dict[str, Any] | None:
    patch = manifest.get("city_patch")
    if patch is None:
        return None
    if not isinstance(patch, dict) or patch.get("all_visible_lods_covered") is not True:
        raise ValueError("City patch has no complete visible-LOD proof")
    hash_keys = ("base_city_manifest_sha256", "base_city_tileset_sha256",
                 "tileset_sha256", "region_inventory_sha256", "tile_inventory_sha256",
                 "suppressed_registry_ids_sha256", "pair_proof_sha256",
                 "detail_proof_manifest_sha256",
                 "overview_proof_manifest_sha256")
    if any(not isinstance(patch.get(key), str) or SHA_RE.fullmatch(patch[key]) is None
           for key in hash_keys):
        raise ValueError("City patch has invalid evidence hashes")
    if (_city_sha("manifest.json", MAX_MANIFEST_BYTES) != patch["base_city_manifest_sha256"] or
            _city_sha("tileset.json", MAX_TILESET_BYTES) != patch["base_city_tileset_sha256"]):
        raise ValueError("City patch pins another public city generation")
    files = (("city-patch/tileset.json", "tileset_sha256", MAX_TILESET_BYTES),
             ("city-patch/region-inventory.jsonl", "region_inventory_sha256",
              MAX_INVENTORY_BYTES),
             ("city-patch/tile-inventory.jsonl", "tile_inventory_sha256",
              MAX_INVENTORY_BYTES),
             ("city-patch/suppressed-registry-ids.json", "suppressed_registry_ids_sha256",
              MAX_INVENTORY_BYTES),
             ("city-patch/pair-proof.jsonl", "pair_proof_sha256", MAX_INVENTORY_BYTES))
    if any(_sha(run / name, limit) != patch[key] for name, key, limit in files):
        raise ValueError("City patch bytes differ from manifest")
    suppressed = json.loads((run / "city-patch/suppressed-registry-ids.json").read_text())
    if (not isinstance(suppressed, list) or
            any(not isinstance(value, str) or value not in features for value in suppressed) or
            suppressed != sorted(set(suppressed)) or
            patch.get("suppressed_total") != len(suppressed) or not suppressed):
        raise ValueError("City patch suppressed building identities are invalid")
    pairs = _jsonl(run / "city-patch/pair-proof.jsonl", MAX_INVENTORY_BYTES)
    if patch.get("pair_proof_total") != len(pairs) or len(pairs) != len(suppressed):
        raise ValueError("City patch pair proof coverage differs")
    pair_order: list[tuple[str, str]] = []
    paired_registry_ids: set[str] = set()
    paired_city_ids: set[str] = set()
    lake_registry_ids: list[str] = []
    for pair in pairs:
        registry_id = pair.get("registry_id")
        city_id = pair.get("city_source_id")
        feature = features.get(registry_id) if isinstance(registry_id, str) else None
        expected_geometry_id = feature.get("source_geometry_id") if feature else None
        if expected_geometry_id is None and feature and feature.get("source_group") == "proplanirovki":
            expected_geometry_id = (f"proplanirovki:{feature.get('publisher_project_id')}:"
                                    f"{feature.get('publisher_building_id')}")
        if (feature is None or registry_id not in suppressed or
                not isinstance(city_id, str) or not city_id or len(city_id) > 128 or
                city_id != feature.get("city_feature_id_candidate") or
                pair.get("source_polygon_sha256") != feature["polygon_sha256"] or
                pair.get("geometry_source_id") != expected_geometry_id):
            raise ValueError("City patch pair identity differs from source feature")
        if expected_geometry_id == "osm:way/1317814900@2":
            if (feature.get("source_group") != "fsk" or
                    feature.get("source_geometry_basis") !=
                    "osm_exact_building_footprint_way_1317814900_v2"):
                raise ValueError("Lake city pair lacks exact OSM building evidence")
            lake_registry_ids.append(registry_id)
        pair_order.append((city_id, registry_id))
        paired_registry_ids.add(registry_id)
        paired_city_ids.add(city_id)
    if (pair_order != sorted(pair_order) or len(paired_registry_ids) != len(pairs) or
            len(paired_city_ids) != len(pairs) or paired_registry_ids != set(suppressed)):
        raise ValueError("City patch pair proof is duplicate or incomplete")
    if manifest.get("lake_persistent") is True:
        lake_sha = manifest.get("lake_persistent_model_sha256")
        if (len(lake_registry_ids) != 1 or
                manifest.get("lake_persistent_policy") != "near_only_reuse_selected_model_instance" or
                not isinstance(lake_sha, str) or SHA_RE.fullmatch(lake_sha) is None or
                model_assets.get(lake_registry_ids[0]) != lake_sha):
            raise ValueError("Persistent Lake footprint differs from selected model")
    elif lake_registry_ids:
        raise ValueError("Lake city suppression lacks its selected model pin")

    inventories: dict[str, dict[str, Any]] = {}
    lod_members: dict[str, set[str]] = {"overview": set(), "detail": set()}
    for name, pattern, expected_total, source_key in (
        ("city-patch/region-inventory.jsonl", PATCH_REGION_RE, "regions_total", "source_region"),
        ("city-patch/tile-inventory.jsonl", PATCH_TILE_RE, "replacement_tiles_total", "source_tile"),
    ):
        rows = _jsonl(run / name, MAX_INVENTORY_BYTES)
        if len(rows) > MAX_PATCH_FILES or patch.get(expected_total) != len(rows):
            raise ValueError("City patch asset inventory count differs")
        for row in rows:
            path = row.get("path")
            match = pattern.fullmatch(path) if isinstance(path, str) else None
            source_name = row.get(source_key)
            if (match is None or path in inventories or row.get("sha256") != match.group(1) or
                    type(row.get("bytes")) is not int or
                    not 0 < row["bytes"] <= MAX_PATCH_BYTES or
                    not isinstance(source_name, str) or
                    BASE_CITY_NAME_RE.fullmatch(source_name) is None or
                    not isinstance(row.get("source_sha256"), str) or
                    SHA_RE.fullmatch(row["source_sha256"]) is None):
                raise ValueError("Invalid city patch asset inventory")
            if (_sha(run / path, MAX_PATCH_BYTES) != row["sha256"] or
                    (run / path).stat().st_size != row["bytes"] or
                    _city_sha(source_name, MAX_PATCH_BYTES) != row["source_sha256"]):
                raise ValueError("City patch replacement or source bytes differ")
            if pattern is PATCH_TILE_RE and (row.get("lod") not in {"overview", "detail"} or
                                              not isinstance(row.get("suppressed_registry_ids"), list)):
                raise ValueError("Invalid city patch tile proof")
            if pattern is PATCH_TILE_RE:
                members = row["suppressed_registry_ids"]
                if (any(not isinstance(member, str) or member not in suppressed
                        for member in members) or members != sorted(set(members))):
                    raise ValueError("Invalid city patch suppressed tile members")
                lod_members[row["lod"]].update(members)
            inventories[path] = row
    if any(members != set(suppressed) for members in lod_members.values()):
        raise ValueError("City patch does not cover every suppressed building at both LODs")
    tileset = json.loads(_safe_file(run / "city-patch/tileset.json",
                                    MAX_TILESET_BYTES).read_text(encoding="utf-8"))
    if not isinstance(tileset, dict):
        raise ValueError("Invalid city patch tileset")
    referenced, base_refs = _patch_refs(tileset, region=False)
    for path in inventories:
        if PATCH_REGION_RE.fullmatch(path):
            region_doc = json.loads(_safe_file(run / path,
                                              MAX_TILESET_BYTES).read_text(encoding="utf-8"))
            if not isinstance(region_doc, dict):
                raise ValueError("Invalid city patch region tileset")
            region_refs, region_base_refs = _patch_refs(region_doc, region=True)
            referenced.update(region_refs)
            base_refs.update(region_base_refs)
    if referenced != set(inventories):
        raise ValueError("City patch tileset does not match its asset inventory")
    for name in base_refs:
        _city_sha(name, MAX_PATCH_BYTES)
    return {"manifest": patch, "tileset": tileset, "files": inventories,
            "suppressed_registry_ids": suppressed,
            "lake_registry_id": lake_registry_ids[0] if lake_registry_ids else None}


def _batch_style_ids(body: bytes) -> list[tuple[str, str | None]]:
    """Read only the bounded b3dm batch identity table, never its GLB payload."""
    if len(body) < 28 or body[:4] != b"b3dm":
        raise ValueError("Styled developer tile is not b3dm")
    version, length, feature_json, feature_binary, batch_json, batch_binary = struct.unpack_from(
        "<6I", body, 4)
    end = 28 + feature_json + feature_binary + batch_json + batch_binary
    if version != 1 or length != len(body) or end > len(body) or batch_json > 2_000_000:
        raise ValueError("Styled developer tile has an invalid header")
    feature = json.loads(body[28:28 + feature_json].decode("utf-8"))
    start = 28 + feature_json + feature_binary
    batch = json.loads(body[start:start + batch_json].decode("utf-8"))
    if not isinstance(feature, dict) or not isinstance(batch, dict):
        raise ValueError("Styled developer tile has invalid batch metadata")
    ids = batch.get("registry_id")
    styles = batch.get("facade_style_id", [None] * len(ids) if isinstance(ids, list) else [])
    if (not isinstance(ids, list) or not isinstance(styles, list) or
            len(ids) != len(styles) or feature.get("BATCH_LENGTH") != len(ids) or
            any(not isinstance(item, str) for item in ids) or
            any(item is not None and not isinstance(item, str) for item in styles)):
        raise ValueError("Styled developer tile has invalid feature identities")
    return list(zip(ids, styles, strict=True))


def _validated_facade_styles(
    run: Path, manifest: dict[str, Any], features: dict[str, dict[str, Any]],
    inventory_rows: list[dict[str, Any]],
    style_bindings: dict[str, tuple[str, str, str]],
    materials_sha256: str | None,
) -> dict[str, Any] | None:
    descriptor = manifest.get("facade_styles")
    marked = {registry_id: row for registry_id, row in features.items()
              if row.get("style_id") is not None}
    if any(row.get("style_id") is None and any(
        row.get(key) is not None for key in ("facade_style_basis", "facade_texture_sha256"))
           for row in features.values()):
        raise ValueError("Unbound developer facade feature metadata")
    if descriptor is None:
        if marked:
            raise ValueError("Developer facade markers lack a pinned style descriptor")
        return None
    if (not isinstance(descriptor, dict) or
            descriptor.get("basis") != "project_style_inference" or
            descriptor.get("label") != "Реконструкция" or
            descriptor.get("source_model_run_id") != manifest["model_run_id"] or
            descriptor.get("styles_materials_sha256") != materials_sha256 or
            descriptor.get("texture_level") != "near" or
            descriptor.get("far_untextured") is not True or
            descriptor.get("near_textures_embedded_in_detail_tiles") is not True or
            type(descriptor.get("styled_projects_total")) is not int or
            type(descriptor.get("styled_persistent_features_total")) is not int or
            descriptor["styled_persistent_features_total"] != len(marked) or not marked):
        raise ValueError("Developer facade style descriptor differs from selected models")
    for registry_id, row in marked.items():
        binding = style_bindings.get(registry_id)
        height = row.get("volume_height_m")
        if (binding is None or
                (row.get("publisher_project_id"), row.get("style_id"),
                 row.get("facade_texture_sha256")) != binding or
                row.get("facade_style_basis") != "project_style_inference" or
                type(height) not in {int, float} or not math.isfinite(height) or height <= 0):
            raise ValueError("Developer facade feature is not bound to its source project")
    if descriptor["styled_projects_total"] != len({row["publisher_project_id"]
                                                       for row in marked.values()}):
        raise ValueError("Developer facade project count differs")
    seen_detail: dict[str, str | None] = {}
    parsed: dict[str, list[tuple[str, str | None]]] = {}
    for item in inventory_rows:
        path = item["path"]
        if path not in parsed:
            parsed[path] = _batch_style_ids((run / path).read_bytes())
        pairs = parsed[path]
        if len(pairs) != item["feature_count"]:
            raise ValueError("Developer facade batch count differs from tile inventory")
        if item["lod"] == "flat":
            if any(style is not None for _, style in pairs):
                raise ValueError("Far developer tile carries inferred facade material")
        else:
            for registry_id, style in pairs:
                if registry_id in seen_detail or registry_id not in features:
                    raise ValueError("Developer detail tile repeats an unknown building")
                seen_detail[registry_id] = style
    if (set(seen_detail) != set(features) or
            any(style != (marked[registry_id]["style_id"] if registry_id in marked else None)
                for registry_id, style in seen_detail.items())):
        raise ValueError("Developer detail tile facade IDs differ from source features")
    return descriptor


@lru_cache(maxsize=3)
def _validated(run_id: str, expected_manifest_sha256: str,
               signatures: tuple[tuple[int, int], ...],
               pins: tuple[tuple[str, str], ...],
               point_bindings: tuple[tuple[str, str, str, str | None, str | None], ...],
               model_bindings: tuple[tuple[str, str], ...],
               style_bindings: tuple[tuple[str, str, str, str], ...],
               styles_materials_sha256: str | None) -> dict[str, Any]:
    del signatures  # Changed immutable files re-enter the validator.
    run = BASE / "runs" / run_id
    manifest_path = run / "manifest.json"
    if _sha(manifest_path, MAX_MANIFEST_BYTES) != expected_manifest_sha256:
        raise ValueError("Developer overlay manifest hash differs from pointer")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or
            manifest.get("schema_version") != "developer-overlay-snapshot/1" or
            manifest.get("run_id") != run_id or
            any(manifest.get(key) != value for key, value in pins)):
        raise ValueError("Developer overlay pins a different experience")
    required_hashes = ("features_sha256", "tileset_sha256", "tile_inventory_sha256")
    if any(not isinstance(manifest.get(key), str) or
           SHA_RE.fullmatch(manifest[key]) is None for key in required_hashes):
        raise ValueError("Invalid developer overlay file hashes")
    files = (("features.jsonl", "features_sha256", MAX_FEATURE_BYTES),
             ("tileset.json", "tileset_sha256", MAX_TILESET_BYTES),
             ("tile-inventory.jsonl", "tile_inventory_sha256", MAX_INVENTORY_BYTES))
    for filename, key, limit in files:
        if _sha(run / filename, limit) != manifest[key]:
            raise ValueError("Developer overlay file hash mismatch")

    expected_points = {registry_id: (point_id, source_group, project_id, building_id)
                       for registry_id, point_id, source_group, project_id, building_id
                       in point_bindings}
    features: dict[str, dict[str, Any]] = {}
    for row in _jsonl(run / "features.jsonl", MAX_FEATURE_BYTES):
        registry_id = row.get("registry_id")
        if (not isinstance(registry_id, str) or registry_id in features or
                expected_points.get(registry_id) !=
                (row.get("point_id"), row.get("source_group"),
                 row.get("publisher_project_id"), row.get("publisher_building_id")) or
                not _bbox(row.get("bounds_lonlat")) or
                not isinstance(row.get("polygon_sha256"), str) or
                SHA_RE.fullmatch(row["polygon_sha256"]) is None):
            raise ValueError("Developer overlay feature identity or bounds mismatch")
        features[registry_id] = row
    if (set(features) != set(expected_points) or
            manifest.get("features_total") != len(features)):
        raise ValueError("Developer overlay omits a mapped building")

    inventory: dict[str, dict[str, Any]] = {}
    cell_lods: set[tuple[int, int, str]] = set()
    tile_bytes = 0
    rows = _jsonl(run / "tile-inventory.jsonl", MAX_INVENTORY_BYTES)
    if len(rows) > MAX_TILES:
        raise ValueError("Developer overlay has too many tiles")
    for row in rows:
        path = row.get("path")
        match = TILE_RE.fullmatch(path) if isinstance(path, str) else None
        cell = row.get("cell")
        cell_lod = (cell[0], cell[1], row.get("lod")) if (
            isinstance(cell, list) and len(cell) == 2 and
            all(type(value) is int for value in cell)) else None
        if (match is None or cell_lod is None or cell_lod in cell_lods or
                row.get("sha256") != match.group(1) or
                type(row.get("bytes")) is not int or not 0 < row["bytes"] <= MAX_TILE_BYTES or
                type(row.get("feature_count")) is not int or row["feature_count"] <= 0 or
                row.get("lod") not in {"flat", "detail"} or
                not _bbox(row.get("bbox_lonlat"))):
            raise ValueError("Invalid developer overlay tile inventory")
        tile_path = run / path
        if tile_path.stat().st_size != row["bytes"] or _sha(tile_path, MAX_TILE_BYTES) != row["sha256"]:
            raise ValueError("Developer overlay tile bytes differ from inventory")
        cell_lods.add(cell_lod)
        prior = inventory.get(path)
        if prior is None:
            inventory[path] = row
            tile_bytes += row["bytes"]
        elif prior["sha256"] != row["sha256"] or prior["bytes"] != row["bytes"]:
            raise ValueError("Developer overlay reuses a tile path with different bytes")
    if (manifest.get("tiles_total") != len(rows) or
            manifest.get("tile_bytes") != tile_bytes):
        raise ValueError("Developer overlay tile inventory count mismatch")
    tileset = json.loads(_safe_file(run / "tileset.json", MAX_TILESET_BYTES).read_text(encoding="utf-8"))
    if not isinstance(tileset, dict) or set(_tile_uris(tileset.get("root"))) != set(inventory):
        raise ValueError("Developer overlay tileset references unknown or missing tiles")
    city_patch = _validated_city_patch(run, manifest, features, dict(model_bindings))
    facade_styles = _validated_facade_styles(
        run, manifest, features, rows,
        {registry_id: (project_id, style_id, near_sha)
         for registry_id, project_id, style_id, near_sha in style_bindings},
        styles_materials_sha256)
    return {"manifest": manifest, "features": features, "tiles": inventory,
            "tileset": tileset, "city_patch": city_patch,
            "facade_styles": facade_styles, "run_path": run}


def load_staged(run_id: str, map_run: dict[str, Any], menu: dict[str, Any],
                models: dict[str, Any], enrichment: dict[str, Any], *,
                expected_manifest_sha256: str) -> dict[str, Any]:
    if (not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None or
            not isinstance(expected_manifest_sha256, str) or
            SHA_RE.fullmatch(expected_manifest_sha256) is None):
        raise ValueError("Invalid developer overlay pointer identity")
    run = BASE / "runs" / run_id
    paths = ((run / "manifest.json", MAX_MANIFEST_BYTES),
             (run / "features.jsonl", MAX_FEATURE_BYTES),
             (run / "tileset.json", MAX_TILESET_BYTES),
             (run / "tile-inventory.jsonl", MAX_INVENTORY_BYTES))
    signatures = tuple((safe.stat().st_size, safe.stat().st_mtime_ns)
                       for path, limit in paths for safe in (_safe_file(path, limit),))
    tile_paths = {row.get("path") for row in _jsonl(run / "tile-inventory.jsonl",
                                                    MAX_INVENTORY_BYTES)}
    if any(not isinstance(path, str) or TILE_RE.fullmatch(path) is None
           for path in tile_paths):
        raise ValueError("Invalid developer overlay tile path")
    signatures += tuple((safe.stat().st_size, safe.stat().st_mtime_ns)
                        for path in sorted(tile_paths)
                        for safe in (_safe_file(run / path, MAX_TILE_BYTES),))
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("city_patch") is not None:
        patch_paths = ("city-patch/tileset.json", "city-patch/region-inventory.jsonl",
                       "city-patch/tile-inventory.jsonl",
                       "city-patch/suppressed-registry-ids.json",
                       "city-patch/pair-proof.jsonl")
        signatures += tuple((safe.stat().st_size, safe.stat().st_mtime_ns)
                            for path in patch_paths
                            for safe in (_safe_file(run / path, MAX_TILESET_BYTES),))
        for inventory_name, pattern in ((patch_paths[1], PATCH_REGION_RE),
                                        (patch_paths[2], PATCH_TILE_RE)):
            for row in _jsonl(run / inventory_name, MAX_INVENTORY_BYTES):
                path = row.get("path")
                if not isinstance(path, str) or pattern.fullmatch(path) is None:
                    raise ValueError("Invalid city patch inventory path")
                safe = _safe_file(run / path, MAX_PATCH_BYTES)
                signatures += ((safe.stat().st_size, safe.stat().st_mtime_ns),)
                source_name = row.get("source_region" if pattern is PATCH_REGION_RE
                                      else "source_tile")
                if not isinstance(source_name, str) or BASE_CITY_NAME_RE.fullmatch(source_name) is None:
                    raise ValueError("Invalid source city patch inventory path")
                source_file = BASE_CITY_ROOT / source_name
                if source_file.is_symlink() or not source_file.is_file():
                    raise ValueError("Pinned source city asset is missing")
                signatures += ((source_file.stat().st_size, source_file.stat().st_mtime_ns),)
        signatures += tuple((safe.stat().st_size, safe.stat().st_mtime_ns)
                            for filename in ("manifest.json", "tileset.json")
                            for safe in (BASE_CITY_ROOT / filename,))
    map_id = map_run["manifest"]["run_id"]
    unit_id = menu["manifest"]["run_id"]
    model_id = models["manifest"]["run_id"]
    enrichment_id = enrichment["manifest"]["run_id"]
    pins = (("map_run_id", map_id),
            ("map_snapshot_id", map_run["manifest"]["snapshot_id"]),
            ("map_manifest_sha256", _pin_sha(ROOT / "data/property/developer_map/runs" /
                                            map_id / "manifest.json")),
            ("unit_run_id", unit_id),
            ("unit_manifest_sha256", _pin_sha(ROOT / "data/property/developer_units/runs" /
                                             unit_id / "manifest.json")),
            ("model_run_id", model_id),
            ("model_manifest_sha256", _pin_sha(ROOT / "data/property/developer_models/runs" /
                                              model_id / "manifest.json")),
            ("enrichment_run_id", enrichment_id),
            ("enrichment_manifest_sha256", _pin_sha(
                ROOT / "data/property/developer_enrichment/runs" / enrichment_id /
                "manifest.json")))
    points = tuple(sorted((row["registry_id"], row["point_id"],
                           row["source_group"], row.get("publisher_project_id"),
                           row.get("publisher_building_id"))
                          for row in map_run["rows"] if row["map_eligible"]))
    model_assets = tuple(sorted((registry_id, row["asset_sha256"])
                                for registry_id, row in models["by_registry"].items()))
    style_bindings = tuple(sorted(
        (registry_id, row["source_project_id"], row["style_id"],
         models["styles"][row["style_id"]]["by_level"]["near"]["sha256"])
        for registry_id, row in models["by_registry"].items()
        if isinstance(row.get("style_id"), str)))
    return _validated(run_id, expected_manifest_sha256, signatures, pins, points,
                      model_assets, style_bindings,
                      models["manifest"].get("styles_materials_sha256"))


def tile_bytes(snapshot: dict[str, Any], sha256: str) -> bytes:
    if not isinstance(sha256, str) or SHA_RE.fullmatch(sha256) is None:
        raise FileNotFoundError("Unknown developer overlay tile")
    path = f"tiles/{sha256}.b3dm"
    row = snapshot["tiles"].get(path)
    if row is None:
        raise FileNotFoundError("Unknown developer overlay tile")
    file = snapshot["run_path"] / path
    if _sha(file, MAX_TILE_BYTES) != row["sha256"]:
        raise ValueError("Developer overlay tile changed after validation")
    return file.read_bytes()


def city_patch_bytes(snapshot: dict[str, Any], kind: str, sha256: str) -> bytes:
    if snapshot.get("city_patch") is None or not isinstance(sha256, str) or not SHA_RE.fullmatch(sha256):
        raise FileNotFoundError("Unknown city patch asset")
    if kind == "tile":
        path = f"city-patch/tiles/{sha256}.glb"
    elif kind == "region":
        path = f"city-patch/regions/{sha256}.json"
    else:
        raise FileNotFoundError("Unknown city patch asset kind")
    row = snapshot["city_patch"]["files"].get(path)
    if row is None:
        raise FileNotFoundError("Unknown city patch asset")
    file = snapshot["run_path"] / path
    if _sha(file, MAX_PATCH_BYTES) != row["sha256"]:
        raise ValueError("City patch asset changed after validation")
    return file.read_bytes()
