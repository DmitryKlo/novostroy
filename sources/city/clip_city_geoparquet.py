#!/usr/bin/env python3
"""Stream Overture building GeoParquet and clip features to the OSM city boundary."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry import GeometryCollection, shape

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = ROOT / "data/city/overture_moscow_envelope_2026-09-23.1_attempt01_20261009T092726Z.parquet"
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"
DEFAULT_OUTPUT = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
BATCH_SIZE = 8192


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def polygonal(geometry: Any) -> Any | None:
    if geometry.is_empty:
        return None
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    parts = []
    for part in getattr(geometry, "geoms", []):
        result = polygonal(part)
        if result is not None:
            if result.geom_type == "Polygon":
                parts.append(result)
            else:
                parts.extend(result.geoms)
    if not parts:
        return None
    merged = shapely.union_all(parts)
    if merged.geom_type in {"Polygon", "MultiPolygon"}:
        return merged
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(".manifest.json").exists():
        parser.error("refusing to overwrite an existing output; choose a new --output")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    boundary_document = json.loads(args.boundary.read_text(encoding="utf-8"))
    boundary = shape(boundary_document["features"][0]["geometry"])
    if boundary.is_empty or not boundary.is_valid:
        parser.error("boundary geometry must be nonempty and valid")
    boundary_manifest = json.loads(args.boundary_manifest.read_text(encoding="utf-8"))
    source_file = pq.ParquetFile(args.input)
    schema = source_file.schema_arrow
    geometry_index = schema.get_field_index("geometry")
    bbox_index = schema.get_field_index("bbox")
    id_index = schema.get_field_index("id")
    sources_index = schema.get_field_index("sources")
    height_index = schema.get_field_index("height")
    floors_index = schema.get_field_index("num_floors")
    if min(geometry_index, bbox_index, id_index, sources_index) < 0:
        parser.error("source schema lacks geometry, bbox, id, or sources column")

    counts = {
        "input_rows": 0,
        "output_rows": 0,
        "outside_boundary_rows_dropped": 0,
        "intersecting_rows_clipped": 0,
        "boundary_touch_only_rows_dropped": 0,
        "invalid_source_geometries": 0,
        "invalid_source_geometries_repaired": 0,
        "valid_output_geometries": 0,
        "invalid_output_geometries": 0,
        "height_present_rows": 0,
        "floors_present_rows": 0,
        "feature_rows_without_sources": 0,
        "source_record_count": 0,
    }
    geometry_types: dict[str, int] = {}
    source_dataset_counts: dict[str, int] = {}
    source_license_counts: dict[str, int] = {}
    source_records_per_feature: dict[str, int] = {}
    unique_ids: set[str] = set()
    writer = pq.ParquetWriter(args.output, schema, compression="zstd", version="2.6")
    try:
        for batch in source_file.iter_batches(batch_size=args.batch_size):
            counts["input_rows"] += batch.num_rows
            source_wkb = batch.column(geometry_index).to_pylist()
            geometries = shapely.from_wkb(source_wkb)
            source_valid = shapely.is_valid(geometries)
            invalid_indexes = [i for i, valid in enumerate(source_valid) if not bool(valid)]
            counts["invalid_source_geometries"] += len(invalid_indexes)
            if invalid_indexes:
                repaired = shapely.make_valid(geometries)
                for index in invalid_indexes:
                    geometry = polygonal(repaired[index])
                    geometries[index] = geometry if geometry is not None else GeometryCollection()
                counts["invalid_source_geometries_repaired"] += len(invalid_indexes)
            intersects = shapely.intersects(boundary, geometries)
            kept_indexes: list[int] = []
            kept_wkb: list[bytes] = []
            kept_bounds: list[dict[str, float]] = []
            for index in (i for i, value in enumerate(intersects) if bool(value)):
                geometry = geometries[index]
                covered = bool(shapely.covers(boundary, geometry))
                if covered and bool(source_valid[index]):
                    output_geometry = geometry
                    output_bytes = source_wkb[index]
                else:
                    if not covered:
                        counts["intersecting_rows_clipped"] += 1
                    output_geometry = geometry if covered else polygonal(shapely.intersection(geometry, boundary))
                    if output_geometry is None or output_geometry.is_empty:
                        counts["boundary_touch_only_rows_dropped"] += 1
                        continue
                    output_bytes = shapely.to_wkb(output_geometry)
                valid = bool(shapely.is_valid(output_geometry))
                if valid:
                    counts["valid_output_geometries"] += 1
                else:
                    counts["invalid_output_geometries"] += 1
                geometry_types[output_geometry.geom_type] = geometry_types.get(output_geometry.geom_type, 0) + 1
                kept_indexes.append(index)
                kept_wkb.append(output_bytes)
                xmin, ymin, xmax, ymax = output_geometry.bounds
                kept_bounds.append({"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax})
                value = batch.column(id_index)[index].as_py()
                if value in unique_ids:
                    raise ValueError(f"duplicate Overture ID in clipped result: {value}")
                unique_ids.add(value)
                source_records = batch.column(sources_index)[index].as_py() or []
                if not source_records:
                    counts["feature_rows_without_sources"] += 1
                source_records_per_feature[str(len(source_records))] = (
                    source_records_per_feature.get(str(len(source_records)), 0) + 1
                )
                for source_record in source_records:
                    dataset = source_record.get("dataset") or "<null>"
                    license_name = source_record.get("license") or "<null>"
                    source_dataset_counts[dataset] = source_dataset_counts.get(dataset, 0) + 1
                    source_license_counts[license_name] = source_license_counts.get(license_name, 0) + 1
                    counts["source_record_count"] += 1
            counts["outside_boundary_rows_dropped"] += batch.num_rows - sum(bool(x) for x in intersects)
            counts["output_rows"] += len(kept_indexes)
            if height_index >= 0:
                counts["height_present_rows"] += sum(
                    batch.column(height_index)[i].as_py() is not None for i in kept_indexes
                )
            if floors_index >= 0:
                counts["floors_present_rows"] += sum(
                    batch.column(floors_index)[i].as_py() is not None for i in kept_indexes
                )
            if not kept_indexes:
                continue
            take_indexes = pa.array(kept_indexes, type=pa.int64())
            columns = [batch.column(i).take(take_indexes) for i in range(batch.num_columns)]
            columns[geometry_index] = pa.array(kept_wkb, type=schema.field(geometry_index).type)
            columns[bbox_index] = pa.array(kept_bounds, type=schema.field(bbox_index).type)
            writer.write_batch(pa.RecordBatch.from_arrays(columns, schema=schema))
    finally:
        writer.close()

    output_rows_metadata = pq.ParquetFile(args.output).metadata.num_rows
    if output_rows_metadata != counts["output_rows"]:
        raise ValueError("written Parquet row count does not match streamed output count")
    manifest = {
        "status": "city_boundary_clipped",
        "source_file": args.input.name,
        "source_bytes": args.input.stat().st_size,
        "source_sha256": sha256(args.input),
        "source_release": "2026-09-23.1",
        "source_schema_version": "2.0.0",
        "boundary_relation_id": boundary_manifest.get("relation_id"),
        "boundary_relation_version": boundary_manifest.get("relation_version"),
        "boundary_relation_timestamp_utc": boundary_manifest.get("relation_timestamp_utc"),
        "boundary_license": "ODbL-1.0",
        "boundary_sha256": boundary_manifest.get("output_sha256"),
        "clip_rule": "keep polygonal building geometry intersecting boundary; keep fully covered geometry unchanged; otherwise use Shapely intersection and retain Polygon/MultiPolygon components; discard nonpolygonal touch-only results",
        "output_file": args.output.name,
        "output_bytes": args.output.stat().st_size,
        "output_sha256": sha256(args.output),
        "output_parquet_rows": output_rows_metadata,
        "unique_id_count": len(unique_ids),
        "duplicate_id_count": 0,
        "geometry_types": geometry_types,
        "source_record_count_by_dataset": source_dataset_counts,
        "source_record_count_by_license": source_license_counts,
        "source_records_per_feature": source_records_per_feature,
        "valid_geometry_count": counts["valid_output_geometries"],
        "invalid_geometry_count": counts["invalid_output_geometries"],
        "height_present_rows": counts["height_present_rows"],
        "floors_present_rows": counts["floors_present_rows"],
        "batch_size": args.batch_size,
        "pyarrow_version": pa.__version__,
        "shapely_version": shapely.__version__,
        "license": "ODbL-1.0 (Overture Buildings and OSM boundary-derived clipping)",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
        "source_geometry_preserved": "yes in source_file; output geometry is clipped to the OSM relation polygon",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        **counts,
    }
    manifest_path = args.output.with_suffix(".manifest.json")
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: manifest[k] for k in (
        "status", "input_rows", "output_rows", "unique_id_count", "outside_boundary_rows_dropped",
        "intersecting_rows_clipped", "height_present_rows", "floors_present_rows",
        "valid_geometry_count", "invalid_geometry_count", "output_bytes", "output_sha256",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"city clipping error: {exc}")
