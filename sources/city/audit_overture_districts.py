#!/usr/bin/env python3
"""Audit the mapped Overture district-candidate coverage against the OSM city mask."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATES = ROOT / "data/city/environment/moscow_district_candidates_2026-09-23.1.geojson"
DEFAULT_CANDIDATE_MANIFEST = DEFAULT_CANDIDATES.with_suffix(".manifest.json")
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"
DEFAULT_OUTPUT = ROOT / "data/city/environment/moscow_district_candidates_audit.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--candidate-manifest", type=Path, default=DEFAULT_CANDIDATE_MANIFEST)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    for path in (args.candidates, args.candidate_manifest, args.boundary, args.boundary_manifest):
        if not path.is_file():
            parser.error(f"required audit input is missing: {path}")
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    from shapely.geometry import shape
    from shapely.ops import unary_union

    candidates_doc = json.loads(args.candidates.read_text(encoding="utf-8"))
    boundary_doc = json.loads(args.boundary.read_text(encoding="utf-8"))
    boundary = shape(boundary_doc["features"][0]["geometry"])
    feature_rows = candidates_doc.get("features", [])
    geometries = [shape(feature["geometry"]) for feature in feature_rows]
    union = unary_union(geometries)
    uncovered = boundary.difference(union)
    outside = union.difference(boundary)
    names = [str((feature.get("properties") or {}).get("name") or "") for feature in feature_rows]
    ids = [str(feature.get("id")) for feature in feature_rows]
    osm_ids = [
        (feature.get("properties") or {}).get("upstream_osm_record_id")
        for feature in feature_rows
    ]
    osm_ids = [str(value) for value in osm_ids if value]
    boundary_area = float(boundary.area)
    union_area = float(union.area)
    overlap_excess = max(0.0, sum(float(geom.area) for geom in geometries) - union_area)
    candidate_manifest = json.loads(args.candidate_manifest.read_text(encoding="utf-8"))
    mask_manifest = json.loads(args.boundary_manifest.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "mapped_candidate_geometry_audit_not_legal_validation",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidate_file": str(args.candidates.relative_to(ROOT)),
        "candidate_sha256": sha256_file(args.candidates),
        "candidate_manifest_sha256": sha256_file(args.candidate_manifest),
        "mask_file": str(args.boundary.relative_to(ROOT)),
        "mask_sha256": mask_manifest.get("output_sha256"),
        "mask_osm_relation_id": mask_manifest.get("relation_id"),
        "mask_osm_relation_version": mask_manifest.get("relation_version"),
        "feature_count": len(feature_rows),
        "valid_geometry_count": sum(geom.is_valid and not geom.is_empty for geom in geometries),
        "polygon_or_multipolygon_count": sum(geom.geom_type in {"Polygon", "MultiPolygon"} for geom in geometries),
        "unique_overture_id_count": len(set(ids)),
        "unique_name_count": len(set(names)),
        "duplicate_names": sorted(name for name, count in Counter(names).items() if count > 1),
        "unique_upstream_osm_record_count": len(set(osm_ids)),
        "candidate_union_coverage_of_mask_pct": round(union_area / boundary_area * 100, 6) if boundary_area else None,
        "uncovered_mask_area_degrees_squared": round(float(uncovered.area), 12),
        "uncovered_mask_area_pct": round(float(uncovered.area) / boundary_area * 100, 6) if boundary_area else None,
        "candidate_union_outside_mask_area_degrees_squared": round(float(outside.area), 12),
        "pairwise_overlap_excess_area_degrees_squared": round(overlap_excess, 12),
        "area_unit_note": "Planar WGS84 degree-squared values are used only for same-mask ratios; they are not metric areas.",
        "candidate_filter": candidate_manifest.get("filter"),
        "candidate_source_release": candidate_manifest.get("source_release"),
        "candidate_source_sha256": candidate_manifest.get("source_sha256"),
        "legal_reference": {
            "url": "https://www.mos.ru/upload/documents/docs/9303/967-PP-bngh1.pdf",
            "document": "Moscow Government Decree No. 967-PP, 2024-05-14, list of district administrations ends at index 133",
            "count_comparison": "The official administration-list index and mapped polygon-candidate count are different tests; the 130 features are not certified as a complete legal district set.",
            "authoritative_gis_layer_verified": False,
        },
        "candidate_records": [
            {
                "name": (feature.get("properties") or {}).get("name"),
                "overture_id": feature.get("id"),
                "upstream_osm_record_id": (feature.get("properties") or {}).get("upstream_osm_record_id"),
                "upstream_update_time": next(
                    (source.get("update_time") for source in (feature.get("properties") or {}).get("upstream_sources", []) if source.get("dataset") == "OpenStreetMap"),
                    None,
                ),
            }
            for feature in feature_rows
        ],
        "coverage_note": "Candidates are mapped Overture/OSM relations clipped to the stored OSM city mask. The set contains 130 unique named polygons, while the official district-administration list is indexed to 133; completeness and legal equivalence are not established. Do not assign or aggregate district facts using this set as authoritative.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "feature_count": report["feature_count"], "valid_geometry_count": report["valid_geometry_count"], "unique_name_count": report["unique_name_count"], "candidate_union_coverage_of_mask_pct": report["candidate_union_coverage_of_mask_pct"], "uncovered_mask_area_pct": report["uncovered_mask_area_pct"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
