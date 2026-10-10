#!/usr/bin/env python3
"""Select a conservative OSM-linked sample or full allowlist of multilevel Overture parts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OSM = ROOT / "data/city/osm_moscow_buildings_parts_261008_v2.parquet"
DEFAULT_PARTS = ROOT / "data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.parquet"
DEFAULT_BUILDINGS = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
DEFAULT_OUTPUT = ROOT / "data/city/osm_overture_clean_multilevel_part_samples_261008.geojson"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metric_geometry(geometry: Any) -> Any:
    lon0, lat0 = 37.4, 55.6
    x_scale = 111_320.0 * math.cos(math.radians(lat0))
    y_scale = 111_132.0

    def project(coordinates: Any) -> Any:
        return np.column_stack(((coordinates[:, 0] - lon0) * x_scale,
                                (coordinates[:, 1] - lat0) * y_scale))

    return shapely.transform(geometry, project, include_z=False)


def source_id(record_id: str | None) -> tuple[str, int, int | None] | None:
    match = re.fullmatch(r"([nwr])(\d+)(?:@(\d+))?", record_id or "")
    if not match:
        return None
    code, ident, version = match.groups()
    return {"n": "node", "w": "way", "r": "relation"}[code], int(ident), int(version) if version else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, default=DEFAULT_OSM)
    parser.add_argument("--parts", type=Path, default=DEFAULT_PARTS)
    parser.add_argument("--buildings", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--all", action="store_true", help="export every eligible parent group")
    parser.add_argument("--parquet-output", type=Path, help="also write selected features as GeoParquet")
    args = parser.parse_args()
    for name in ("osm", "parts", "buildings", "output", "parquet_output"):
        path = getattr(args, name)
        if path is not None and not path.is_absolute():
            setattr(args, name, ROOT / path)
    manifest_path = args.output.with_suffix(".manifest.json")
    exclusions_path = args.output.with_suffix(".excluded_parents.jsonl")
    output_paths = [args.output, manifest_path, exclusions_path]
    if args.parquet_output:
        output_paths.append(args.parquet_output)
    if any(path.exists() for path in output_paths):
        parser.error("refusing to overwrite existing output")
    if args.limit < 1:
        parser.error("--limit must be positive")
    for path in (args.osm, args.parts, args.buildings):
        if not path.is_file():
            parser.error(f"missing input: {path}")

    osm_rows: dict[tuple[str, int], dict[str, Any]] = {}
    osm_pf = pq.ParquetFile(args.osm)
    osm_columns = ["osm_type", "osm_id", "osm_version", "entity_kind", "height_tag_raw", "height_m",
                   "height_parse_status", "min_height_tag_raw", "min_height_m", "min_height_parse_status",
                   "roof_height_tag_raw", "roof_height_m", "building_levels_tag_raw", "building_levels",
                   "building_relation_ids"]
    for batch in osm_pf.iter_batches(batch_size=8192, columns=osm_columns):
        for row in batch.to_pylist():
            osm_rows[(row["osm_type"], int(row["osm_id"]))] = row

    parts_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rejected_parent_reasons: dict[str, set[str]] = defaultdict(set)
    seen_parts_by_parent: dict[str, int] = defaultdict(int)
    counters = defaultdict(int)
    parts_pf = pq.ParquetFile(args.parts)
    part_columns = ["id", "building_id", "geometry", "height", "min_height", "num_floors", "min_floor",
                    "roof_height", "roof_shape", "roof_direction", "is_underground", "sources"]
    for batch in parts_pf.iter_batches(batch_size=8192, columns=part_columns):
        for row in batch.to_pylist():
            counters["parts_seen"] += 1
            if not row["building_id"]:
                counters["missing_parent_id"] += 1
                continue
            seen_parts_by_parent[row["building_id"]] += 1

            def reject(reason: str) -> None:
                counters[reason] += 1
                rejected_parent_reasons[row["building_id"]].add(reason)

            source = next((s for s in row["sources"] or [] if s.get("dataset") == "OpenStreetMap"), None)
            identity = source_id(source.get("record_id") if source else None)
            osm = osm_rows.get((identity[0], identity[1])) if identity else None
            if not osm:
                reject("no_exact_local_osm_id")
                continue
            if identity[2] is None or osm["osm_version"] != identity[2]:
                reject("source_version_not_current")
                continue
            if osm["entity_kind"] not in {"building_part", "relation_member_part", "building_multipolygon_part_relation"}:
                reject("osm_id_not_classified_as_part")
                continue
            if row["is_underground"] is True:
                reject("underground_excluded")
                continue
            if osm["height_parse_status"] != "parsed" or osm["height_m"] is None or osm["height_m"] <= 0:
                reject("missing_or_invalid_osm_height")
                continue
            if osm["min_height_parse_status"] not in {"missing", "parsed"}:
                reject("invalid_min_height")
                continue
            base = float(osm["min_height_m"] or 0.0)
            top = float(osm["height_m"])
            if base < 0 or top < base:
                reject("invalid_vertical_interval")
                continue
            if row["height"] is None or abs(float(row["height"]) - top) > 0.5:
                reject("overture_height_disagrees_with_same_version_osm")
                continue
            if row["min_height"] is not None and abs(float(row["min_height"]) - base) > 0.5:
                reject("overture_min_height_disagrees_with_same_version_osm")
                continue
            part_geom = shapely.from_wkb(row["geometry"])
            if not part_geom.is_valid or part_geom.is_empty:
                reject("invalid_geometry")
                continue
            parts_by_parent[row["building_id"]].append({
                **row, "source_record_id": source["record_id"], "osm": osm,
                "base_m": base, "top_m": top, "thickness_m": top - base,
                "geometry_shape": part_geom, "geometry_metric": metric_geometry(part_geom),
            })

    parent_ids = set(parts_by_parent)
    parent_geoms: dict[str, Any] = {}
    parent_geoms_wgs84: dict[str, Any] = {}
    building_pf = pq.ParquetFile(args.buildings)
    for batch in building_pf.iter_batches(batch_size=8192, columns=["id", "geometry"]):
        for row in batch.to_pylist():
            if row["id"] in parent_ids:
                parent_geoms_wgs84[row["id"]] = shapely.from_wkb(row["geometry"])
                parent_geoms[row["id"]] = metric_geometry(parent_geoms_wgs84[row["id"]])

    eligible_groups = []
    excluded_groups: list[dict[str, Any]] = []
    group_rejections = defaultdict(int)
    all_parent_ids = set(seen_parts_by_parent)
    for parent_id in sorted(all_parent_ids):
        parts = parts_by_parent.get(parent_id, [])
        row_reasons = sorted(rejected_parent_reasons.get(parent_id, set()))
        if row_reasons or len(parts) != seen_parts_by_parent[parent_id]:
            group_rejections["parent_has_any_ineligible_or_unresolved_part"] += 1
            excluded_groups.append({"building_id": parent_id,
                                    "input_part_count": seen_parts_by_parent[parent_id],
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "parent_has_any_ineligible_or_unresolved_part",
                                    "row_rejection_reasons": row_reasons})
            continue
        if len(parts) < 2 or len(parts) > 12:
            group_rejections["group_size_outside_2_12"] += 1
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "group_size_outside_2_12",
                                    "row_rejection_reasons": []})
            continue
        parent = parent_geoms.get(parent_id)
        if parent is None:
            group_rejections["parent_not_found"] += 1
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "parent_not_found",
                                    "row_rejection_reasons": []})
            continue
        outside = [part["geometry_metric"].difference(parent).area for part in parts]
        if any(area > 1.0 for area in outside):
            group_rejections["part_outside_parent_over_1m2"] += 1
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "part_outside_parent_over_1m2",
                                    "row_rejection_reasons": [],
                                    "max_part_area_outside_parent_m2": max(outside)})
            continue
        union_parts = shapely.union_all([part["geometry_metric"] for part in parts])
        parent_area = parent.area
        coverage = union_parts.intersection(parent).area / parent_area if parent_area > 0 else 0.0
        coverage = min(1.0, max(0.0, coverage))
        if coverage < 0.99:
            group_rejections["part_union_covers_less_than_99_percent_of_parent"] += 1
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "part_union_covers_less_than_99_percent_of_parent",
                                    "row_rejection_reasons": [], "part_union_parent_coverage_fraction": coverage})
            continue
        tops = [part["top_m"] for part in parts]
        if max(tops) - min(tops) < 2.0:
            group_rejections["no_two_distinct_tops"] += 1
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "no_two_distinct_tops",
                                    "row_rejection_reasons": [], "distinct_top_heights_m": sorted(set(tops))})
            continue
        group_valid = True
        group_tree = STRtree([part["geometry_metric"] for part in parts])
        for i, part in enumerate(parts):
            for candidate in group_tree.query(part["geometry_metric"]):
                j = int(candidate)
                if j <= i:
                    continue
                area = part["geometry_metric"].intersection(parts[j]["geometry_metric"]).area
                if area <= 1.0:
                    continue
                z_overlap = min(part["top_m"], parts[j]["top_m"]) - max(part["base_m"], parts[j]["base_m"])
                if z_overlap > 0.1:
                    group_valid = False
                    group_rejections["group_has_3d_overlap_over_1m2"] += 1
                    break
            if not group_valid:
                break
        if not group_valid:
            excluded_groups.append({"building_id": parent_id, "input_part_count": len(parts),
                                    "row_eligible_part_count": len(parts),
                                    "primary_rejection_reason": "group_has_3d_overlap_over_1m2",
                                    "row_rejection_reasons": []})
            continue
        residual_area = max(0.0, parent_area - union_parts.intersection(parent).area)
        eligible_groups.append((parent_id, parts, max(tops) - min(tops), coverage, residual_area, parent_area))

    eligible_groups.sort(key=lambda item: (len(item[1]), -item[2], item[0]))
    selected = eligible_groups if args.all else eligible_groups[:args.limit]
    features = []
    selected_manifest = []
    parquet_rows = []
    for parent_id, parts, height_range, coverage, residual_area, parent_area in selected:
        parent_geometry = parent_geoms[parent_id]
        parent_wgs = parent_geoms_wgs84[parent_id]
        center = list(parent_wgs.centroid.coords)[0] if parent_wgs else None
        selected_manifest.append({
            "building_id": parent_id, "part_count": len(parts), "distinct_top_heights_m": sorted({p["top_m"] for p in parts}),
            "height_range_m": height_range, "center_lon_lat": list(center) if center else None,
            "part_union_parent_coverage_fraction": coverage, "uncovered_parent_area_m2": residual_area,
            "parent_area_m2": parent_area,
            "selection_status": "exact-source-version; parent-linked; inside-parent; no known same-parent 3D overlap above 1m2",
        })
        for part in parts:
            osm = part["osm"]
            features.append({
                "type": "Feature", "id": part["id"], "geometry": shapely.geometry.mapping(part["geometry_shape"]),
                "properties": {
                    "overture_part_id": part["id"], "building_id": parent_id,
                    "osm_source_record_id": part["source_record_id"], "osm_version": osm["osm_version"],
                    "osm_height_tag": osm["height_tag_raw"], "height_top_m": part["top_m"],
                    "osm_min_height_tag": osm["min_height_tag_raw"], "vertical_base_m": part["base_m"],
                    "vertical_thickness_m": part["thickness_m"],
                    "osm_roof_height_tag": osm["roof_height_tag_raw"], "roof_height_m": osm["roof_height_m"],
                    "osm_building_levels_tag": osm["building_levels_tag_raw"],
                    "building_levels": osm["building_levels"], "is_underground": part["is_underground"],
                    "provenance": "Exact Overture source OSM record ID and object version; community-mapped tags, not survey-verified.",
                    "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
                },
            })
            identity = source_id(part["source_record_id"])
            parquet_rows.append({
                "overture_part_id": part["id"], "building_id": parent_id,
                "osm_source_record_id": part["source_record_id"],
                "source_osm_type": identity[0], "source_osm_id": identity[1],
                "source_osm_version": identity[2], "osm_version": osm["osm_version"],
                "source_match_method": "exact_osm_type_id_and_same_version",
                "osm_height_tag": osm["height_tag_raw"], "osm_height_m": osm["height_m"],
                "height_top_m": part["top_m"],
                "osm_min_height_tag": osm["min_height_tag_raw"], "osm_min_height_m": osm["min_height_m"],
                "vertical_base_m": part["base_m"], "vertical_thickness_m": part["thickness_m"],
                "osm_roof_height_tag": osm["roof_height_tag_raw"], "roof_height_m": osm["roof_height_m"],
                "osm_building_levels_tag": osm["building_levels_tag_raw"], "building_levels": osm["building_levels"],
                "overture_height_m": part["height"], "overture_min_height_m": part["min_height"],
                "overture_num_floors": part["num_floors"], "overture_min_floor": part["min_floor"],
                "overture_roof_height_m": part["roof_height"], "overture_roof_shape": part["roof_shape"],
                "overture_roof_direction": part["roof_direction"], "is_underground": part["is_underground"],
                "parent_part_count": len(parts), "parent_height_range_m": height_range,
                "part_union_parent_coverage_fraction": coverage, "uncovered_parent_area_m2": residual_area,
                "parent_area_m2": parent_area, "gates_profile": "clean_multilevel_parts_v1",
                "gates_passed_json": json.dumps([
                    "exact_osm_source_id", "same_osm_version", "entity_classified_as_part",
                    "explicit_positive_osm_height", "valid_min_height_or_zero_base",
                    "overture_height_agrees_with_osm_within_0_5m",
                    "overture_min_height_agrees_when_present_within_0_5m",
                    "non_underground", "valid_polygon", "exact_parent_link",
                    "part_outside_parent_area_le_1m2", "group_size_2_to_12",
                    "part_union_parent_coverage_ge_0_99", "height_range_ge_2m",
                    "no_same_parent_3d_overlap_area_gt_1m2_and_z_overlap_gt_0_1m",
                ], separators=(",", ":")),
                "geometry": shapely.to_wkb(part["geometry_shape"], hex=False),
            })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if args.parquet_output:
        args.parquet_output.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(parquet_rows)
        geo_metadata = {
            "version": "1.1.0", "primary_column": "geometry",
            "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Polygon", "MultiPolygon"],
                                       "crs": None, "edges": "planar"}},
        }
        metadata = dict(table.schema.metadata or {})
        metadata[b"geo"] = json.dumps(geo_metadata, separators=(",", ":")).encode("utf-8")
        table = table.replace_schema_metadata(metadata)
        pq.write_table(table, args.parquet_output, compression="zstd", version="2.6")
    exclusions_path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                           for row in excluded_groups), encoding="utf-8")
    manifest = {
        "status": "bounded_clean_multilevel_part_candidates_not_citywide_coverage",
        "inputs": {
            "osm_file": str(args.osm.relative_to(ROOT)), "osm_sha256": sha256(args.osm),
            "parts_file": str(args.parts.relative_to(ROOT)), "parts_sha256": sha256(args.parts),
            "buildings_file": str(args.buildings.relative_to(ROOT)), "buildings_sha256": sha256(args.buildings),
        },
        "filter": "Require every part of a candidate parent group to have an exact OSM source ID and current version, explicit positive OSM height, valid nonnegative OSM min_height or base=0 if absent, Overture field agreement within 0.5m, exact Overture parent link, at most 1m2 outside parent, non-underground, group size 2..12, at least 99 percent part-union footprint coverage, at least 2m height range, and no same-parent positive-area overlap with intersecting vertical intervals.",
        "counts": {"input_part_rows": parts_pf.metadata.num_rows,
                   "input_parent_groups_with_id": len(all_parent_ids),
                   "eligible_parent_groups": len(eligible_groups),
                   "selected_parent_groups": len(selected), "selected_part_rows": len(features),
                   "excluded_parent_groups": len(excluded_groups),
                   "unparented_part_rows": counters.get("missing_parent_id", 0),
                   "filter_rejections": dict(group_rejections), "row_rejections": dict(counters)},
        "selected_parents": selected_manifest,
        "output": {"file": str(args.output.relative_to(ROOT)), "sha256": sha256(args.output), "feature_count": len(features)},
        "excluded_parent_groups_file": {"file": str(exclusions_path.relative_to(ROOT)),
                                        "sha256": sha256(exclusions_path), "group_count": len(excluded_groups),
                                        "reason_scope": "One primary group-stage reason per rejected parent; row_rejection_reasons is a non-exclusive list of row-level causes. Overlap gate outcomes are not asserted source errors."},
        "geoparquet_output": ({"file": str(args.parquet_output.relative_to(ROOT)),
                               "sha256": sha256(args.parquet_output), "feature_count": len(parquet_rows),
                               "geometry": "WKB; clipped Overture part geometry in OGC:CRS84; no un-clipped geometry claimed"}
                              if args.parquet_output else None),
        "vertical_semantics": "For this OSM-source-linked extract, `height` is the top above ground and `min_height` is bottom elevation; interval [min_height or 0, height]. roof_height is within total height and is not added.",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path.relative_to(ROOT)), "counts": manifest["counts"],
                      "selected_parent_ids": len(selected_manifest),
                      "parquet_output": str(args.parquet_output.relative_to(ROOT)) if args.parquet_output else None},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
