"""Build an ODbL-attributed, city-clipped road GeoParquet from an OSM PBF.

This utility does not download data or call a route provider. It uses a local
``osmium`` executable to export highway ways and clips their line geometries
to a pinned boundary before writing WKB GeoParquet plus a provenance manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import to_wkb
from shapely.geometry import LineString, MultiLineString, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


ROAD_COLUMNS: dict[str, pa.DataType] = {
    "geometry": pa.binary(),
    "id": pa.string(),
    "osm_type": pa.string(),
    "osm_id": pa.int64(),
    "highway": pa.string(),
    "name": pa.string(),
    "foot": pa.string(),
    "access": pa.string(),
    "vehicle": pa.string(),
    "motor_vehicle": pa.string(),
    "bicycle": pa.string(),
    "oneway": pa.string(),
    "bridge": pa.string(),
    "tunnel": pa.string(),
    "level": pa.string(),
    "layer": pa.string(),
    "lit": pa.string(),
    "sidewalk": pa.string(),
    "surface": pa.string(),
    "smoothness": pa.string(),
    "maxspeed": pa.string(),
    "lanes": pa.string(),
    "tags_json": pa.string(),
}

TAG_COLUMNS = tuple(column for column in ROAD_COLUMNS if column not in {"geometry", "id", "osm_type", "osm_id", "tags_json"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_boundary(path: Path) -> BaseGeometry:
    document = json.loads(path.read_text(encoding="utf-8"))
    geometries = [shape(feature["geometry"]) for feature in document.get("features", [])]
    if not geometries and document.get("type") in {"Polygon", "MultiPolygon"}:
        geometries = [shape(document)]
    if not geometries:
        raise ValueError(f"No polygon geometry found in {path}")
    boundary = unary_union(geometries)
    if boundary.geom_type not in {"Polygon", "MultiPolygon"} or boundary.is_empty:
        raise ValueError(f"Boundary must be a non-empty polygon, got {boundary.geom_type}")
    return boundary


def _linear_parts(geometry: BaseGeometry) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from _linear_parts(part)


def _geo_schema() -> pa.Schema:
    metadata = {
        b"geo": json.dumps(
            {
                "version": "1.1.0",
                "primary_column": "geometry",
                "columns": {
                    "geometry": {
                        "encoding": "WKB",
                        "geometry_types": ["LineString", "MultiLineString"],
                        "crs": None,
                    }
                },
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        b"novostroy.source": b"OpenStreetMap contributors; ODbL-1.0",
    }
    return pa.schema([pa.field(name, data_type) for name, data_type in ROAD_COLUMNS.items()], metadata=metadata)


def _feature_rows(
    source: Path,
    boundary: BaseGeometry,
    *,
    batch_size: int,
    writer: pq.ParquetWriter,
) -> tuple[int, int, dict[str, int], list[float] | None]:
    batch: list[dict[str, Any]] = []
    way_count = 0
    clipped_count = 0
    class_counts: Counter[str] = Counter()
    bbox: list[float] | None = None
    schema = _geo_schema()

    def flush() -> None:
        if not batch:
            return
        arrays = [pa.array([row[name] for row in batch], type=data_type) for name, data_type in ROAD_COLUMNS.items()]
        writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
        batch.clear()

    with source.open(encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.lstrip("\x1e\r\n ")
            if not line:
                continue
            feature = json.loads(line)
            props = feature.get("properties") or {}
            raw_id = str(feature.get("id", ""))
            if not raw_id.startswith("w") or not props.get("highway"):
                continue
            way_count += 1
            geom_doc = feature.get("geometry")
            if not geom_doc:
                continue
            clipped = shape(geom_doc).intersection(boundary)
            parts = [part for part in _linear_parts(clipped) if not part.is_empty and len(part.coords) >= 2]
            if not parts:
                continue
            clipped_count += 1
            way_id = int(raw_id[1:])
            geom_out: BaseGeometry = parts[0] if len(parts) == 1 else MultiLineString(parts)
            minx, miny, maxx, maxy = geom_out.bounds
            if bbox is None:
                bbox = [minx, miny, maxx, maxy]
            else:
                bbox = [min(bbox[0], minx), min(bbox[1], miny), max(bbox[2], maxx), max(bbox[3], maxy)]
            highway = str(props["highway"])
            class_counts[highway] += 1
            tags = {str(key): str(value) for key, value in props.items()}
            row: dict[str, Any] = {
                "geometry": to_wkb(geom_out, hex=False, output_dimension=2),
                "id": f"osm:way:{way_id}",
                "osm_type": "way",
                "osm_id": way_id,
                "highway": highway,
                "tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
            for column in TAG_COLUMNS:
                value = props.get(column)
                row[column] = None if value is None else str(value)
            batch.append(row)
            if len(batch) >= batch_size:
                flush()
    flush()
    return way_count, clipped_count, dict(sorted(class_counts.items())), bbox


def build_roads(
    source_pbf: Path,
    boundary_geojson: Path,
    output_parquet: Path,
    output_manifest: Path,
    *,
    osmium: str,
    osm_snapshot: str,
    boundary_relation_id: int,
    boundary_version: int,
    batch_size: int = 5000,
) -> dict[str, Any]:
    for path in (source_pbf, boundary_geojson):
        if not path.is_file():
            raise FileNotFoundError(path)
    for path in (output_parquet, output_manifest):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    boundary = _read_boundary(boundary_geojson)

    with tempfile.TemporaryDirectory(prefix="novostroy-osm-roads-") as temp_dir:
        temp = Path(temp_dir)
        filtered_pbf = temp / "highways.osm.pbf"
        exported_seq = temp / "highways.geojsonl"
        subprocess.run(
            [osmium, "tags-filter", "--output", str(filtered_pbf), str(source_pbf), "w/highway"],
            check=True,
        )
        subprocess.run(
            [
                osmium,
                "export",
                "--output-format=geojsonseq",
                "--add-unique-id=type_id",
                "--geometry-types=linestring",
                "--output",
                str(exported_seq),
                str(filtered_pbf),
            ],
            check=True,
        )
        temp_parquet = output_parquet.with_name(output_parquet.name + ".partial")
        if temp_parquet.exists():
            raise FileExistsError(f"Refusing to overwrite partial output {temp_parquet}")
        schema = _geo_schema()
        writer = pq.ParquetWriter(temp_parquet, schema, compression="zstd", version="2.6")
        try:
            seen, clipped, class_counts, bbox = _feature_rows(
                exported_seq, boundary, batch_size=batch_size, writer=writer
            )
        finally:
            writer.close()
        if not clipped or bbox is None:
            temp_parquet.unlink(missing_ok=True)
            raise RuntimeError("No highway line geometries intersected the supplied boundary")
        os.replace(temp_parquet, output_parquet)

    manifest: dict[str, Any] = {
        "dataset": "OpenStreetMap highway ways clipped to Moscow city relation 102269",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "dataset": "Geofabrik Central Federal District PBF",
            "file": str(source_pbf),
            "sha256": _sha256(source_pbf),
            "osm_snapshot_timestamp": osm_snapshot,
            "upstream_url": "https://download.geofabrik.de/russia/central-fed-district-261008.osm.pbf",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        },
        "clip": {
            "boundary_file": str(boundary_geojson),
            "boundary_sha256": _sha256(boundary_geojson),
            "osm_relation_id": boundary_relation_id,
            "osm_relation_version": boundary_version,
            "method": "Shapely intersection of each OSM way LineString with the pinned WGS84 boundary polygon",
            "coverage_scope": "within the supplied relation polygon only; OSM mapping completeness is unknown",
            "bbox_wgs84": bbox,
        },
        "output": {
            "file": str(output_parquet),
            "format": "GeoParquet 1.1.0, WKB, EPSG:4326 / OGC:CRS84 coordinate order",
            "sha256": _sha256(output_parquet),
            "row_count": clipped,
            "input_highway_way_count": seen,
            "ways_intersecting_boundary": clipped,
            "highway_class_counts": class_counts,
            "columns": list(ROAD_COLUMNS),
            "raw_osm_tags_column": "tags_json",
            "preserved_routing_tags": ["foot", "access", "vehicle", "motor_vehicle", "bicycle", "oneway", "bridge", "tunnel", "level", "layer"],
            "geometry_is_polygon_clipped": True,
        },
        "rights": {
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "notice": "Use and redistribution must comply with ODbL database and attribution obligations.",
        },
        "limitations": [
            "This is an OSM mapped-road snapshot, not proof that every road or public passage is mapped.",
            "No pedestrian routing graph or travel times are included.",
            "Preserved access tags have not yet been interpreted into a routability profile.",
            "The relation geometry is a community-mapped administrative boundary, not legal certification.",
        ],
    }
    temp_manifest = output_manifest.with_name(output_manifest.name + ".partial")
    if temp_manifest.exists():
        raise FileExistsError(f"Refusing to overwrite partial output {temp_manifest}")
    temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_manifest, output_manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pbf", type=Path, required=True)
    parser.add_argument("--boundary-geojson", type=Path, required=True)
    parser.add_argument("--output-parquet", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--osmium", default=shutil.which("osmium") or "osmium")
    parser.add_argument("--osm-snapshot", required=True, help="UTC replication timestamp of source snapshot")
    parser.add_argument("--boundary-relation-id", type=int, default=102269)
    parser.add_argument("--boundary-version", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()
    result = build_roads(
        args.source_pbf,
        args.boundary_geojson,
        args.output_parquet,
        args.output_manifest,
        osmium=args.osmium,
        osm_snapshot=args.osm_snapshot,
        boundary_relation_id=args.boundary_relation_id,
        boundary_version=args.boundary_version,
        batch_size=args.batch_size,
    )
    print(json.dumps(result["output"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
