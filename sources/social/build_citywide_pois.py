"""Extract classified social POIs from a local OSM PBF into clipped GeoParquet.

The command uses local ``osmium`` tools only. It preserves source geometry and
does not create centroids, entrances, route times, or completeness claims.
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
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from sources.social.taxonomy import TAXONOMY_VERSION, classify_criteria_tags, source_access_kind


POI_COLUMNS: dict[str, pa.DataType] = {
    "geometry": pa.binary(),
    "id": pa.string(),
    "osm_type": pa.string(),
    "osm_id": pa.int64(),
    "osm_version": pa.int32(),
    "osm_timestamp": pa.string(),
    "category": pa.string(),
    "name": pa.string(),
    "access_kind": pa.string(),
    "access_point": pa.binary(),
    "tags_json": pa.string(),
}

FILTERS = [
    "nwr/amenity=clinic,kindergarten,school,pharmacy,police,fire_station",
    "nwr/healthcare=clinic",
    "nwr/leisure=park",
    "nwr/shop=supermarket",
    "nwr/highway=bus_stop",
    "nwr/public_transport=platform,stop_position,station",
    "nwr/railway=tram_stop,station,subway_entrance",
    "nwr/emergency=ambulance_station",
    "nwr/bus=yes",
    "nwr/tram=yes",
    "nwr/subway=yes",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_boundary(path: Path) -> BaseGeometry:
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


def _parts(geometry: BaseGeometry, wanted: str) -> Iterable[BaseGeometry]:
    if wanted == "point" and isinstance(geometry, Point):
        yield geometry
    elif wanted == "line" and isinstance(geometry, LineString):
        yield geometry
    elif wanted == "line" and isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif wanted == "polygon" and isinstance(geometry, Polygon):
        yield geometry
    elif wanted == "polygon" and isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from _parts(part, wanted)


def _geo_schema() -> pa.Schema:
    metadata = {
        b"geo": json.dumps(
            {
                "version": "1.1.0",
                "primary_column": "geometry",
                "columns": {
                    "geometry": {
                        "encoding": "WKB",
                        "geometry_types": ["Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"],
                        "crs": None,
                    },
                    "access_point": {
                        "encoding": "WKB",
                        "geometry_types": ["Point"],
                        "crs": None,
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        b"novostroy.source": b"OpenStreetMap contributors; ODbL-1.0",
    }
    return pa.schema([pa.field(name, kind) for name, kind in POI_COLUMNS.items()], metadata=metadata)


def _clip_geometry(geometry: BaseGeometry, boundary: BaseGeometry) -> BaseGeometry | None:
    if isinstance(geometry, Point):
        return geometry if boundary.covers(geometry) else None
    if isinstance(geometry, (LineString, MultiLineString)):
        clipped = geometry.intersection(boundary)
        parts = [part for part in _parts(clipped, "line") if not part.is_empty and len(part.coords) >= 2]
        return None if not parts else (parts[0] if len(parts) == 1 else MultiLineString(parts))
    if isinstance(geometry, (Polygon, MultiPolygon)):
        clipped = geometry.intersection(boundary)
        parts = [part for part in _parts(clipped, "polygon") if not part.is_empty and part.area > 0]
        return None if not parts else (parts[0] if len(parts) == 1 else MultiPolygon(parts))
    return None


def build_pois(
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
    if not source_pbf.is_file() or not boundary_geojson.is_file():
        raise FileNotFoundError("Source PBF and boundary GeoJSON must exist")
    for path in (output_parquet, output_manifest):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}")
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    boundary = read_boundary(boundary_geojson)
    schema = _geo_schema()
    temporary_output = output_parquet.with_name(output_parquet.name + ".partial")
    if temporary_output.exists():
        raise FileExistsError(f"Refusing to overwrite {temporary_output}")

    category_counts: Counter[str] = Counter()
    geometry_counts: Counter[str] = Counter()
    bbox: list[float] | None = None
    source_rows = 0
    output_rows = 0
    rows: list[dict[str, Any]] = []
    writer = pq.ParquetWriter(temporary_output, schema, compression="zstd", version="2.6")

    def flush() -> None:
        if not rows:
            return
        arrays = [pa.array([row[name] for row in rows], type=kind) for name, kind in POI_COLUMNS.items()]
        writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
        rows.clear()

    try:
        with tempfile.TemporaryDirectory(prefix="novostroy-osm-pois-") as temp_dir:
            temp = Path(temp_dir)
            filters_file = temp / "filters.txt"
            filters_file.write_text("\n".join(FILTERS) + "\n", encoding="utf-8")
            filtered_pbf = temp / "social.osm.pbf"
            exported_seq = temp / "social.geojsonl"
            subprocess.run(
                [osmium, "tags-filter", "--expressions", str(filters_file), "--output", str(filtered_pbf), str(source_pbf)],
                check=True,
            )
            subprocess.run(
                [
                    osmium,
                    "export",
                    "--output-format=geojsonseq",
                    "--add-unique-id=type_id",
                    "--geometry-types=point,linestring,polygon",
                    "--attributes=type,id,version,timestamp",
                    "--output",
                    str(exported_seq),
                    str(filtered_pbf),
                ],
                check=True,
            )
            with exported_seq.open(encoding="utf-8") as stream:
                for raw_line in stream:
                    line = raw_line.lstrip("\x1e\r\n ")
                    if not line:
                        continue
                    feature = json.loads(line)
                    if not feature.get("geometry"):
                        continue
                    raw_props = feature.get("properties") or {}
                    osm_type = str(raw_props.get("@type", ""))
                    if osm_type not in {"node", "way", "relation"} or "@id" not in raw_props:
                        continue
                    osm_id = int(raw_props["@id"])
                    osm_version = int(raw_props["@version"]) if "@version" in raw_props else None
                    timestamp_raw = raw_props.get("@timestamp")
                    osm_timestamp = (
                        datetime.fromtimestamp(int(timestamp_raw), timezone.utc).isoformat().replace("+00:00", "Z")
                        if timestamp_raw is not None
                        else None
                    )
                    tags = {str(k): str(v) for k, v in raw_props.items() if not str(k).startswith("@")}
                    categories = classify_criteria_tags(tags)
                    if not categories:
                        continue
                    source_rows += 1
                    geometry = shape(feature["geometry"])
                    clipped = _clip_geometry(geometry, boundary)
                    if clipped is None:
                        continue
                    geometry_counts[clipped.geom_type] += 1
                    minx, miny, maxx, maxy = clipped.bounds
                    bbox = (
                        [minx, miny, maxx, maxy]
                        if bbox is None
                        else [min(bbox[0], minx), min(bbox[1], miny), max(bbox[2], maxx), max(bbox[3], maxy)]
                    )
                    explicit_access = source_access_kind(tags)
                    for category in categories:
                        if clipped.geom_type == "Point" and category in {"bus_stop", "tram_stop"}:
                            access_kind = "mapped_waiting_point"
                            access_point = to_wkb(clipped, hex=False, output_dimension=2)
                        elif clipped.geom_type == "Point" and explicit_access == "entrance_known":
                            access_kind = "entrance_known"
                            access_point = to_wkb(clipped, hex=False, output_dimension=2)
                        elif clipped.geom_type == "Point":
                            access_kind = "poi_node_access_unverified"
                            access_point = None
                        else:
                            access_kind = "no_explicit_access_point"
                            access_point = None
                        row = {
                            "geometry": to_wkb(clipped, hex=False, output_dimension=2),
                            "id": f"osm:{osm_type}:{osm_id}:{category}",
                            "osm_type": osm_type,
                            "osm_id": osm_id,
                            "osm_version": osm_version,
                            "osm_timestamp": osm_timestamp,
                            "category": category,
                            "name": tags.get("name"),
                            "access_kind": access_kind,
                            "access_point": access_point,
                            "tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        }
                        rows.append(row)
                        category_counts[category] += 1
                        output_rows += 1
                        if len(rows) >= batch_size:
                            flush()
    finally:
        flush()
        writer.close()

    if output_rows == 0 or bbox is None:
        temporary_output.unlink(missing_ok=True)
        raise RuntimeError("No classified social POIs intersected the boundary; no output was published")
    os.replace(temporary_output, output_parquet)
    manifest: dict[str, Any] = {
        "dataset": "OSM social POI candidates clipped to Moscow city relation 102269",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "dataset": "Geofabrik Central Federal District PBF, locally extracted to Moscow",
            "file": str(source_pbf),
            "sha256": sha256_file(source_pbf),
            "osm_snapshot_timestamp": osm_snapshot,
            "upstream_url": "https://download.geofabrik.de/russia/central-fed-district-261008.osm.pbf",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        },
        "clip": {
            "boundary_file": str(boundary_geojson),
            "boundary_sha256": sha256_file(boundary_geojson),
            "osm_relation_id": boundary_relation_id,
            "osm_relation_version": boundary_version,
            "bbox_wgs84": bbox,
            "method": "osmium export assembles OSM geometries; Shapely clips non-point geometries and tests point coverage",
        },
        "classification": {
            "taxonomy_version": TAXONOMY_VERSION,
            "categories": dict(sorted(category_counts.items())),
            "source_features_classified_before_clip": source_rows,
            "output_records_after_clip": output_rows,
            "coverage": "city boundary extract; OSM feature completeness is unknown",
        },
        "output": {
            "file": str(output_parquet),
            "sha256": sha256_file(output_parquet),
            "row_count": output_rows,
            "geometry_type_counts": dict(sorted(geometry_counts.items())),
            "crs": "EPSG:4326 / OGC:CRS84 coordinate order",
            "source_ids": ["osm_type", "osm_id", "category"],
            "source_fields": ["osm_version", "osm_timestamp"],
            "access_point_policy": "only explicit entrance nodes and mapped bus/tram waiting points get access_point; no centroid or inferred entrance is generated",
            "unknown_policy": "missing OSM features or source access points do not prove absence; route/access coverage remains unknown",
        },
        "rights": {
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "notice": "Use and redistribution must comply with ODbL database and attribution obligations.",
        },
        "limitations": [
            "OSM point coverage and tagging completeness are unknown; this is not an absence registry.",
            "Non-point POIs preserve clipped source geometry but have no inferred entrance coordinate.",
            "No pedestrian route times, metro station joins, direct transit itineraries, clinic ownership, OMS or DMS metadata are included.",
            "The relation geometry is a community-mapped administrative boundary, not legal certification.",
        ],
    }
    temp_manifest = output_manifest.with_name(output_manifest.name + ".partial")
    if temp_manifest.exists():
        raise FileExistsError(f"Refusing to overwrite {temp_manifest}")
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
    parser.add_argument("--osm-snapshot", required=True)
    parser.add_argument("--boundary-relation-id", type=int, default=102269)
    parser.add_argument("--boundary-version", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()
    result = build_pois(
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
    print(json.dumps(result["classification"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
