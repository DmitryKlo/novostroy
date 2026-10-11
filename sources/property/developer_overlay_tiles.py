"""Stage source-backed developer footprints as batched, immutable 3D Tiles.

This module does not change an active pointer. City overlap is a render policy,
never an identity join: only exact source corpus IDs select the geometry/height.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Any

from shapely.geometry import Point, Polygon, shape

from backend import developer_map as map_reader
from backend import developer_models as model_reader
from backend import developer_units as unit_reader
from backend import developer_enrichment as enrichment_reader
from pipeline.build_tiles import enu_transform, region
from sources.models.geometry import BASE_HEIGHT_M, ModelMesh
from sources.property.developer_map import (
    ROOT, _canonical, _checked_delivery, _checked_file, _file_sha,
    _fsync_dir, _rows, _sha, _write_private,
)
from sources.property.developer_models import _geometry
from sources.property.developer_overlay_city_patch import assemble as assemble_city_patch
from sources.property.developer_overlay_facades import styled_batched_tile


OUTPUT = ROOT / "data/property/developer_overlay"
RULES = "developer-source-footprint-batched-v1"
CELL_DEGREES = 0.08
MAX_FEATURES_PER_TILE = 256
MAX_TILE_BYTES = 10_000_000
LAKE_REGISTRY_ID = "registry-b1feab9d36bced349dfa4b1d"


def _json_bytes(value: Any) -> bytes:
    return _canonical(value) + b"\n"


def _glb_with_batch_ids(glb: bytes, batch_ids: list[int]) -> bytes:
    """Add legacy 3D Tiles _BATCHID to each vertex of a bounded GLB."""
    if len(glb) < 28 or glb[:4] != b"glTF":
        raise ValueError("Invalid GLB input")
    version, length = struct.unpack_from("<II", glb, 4)
    if version != 2 or length != len(glb):
        raise ValueError("GLB header mismatch")
    json_length, json_type = struct.unpack_from("<I4s", glb, 12)
    if json_type != b"JSON":
        raise ValueError("GLB JSON chunk missing")
    document = json.loads(glb[20:20 + json_length])
    binary_at = 20 + json_length
    binary_length, binary_type = struct.unpack_from("<I4s", glb, binary_at)
    if binary_type != b"BIN\x00":
        raise ValueError("GLB binary chunk missing")
    binary = bytearray(glb[binary_at + 8:binary_at + 8 + binary_length])
    vertex_count = document["accessors"][0]["count"]
    if len(batch_ids) != vertex_count or not batch_ids or max(batch_ids) >= 65536:
        raise ValueError("Batch IDs do not cover exactly the GLB vertices")
    if len(binary) % 4:
        binary.extend(b"\x00" * (-len(binary) % 4))
    offset = len(binary)
    binary.extend(struct.pack("<" + "H" * len(batch_ids), *batch_ids))
    binary.extend(b"\x00" * (-len(binary) % 4))
    view_id = len(document["bufferViews"])
    accessor_id = len(document["accessors"])
    document["bufferViews"].append({"buffer": 0, "byteOffset": offset,
                                     "byteLength": len(batch_ids) * 2, "target": 34962})
    document["accessors"].append({"bufferView": view_id, "componentType": 5123,
                                  "count": len(batch_ids), "type": "SCALAR",
                                  "min": [min(batch_ids)], "max": [max(batch_ids)]})
    for primitive in document["meshes"][0]["primitives"]:
        primitive["attributes"]["_BATCHID"] = accessor_id
    document["buffers"][0]["byteLength"] = len(binary)
    encoded = _canonical(document)
    encoded += b" " * (-len(encoded) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(binary))
            + struct.pack("<I4s", len(encoded), b"JSON") + encoded
            + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def batched_tile(mesh: ModelMesh, registry_ids: list[str],
                 modes: list[str], heights: list[float | None],
                 batch_ids: list[int]) -> bytes:
    """3D Tiles 1.0 b3dm with a styleable batch-table registry ID."""
    if not registry_ids or not (len(registry_ids) == len(modes) == len(heights)):
        raise ValueError("Invalid feature table")
    if len(registry_ids) > MAX_FEATURES_PER_TILE or len(set(registry_ids)) != len(registry_ids):
        raise ValueError("Tile has duplicate or excessive features")
    if set(batch_ids) != set(range(len(registry_ids))):
        raise ValueError("Batch IDs do not match feature table")
    glb = _glb_with_batch_ids(mesh.to_glb(), batch_ids)
    feature_table = _canonical({"BATCH_LENGTH": len(registry_ids)})
    feature_table += b" " * (-(28 + len(feature_table)) % 8)
    batch_table = _canonical({"registry_id": registry_ids,
                              "render_mode": modes, "height_m": heights})
    batch_table += b" " * (-(28 + len(feature_table) + len(batch_table)) % 8)
    total = 28 + len(feature_table) + len(batch_table) + len(glb)
    if total > MAX_TILE_BYTES:
        raise ValueError("Tile exceeds bounded byte size")
    return (struct.pack("<4sIIIIII", b"b3dm", 1, total, len(feature_table), 0,
                        len(batch_table), 0) + feature_table + batch_table + glb)


def _source_corpora(path: Path, needed: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _rows(path):
        if row.get("source_group") != "proplanirovki" or row.get("record_type") != "corpus":
            continue
        key = row.get("publisher_project_id"), row.get("publisher_building_id")
        if key not in needed:
            continue
        if key in found:
            raise ValueError("Repeated exact publisher corpus")
        found[key] = row
    if set(found) != needed:
        raise ValueError("Missing exact publisher corpus")
    return found


def _alignment(path: Path, expected_sha: str,
               eligible: set[str]) -> dict[str, dict[str, Any]]:
    if path.is_symlink() or _file_sha(path) != expected_sha:
        raise ValueError("Alignment evidence SHA mismatch")
    result = {row["registry_id"]: row for row in _rows(path)}
    if set(result) != eligible:
        raise ValueError("Alignment audit must cover every Proplanirovki polygon")
    return result


def _cell(polygon: Polygon) -> tuple[int, int]:
    point = polygon.representative_point()
    return math.floor(point.x / CELL_DEGREES), math.floor(point.y / CELL_DEGREES)


def _tile_features(items: list[dict[str, Any]], *, detail: bool,
                   origin_lon: float, origin_lat: float,
                   styles: dict[str, dict[str, Any]] | None = None) -> bytes:
    if detail and styles and any(item["volume_height_m"] and
                                 item["publisher_project_id"] in styles
                                 for item in items):
        return styled_batched_tile(items, origin_lon, origin_lat, styles)
    mesh = ModelMesh(origin_lon, origin_lat, None)
    ids: list[str] = []
    modes: list[str] = []
    heights: list[float | None] = []
    batches: list[int] = []
    for item in items:
        before = len(mesh.positions) // 3
        # Never share vertices across two source identities: batch assignment
        # would otherwise be ambiguous even when polygon corners coincide.
        mesh.vertices.clear()
        height = item["volume_height_m"] if detail else None
        mesh.add(item["geometry"], top_height_m=(BASE_HEIGHT_M + height if height else None))
        after = len(mesh.positions) // 3
        batches.extend([len(ids)] * (after - before))
        ids.append(item["registry_id"])
        modes.append("source_footprint_schematic_volume" if height else "source_footprint_flat")
        heights.append(height)
    return batched_tile(mesh, ids, modes, heights, batches)


def build(delivery_manifest: Path, alignment_path: Path, alignment_sha256: str,
          map_run_id: str, unit_run_id: str, model_run_id: str,
          enrichment_run_id: str, lake_osm_path: Path, *,
          detail_patch_manifest: Path | None = None,
          overview_patch_manifest: Path | None = None,
          include_lake_in_patch: bool = False,
          output_root: Path = OUTPUT) -> dict[str, Any]:
    """Build source footprints; promote reviewed volumes only with an all-LOD patch."""
    if (detail_patch_manifest is None) != (overview_patch_manifest is None):
        raise ValueError("City detail and overview proof must be supplied together")
    if include_lake_in_patch and detail_patch_manifest is None:
        raise ValueError("Lake persistence requires exact all-LOD city patch proof")
    patch_requested = detail_patch_manifest is not None
    delivery, _, delivery_sha = _checked_delivery(delivery_manifest)
    corpus_path = _checked_file(delivery_manifest.parent, delivery["files"], "projects_corpora")
    map_run = map_reader.load_staged(map_run_id)
    unit_run = unit_reader.load_staged(unit_run_id, map_run)
    model_run = model_reader.load_staged(model_run_id, map_run)
    model_manifest = model_run["manifest"]
    style_materials_sha = model_manifest.get("styles_materials_sha256")
    styles: dict[str, dict[str, Any]] = {}
    if model_manifest.get("styles_materials_total", 0):
        style_root = model_reader.BASE / "runs" / model_run_id / "styles"
        material_path = style_root / "materials.jsonl"
        if material_path.is_symlink() or _file_sha(material_path) != style_materials_sha:
            raise ValueError("Selected-model project style manifest mismatch")
        for material in _rows(material_path):
            project_id = material["publisher_project_id"]
            near = next(item for item in material["lods"] if item["level"] == "near")
            png_path = style_root / near["asset_path"]
            if (png_path.is_symlink() or _file_sha(png_path) != near["sha256"] or
                    project_id in styles):
                raise ValueError("Unpinned or duplicated project facade material")
            styles[project_id] = {"style_id": material["style_id"],
                                  "png": png_path.read_bytes(),
                                  "sha256": near["sha256"]}
    enrichment_sha = _file_sha(enrichment_reader.BASE / "runs" / enrichment_run_id / "manifest.json")
    enrichment_reader.load_staged(enrichment_run_id, map_run, unit_run, model_run,
                                  expected_manifest_sha256=enrichment_sha)
    if model_run["manifest"].get("menu_run_id") != unit_run_id:
        raise ValueError("Model/menu pin mismatch")
    map_sha = _file_sha(map_reader.BASE / "runs" / map_run_id / "manifest.json")
    unit_sha = _file_sha(unit_reader.BASE / "runs" / unit_run_id / "manifest.json")
    model_sha = _file_sha(model_reader.BASE / "runs" / model_run_id / "manifest.json")
    selected = [row for row in map_run["by_registry"].values()
                if row.get("source_group") == "proplanirovki" and row.get("map_eligible")]
    needed = {(row["publisher_project_id"], row["publisher_building_id"]) for row in selected}
    corpora = _source_corpora(corpus_path, needed)
    alignments = _alignment(alignment_path, alignment_sha256,
                            {row["registry_id"] for row in selected})
    reviewed_ids = {row["registry_id"] for row in selected
                    if (alignments[row["registry_id"]]["alignment_status"] ==
                        "strong_mutual_candidate" and
                        row["registry_id"] in model_run["by_registry"])}
    city_patch: dict[str, Any] | None = None
    city_patch_files: dict[str, bytes] = {}
    if len(reviewed_ids) != 57:
        raise ValueError("Pinned visual-alignment review is not 57 modeled corpora")
    features: list[dict[str, Any]] = []
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for point in sorted(selected, key=lambda row: row["registry_id"]):
        registry_id = point["registry_id"]
        key = point["publisher_project_id"], point["publisher_building_id"]
        corpus = corpora[key]
        polygon = _geometry(corpus, point)
        evidence = alignments[registry_id]
        polygon_sha = _sha(_canonical(corpus["source_geometry"]))
        if (evidence.get("publisher_project_id") != key[0] or
                evidence.get("publisher_building_id") != key[1] or
                evidence.get("polygon_sha256") != polygon_sha or
                evidence.get("point_id") != point["point_id"]):
            raise ValueError("Source geometry/alignment audit identity mismatch")
        model = model_run["by_registry"].get(registry_id)
        status = evidence["alignment_status"]
        if status not in {"no_city_footprint_bbox_overlap", "strong_mutual_candidate",
                          "nearby_or_partial_overlap"}:
            raise ValueError("Unexpected alignment status")
        if bool(evidence.get("model")) != bool(model):
            raise ValueError("Audit model membership differs from pinned selected models")
        # A strong geometry-only visual match may become a schematic volume only
        # when an immutable city patch suppresses its background at *both* LODs.
        # The original source polygon/identity/height remain unchanged.
        volume_height = (float(model["height_m"]) if model and
                         (status == "no_city_footprint_bbox_overlap" or
                          (status == "strong_mutual_candidate" and patch_requested))
                         else None)
        counts[status] += 1
        if volume_height is not None:
            counts["persistent_volumes"] += 1
        source = {"registry_id": registry_id, "point_id": point["point_id"],
                  "source_group": "proplanirovki",
                  "publisher_project_id": key[0], "publisher_building_id": key[1],
                  "source_geometry_id": "proplanirovki:" + key[0] + ":" + key[1],
                  "polygon_sha256": polygon_sha,
                  "bounds_lonlat": list(polygon.bounds),
                  "alignment_status": status,
                  "alignment_basis": ("no_overture_bbox_overlap" if status ==
                                      "no_city_footprint_bbox_overlap" else
                                      "geometry_only_inferred_visual_alignment" if status ==
                                      "strong_mutual_candidate" else "ambiguous_partial_overlap"),
                  "city_feature_id_candidate": (evidence.get("best_city_match") or {}).get("city_feature_id"),
                  "volume_height_m": volume_height,
                  "height_basis": model.get("height_basis") if model else None,
                  "render_policy": "persistent_volume" if volume_height else "persistent_flat",
                  "source_geometry_basis": corpus["geometry_basis"],
                  "source_observed_at": corpus["observed_at"]}
        features.append(source)
        grouped[_cell(polygon)].append({**source, "geometry": polygon})
    if len(features) != 1026 or counts["persistent_volumes"] != (258 if patch_requested else 201):
        raise ValueError("Unexpected pinned source/volume coverage")

    # The Lake's exact OSM footprint is source-backed too. Its textured volume
    # becomes a persistent near instance only with an exact all-LOD city patch.
    lake = map_run["by_registry"].get(LAKE_REGISTRY_ID)
    if lake is None or not lake.get("map_eligible") or LAKE_REGISTRY_ID not in model_run["by_registry"]:
        raise ValueError("The Lake selected source footprint is missing")
    if (lake_osm_path.is_symlink() or _file_sha(lake_osm_path) !=
            model_run["manifest"].get("lake_osm_sha256")):
        raise ValueError("The Lake OSM footprint differs from selected model evidence")
    lake_source = json.loads(lake_osm_path.read_text(encoding="utf-8"))
    lake_way = lake_source.get("osm_object") or {}
    nodes = lake_way.get("closed_ring_nodes") or []
    if (lake_way.get("osm_id") != 1317814900 or lake_way.get("osm_version") != 2 or
            lake_way.get("tags", {}).get("addr:housenumber") != "20А" or
            len(nodes) < 4 or nodes[0] != nodes[-1]):
        raise ValueError("The Lake exact building way is unverified")
    lake_polygon = Polygon([(node["longitude"], node["latitude"]) for node in nodes])
    if not lake_polygon.is_valid or not lake_polygon.covers(Point(lake["location"])):
        raise ValueError("The Lake exact footprint does not contain its map point")
    features.append({"registry_id": LAKE_REGISTRY_ID, "point_id": lake["point_id"],
                     "source_group": "fsk", "publisher_project_id": lake["publisher_project_id"],
                     "publisher_building_id": lake["publisher_building_id"],
                     "source_geometry_id": "osm:way/1317814900@2",
                     "polygon_sha256": _sha(_canonical(lake_polygon.__geo_interface__)),
                     "bounds_lonlat": list(lake_polygon.bounds),
                     "alignment_status": "same_osm_way_as_background_pending_patch",
                     "alignment_basis": "exact_osm_way_1317814900_v2",
                     "city_feature_id_candidate": None, "volume_height_m": None,
                     "height_basis": model_run["by_registry"][LAKE_REGISTRY_ID]["height_basis"],
                     "render_policy": "selected_textured_only_until_exact_city_patch",
                     "source_geometry_basis": "osm_exact_building_footprint_way_1317814900_v2",
                     "source_observed_at": model_run["by_registry"][LAKE_REGISTRY_ID]["source_observed_at"]})
    grouped[_cell(lake_polygon)].append({**features[-1], "geometry": lake_polygon})

    if detail_patch_manifest is not None and overview_patch_manifest is not None:
        patch_ids = reviewed_ids | ({LAKE_REGISTRY_ID} if include_lake_in_patch else set())
        by_registry = {row["registry_id"]: row for row in features}
        reviewed_geometry = {}
        for registry_id in patch_ids:
            feature = by_registry[registry_id]
            reviewed_geometry[registry_id] = {
                "source_group": feature["source_group"],
                "publisher_project_id": feature["publisher_project_id"],
                "publisher_building_id": feature["publisher_building_id"],
                "source_polygon_sha256": feature["polygon_sha256"],
            }
        city_patch, city_patch_files = assemble_city_patch(
            detail_patch_manifest, overview_patch_manifest, patch_ids,
            reviewed_geometry)
        if city_patch["suppressed_total"] != (58 if include_lake_in_patch else 57):
            raise ValueError("City patch target count differs from render policy")
        if include_lake_in_patch:
            pair_rows = [json.loads(line) for line in
                         city_patch_files["city-patch/pair-proof.jsonl"].splitlines()]
            lake_pairs = [row for row in pair_rows
                          if row["registry_id"] == LAKE_REGISTRY_ID]
            if (len(lake_pairs) != 1 or
                    lake_pairs[0]["geometry_source_id"] != "osm:way/1317814900@2"):
                raise ValueError("Lake city patch lacks its exact reviewed OSM identity")
            lake_feature = by_registry[LAKE_REGISTRY_ID]
            lake_feature["city_feature_id_candidate"] = lake_pairs[0]["city_source_id"]
            lake_feature["alignment_status"] = "exact_osm_way_background_patched"
            lake_feature["render_policy"] = "near_textured_persistent_reuse_selected_model"

    for feature in features:
        material = styles.get(feature["publisher_project_id"])
        if material is not None and feature["volume_height_m"] is not None:
            feature["style_id"] = material["style_id"]
            feature["facade_style_basis"] = "project_style_inference"
            feature["facade_texture_sha256"] = material["sha256"]
    for items in grouped.values():
        for item in items:
            material = styles.get(item["publisher_project_id"])
            if material is not None and item["volume_height_m"] is not None:
                item["style_id"] = material["style_id"]
                item["facade_style_basis"] = "project_style_inference"
                item["facade_texture_sha256"] = material["sha256"]

    blobs: dict[str, bytes] = {}
    children: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    for cell, items in sorted(grouped.items()):
        if len(items) > MAX_FEATURES_PER_TILE:
            raise ValueError("Spatial cell exceeds feature limit")
        west = min(item["geometry"].bounds[0] for item in items)
        south = min(item["geometry"].bounds[1] for item in items)
        east = max(item["geometry"].bounds[2] for item in items)
        north = max(item["geometry"].bounds[3] for item in items)
        lon, lat = (west + east) / 2, (south + north) / 2
        ordered = sorted(items, key=lambda item: item["registry_id"])
        flat = _tile_features(ordered, detail=False, origin_lon=lon, origin_lat=lat)
        detailed = _tile_features(ordered, detail=True, origin_lon=lon,
                                  origin_lat=lat, styles=styles)
        flat_sha, detail_sha = _sha(flat), _sha(detailed)
        blobs[flat_sha], blobs[detail_sha] = flat, detailed
        top = max([item["volume_height_m"] or 0 for item in items] + [BASE_HEIGHT_M]) + 2
        bbox = region(west, south, east, north, top)
        children.append({"boundingVolume": {"region": bbox}, "geometricError": 64,
                         "refine": "REPLACE", "transform": enu_transform(lon, lat),
                         "content": {"uri": f"tiles/{flat_sha}.b3dm"},
                         "children": [{"boundingVolume": {"region": bbox},
                                       "geometricError": 0,
                                       "content": {"uri": f"tiles/{detail_sha}.b3dm"}}]})
        for lod, digest, blob in (("flat", flat_sha, flat), ("detail", detail_sha, detailed)):
            inventory.append({"path": f"tiles/{digest}.b3dm", "sha256": digest,
                              "bytes": len(blob), "lod": lod, "feature_count": len(items),
                              "cell": list(cell), "bbox_lonlat": [west, south, east, north]})
    bounds = [min(row["bounds_lonlat"][0] for row in features if row["bounds_lonlat"]),
              min(row["bounds_lonlat"][1] for row in features if row["bounds_lonlat"]),
              max(row["bounds_lonlat"][2] for row in features if row["bounds_lonlat"]),
              max(row["bounds_lonlat"][3] for row in features if row["bounds_lonlat"])]
    tileset = {"asset": {"version": "1.0", "tilesetVersion": RULES},
               "geometricError": 2048,
               "root": {"boundingVolume": {"region": region(*bounds, 1502)},
                        "geometricError": 1024, "refine": "REPLACE", "children": children},
               "extras": {"depiction": "source_footprints_and_schematic_source_floor_massing",
                          "height_measurement": False,
                          "alignment_identity_claim": False}}
    features_bytes = b"".join(_json_bytes(row) for row in features)
    inventory_bytes = b"".join(_json_bytes(row) for row in inventory)
    tileset_bytes = _json_bytes(tileset)
    pins = {"delivery_manifest_sha256": delivery_sha,
            "projects_corpora_sha256": _file_sha(corpus_path),
            "alignment_audit_sha256": alignment_sha256,
            "map_run_id": map_run_id, "map_snapshot_id": map_run["manifest"]["snapshot_id"],
            "map_manifest_sha256": map_sha,
            "unit_run_id": unit_run_id, "unit_manifest_sha256": unit_sha,
            "model_run_id": model_run_id, "model_manifest_sha256": model_sha}
    pins["enrichment_run_id"] = enrichment_run_id
    pins["enrichment_manifest_sha256"] = enrichment_sha
    pins["lake_osm_sha256"] = _file_sha(lake_osm_path)
    styled_persistent = [row for row in features if row["volume_height_m"] and
                         row["publisher_project_id"] in styles]
    facade_styles = ({"basis": "project_style_inference", "label": "Реконструкция",
                      "source_model_run_id": model_run_id,
                      "styles_materials_sha256": style_materials_sha,
                      "texture_level": "near",
                      "styled_projects_total": len({row["publisher_project_id"]
                                                   for row in styled_persistent}),
                      "styled_persistent_features_total": len(styled_persistent),
                      "far_untextured": True,
                      "near_textures_embedded_in_detail_tiles": True}
                     if styled_persistent else None)
    digest = _sha(_canonical({"rules": RULES, "pins": pins,
                             "features_sha256": _sha(features_bytes),
                             "tileset_sha256": _sha(tileset_bytes),
                             "inventory_sha256": _sha(inventory_bytes),
                             "city_patch": city_patch,
                             "facade_styles": facade_styles}))
    run_id = "developer-overlay-v1-" + digest[:16]
    manifest = {"schema_version": "developer-overlay-snapshot/1", "run_id": run_id,
                "rules_version": RULES, **pins,
                "features_sha256": _sha(features_bytes), "features_total": len(features),
                "tileset_sha256": _sha(tileset_bytes),
                "tile_inventory_sha256": _sha(inventory_bytes),
                "tiles_total": len(inventory), "tile_bytes": sum(len(b) for b in blobs.values()),
                "volume_policy": ("no_overlap_plus_reviewed_all_lod_city_patch" if city_patch else
                                  "no_background_overlap_only_pending_reviewed_city_patch"),
                "counts": dict(sorted(counts.items())),
                "lake_persistent": include_lake_in_patch,
                "lake_persistent_model_sha256": (model_run["by_registry"][LAKE_REGISTRY_ID]["asset_sha256"]
                                                     if include_lake_in_patch else None),
                "lake_persistent_policy": ("near_only_reuse_selected_model_instance"
                                            if include_lake_in_patch else None),
                "facade_styles": facade_styles,
                "city_patch": city_patch}
    target = output_root / "runs" / run_id
    if target.exists():
        if (_file_sha(target / "manifest.json") != _sha(_json_bytes(manifest)) or
                _file_sha(target / "features.jsonl") != manifest["features_sha256"] or
                _file_sha(target / "tileset.json") != manifest["tileset_sha256"] or
                (city_patch is not None and
                 _file_sha(target / "city-patch/tileset.json") != city_patch["tileset_sha256"])):
            raise ValueError("Immutable overlay run collision")
        return manifest
    (output_root / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".stage-developer-overlay-", dir=output_root / "runs"))
    os.chmod(stage, 0o700)
    try:
        tiles = stage / "tiles"
        tiles.mkdir(mode=0o700)
        for sha, blob in blobs.items():
            _write_private(tiles / f"{sha}.b3dm", blob)
        _write_private(stage / "features.jsonl", features_bytes)
        _write_private(stage / "tile-inventory.jsonl", inventory_bytes)
        _write_private(stage / "tileset.json", tileset_bytes)
        _write_private(stage / "manifest.json", _json_bytes(manifest))
        if city_patch_files:
            for path, blob in sorted(city_patch_files.items()):
                destination = stage / path
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_private(destination, blob)
            for directory in (stage / "city-patch/tiles", stage / "city-patch/regions",
                              stage / "city-patch"):
                _fsync_dir(directory)
        _fsync_dir(tiles)
        _fsync_dir(stage)
        os.replace(stage, target)
        _fsync_dir(output_root / "runs")
    finally:
        if stage.exists():
            import shutil
            shutil.rmtree(stage)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-manifest", type=Path, required=True)
    parser.add_argument("--alignment-path", type=Path, required=True)
    parser.add_argument("--alignment-sha256", required=True)
    parser.add_argument("--map-run-id", required=True)
    parser.add_argument("--unit-run-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--enrichment-run-id", required=True)
    parser.add_argument("--lake-osm-path", type=Path, required=True)
    parser.add_argument("--detail-patch-manifest", type=Path)
    parser.add_argument("--overview-patch-manifest", type=Path)
    parser.add_argument("--include-lake-in-patch", action="store_true")
    args = parser.parse_args()
    result = build(args.delivery_manifest, args.alignment_path, args.alignment_sha256,
                   args.map_run_id, args.unit_run_id, args.model_run_id,
                   args.enrichment_run_id, args.lake_osm_path,
                   detail_patch_manifest=args.detail_patch_manifest,
                   overview_patch_manifest=args.overview_patch_manifest,
                   include_lake_in_patch=args.include_lake_in_patch)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
