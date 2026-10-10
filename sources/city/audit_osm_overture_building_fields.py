#!/usr/bin/env python3
"""Compare OSM tags and exact-ID Overture fields in the pinned city artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data/city/osm_moscow_buildings_parts_261008_v2.parquet"
OUTPUT = ROOT / "data/city/osm_overture_building_parts_field_audit_261008.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if not args.input.is_absolute():
        args.input = ROOT / args.input
    if not args.output.is_absolute():
        args.output = ROOT / args.output
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    if not args.input.is_file():
        parser.error(f"missing input: {args.input}")

    counts = Counter()
    conflicts: dict[str, list[dict[str, Any]]] = {"height_m": [], "min_height_m": [], "roof_height_m": [], "building_levels": []}
    part_summary = Counter()
    part_fields = Counter()
    fields = [
        ("height_m", "overture_height_m", 0.5, False),
        ("min_height_m", "overture_min_height_m", 0.5, False),
        ("roof_height_m", "overture_roof_height_m", 0.5, False),
        ("building_levels", "overture_num_floors", 1.0, True),
    ]
    columns = [
        "osm_type", "osm_id", "osm_version", "entity_kind", "height_tag_raw", "height_m",
        "min_height_tag_raw", "min_height_m", "roof_height_tag_raw", "roof_height_m",
        "building_levels_tag_raw", "building_levels", "roof_shape", "roof_direction_tag_raw",
        "overture_match_method", "overture_feature_id", "overture_source_record_id", "overture_source_version",
        "overture_feature_type", "overture_building_id", "overture_height_m", "overture_min_height_m",
        "overture_min_floor", "overture_roof_height_m", "overture_roof_shape", "overture_roof_direction_deg",
        "overture_num_floors",
    ]
    pf = pq.ParquetFile(args.input)
    for batch in pf.iter_batches(batch_size=8192, columns=columns):
        for row in batch.to_pylist():
            if row["overture_match_method"] != "exact_osm_source_id":
                continue
            counts["exact_id_rows"] += 1
            version_bucket = "same_source_version" if row["osm_version"] == row["overture_source_version"] else "source_version_differs"
            counts[version_bucket] += 1
            part_row = row["overture_feature_type"] == "building_part"
            if part_row:
                part_summary["exact_building_part_id_rows"] += 1
                part_summary["same_source_version" if row["osm_version"] == row["overture_source_version"] else "source_version_differs"] += 1
            for osm_field, overture_field, tolerance, inclusive_threshold in fields:
                left, right = row[osm_field], row[overture_field]
                if left is None or right is None:
                    continue
                key = f"{version_bucket}:{osm_field}"
                counts[f"{key}:both_present"] += 1
                part_key = f"{version_bucket}:{osm_field}"
                if part_row:
                    part_fields[f"{part_key}:both_present"] += 1
                delta = abs(float(left) - float(right))
                mismatch = delta >= tolerance if inclusive_threshold else delta > tolerance
                if not mismatch:
                    counts[f"{key}:equal_within_{tolerance:g}"] += 1
                    if part_row:
                        part_fields[f"{part_key}:equal_within_{tolerance:g}"] += 1
                else:
                    counts[f"{key}:different_over_{tolerance:g}"] += 1
                    if part_row:
                        part_fields[f"{part_key}:different_over_{tolerance:g}"] += 1
                    conflict = {
                        "osm_id": f"{row['osm_type']}/{row['osm_id']}",
                        "osm_version": row["osm_version"],
                        "overture_source_record_id": row["overture_source_record_id"],
                        "overture_source_version": row["overture_source_version"],
                        "entity_kind": row["entity_kind"], "overture_feature_type": row["overture_feature_type"],
                        "overture_feature_id": row["overture_feature_id"],
                        "osm_raw": row.get({
                            "height_m": "height_tag_raw", "min_height_m": "min_height_tag_raw",
                            "roof_height_m": "roof_height_tag_raw", "building_levels": "building_levels_tag_raw",
                        }[osm_field]),
                        "osm_value": left, "overture_value": right, "absolute_difference": delta,
                    }
                    if len(conflicts[osm_field]) < 50:
                        conflicts[osm_field].append(conflict)

    output = {
        "status": "exact_osm_source_id_field_comparison",
        "input": {"file": str(args.input.relative_to(ROOT)), "sha256": sha256(args.input),
                  "rows": pf.metadata.num_rows},
        "counts": dict(counts), "building_part_source_match_counts": dict(part_summary),
        "building_part_field_counts": dict(part_fields),
        "field_conflicts_sample_max_50_each": conflicts,
        "interpretation": {
            "height": "For exact OSM source IDs at the same source version, stored height fields match within the stated tolerance. Source-version changes can explain differences; preserve each version/value instead of overwriting.",
            "height_interval": "For OSM Simple 3D Building values, height is ground-to-top and min_height is the bottom offset; interval [min_height or 0, height].",
            "roof_height": "roof_height/roof:height is a component within total height and is not added on top.",
            "survey": "OSM tags are community mapping, not independently survey-verified measurements.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.relative_to(ROOT)), "counts": dict(counts),
                      "building_part_source_match_counts": dict(part_summary), "conflicts": conflicts},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
