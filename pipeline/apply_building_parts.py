"""Stage a bounded, reviewed multilevel BuildingPart city revision.

Only reviewed exact-source parent groups with at least 99% non-overlapping
part coverage are rendered. Their parent envelope is replaced by part volumes
plus any small uncovered footprint residual. All other buildings retain prior
massing. Public delivery uses a separate source-ID-redacted asset release.
"""

from __future__ import annotations

import argparse

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from shapely import wkb
from shapely.geometry import shape
from shapely.ops import unary_union

from backend.store import ASSET_ROOT, ROOT
from pipeline.apply_render_heights import _glb_document, _needed_geometries, _rebuild_region, sha256
from pipeline.build_citywide import extrude_footprint
from pipeline.build_tiles import Mesh, glb, relative_xy, save_bytes, save_json


BASE = ASSET_ROOT / "citywide-v3"
OUTPUT = ASSET_ROOT / "citywide-v4"
SAMPLE = ROOT / "data/city/osm_overture_clean_multilevel_part_samples_261008_final.geojson"
SAMPLE_MANIFEST = ROOT / "data/city/osm_overture_clean_multilevel_part_samples_261008_final.manifest.json"


def _samples(path: Path, manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if sha256(path) != manifest["output"]["sha256"]:
        raise ValueError("Reviewed multilevel sample hash changed")
    feature_collection = json.loads(path.read_text(encoding="utf-8"))
    features = feature_collection["features"]
    if len(features) != manifest["output"]["feature_count"]:
        raise ValueError("Multilevel sample count changed")
    grouped: dict[str, list[dict[str, Any]]] = {}
    part_ids = set()
    for feature in features:
        properties = feature["properties"]
        part_id = properties["overture_part_id"]
        parent_id = properties["building_id"]
        base, top = properties["vertical_base_m"], properties["height_top_m"]
        if (part_id in part_ids or not properties["osm_source_record_id"] or
                not isinstance(base, (int, float)) or not isinstance(top, (int, float)) or
                not math.isfinite(base) or not math.isfinite(top) or base < 0 or top <= base or
                properties.get("is_underground") is not False):
            raise ValueError("Invalid reviewed part/source/vertical interval")
        part_ids.add(part_id)
        grouped.setdefault(parent_id, []).append({"part_id": part_id, "parent_id": parent_id,
            "osm_source_record_id": properties["osm_source_record_id"],
            "osm_version": properties["osm_version"], "base_m": float(base),
            "top_m": float(top), "geometry": shape(feature["geometry"])})
    if set(grouped) != {item["building_id"] for item in manifest["selected_parents"]}:
        raise ValueError("Selected parent set changed")
    for parts in grouped.values():
        parts.sort(key=lambda item: item["part_id"])
    return grouped


def _source_entries(base: Path, parent_ids: set[str]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    located = {}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["source_id"] in parent_ids:
                located[row["source_id"]] = row["tile"]
    if set(located) != parent_ids:
        raise ValueError("Multilevel sample parent is absent from base city index")
    affected = set(located.values())
    grouped = {name: [] for name in affected}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["tile"] in affected:
                grouped[row["tile"]].append(row)
    return grouped, affected


def _validate_coverage(parts: list[dict[str, Any]], parent_geometry: Any) -> tuple[Any, float]:
    # Match the pinned allowlist's local area projection exactly.
    area_factor = 111320 * math.cos(math.radians(55.6)) * 111132
    shapes = [part["geometry"] for part in parts]
    union = unary_union(shapes)
    parent_area = parent_geometry.area * area_factor
    residual = parent_geometry.difference(union)
    residual_area = residual.area * area_factor
    if parent_area <= 0 or residual_area / parent_area > 0.01 + 1e-8:
        raise ValueError("Reviewed parts cover less than 99 percent of parent")
    if any(part["geometry"].difference(parent_geometry).area * area_factor > 1 + 1e-6
           for part in parts):
        raise ValueError("A reviewed part extends over 1 square metre outside its parent")
    if len({part["top_m"] for part in parts}) < 2:
        raise ValueError("Part sample has no distinct levels")
    for position, first in enumerate(parts):
        for second in parts[position + 1:]:
            vertical_overlap = min(first["top_m"], second["top_m"]) - max(first["base_m"], second["base_m"])
            if vertical_overlap > 0 and first["geometry"].intersection(second["geometry"]).area * area_factor > 1:
                raise ValueError("Reviewed part sample overlaps another 3D volume")
    return residual, residual_area


def _rebuild_cell(stage: Path, uri: str, entries: list[dict[str, Any]],
                  geometries: dict[str, dict[str, Any]], parts_by_parent: dict[str, list[dict[str, Any]]],
                  detail: dict[str, Any], part_index: Any) -> tuple[int, int]:
    west, south, east, north, _, _ = detail["boundingVolume"]["region"]
    origin_lon = math.degrees((west + east) / 2)
    origin_lat = math.degrees((south + north) / 2)
    mesh = Mesh()
    feature_ids = []
    highest = 0.0
    fallback_count = 0
    part_count = 0
    for entry in entries:
        source_id = entry["source_id"]
        record = geometries[source_id]
        selected = parts_by_parent.get(source_id)
        if selected:
            parent_geometry = wkb.loads(record["geometry"])
            residual, residual_area_m2 = _validate_coverage(selected, parent_geometry)
            for part in selected:
                feature_id = len(feature_ids)
                if extrude_footprint(mesh, part["geometry"], part["top_m"], feature_id,
                                     origin_lon, origin_lat, bottom=part["base_m"],
                                     simplify_tolerance_m=0.02, min_area_m2=0.01) == 0:
                    raise ValueError(f"Reviewed part {part['part_id']} of parent {source_id} produced no valid roof triangles")
                feature_ids.append(part["part_id"])
                highest = max(highest, part["top_m"])
                part_count += 1
                part_index.write(json.dumps({"part_id": part["part_id"], "parent_building_id": source_id,
                    "tile": uri, "base_m": part["base_m"], "top_m": part["top_m"],
                    "osm_source_record_id": part["osm_source_record_id"],
                    "osm_version": part["osm_version"],
                    "render_status": "source_tag_exact_version_not_surveyed"}, ensure_ascii=False) + "\n")
            if residual_area_m2 >= 0.5:
                # Render only the uncovered footprint, never a parent cap over
                # the source-mapped part polygons or their lower wings.
                feature_id = len(feature_ids)
                height = float(entry["height_m"])
                if extrude_footprint(mesh, residual, height, feature_id,
                                     origin_lon, origin_lat) == 0:
                    raise ValueError("Uncovered parent residual could not be triangulated")
                feature_ids.append(source_id)
                highest = max(highest, height)
            continue
        height = float(entry["height_m"])
        highest = max(highest, height)
        feature_id = len(feature_ids)
        previous = mesh.groups.get(4)
        lengths = tuple(len(values) for values in previous) if previous else (0, 0, 0, 0)
        previous_ids = len(mesh.feature_ids.get(4, []))
        try:
            if extrude_footprint(mesh, wkb.loads(record["geometry"]), height, feature_id,
                                 origin_lon, origin_lat) == 0:
                raise ValueError("No roof triangles")
        except (ValueError, TypeError, RuntimeError):
            if 4 in mesh.groups:
                for values, length in zip(mesh.groups[4], lengths):
                    del values[length:]
                del mesh.feature_ids[4][previous_ids:]
            bbox = record["bbox"]
            fw, fe, fs, fn = bbox["xmin"], bbox["xmax"], bbox["ymin"], bbox["ymax"]
            cx, cy = relative_xy((fw + fe) / 2, (fs + fn) / 2, origin_lon, origin_lat)
            width = max(1, relative_xy(fe, origin_lat, fw, origin_lat)[0])
            depth = max(1, relative_xy(origin_lon, fn, origin_lon, fs)[1])
            mesh.box(cx, cy, 0, width, depth, height, 4, feature_id)
            fallback_count += 1
        feature_ids.append(source_id)
    save_bytes(stage / uri, glb(mesh, {
        "geometry_type": "synthetic_massing_with_reviewed_multilevel_part_sample",
        "source_ids": feature_ids,
        "source_count": len(feature_ids),
        "feature_id_mapping": "_FEATURE_ID_0 indexes source_ids",
        "rendered_part_count": part_count,
        "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
    }))
    detail["boundingVolume"]["region"][5] = highest + 5
    detail["geometricError"] = 0
    document = _glb_document(stage / uri)
    if document["asset"]["extras"]["source_ids"] != feature_ids:
        raise ValueError("Part feature-ID mapping changed during GLB export")
    for primitive in document["meshes"][0]["primitives"]:
        position = document["accessors"][primitive["attributes"]["POSITION"]]
        normal = document["accessors"][primitive["attributes"]["NORMAL"]]
        if position["count"] != normal["count"] or position["count"] <= 0:
            raise ValueError("Invalid multilevel GLB topology")
    return fallback_count, part_count


def build(base: Path = BASE, output: Path = OUTPUT, sample_path: Path = SAMPLE,
          sample_manifest_path: Path = SAMPLE_MANIFEST) -> dict[str, Any]:
    reviewed = json.loads(sample_manifest_path.read_text(encoding="utf-8"))
    original = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if original.get("partial") or original["source_sha256"] != reviewed["inputs"]["buildings_sha256"]:
        raise ValueError("Base city revision does not match reviewed multilevel source")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("part_sample_sha256") != reviewed["output"]["sha256"]:
            raise ValueError("Existing multilevel revision differs from reviewed sample")
        return {"status": "no_op", **saved}
    parts_by_parent = _samples(sample_path, reviewed)
    grouped, affected = _source_entries(base, set(parts_by_parent))
    geometries = _needed_geometries(ROOT / reviewed["inputs"]["buildings_file"], grouped)
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    parents = tileset["root"]["children"]
    detail_by_uri = {child["content"]["uri"]: child
                     for parent in parents for child in parent["children"]}
    if not affected <= set(detail_by_uri):
        raise ValueError("Selected parent tile missing from base graph")
    affected_regions = {parent["content"]["uri"] for parent in parents
                        if any(child["content"]["uri"] in affected for child in parent["children"])}
    with tempfile.TemporaryDirectory(prefix=".citywide-v4.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"tileset.json", "manifest.json", "source_tile_index.jsonl"}:
                continue
            if path.name not in affected and path.name not in affected_regions:
                os.link(path, stage / path.name)
        original_fallbacks = sum(entry["geometry_policy"] == "bbox_fallback_geometry_error"
                                 for entries in grouped.values() for entry in entries)
        revised_fallbacks = 0
        part_count = 0
        with (stage / "part_tile_index.jsonl").open("w", encoding="utf-8") as part_index:
            for uri in sorted(affected):
                made_fallbacks, made_parts = _rebuild_cell(stage, uri, grouped[uri], geometries,
                    parts_by_parent, detail_by_uri[uri], part_index)
                revised_fallbacks += made_fallbacks
                part_count += made_parts
            part_index.flush()
            os.fsync(part_index.fileno())
        if part_count != reviewed["counts"]["selected_part_rows"]:
            raise ValueError("Not every reviewed part was rendered")
        for parent in parents:
            uri = parent["content"]["uri"]
            if uri in affected_regions:
                _rebuild_region(stage, parent, uri)
        tileset["root"]["boundingVolume"]["region"][5] = max(
            parent["boundingVolume"]["region"][5] for parent in parents)
        revision = output.name.rsplit("-v", 1)[-1]
        if not revision.isdigit():
            raise ValueError("City output folder must end in a numeric revision")
        tileset["asset"]["tilesetVersion"] = f"citywide-massing-reviewed-multilevel-v{revision}"
        tileset["extras"]["multilevel_policy"] = (
            f"{reviewed['counts']['selected_parent_groups']} exact-source clean parent groups; synthetic massing")
        save_json(stage / "tileset.json", tileset)
        changed_parents = 0
        with (base / "source_tile_index.jsonl").open(encoding="utf-8") as source, \
                (stage / "source_tile_index.jsonl").open("w", encoding="utf-8") as destination:
            for line in source:
                entry = json.loads(line)
                selected = parts_by_parent.get(entry["source_id"])
                if selected:
                    residual, residual_area_m2 = _validate_coverage(
                        selected, wkb.loads(geometries[entry["source_id"]]["geometry"]))
                    entry.update({"geometry_policy": "reviewed_multilevel_parts_with_residual_parent"
                                  if residual_area_m2 >= 0.5 else "reviewed_multilevel_parts_parent_suppressed",
                                  "rendered_top_m": max(max(part["top_m"] for part in selected),
                                                        float(entry["height_m"]) if residual_area_m2 >= 0.5 else 0),
                                  "rendered_part_count": len(selected),
                                  "residual_parent_area_m2": round(residual_area_m2, 3)})
                    line = json.dumps(entry, ensure_ascii=False) + "\n"
                    changed_parents += 1
                destination.write(line)
            destination.flush()
            os.fsync(destination.fileno())
        if changed_parents != reviewed["counts"]["selected_parent_groups"]:
            raise ValueError("Parent source index did not record every part group")
        revised = {**original,
            "version": revision, "tileset_url": f"/assets/{output.name}/tileset.json",
            "material_recolor_only": False, "height_revision_only": False,
            "multilevel_sample_render_only": True,
            "rendered_part_parent_count": changed_parents,
            "rendered_part_count": part_count,
            "revised_detail_tiles": len(affected),
            "revised_coarse_region_tiles": len(affected_regions),
            "geometry_fallback_count": original["geometry_fallback_count"] - original_fallbacks + revised_fallbacks,
            "part_sample_sha256": reviewed["output"]["sha256"],
            "part_source_sha256": reviewed["inputs"]["parts_sha256"],
            "part_vertical_semantics": "exact OSM source interval [min_height or 0, height]; roof included",
            "part_sample_scope": (
                f"{reviewed['counts']['selected_parent_groups']} reviewed parents only; "
                "not citywide coverage")}
        save_json(stage / "manifest.json", revised)
        if any(not (stage / child["content"]["uri"]).is_file()
               for parent in parents for child in parent["children"]):
            raise ValueError("Multilevel revision has missing detailed GLBs")
        if any(not (stage / parent["content"]["uri"]).is_file() for parent in parents):
            raise ValueError("Multilevel revision has missing coarse GLBs")
        stage.replace(output)
    return {"status": "built", **revised}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--sample-manifest", type=Path, default=SAMPLE_MANIFEST)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.output, args.sample, args.sample_manifest),
                     ensure_ascii=False, indent=2))
