"""Validate the immutable developer overlay inventory before export or apply."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import struct
from typing import Any, Callable


RUN_RE = re.compile(r"developer-overlay-v1-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
TILE_RE = re.compile(r"tiles/([0-9a-f]{64})\.b3dm\Z")
IDENTITY_FIELDS = (
    "map_run_id", "map_snapshot_id", "map_manifest_sha256",
    "unit_run_id", "unit_manifest_sha256", "model_run_id", "model_manifest_sha256",
    "enrichment_run_id", "enrichment_manifest_sha256",
)


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Non-finite overlay JSON value: {value}")


def _json(data: bytes) -> Any:
    return json.loads(data, parse_constant=_invalid_constant)


def _regular_file(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or "\\" in relative or "\0" in relative
            or any(not part or part.startswith(".") for part in relative.split("/"))):
        raise ValueError("Unsafe developer overlay path")
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("Developer overlay path is a symlink")
    if not path.is_file():
        raise ValueError(f"Missing developer overlay file: {relative}")
    return path


def _bbox(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in value)
            and -180 <= value[0] <= value[2] <= 180
            and -90 <= value[1] <= value[3] <= 90)


def _tile_registry_ids(path: Path, expected_count: int) -> set[str]:
    data = path.read_bytes()
    if len(data) < 40:
        raise ValueError("Developer overlay tile is truncated")
    magic, version, size, ft_json, ft_bin, bt_json, bt_bin = struct.unpack("<4s6I", data[:28])
    glb_offset = 28 + ft_json + ft_bin + bt_json + bt_bin
    if (magic != b"b3dm" or version != 1 or size != len(data)
            or glb_offset + 12 > size or ft_json == 0 or bt_json == 0):
        raise ValueError("Invalid developer overlay b3dm header")
    feature_table = _json(data[28:28 + ft_json])
    batch_offset = 28 + ft_json + ft_bin
    batch_table = _json(data[batch_offset:batch_offset + bt_json])
    ids = batch_table.get("registry_id") if isinstance(batch_table, dict) else None
    if (not isinstance(feature_table, dict)
            or type(feature_table.get("BATCH_LENGTH")) is not int
            or feature_table["BATCH_LENGTH"] != expected_count
            or not isinstance(ids, list) or len(ids) != expected_count
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("Developer overlay tile feature identities differ from inventory")
    glb_magic, glb_version, glb_size = struct.unpack("<4s2I", data[glb_offset:glb_offset + 12])
    if glb_magic != b"glTF" or glb_version != 2 or glb_size != size - glb_offset:
        raise ValueError("Developer overlay tile lacks its complete embedded GLB")
    return set(ids)


def _tileset_uris(value: Any) -> set[str]:
    if not isinstance(value, dict) or not isinstance(value.get("root"), dict):
        raise ValueError("Invalid developer overlay tileset")
    if (not isinstance(value.get("asset"), dict)
            or value["asset"].get("version") not in {"1.0", "1.1"}):
        raise ValueError("Invalid developer overlay tileset version")
    pending = [value["root"]]
    uris: set[str] = set()
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if not isinstance(node, dict) or visited > 20_000:
            raise ValueError("Invalid or oversized developer overlay tile tree")
        content = node.get("content")
        if content is not None:
            if (not isinstance(content, dict) or set(content) != {"uri"}
                    or not isinstance(content["uri"], str)
                    or TILE_RE.fullmatch(content["uri"]) is None):
                raise ValueError("Overlay tileset contains an unsafe or external content URI")
            uris.add(content["uri"])
        if "contents" in node:
            raise ValueError("Overlay multi-content tiles are not in the frozen contract")
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("Invalid overlay tile children")
        pending.extend(children)
    return uris


def _city_patch_files(root: Path, patch: dict[str, Any], features: list[dict[str, Any]],
                      checked: Callable[[str, Any, int], Path],
                      hashes: dict[str, str] | None) -> list[str]:
    """Pin selective replacement bytes and require both overview/detail coverage."""
    base_prefix = "assets/public-citywide-v12/"
    sha_fields = {
        "base_city_manifest_sha256", "base_city_tileset_sha256", "tileset_sha256",
        "region_inventory_sha256", "tile_inventory_sha256", "suppressed_registry_ids_sha256",
        "detail_proof_manifest_sha256", "overview_proof_manifest_sha256",
        "pair_proof_sha256",
    }
    if (not isinstance(patch, dict) or patch.get("all_visible_lods_covered") is not True
            or any(not isinstance(patch.get(key), str)
                   or SHA_RE.fullmatch(patch[key]) is None for key in sha_fields)):
        raise ValueError("Invalid or incomplete all-LOD developer city patch")
    assets = _json(_regular_file(root, "assets/manifest.json").read_bytes())
    if assets.get("city_tileset_url") != "/" + base_prefix + "tileset.json":
        raise ValueError("Developer city patch uses a different active base city")

    def base_file(name: Any, expected_sha: str | None = None) -> str:
        if not isinstance(name, str):
            raise ValueError("Invalid developer city patch source path")
        if name.startswith("/" + base_prefix):
            name = name[len(base_prefix) + 1:]
        elif name.startswith(base_prefix):
            name = name[len(base_prefix):]
        if "/" in name or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*", name):
            raise ValueError("Unsafe developer city patch source path")
        relative = base_prefix + name
        path = _regular_file(root, relative)
        if hashes is not None and relative not in hashes:
            raise ValueError("Developer city patch source is absent from release inventory")
        if expected_sha is not None:
            if not isinstance(expected_sha, str) or SHA_RE.fullmatch(expected_sha) is None:
                raise ValueError("Invalid developer city patch source SHA")
            with path.open("rb") as stream:
                actual = hashes[relative] if hashes is not None else hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected_sha:
                raise ValueError("Developer city patch source hash differs")
        return relative

    base_file("manifest.json", patch["base_city_manifest_sha256"])
    base_file("tileset.json", patch["base_city_tileset_sha256"])
    feature_by_id = {row["registry_id"]: row for row in features}
    suppressed_path = checked("city-patch/suppressed-registry-ids.json",
                              patch["suppressed_registry_ids_sha256"], 1_000_000)
    suppressed = _json(suppressed_path.read_bytes())
    if (not isinstance(suppressed, list) or not suppressed
            or any(not isinstance(value, str) or not value for value in suppressed)
            or suppressed != sorted(set(suppressed))
            or type(patch.get("suppressed_total")) is not int
            or len(suppressed) != patch["suppressed_total"]
            or not set(suppressed).issubset(feature_by_id)):
        raise ValueError("Invalid developer city suppression identities")
    suppressed_ids = set(suppressed)
    pair_path = checked("city-patch/pair-proof.jsonl", patch["pair_proof_sha256"], 1_000_000)
    pairs = [_json(line) for line in pair_path.read_bytes().splitlines()]
    if (type(patch.get("pair_proof_total")) is not int
            or patch["pair_proof_total"] != len(pairs) or len(pairs) != len(suppressed)):
        raise ValueError("Developer city patch pair proof count differs")
    paired_registry: set[str] = set()
    paired_city: set[str] = set()
    pair_order: list[tuple[str, str]] = []
    lake_ids: list[str] = []
    for row in pairs:
        if (not isinstance(row, dict) or set(row) != {
                "registry_id", "city_source_id", "geometry_source_id", "source_polygon_sha256"}
                or any(not isinstance(value, str) or not value for value in row.values())):
            raise ValueError("Invalid developer city patch pair proof row")
        feature = feature_by_id.get(row["registry_id"], {})
        geometry_source = feature.get("source_geometry_id") or ":".join(
            str(feature.get(key, "")) for key in (
                "source_group", "publisher_project_id", "publisher_building_id"))
        if (row["registry_id"] not in suppressed_ids or row["registry_id"] in paired_registry
                or row["city_source_id"] in paired_city
                or row["city_source_id"] != feature.get("city_feature_id_candidate")
                or row["source_polygon_sha256"] != feature.get("polygon_sha256")
                or SHA_RE.fullmatch(row["source_polygon_sha256"]) is None
                or row["geometry_source_id"] != geometry_source):
            raise ValueError("Developer city patch registry/city/source identity pair differs")
        paired_registry.add(row["registry_id"])
        paired_city.add(row["city_source_id"])
        pair_order.append((row["city_source_id"], row["registry_id"]))
        if geometry_source == "osm:way/1317814900@2":
            lake_ids.append(row["registry_id"])
    if pair_order != sorted(pair_order):
        raise ValueError("Developer city patch pair proof is not canonically ordered")
    region_inventory = checked("city-patch/region-inventory.jsonl",
                               patch["region_inventory_sha256"], 2_000_000)
    tile_inventory = checked("city-patch/tile-inventory.jsonl",
                             patch["tile_inventory_sha256"], 2_000_000)
    regions = [_json(line) for line in region_inventory.read_bytes().splitlines()]
    tiles = [_json(line) for line in tile_inventory.read_bytes().splitlines()]
    for rows, count_field in ((regions, "regions_total"), (tiles, "replacement_tiles_total")):
        if (type(patch.get(count_field)) is not int or patch[count_field] != len(rows)
                or not 0 < len(rows) <= 4096):
            raise ValueError("Developer city patch inventory count differs")
    known_paths: dict[str, tuple[str, int]] = {}
    source_paths: set[str] = set()
    logical_sources = set()
    documents = {}
    coverage = {"overview": set(), "detail": set()}
    for row, region in [(row, True) for row in regions] + [(row, False) for row in tiles]:
        keys = {"path", "sha256", "bytes", "source_sha256", "source_region" if region else "source_tile"}
        if not region:
            keys |= {"lod", "suppressed_registry_ids"}
        if not isinstance(row, dict) or set(row) != keys:
            raise ValueError("Invalid developer city patch inventory row")
        pattern = r"city-patch/regions/([0-9a-f]{64})\.json" if region else r"city-patch/tiles/([0-9a-f]{64})\.glb"
        match = re.fullmatch(pattern, row["path"]) if isinstance(row["path"], str) else None
        if (match is None or row["sha256"] != match[1]
                or type(row["bytes"]) is not int or not 0 < row["bytes"] <= 64_000_000):
            raise ValueError("Invalid developer city patch content address")
        source = base_file(row["source_region" if region else "source_tile"], row["source_sha256"])
        source_paths.add(source)
        if not region:
            if (not isinstance(row["lod"], str) or row["lod"] not in coverage
                    or not isinstance(row["suppressed_registry_ids"], list)
                    or any(not isinstance(value, str) for value in row["suppressed_registry_ids"])
                    or not row["suppressed_registry_ids"]
                    or row["suppressed_registry_ids"] != sorted(set(row["suppressed_registry_ids"]))
                    or not set(row["suppressed_registry_ids"]).issubset(suppressed_ids)):
                raise ValueError("Invalid developer city patch LOD suppression coverage")
            coverage[row["lod"]].update(row["suppressed_registry_ids"])
        logical = (source, "region" if region else row["lod"])
        if logical in logical_sources:
            raise ValueError("Duplicate developer city patch source/LOD")
        logical_sources.add(logical)
        descriptor = (row["sha256"], row["bytes"])
        if row["path"] in known_paths and known_paths[row["path"]] != descriptor:
            raise ValueError("Conflicting developer city patch blob descriptors")
        known_paths[row["path"]] = descriptor
        path = checked(row["path"], row["sha256"], 64_000_000)
        if path.stat().st_size != row["bytes"]:
            raise ValueError("Developer city patch blob size differs")
        if region:
            documents[row["path"]] = _json(path.read_bytes())
        else:
            with path.open("rb") as stream:
                header = stream.read(12)
            if (len(header) != 12 or struct.unpack("<4s2I", header) !=
                    (b"glTF", 2, row["bytes"])):
                raise ValueError("Developer city patch is not a complete GLB")
    if any(ids != suppressed_ids for ids in coverage.values()):
        raise ValueError("Developer city patch does not cover every suppressed building at both LODs")
    root_path = checked("city-patch/tileset.json", patch["tileset_sha256"], 5_000_000)
    documents["city-patch/tileset.json"] = _json(root_path.read_bytes())
    referenced: set[str] = set()
    base_referenced: set[str] = set()
    for name, document in documents.items():
        if not isinstance(document, dict) or not isinstance(document.get("root"), dict):
            raise ValueError("Invalid developer city patch tileset document")
        pending = [document["root"]]
        visited = 0
        while pending:
            node = pending.pop()
            visited += 1
            if not isinstance(node, dict) or visited > 20_000:
                raise ValueError("Invalid developer city patch tile tree")
            contents = node.get("contents", [node["content"]] if "content" in node else [])
            if not isinstance(contents, list):
                raise ValueError("Invalid developer city patch content list")
            for content in contents:
                uri = content.get("uri") if isinstance(content, dict) else None
                if (not isinstance(uri, str) or any(char in uri for char in (":", "?", "#", "%", "\\", "\0"))):
                    raise ValueError("Unsafe developer city patch URI")
                if uri.startswith("/" + base_prefix):
                    base_referenced.add(base_file(uri))
                else:
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), uri))
                    if uri.startswith("/") or resolved not in known_paths:
                        raise ValueError("Developer city patch references an unlisted blob")
                    referenced.add(resolved)
            children = node.get("children", [])
            if not isinstance(children, list):
                raise ValueError("Invalid developer city patch children")
            pending.extend(children)
    if referenced != set(known_paths):
        raise ValueError("Developer city patch inventory contains unreferenced blobs")
    if source_paths & base_referenced:
        raise ValueError("Developer city patch still references replaced original geometry")
    return lake_ids


def developer_overlay_files(root: Path, pointer: dict[str, Any],
                            hashes: dict[str, str] | None = None) -> list[str]:
    """Return only pinned derived files; raw evidence and caches are excluded."""
    run_id = pointer.get("overlay_run_id")
    manifest_sha = pointer.get("overlay_manifest_sha256")
    if (not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None
            or not isinstance(manifest_sha, str) or SHA_RE.fullmatch(manifest_sha) is None):
        raise ValueError("Invalid developer overlay pointer identity")
    prefix = f"data/property/developer_overlay/runs/{run_id}"
    relatives: list[str] = []

    def checked(name: str, expected_sha: Any, limit: int) -> Path:
        relative = f"{prefix}/{name}"
        path = _regular_file(root, relative)
        if path.stat().st_size > limit:
            raise ValueError(f"Oversized developer overlay file: {name}")
        digest = hashes.get(relative) if hashes is not None else hashlib.sha256(path.read_bytes()).hexdigest()
        if (not isinstance(expected_sha, str) or SHA_RE.fullmatch(expected_sha) is None
                or digest != expected_sha):
            raise ValueError(f"Developer overlay hash mismatch: {name}")
        if relative not in relatives:
            relatives.append(relative)
        return path

    manifest = _json(checked("manifest.json", manifest_sha, 1_000_000).read_bytes())
    if (not isinstance(manifest, dict)
            or manifest.get("schema_version") != "developer-overlay-snapshot/1"
            or manifest.get("run_id") != run_id
            or any(key not in pointer or manifest.get(key) != pointer[key] for key in IDENTITY_FIELDS)):
        raise ValueError("Developer overlay differs from its pinned experience")
    if any(value is not None and (not isinstance(value, str) or SHA_RE.fullmatch(value) is None)
           for key, value in manifest.items() if key.endswith("_sha256")):
        raise ValueError("Invalid developer overlay source evidence SHA")

    feature_path = checked("features.jsonl", manifest.get("features_sha256"), 20_000_000)
    features = [_json(line) for line in feature_path.read_bytes().splitlines()]
    feature_ids = [row.get("registry_id") if isinstance(row, dict) else None for row in features]
    if (type(manifest.get("features_total")) is not int
            or manifest["features_total"] != len(features) or not features
            or any(not isinstance(value, str) or not value for value in feature_ids)
            or len(set(feature_ids)) != len(feature_ids)):
        raise ValueError("Invalid or duplicate developer overlay feature identities")
    allowed_ids = set(feature_ids)

    inventory_path = checked("tile-inventory.jsonl", manifest.get("tile_inventory_sha256"), 2_000_000)
    inventory = [_json(line) for line in inventory_path.read_bytes().splitlines()]
    if (type(manifest.get("tiles_total")) is not int
            or manifest["tiles_total"] != len(inventory) or not 0 < len(inventory) <= 4096):
        raise ValueError("Developer overlay tile inventory count differs")
    per_path: dict[str, tuple[str, int, frozenset[str]]] = {}
    cells = set()
    tile_keys = {"path", "sha256", "bytes", "lod", "feature_count", "cell", "bbox_lonlat"}
    for row in inventory:
        if not isinstance(row, dict) or set(row) != tile_keys:
            raise ValueError("Invalid developer overlay tile descriptor")
        match = TILE_RE.fullmatch(row["path"]) if isinstance(row["path"], str) else None
        if (match is None or row["sha256"] != match[1]
                or type(row["bytes"]) is not int or not 0 < row["bytes"] <= 20_000_000
                or type(row["feature_count"]) is not int or not 0 < row["feature_count"] <= len(features)
                or not isinstance(row["lod"], str) or row["lod"] not in {"flat", "detail"}
                or not isinstance(row["cell"], list) or len(row["cell"]) != 2
                or any(type(value) is not int for value in row["cell"])
                or not _bbox(row["bbox_lonlat"])):
            raise ValueError("Invalid developer overlay tile inventory values")
        cell = (*row["cell"], row["lod"])
        if cell in cells:
            raise ValueError("Duplicate developer overlay cell/LOD")
        cells.add(cell)
        tile = checked(row["path"], row["sha256"], 20_000_000)
        if tile.stat().st_size != row["bytes"]:
            raise ValueError("Developer overlay tile size differs from inventory")
        ids = frozenset(_tile_registry_ids(tile, row["feature_count"]))
        if not ids.issubset(allowed_ids):
            raise ValueError("Developer overlay tile contains unknown registry identities")
        descriptor = (row["sha256"], row["bytes"], ids)
        if row["path"] in per_path and per_path[row["path"]] != descriptor:
            raise ValueError("Conflicting developer overlay tile references")
        per_path[row["path"]] = descriptor
    if (type(manifest.get("tile_bytes")) is not int
            or manifest["tile_bytes"] != sum(value[1] for value in per_path.values())):
        raise ValueError("Developer overlay unique tile byte count differs")
    tileset = _json(checked("tileset.json", manifest.get("tileset_sha256"), 5_000_000).read_bytes())
    if _tileset_uris(tileset) != set(per_path):
        raise ValueError("Developer overlay tileset and tile inventory differ")
    lake_ids = (_city_patch_files(root, manifest["city_patch"], features, checked, hashes)
                if manifest.get("city_patch") is not None else [])
    if manifest.get("lake_persistent") is True:
        model_prefix = f"data/property/developer_models/runs/{pointer['model_run_id']}"
        model_bytes = _regular_file(root, model_prefix + "/manifest.json").read_bytes()
        model_manifest = _json(model_bytes)
        rows_bytes = _regular_file(root, model_prefix + "/models.jsonl").read_bytes()
        models = [_json(line) for line in rows_bytes.splitlines()]
        expected_sha = manifest.get("lake_persistent_model_sha256")
        matching = [row for row in models if row.get("registry_id") in lake_ids]
        if (len(lake_ids) != 1 or len(matching) != 1
                or manifest.get("lake_persistent_policy") != "near_only_reuse_selected_model_instance"
                or not isinstance(expected_sha, str) or SHA_RE.fullmatch(expected_sha) is None
                or hashlib.sha256(model_bytes).hexdigest() != pointer["model_manifest_sha256"]
                or hashlib.sha256(rows_bytes).hexdigest() != model_manifest.get("models_sha256")
                or matching[0].get("asset_sha256") != expected_sha):
            raise ValueError("Persistent Lake geometry differs from its selected model pin")
    elif lake_ids:
        raise ValueError("Lake city suppression lacks its selected model pin")
    from scripts.runtime_overlay_styles import verify_overlay_styles
    verify_overlay_styles(root, feature_path.parent, pointer, manifest, features, inventory)
    return relatives
