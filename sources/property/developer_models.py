"""Stage selected-only schematic GLBs from exact source corpus polygons.

No network is used and no active pointer is changed. A height is rendered only
when the pinned apartment menu has one nonconflicting source-reported corpus
floor count; apartment floor maxima and project markers never become heights.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from backend import developer_map as map_reader
from backend import developer_units as menu_reader
from pipeline.build_tiles import enu_transform
from sources.models.geometry import ModelMesh, polygon_parts
from sources.property.developer_unit_media import _png_dimensions, _private_source
from sources.property.textured_model import textured_glb
from sources.property.developer_map import (
    ROOT, _canonical, _checked_delivery, _checked_file, _file_sha, _fsync_dir,
    _rows, _sha, _write_private,
)


OUTPUT = ROOT / "data/property/developer_models"
RULES = "developer-selected-schematic-v1"
HEIGHT_BASIS = "source_total_floors_times_3m_estimate"
MAX_ASSET_BYTES = 20_000_000
MAX_POLYGON_VERTICES = 10_000
LAKE_REGISTRY_ID = "registry-b1feab9d36bced349dfa4b1d"
LAKE_BUILDING_ID = "639b34e2b176d8bf3ad3d6b1"
LAKE_TEXTURE_SHA = "d48441f62051b570206fdd86f02886dfd2a247576bf03a73f480b124f4febaef"


def _geometry(corpus: dict[str, Any], map_point: dict[str, Any]) -> BaseGeometry:
    if (corpus.get("record_type") != "corpus" or
            corpus.get("source_group") != "proplanirovki" or
            corpus.get("geometry_inside_moscow") != "inside" or
            corpus.get("geometry_basis") != "aggregator_unverified"):
        raise ValueError("Corpus source geometry is not an admitted Moscow polygon")
    polygon = shape(corpus.get("source_geometry"))
    if (polygon.geom_type not in {"Polygon", "MultiPolygon"} or
            not polygon.is_valid or polygon.is_empty or polygon.area <= 0):
        raise ValueError("Corpus polygon is invalid")
    vertices = sum(len(part.exterior.coords) + sum(len(ring.coords) for ring in part.interiors)
                   for part in polygon_parts(polygon))
    if vertices > MAX_POLYGON_VERTICES:
        raise ValueError("Corpus polygon exceeds bounded mesh complexity")
    location = map_point.get("location")
    if (not isinstance(location, list) or len(location) != 2 or
            not polygon.covers(Point(location))):
        raise ValueError("Map point is not covered by its exact corpus polygon")
    return polygon


def _corpora(path: Path, needed: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _rows(path):
        if row.get("source_group") != "proplanirovki" or row.get("record_type") != "corpus":
            continue
        key = row.get("publisher_project_id"), row.get("publisher_building_id")
        if key not in needed:
            continue
        if key in found:
            raise ValueError("Repeated exact source corpus identity")
        found[key] = row
    if set(found) != needed:
        raise ValueError("Selected model corpus missing from pinned harvest delivery")
    return found


def _model(point: dict[str, Any], floors: dict[str, Any], corpus: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    total = floors.get("total_floors")
    height = floors.get("estimated_height_m")
    if (floors.get("registry_id") != point.get("registry_id") or
            floors.get("total_floors_basis") != "source_apartment_record_building_floors" or
            type(total) is not int or not 1 <= total <= 300 or
            type(height) not in {int, float} or not math.isfinite(height) or
            height != total * 3.0 or floors.get("height_conflict")):
        raise ValueError("Selected model has no single eligible source floor count")
    polygon = _geometry(corpus, point)
    west, south, east, north = polygon.bounds
    origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
    mesh = ModelMesh(origin_lon, origin_lat, height)
    mesh.add(polygon)
    if mesh.holes != sum(len(part.interiors) for part in polygon_parts(polygon)):
        raise ValueError("Mesh courtyard differs from the source footprint")
    blob = mesh.to_glb()
    if len(blob) > MAX_ASSET_BYTES:
        raise ValueError("Selected model GLB exceeds 20 MB")
    asset_sha = _sha(blob)
    row = {
        "registry_id": point["registry_id"],
        "asset_sha256": asset_sha, "asset_bytes": len(blob),
        "model_transform": enu_transform(origin_lon, origin_lat),
        "height_m": height, "height_basis": HEIGHT_BASIS,
        "source_total_floors": total,
        "source_geometry_basis": "aggregator_unverified",
        "source_geometry_evidence_sha256": _sha(_canonical(corpus["source_geometry"])),
        "source_observed_at": corpus["observed_at"],
        "depiction": "schematic_massing", "label": "реконструкция",
        "model_quality": "source_building_footprint_estimated_height",
        "render_policy": "selected_only_no_global_overlay",
        "triangles": mesh.triangles, "courtyards": mesh.holes,
    }
    return row, blob


def _lake_model(point: dict[str, Any], floors: dict[str, Any],
                osm_path: Path, texture_path: Path) -> tuple[dict[str, Any], bytes]:
    if (point.get("registry_id") != LAKE_REGISTRY_ID or
            point.get("source_group") != "fsk" or
            point.get("publisher_building_id") != LAKE_BUILDING_ID or
            floors.get("total_floors") is not None or
            floors.get("source_section_total_floors") != {"1": [16], "2": [10]}):
        raise ValueError("Textured pilot no longer matches exact Lake building/sections")
    osm_file = _private_source(str(osm_path))
    evidence = json.loads(osm_file.read_text(encoding="utf-8"))
    osm = evidence.get("osm_object") or {}
    if (evidence.get("schema_version") != "minimal-osm-way-geometry-snapshot/1" or
            osm.get("osm_id") != 1317814900 or osm.get("osm_version") != 2 or
            osm.get("tags", {}).get("addr:housenumber") != "20А" or
            osm.get("tags", {}).get("addr:street") != "Снежная улица"):
        raise ValueError("Lake pilot does not match the reviewed OSM building/address")
    nodes = osm.get("closed_ring_nodes") or []
    if len(nodes) < 4 or len(nodes) > 100 or nodes[0] != nodes[-1]:
        raise ValueError("Lake exact OSM ring is not a bounded closed building")
    polygon = Polygon([(node["longitude"], node["latitude"]) for node in nodes])
    if not polygon.is_valid or not polygon.covers(Point(point["location"])):
        raise ValueError("Lake map point differs from the reviewed exact building polygon")
    texture_file = _private_source(str(texture_path))
    texture = texture_file.read_bytes()
    if _sha(texture) != LAKE_TEXTURE_SHA:
        raise ValueError("Unreviewed generated facade texture")
    _png_dimensions(texture)
    west, south, east, north = polygon.bounds
    origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
    height = 10 * 3.0  # schematic minimum section count; no section-part assignment
    blob, metrics = textured_glb(polygon, origin_lon, origin_lat, height, texture)
    if len(blob) > MAX_ASSET_BYTES:
        raise ValueError("Lake textured GLB exceeds selected-model asset bound")
    row = {"registry_id": LAKE_REGISTRY_ID,
           "asset_sha256": _sha(blob), "asset_bytes": len(blob),
           "model_transform": enu_transform(origin_lon, origin_lat),
           "height_m": height,
           "height_basis": "schematic_min_section_floors_times_3m_estimate",
           "source_section_total_floors": {"1": 16, "2": 10},
           "source_geometry_basis": "osm_exact_building_footprint_way_1317814900_v2",
           "source_geometry_evidence_sha256": _file_sha(osm_file),
           "source_observed_at": evidence["observed_at"],
           "depiction": "schematic_massing", "label": "реконструкция",
           "model_quality": "photo_informed_schematic_reconstruction",
           "render_policy": "selected_only_no_global_overlay",
           "photo_facade_reconstruction": True,
           "texture_basis": "generated_photo_informed_repeat_unoriented",
           "texture_sha256": LAKE_TEXTURE_SHA,
           "attribution": "© OpenStreetMap contributors", "rights_license": "ODbL-1.0",
           "section_part_geometry_known": False, **metrics}
    return row, blob


def build(delivery_manifest: Path, map_run_id: str, menu_run_id: str,
          lake_osm_path: Path | None = None, lake_texture_path: Path | None = None,
          output_root: Path = OUTPUT) -> dict[str, Any]:
    if (lake_osm_path is None) != (lake_texture_path is None):
        raise ValueError("Lake OSM footprint and generated texture must be supplied together")
    delivery, _, delivery_sha = _checked_delivery(delivery_manifest)
    corpus_path = _checked_file(delivery_manifest.parent, delivery["files"], "projects_corpora")
    map_run = map_reader.load_staged(map_run_id)
    active_map = map_reader.resolve_active()
    if active_map is None or active_map["manifest"]["run_id"] != map_run_id:
        raise ValueError("Model stage must bind the current developer map")
    menu = menu_reader.load_staged(menu_run_id, map_run)
    map_manifest_sha = _file_sha(map_reader.BASE / "runs" / map_run_id / "manifest.json")
    menu_manifest_sha = _file_sha(menu_reader.BASE / "runs" / menu_run_id / "manifest.json")
    selected = []
    needed: set[tuple[str, str]] = set()
    for registry_id, point in map_run["by_registry"].items():
        floors = menu["buildings"][registry_id]
        if (point.get("source_group") != "proplanirovki" or not point.get("map_eligible") or
                floors.get("total_floors") is None):
            continue
        key = point.get("publisher_project_id"), point.get("publisher_building_id")
        if any(not isinstance(value, str) or not value for value in key):
            raise ValueError("Selected model lacks exact publisher corpus identity")
        needed.add(key)
        selected.append((point, floors, key))
    corpora = _corpora(corpus_path, needed)
    rows: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    for point, floors, key in sorted(selected, key=lambda item: item[0]["registry_id"]):
        row, blob = _model(point, floors, corpora[key])
        old = blobs.setdefault(row["asset_sha256"], blob)
        if old != blob:
            raise ValueError("Content-addressed GLB collision")
        rows.append(row)
    if lake_osm_path is not None and lake_texture_path is not None:
        lake_point = map_run["by_registry"][LAKE_REGISTRY_ID]
        lake_floors = menu["buildings"][LAKE_REGISTRY_ID]
        row, blob = _lake_model(lake_point, lake_floors, lake_osm_path, lake_texture_path)
        old = blobs.setdefault(row["asset_sha256"], blob)
        if old != blob:
            raise ValueError("Textured selected-model content collision")
        rows.append(row)
    rows.sort(key=lambda item: item["registry_id"])
    payload = b"".join(_canonical(row) + b"\n" for row in rows)
    inputs = {"delivery_manifest_sha256": delivery_sha,
              "corpus_source_sha256": _file_sha(corpus_path),
              "map_run_id": map_run_id,
              "map_snapshot_id": map_run["manifest"]["snapshot_id"],
              "map_manifest_sha256": map_manifest_sha,
              "menu_run_id": menu_run_id,
              "menu_manifest_sha256": menu_manifest_sha,
              "lake_osm_sha256": (_file_sha(_private_source(str(lake_osm_path)))
                                  if lake_osm_path is not None else None),
              "lake_generated_texture_sha256": (LAKE_TEXTURE_SHA
                                                  if lake_texture_path is not None else None)}
    run_hash = _sha(_canonical({"rules": RULES, "inputs": inputs,
                               "models_sha256": _sha(payload),
                               "assets": sorted(blobs)}))
    run_id = "developer-models-v1-" + run_hash[:16]
    manifest = {"schema_version": "developer-models-snapshot/1", "rules_version": RULES,
                "run_id": run_id, **inputs,
                "models_sha256": _sha(payload), "models_total": len(rows),
                "assets_total": len(blobs), "assets_bytes": sum(map(len, blobs.values())),
                "height_basis": HEIGHT_BASIS,
                "depiction": "schematic_massing", "render_policy": "selected_only_no_global_overlay",
                "rights_status": "unknown",
                "photo_facade_reconstruction": lake_texture_path is not None}
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs = output_root / "runs"
    runs.mkdir(exist_ok=True, mode=0o700)
    target = runs / run_id
    manifest_bytes = _canonical(manifest) + b"\n"
    if target.exists():
        if (target.is_symlink() or (target / "manifest.json").read_bytes() != manifest_bytes or
                (target / "models.jsonl").read_bytes() != payload or
                any(_file_sha(target / "assets" / (sha + ".glb")) != sha for sha in blobs)):
            raise ValueError("Immutable selected-model run ID collision")
        return manifest
    stage = Path(tempfile.mkdtemp(prefix=".stage-developer-models-", dir=runs))
    os.chmod(stage, 0o700)
    try:
        assets = stage / "assets"
        assets.mkdir(mode=0o700)
        for sha, blob in blobs.items():
            _write_private(assets / (sha + ".glb"), blob)
        _write_private(stage / "models.jsonl", payload)
        _write_private(stage / "manifest.json", manifest_bytes)
        _fsync_dir(assets)
        _fsync_dir(stage)
        os.replace(stage, target)
        _fsync_dir(runs)
    finally:
        if stage.exists():
            for path in (stage / "assets").glob("*"):
                path.unlink()
            (stage / "assets").rmdir()
            for path in stage.iterdir():
                path.unlink()
            stage.rmdir()
    if map_reader.resolve_active()["manifest"]["run_id"] != map_run_id:
        raise ValueError("Map pointer changed during selected-model build")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--delivery-manifest", required=True, type=Path)
    build_cmd.add_argument("--map-run-id", required=True)
    build_cmd.add_argument("--menu-run-id", required=True)
    build_cmd.add_argument("--lake-osm-path", type=Path)
    build_cmd.add_argument("--lake-texture-path", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.delivery_manifest, args.map_run_id, args.menu_run_id,
                           args.lake_osm_path, args.lake_texture_path, args.output_root),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
