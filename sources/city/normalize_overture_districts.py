#!/usr/bin/env python3
"""Extract mapped Moscow district-name polygons from a pinned Overture bbox.

This is a candidate mapping layer, not a legal district boundary certification.
The original Overture GeoParquet remains the immutable source geometry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = ROOT / "data/city/environment/overture_division_area_2026-09-23.1_attempt01_20261009T100517Z.parquet"
DEFAULT_SOURCE_MANIFEST = DEFAULT_SOURCE.with_suffix(".manifest.json")
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _polygonal(geometry: Any) -> Any:
    from shapely.geometry import GeometryCollection

    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    if geometry.geom_type == "GeometryCollection":
        polygons = [part for part in geometry.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
        if not polygons:
            return GeometryCollection()
        from shapely.ops import unary_union

        return unary_union(polygons)
    return GeometryCollection()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--output", type=Path, default=ROOT / "data/city/environment/moscow_district_candidates_2026-09-23.1.geojson")
    args = parser.parse_args()
    for path in (args.source, args.source_manifest, args.boundary, args.boundary_manifest):
        if not path.is_file():
            parser.error(f"required file is missing: {path}")
    for path in (args.output, args.output.with_suffix(".manifest.json")):
        if path.exists():
            parser.error(f"refusing to overwrite {path}")

    import pyarrow.parquet as pq
    from shapely import wkb
    from shapely.geometry import shape, mapping

    source_manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    boundary_manifest = json.loads(args.boundary_manifest.read_text(encoding="utf-8"))
    boundary_doc = json.loads(args.boundary.read_text(encoding="utf-8"))
    boundary = shape(boundary_doc["features"][0]["geometry"])
    table = pq.read_table(
        args.source,
        columns=["id", "geometry", "country", "subtype", "admin_level", "names", "sources", "version", "bbox"],
    )
    features: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_osm_records: set[str] = set()
    invalid = clipped = 0
    intersecting_name_candidates = 0
    osm_versions: list[str] = []
    source_parquet_hash = sha256_file(args.source)
    for row in table.to_pylist():
        names = row.get("names") or {}
        name = names.get("primary") or ""
        if row.get("country") != "RU" or row.get("subtype") != "locality":
            continue
        if "район" not in name.casefold():
            continue
        geom = wkb.loads(row["geometry"])
        if not geom.is_valid:
            invalid += 1
            continue
        if not geom.intersects(boundary):
            continue
        intersecting_name_candidates += 1
        source_records = row.get("sources") or []
        osm_record = next(
            (source.get("record_id") for source in source_records if source.get("dataset") == "OpenStreetMap"),
            None,
        )
        if osm_record:
            if osm_record in seen_osm_records:
                continue
            seen_osm_records.add(osm_record)
            osm_versions.append(osm_record.rsplit("@", 1)[-1])
        source_id = str(row.get("id") or "")
        if not source_id or source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        clipped_geom = _polygonal(geom.intersection(boundary))
        if clipped_geom.is_empty or clipped_geom.area <= 0:
            continue
        if not clipped_geom.equals(geom):
            clipped += 1
        features.append({
            "type": "Feature",
            "id": source_id,
            "geometry": mapping(clipped_geom),
            "properties": {
                "source": "Overture Maps Divisions",
                "source_id": source_id,
                "source_release": source_manifest.get("release"),
                "source_version": row.get("version"),
                "source_type": "division_area",
                "subtype": row.get("subtype"),
                "name": name,
                "names": names,
                "country": row.get("country"),
                "admin_level": row.get("admin_level"),
                "upstream_sources": source_records,
                "upstream_osm_record_id": osm_record,
                "boundary_mask_relation_id": boundary_manifest.get("relation_id"),
                "boundary_mask_relation_version": boundary_manifest.get("relation_version"),
                "boundary_mask_sha256": boundary_manifest.get("output_sha256"),
                "boundary_authority": "mapped_candidate_not_legally_verified",
            },
        })

    features.sort(key=lambda item: ((item["properties"].get("name") or "").casefold(), item["id"]))
    if not features:
        raise SystemExit("no matching polygon candidates were found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_doc = {
        "type": "FeatureCollection",
        "name": "Moscow district-name mapped polygon candidates",
        "source_release": source_manifest.get("release"),
        "features": features,
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(output_doc, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    output_hash = sha256_file(args.output)
    manifest = {
        "status": "mapped_candidate_layer_not_legal_boundary_certification",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "output_file": args.output.name,
        "output_bytes": args.output.stat().st_size,
        "output_sha256": output_hash,
        "feature_count": len(features),
        "intersecting_name_candidates_before_dedup": intersecting_name_candidates,
        "unique_overture_id_count": len(seen_ids),
        "unique_upstream_osm_record_id_count": len(seen_osm_records),
        "invalid_source_geometry_count": invalid,
        "clipped_to_city_mask_count": clipped,
        "filter": "country=RU AND subtype=locality AND primary name contains the literal Russian word 'район' AND geometry intersects the stored Moscow city OSM mask",
        "source_file": str(args.source.relative_to(ROOT)),
        "source_sha256": source_parquet_hash,
        "source_release": source_manifest.get("release"),
        "source_row_count": source_manifest.get("row_count"),
        "schema_version": source_manifest.get("schema_version"),
        "source_license": source_manifest.get("license"),
        "attribution": source_manifest.get("attribution"),
        "boundary_mask_file": str(args.boundary.relative_to(ROOT)),
        "boundary_mask_sha256": boundary_manifest.get("output_sha256"),
        "boundary_mask_relation_id": boundary_manifest.get("relation_id"),
        "boundary_mask_relation_version": boundary_manifest.get("relation_version"),
        "boundary_mask_legal_authority": False,
        "coverage_note": "Mapped Overture/OSM polygon candidates only. The input query was a city envelope, not a legal boundary dataset. Features are clipped to the selected OSM city mask. Candidate set may be incomplete, differently classified, or overlap; do not use as authoritative district assignment until compared with the current legal district set.",
    }
    manifest_path = args.output.with_suffix(".manifest.json")
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"features": len(features), "invalid": invalid, "clipped": clipped, "output": str(args.output), "sha256": output_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
