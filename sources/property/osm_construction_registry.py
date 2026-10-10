"""Extract explicitly tagged residential construction footprints from OSM.

This is a local, offline adapter. It does not geocode, infer a complex, create
an apartment offer, or add prices. Output is a mapped-footprint candidate
registry and remains separate from the offer catalogue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SOURCE_ID = "osm_moscow_residential_construction"
SOURCE_SNAPSHOT_AT = "2026-10-08T20:21:06Z"
SOURCE_FILE = "data/social/osm_moscow_city_261008.osm.pbf"
SOURCE_SHA256 = "78aff2d4f6707c584f66febd8fb594c679b434f9046562d8d6a2c40d55da6014"
BOUNDARY_FILE = "data/city/osm_moscow_city_boundary.geojson"
BOUNDARY_SHA256 = "f08ff6b0c794fd3f079ae0f9daf004f9d8de64529a7327e0fc6444788561da29"
SOURCE_URL = "https://download.geofabrik.de/russia/central-fed-district-261008.osm.pbf"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
OSM_LICENSE = "ODbL-1.0"
ATTRIBUTION = "© OpenStreetMap contributors"
RESIDENTIAL_CONSTRUCTION_VALUES = frozenset({"apartments", "residential"})
CANDIDATE_RULE_VERSION = "building-construction-residential/1"
GEOPARQUET_SCHEMA_METADATA = {
    "geo": json.dumps(
        {
            "version": "1.1.0",
            "primary_column": "geometry",
            "columns": {
                "geometry": {
                    "encoding": "WKB",
                    "geometry_types": ["Polygon", "MultiPolygon"],
                    "crs": None,
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8"),
    "novostroy.source": f"{ATTRIBUTION}; {OSM_LICENSE}".encode("utf-8"),
}


def classify_construction_tags(tags: Mapping[str, Any]) -> str | None:
    """Return the strict residential candidate rule, else None.

    Generic construction, `building=apartments`, dormitories, misspellings,
    and inferred residential use are intentionally not admitted.
    """
    if tags.get("building") != "construction":
        return None
    construction = tags.get("construction")
    if construction not in RESIDENTIAL_CONSTRUCTION_VALUES:
        return None
    return f"building=construction AND construction={construction}"


def source_record_id(osm_type: str, osm_id: int, version: int) -> str:
    if osm_type not in {"way", "relation"}:
        raise ValueError("OSM source type must be way or relation")
    if osm_id <= 0 or version <= 0:
        raise ValueError("OSM id and version must be positive")
    return f"{osm_type}/{osm_id}/v{version}"


def _iso_timestamp(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be ISO-8601") from exc
    else:
        raise ValueError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _polygonal_geometry(geometry: Any) -> Any | None:
    """Keep only polygon components after clipping a footprint to Moscow."""
    if geometry.is_empty:
        return None
    if geometry.geom_type == "Polygon":
        return geometry if geometry.area > 0 else None
    if geometry.geom_type == "MultiPolygon":
        return geometry if geometry.area > 0 else None
    polygons = [
        part
        for part in getattr(geometry, "geoms", ())
        if part.geom_type == "Polygon" and part.area > 0
    ]
    if not polygons:
        return None
    from shapely.geometry import MultiPolygon

    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def normalize_osm_area(
    area: Any,
    geometry: Any,
    boundary: Any,
    *,
    observed_at: str,
    snapshot_at: str = SOURCE_SNAPSHOT_AT,
) -> dict[str, Any] | None:
    """Normalize one OSM area if its tags and footprint qualify.

    `geometry` and `boundary` are Shapely geometries. The original tag map is
    retained without field-name normalization. The footprint is intersected
    with the pinned administrative boundary; no address or name is inferred.
    """
    tags = dict(area.tags)
    rule = classify_construction_tags(tags)
    if rule is None:
        return None

    observed = _iso_timestamp(observed_at, field="observed_at")
    source_updated = _iso_timestamp(area.timestamp, field="source_updated_at")
    snapshot = _iso_timestamp(snapshot_at, field="source_snapshot_at")
    clipped = _polygonal_geometry(geometry.intersection(boundary))
    if clipped is None:
        return None

    osm_type = "way" if area.from_way() else "relation"
    osm_id = int(area.orig_id())
    version = int(area.version)
    record_id = source_record_id(osm_type, osm_id, version)
    normalized_tags = json.dumps(tags, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    address_tags = {
        key: value for key, value in sorted(tags.items()) if key.startswith("addr:")
    }
    source_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
    return {
        "geometry": bytes(clipped.wkb),
        "source_id": SOURCE_ID,
        "source_record_id": record_id,
        "record_type": "registry_event",
        "record_mode": "registry_status",
        "data_mode": "real",
        "observed_at": observed,
        "source_updated_at": source_updated,
        "source_version": snapshot,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": version,
        "source_url": source_url,
        "candidate_rule": rule,
        "candidate_rule_version": CANDIDATE_RULE_VERSION,
        "construction_use": str(tags["construction"]),
        "name": tags.get("name"),
        "address_json": json.dumps(
            address_tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "raw_tags": normalized_tags,
        "rights_status": "cleared",
        "rights_license": OSM_LICENSE,
        "attribution": ATTRIBUTION,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_registry(
    pbf_path: Path,
    boundary_path: Path,
    *,
    observed_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract strict residential construction candidates from a local PBF."""
    try:
        import osmium
        import pyarrow as pa
        import pyarrow.parquet as pq  # noqa: F401 - checked with other deps here
        from shapely import from_wkb
        from shapely.geometry import shape
    except ImportError as exc:  # pragma: no cover - exercised in CLI environment
        raise RuntimeError(
            "OSM export dependencies missing; install sources/property/requirements-osm.txt"
        ) from exc

    actual_pbf_hash = _sha256(pbf_path)
    if actual_pbf_hash != SOURCE_SHA256:
        raise ValueError(
            f"unexpected OSM PBF SHA-256 {actual_pbf_hash}; expected pinned {SOURCE_SHA256}"
        )
    actual_boundary_hash = _sha256(boundary_path)
    if actual_boundary_hash != BOUNDARY_SHA256:
        raise ValueError(
            f"unexpected boundary SHA-256 {actual_boundary_hash}; expected pinned {BOUNDARY_SHA256}"
        )

    try:
        observed = _iso_timestamp(observed_at, field="observed_at")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    boundary_geojson = json.loads(boundary_path.read_text(encoding="utf-8"))
    features = boundary_geojson.get("features", [])
    if len(features) != 1:
        raise ValueError("boundary GeoJSON must contain exactly one pinned feature")
    boundary = shape(features[0]["geometry"])
    wkb_factory = osmium.geom.WKBFactory()
    records: list[dict[str, Any]] = []
    construction_counts: Counter[str] = Counter()
    all_construction_areas = 0
    invalid_or_outside = 0

    class Handler(osmium.SimpleHandler):
        def area(self, area: Any) -> None:
            nonlocal all_construction_areas, invalid_or_outside
            tags = dict(area.tags)
            if tags.get("building") != "construction":
                return
            try:
                geom = from_wkb(wkb_factory.create_multipolygon(area))
            except Exception:
                invalid_or_outside += 1
                return
            intersection = geom.intersection(boundary)
            if intersection.is_empty or intersection.area <= 0:
                return
            all_construction_areas += 1
            value = str(tags.get("construction", "<missing>"))
            construction_counts[value] += 1
            record = normalize_osm_area(
                area,
                geom,
                boundary,
                observed_at=observed,
                snapshot_at=SOURCE_SNAPSHOT_AT,
            )
            if record is None:
                return
            clipped_geometry = from_wkb(record["geometry"])
            bounds = clipped_geometry.bounds
            record["geometry_type"] = clipped_geometry.geom_type
            record["geometry_bbox"] = [float(round(v, 7)) for v in bounds]
            records.append(record)

    handler = Handler()
    handler.apply_file(str(pbf_path), locations=True, idx="flex_mem")
    records.sort(key=lambda row: row["source_record_id"])
    stats = {
        "tagged_building_construction_areas_in_moscow": all_construction_areas,
        "strict_residential_candidates": len(records),
        "strict_residential_by_construction_tag": dict(
            sorted(Counter(row["construction_use"] for row in records).items())
        ),
        "candidate_objects_by_osm_type": dict(
            sorted(Counter(row["osm_type"] for row in records).items())
        ),
        "name_tag_present": sum(row["name"] is not None for row in records),
        "address_tag_present": sum(
            json.loads(row["address_json"]) != {} for row in records
        ),
        "source_edit_timestamp_present": sum(
            row["source_updated_at"] is not None for row in records
        ),
        "all_construction_values_in_moscow": dict(sorted(construction_counts.items())),
        "unreadable_area_geometries": invalid_or_outside,
        "candidate_rule_version": CANDIDATE_RULE_VERSION,
    }
    return records, stats


def write_outputs(
    records: list[dict[str, Any]],
    stats: Mapping[str, Any],
    *,
    output_dir: Path,
    pbf_path: Path,
    boundary_path: Path,
    observed_at: str,
) -> dict[str, Any]:
    """Write a GeoParquet registry, JSONL audit, and license/hash manifest."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        from shapely import from_wkb
    except ImportError as exc:  # pragma: no cover - exercised in CLI environment
        raise RuntimeError(
            "GeoParquet export dependencies missing; install sources/property/requirements-osm.txt"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "osm_moscow_residential_construction_2026-10-08"
    parquet_path = output_dir / f"{stem}.parquet"
    audit_path = output_dir / f"{stem}.jsonl"
    manifest_path = output_dir / "source_manifest.json"
    fields = [
        "geometry",
        "source_id",
        "source_record_id",
        "record_type",
        "record_mode",
        "data_mode",
        "observed_at",
        "source_updated_at",
        "source_version",
        "osm_type",
        "osm_id",
        "osm_version",
        "source_url",
        "candidate_rule",
        "candidate_rule_version",
        "construction_use",
        "name",
        "address_json",
        "raw_tags",
        "rights_status",
        "rights_license",
        "attribution",
        "geometry_type",
        "geometry_bbox",
    ]
    arrays: dict[str, Any] = {}
    for field in fields:
        values = [row.get(field) for row in records]
        if field == "geometry":
            arrays[field] = pa.array(values, type=pa.binary())
        elif field in {"osm_id", "osm_version"}:
            arrays[field] = pa.array(values, type=pa.int64())
        elif field == "geometry_bbox":
            arrays[field] = pa.array(values, type=pa.list_(pa.float64()))
        else:
            arrays[field] = pa.array(values, type=pa.string())
    table = pa.table(arrays).replace_schema_metadata(GEOPARQUET_SCHEMA_METADATA)
    pq.write_table(table, parquet_path, compression="zstd", version="2.6")

    audit_rows: list[dict[str, Any]] = []
    for row in records:
        audit_rows.append(
            {
                key: row[key]
                for key in fields
                if key not in {"geometry", "geometry_bbox"}
            }
            | {
                "geometry_sha256": hashlib.sha256(row["geometry"]).hexdigest(),
                "geometry_bbox": row["geometry_bbox"],
            }
        )
    with audit_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in audit_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    bbox = None
    if records:
        geoms = [from_wkb(row["geometry"]) for row in records]
        bbox = [
            float(round(min(g.bounds[0] for g in geoms), 7)),
            float(round(min(g.bounds[1] for g in geoms), 7)),
            float(round(max(g.bounds[2] for g in geoms), 7)),
            float(round(max(g.bounds[3] for g in geoms), 7)),
        ]
    manifest = {
        "schema_version": "property-registry/osm-construction/1",
        "source_id": SOURCE_ID,
        "record_type": "registry_event",
        "record_mode": "registry_status",
        "data_mode": "real",
        "observed_at": _iso_timestamp(observed_at, field="observed_at"),
        "source_snapshot_at": SOURCE_SNAPSHOT_AT,
        "source": {
            "name": "OpenStreetMap via Geofabrik Central Federal District extract",
            "url": SOURCE_URL,
            "copyright_url": OSM_COPYRIGHT_URL,
            "license": OSM_LICENSE,
            "rights_status": "cleared",
            "attribution": ATTRIBUTION,
            "obligations": [
                "Credit OpenStreetMap and its contributors.",
                "Distribute altered or built-upon OSM database output under ODbL-1.0.",
            ],
        },
        "input": {
            "file": str(pbf_path),
            "sha256": _sha256(pbf_path),
            "expected_sha256": SOURCE_SHA256,
            "size_bytes": pbf_path.stat().st_size,
            "snapshot_at": SOURCE_SNAPSHOT_AT,
        },
        "boundary": {
            "file": str(boundary_path),
            "sha256": _sha256(boundary_path),
            "expected_sha256": BOUNDARY_SHA256,
            "osm_relation_id": 102269,
            "osm_relation_version": 422,
            "geometry_crs": "EPSG:4326 / OGC:CRS84 coordinate order",
            "coverage": "only OSM features whose polygon geometry intersects the pinned Moscow relation polygon",
        },
        "selection": {
            "candidate_rule_version": CANDIDATE_RULE_VERSION,
            "required_tags": {
                "building": "construction",
                "construction": sorted(RESIDENTIAL_CONSTRUCTION_VALUES),
            },
            "generic_or_untyped_construction_included": False,
            "geometry_operation": "intersect candidate polygon with pinned boundary; retain polygonal parts only",
        },
        "output": {
            "geoparquet": str(parquet_path),
            "geoparquet_sha256": _sha256(parquet_path),
            "geoparquet_row_count": len(records),
            "geoparquet_format": "GeoParquet 1.1.0; WKB; EPSG:4326 / OGC:CRS84 coordinate order",
            "audit_jsonl": str(audit_path),
            "audit_sha256": _sha256(audit_path),
            "bbox_wgs84": bbox,
            "columns": [
                "geometry",
                "source_id",
                "source_record_id",
                "record_type",
                "record_mode",
                "data_mode",
                "observed_at",
                "source_updated_at",
                "source_version",
                "osm_type",
                "osm_id",
                "osm_version",
                "source_url",
                "candidate_rule",
                "candidate_rule_version",
                "construction_use",
                "name",
                "address_json",
                "raw_tags",
                "rights_status",
                "rights_license",
                "attribution",
                "geometry_type",
                "geometry_bbox",
            ],
        },
        "stats": dict(stats),
        "limitations": [
            "An OSM building=construction tag is a community-mapped label, not an official permit or independently verified current construction status.",
            "Candidate footprints are not residential complexes, apartment units, sales listings, or asking offers; no prices or availability are supplied.",
            "No addresses, names, project identities, or geometry were inferred beyond explicit OSM tags and polygon geometry.",
            "OSM mapping completeness and freshness are unknown; snapshot date does not establish ground-truth status at that date.",
            "This layer must not be joined to offer or complex records without independent verified linkage evidence.",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=Path(SOURCE_FILE))
    parser.add_argument("--boundary", type=Path, default=Path(BOUNDARY_FILE))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/property/osm_construction")
    )
    parser.add_argument("--observed-at", required=True)
    args = parser.parse_args()
    records, stats = extract_registry(
        args.pbf, args.boundary, observed_at=args.observed_at
    )
    manifest = write_outputs(
        records,
        stats,
        output_dir=args.output_dir,
        pbf_path=args.pbf,
        boundary_path=args.boundary,
        observed_at=args.observed_at,
    )
    print(
        json.dumps(
            {
                "records": manifest["output"]["geoparquet_row_count"],
                "geoparquet": manifest["output"]["geoparquet"],
                "manifest": str(args.output_dir / "source_manifest.json"),
                "stats": stats,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
