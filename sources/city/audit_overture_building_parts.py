#!/usr/bin/env python3
"""Audit parent links and 2D/vertical overlaps in the pinned Moscow parts extract."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import shapely
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
PARTS = ROOT / "data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.parquet"
BUILDINGS = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
OUTPUT = ROOT / "data/city/overture_moscow_building_parts_geometry_audit_2026-09-23.1.json"
OSM = ROOT / "data/city/osm_moscow_buildings_parts_261008_v2.parquet"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metric_geometry(geometry: Any, lon0: float, lat0: float) -> Any:
    # Local equirectangular projection is sufficient for relative area/overlap
    # checks inside the Moscow boundary; it is not a production geodesic metric.
    x_scale = 111_320.0 * math.cos(math.radians(lat0))
    y_scale = 111_132.0

    def project(coordinates: Any) -> Any:
        return np.column_stack(((coordinates[:, 0] - lon0) * x_scale,
                                (coordinates[:, 1] - lat0) * y_scale))

    return shapely.transform(geometry, project, include_z=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, default=PARTS)
    parser.add_argument("--buildings", type=Path, default=BUILDINGS)
    parser.add_argument("--osm-artifact", type=Path, default=OSM)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    for name in ("parts", "buildings", "osm_artifact", "output"):
        path = getattr(args, name)
        if not path.is_absolute():
            setattr(args, name, ROOT / path)
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    for path in (args.parts, args.buildings, args.osm_artifact):
        if not path.is_file():
            parser.error(f"missing input: {path}")

    parts_pf = pq.ParquetFile(args.parts)
    osm_ids: set[tuple[str, int]] = set()
    for batch in pq.ParquetFile(args.osm_artifact).iter_batches(batch_size=8192, columns=["osm_type", "osm_id"]):
        osm_ids.update((row["osm_type"], int(row["osm_id"])) for row in batch.to_pylist())
    rows: list[dict[str, Any]] = []
    parent_ids: set[str] = set()
    quality = Counter()
    height_values: list[float] = []
    roof_values: list[float] = []
    floor_values: list[int] = []
    min_height_values: list[float] = []
    min_floor_values: list[int] = []
    part_columns = ["id", "building_id", "geometry", "height", "min_height", "num_floors",
                    "min_floor", "roof_height", "roof_shape", "roof_direction", "is_underground", "sources"]
    for batch in parts_pf.iter_batches(batch_size=8192, columns=part_columns):
        for row in batch.to_pylist():
            if row["building_id"] is not None:
                parent_ids.add(row["building_id"])
            geom = shapely.from_wkb(row["geometry"])
            if not geom.is_valid or geom.is_empty:
                quality["invalid_or_empty_part_geometry"] += 1
            source_id = None
            for source in row["sources"] or []:
                if source.get("dataset") != "OpenStreetMap":
                    continue
                match = re.fullmatch(r"([nwr])(\d+)(?:@\d+)?", source.get("record_id") or "")
                if match:
                    source_id = ({"n": "node", "w": "way", "r": "relation"}[match.group(1)], int(match.group(2)))
                    break
            exact_local_source = source_id in osm_ids if source_id else False
            quality["source_osm_record_ids"] += source_id is not None
            quality["source_osm_id_exactly_present_in_local_osm_artifact"] += exact_local_source
            rows.append({**row, "geometry_shape": geom, "source_osm_id": source_id,
                         "exact_local_source": exact_local_source})
            h, floors = row["height"], row["num_floors"]
            quality["height_present"] += h is not None
            quality["height_positive"] += h is not None and math.isfinite(h) and h > 0
            quality["height_invalid_nonpositive_or_nonfinite"] += h is not None and (not math.isfinite(h) or h <= 0)
            quality["floors_present"] += floors is not None
            quality["floors_positive"] += floors is not None and floors > 0
            quality["floors_invalid_nonpositive"] += floors is not None and floors <= 0
            quality["height_and_floors_present"] += h is not None and floors is not None
            quality["height_or_floors_present"] += h is not None or floors is not None
            quality["neither_height_nor_floors"] += h is None and floors is None
            quality["min_height_present"] += row["min_height"] is not None
            quality["min_height_nonnegative"] += row["min_height"] is not None and row["min_height"] >= 0
            quality["min_height_negative"] += row["min_height"] is not None and row["min_height"] < 0
            quality["min_floor_present"] += row["min_floor"] is not None
            quality["min_floor_positive"] += row["min_floor"] is not None and row["min_floor"] > 0
            quality["roof_height_present"] += row["roof_height"] is not None
            quality["height_and_roof_height_present"] += h is not None and row["roof_height"] is not None
            quality["height_min_and_roof_height_present"] += h is not None and row["min_height"] is not None and row["roof_height"] is not None
            quality["underground_true"] += row["is_underground"] is True
            if h is not None and math.isfinite(h):
                height_values.append(float(h))
            if row["roof_height"] is not None and math.isfinite(row["roof_height"]):
                roof_values.append(float(row["roof_height"]))
            if floors is not None:
                floor_values.append(int(floors))
            if row["min_height"] is not None and math.isfinite(row["min_height"]):
                min_height_values.append(float(row["min_height"]))
            if row["min_floor"] is not None:
                min_floor_values.append(int(row["min_floor"]))

    parent_geometries: dict[str, Any] = {}
    buildings_pf = pq.ParquetFile(args.buildings)
    for batch in buildings_pf.iter_batches(batch_size=8192, columns=["id", "geometry"]):
        for row in batch.to_pylist():
            if row["id"] in parent_ids:
                parent_geometries[row["id"]] = shapely.from_wkb(row["geometry"])

    lon0, lat0 = 37.4, 55.6
    parent_geometries_metric = {key: metric_geometry(geom, lon0, lat0) for key, geom in parent_geometries.items()}
    metric_parts = [metric_geometry(row["geometry_shape"], lon0, lat0) for row in rows]
    for geom in metric_parts:
        if not geom.is_valid:
            quality["invalid_projected_part_geometry"] += 1

    parent_link_counts = Counter()
    parent_part_counts = Counter()
    outside_area_values: list[tuple[float, str]] = []
    for row, part_geom in zip(rows, metric_parts):
        building_id = row["building_id"]
        parent_part_counts[building_id] += 1
        if building_id is None:
            parent_link_counts["missing_building_id"] += 1
            continue
        parent_geom = parent_geometries_metric.get(building_id)
        if parent_geom is None:
            parent_link_counts["parent_id_not_in_clipped_building_extract"] += 1
            continue
        parent_link_counts["parent_id_found"] += 1
        outside_area = part_geom.difference(parent_geom).area
        if outside_area > 1.0:
            parent_link_counts["part_area_outside_parent_gt_1m2"] += 1
            outside_area_values.append((outside_area, row["id"]))
        if outside_area > 25.0:
            parent_link_counts["part_area_outside_parent_gt_25m2"] += 1

    tree = STRtree(metric_parts)
    overlap_counts = Counter()
    overlap_area_m2: list[tuple[float, int, int]] = []
    z_unknown = 0
    for index, geom in enumerate(metric_parts):
        for candidate in tree.query(geom):
            other_index = int(candidate)
            if other_index <= index:
                continue
            intersection_area = geom.intersection(metric_parts[other_index]).area
            if intersection_area <= 1.0:
                continue
            left, right = rows[index], rows[other_index]
            same_parent = left["building_id"] == right["building_id"] and left["building_id"] is not None
            overlap_counts["positive_area_pairs_gt_1m2"] += 1
            overlap_counts["same_parent_pairs_gt_1m2"] += same_parent
            overlap_counts["different_or_unknown_parent_pairs_gt_1m2"] += not same_parent
            overlap_area_m2.append((intersection_area, index, other_index))
            h1, h2 = left["height"], right["height"]
            if (h1 is None or h2 is None or not left["exact_local_source"] or not right["exact_local_source"]):
                z_unknown += 1
                continue
            base1 = float(left["min_height"] or 0.0)
            base2 = float(right["min_height"] or 0.0)
            # In this OSM-only extract, exact-ID source examples confirm height is
            # ground-to-top; do not add min_height to the top.
            top1, top2 = float(h1), float(h2)
            if top1 < base1 or top2 < base2:
                overlap_counts["invalid_vertical_interval_pairs"] += 1
            elif min(top1, top2) - max(base1, base2) > 0.1:
                overlap_counts["known_3d_volume_overlap_pairs_gt_1m2"] += 1
                overlap_counts["known_3d_volume_overlap_same_parent_pairs_gt_1m2"] += same_parent
                overlap_counts["known_3d_volume_overlap_different_or_unknown_parent_pairs_gt_1m2"] += not same_parent
            else:
                overlap_counts["2d_overlap_with_nonoverlapping_vertical_intervals"] += 1
                overlap_counts["2d_overlap_nonoverlapping_same_parent_pairs_gt_1m2"] += same_parent
                overlap_counts["2d_overlap_nonoverlapping_different_or_unknown_parent_pairs_gt_1m2"] += not same_parent

    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"min": None, "max": None}
        return {"min": min(values), "max": max(values)}

    output = {
        "status": "bounded_geometry_and_attribute_audit",
        "inputs": {
            "parts_file": str(args.parts.relative_to(ROOT)), "parts_sha256": sha256(args.parts),
            "buildings_file": str(args.buildings.relative_to(ROOT)), "buildings_sha256": sha256(args.buildings),
            "osm_artifact_file": str(args.osm_artifact.relative_to(ROOT)), "osm_artifact_sha256": sha256(args.osm_artifact),
            "parts_rows": parts_pf.metadata.num_rows, "parent_building_rows": buildings_pf.metadata.num_rows,
        },
        "parent_links": {
            "unique_part_parent_ids": len(parent_ids), "unique_parent_ids_found": len(parent_geometries),
            "part_rows": dict(parent_link_counts),
            "distinct_parent_group_size": {"min": min(parent_part_counts.values()),
                                           "median": float(np.median(list(parent_part_counts.values()))),
                                           "p95": float(np.percentile(list(parent_part_counts.values()), 95)),
                                           "max": max(parent_part_counts.values())},
            "largest_part_outside_parent_m2": max((area for area, _ in outside_area_values), default=0.0),
        },
        "part_attributes": {
            "counts": dict(quality), "height_m": stats(height_values),
            "min_height_m": stats(min_height_values), "roof_height_m": stats(roof_values),
            "num_floors": stats([float(value) for value in floor_values]),
            "min_floor": stats([float(value) for value in min_floor_values]),
            "render_priority": "explicit positive height first; explicit positive floors may be converted only by the renderer's declared policy; unknown otherwise",
            "height_roof_rule": "height is total ground-to-top in the exact OSM source-ID sample; roof_height is within that total and is not added to height.",
        },
        "part_overlaps": {
            "area_threshold_m2": 1.0, "counts": dict(overlap_counts),
            "pairs_without_vertical_interval": z_unknown,
            "vertical_policy": "Use [min_height,height] only when the Overture part has an OpenStreetMap source record ID present in the local OSM artifact; otherwise z interval is unknown and not counted as a confirmed overlap. This source-specific interpretation is confirmed by exact-ID value comparisons and does not override generic Overture schema wording for non-OSM sources.",
        },
        "method": {
            "parent_join": "Exact Overture BuildingPart.building_id == Overture Building.id; no geometry-nearest association.",
            "overlap_area": "Approximate local equirectangular projection centered at lon=37.4, lat=55.6; 1 m² threshold. Counts are bounded diagnostics, not a legal geometry judgement.",
            "clipping": "Both input artifacts are already clipped to OSM boundary relation 102269; no source geometry is modified.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.relative_to(ROOT)), "parent_links": output["parent_links"],
                      "part_attribute_counts": dict(quality), "part_overlaps": output["part_overlaps"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
