"""Build Moscow-clipped OSM park, waterbody and waterway GeoParquet layers.

The local ``osmium export`` step assembles closed ways and multipolygon
relations. Shapely intersects those geometries with a pinned boundary while
retaining polygon interior rings. This script never downloads data or routes.
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
from shapely.geometry import MultiLineString, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


BASE_COLUMNS: dict[str, pa.DataType] = {
    "geometry": pa.binary(),
    "id": pa.string(),
    "osm_type": pa.string(),
    "osm_id": pa.int64(),
    "osm_version": pa.int32(),
    "osm_timestamp": pa.string(),
    "feature_class": pa.string(),
    "name": pa.string(),
    "tags_json": pa.string(),
}


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


def feature_class(tags: dict[str, str], geometry_type: str) -> tuple[str, str] | None:
    """Return (layer, class) from explicit OSM tags; do not infer attributes."""
    leisure = tags.get("leisure")
    if geometry_type in {"Polygon", "MultiPolygon"} and leisure in {
        "park",
        "garden",
        "recreation_ground",
        "nature_reserve",
    }:
        return "parks", leisure

    waterway = tags.get("waterway")
    if geometry_type in {"LineString", "MultiLineString"} and waterway in {
        "river",
        "stream",
        "canal",
        "ditch",
        "drain",
        "brook",
        "tidal_channel",
    }:
        return "waterways", waterway

    if geometry_type in {"Polygon", "MultiPolygon"}:
        if waterway == "riverbank":
            return "water", "riverbank"
        if tags.get("natural") == "water":
            return "water", tags.get("water", "water")
        if tags.get("natural") == "wetland":
            return "water", "wetland"
        if tags.get("landuse") in {"reservoir", "basin"}:
            return "water", tags["landuse"]
        if "water" in tags:
            return "water", tags["water"]
    return None


def polygon_parts(geometry: BaseGeometry) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
                yield from polygon_parts(part)


def clip_polygon_geometry(geometry: BaseGeometry, boundary: BaseGeometry) -> tuple[BaseGeometry, int] | None:
    """Clip a polygonal OSM area and return geometry plus retained hole count."""
    clipped = geometry.intersection(boundary)
    parts = [part for part in polygon_parts(clipped) if not part.is_empty and part.area > 0]
    if not parts:
        return None
    result: BaseGeometry = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    holes = sum(len(part.interiors) for part in parts)
    return result, holes


def line_parts(geometry: BaseGeometry) -> Iterable[Any]:
    if geometry.geom_type == "LineString":
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from line_parts(part)


def schema_for(geometry_types: list[str]) -> pa.Schema:
    metadata = {
        b"geo": json.dumps(
            {
                "version": "1.1.0",
                "primary_column": "geometry",
                "columns": {
                    "geometry": {"encoding": "WKB", "geometry_types": geometry_types, "crs": None}
                },
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        b"novostroy.source": b"OpenStreetMap contributors; ODbL-1.0",
    }
    return pa.schema([pa.field(name, kind) for name, kind in BASE_COLUMNS.items()], metadata=metadata)


def build_layers(
    source_pbf: Path,
    boundary_geojson: Path,
    output_dir: Path,
    *,
    osmium: str,
    osm_snapshot: str,
    boundary_relation_id: int,
    boundary_version: int,
    version_suffix: str = "",
) -> dict[str, Any]:
    if not source_pbf.is_file() or not boundary_geojson.is_file():
        raise FileNotFoundError("Source PBF and boundary GeoJSON must exist")
    output_dir.mkdir(parents=True, exist_ok=True)
    if version_suffix and not version_suffix.replace("_", "").replace("-", "").isalnum():
        raise ValueError("version_suffix may contain only letters, numbers, underscores and hyphens")
    suffix = f"_{version_suffix}" if version_suffix else ""
    outputs = {
        "parks": output_dir / f"osm_moscow_parks_261008{suffix}.parquet",
        "water": output_dir / f"osm_moscow_water_261008{suffix}.parquet",
        "waterways": output_dir / f"osm_moscow_waterways_261008{suffix}.parquet",
    }
    manifest_path = output_dir / f"osm_moscow_surfaces_261008{suffix}.manifest.json"
    collisions = [path for path in [*outputs.values(), manifest_path] if path.exists()]
    partial_collisions = [
        path.with_name(path.name + ".partial")
        for path in [*outputs.values(), manifest_path]
        if path.with_name(path.name + ".partial").exists()
    ]
    collisions.extend(partial_collisions)
    if collisions:
        raise FileExistsError("Refusing to overwrite: " + ", ".join(map(str, collisions)))
    boundary = read_boundary(boundary_geojson)

    expressions = [
        "nwr/leisure=park,garden,recreation_ground,nature_reserve",
        "nwr/natural=water,wetland",
        "nwr/water",
        "nwr/landuse=reservoir,basin",
        "nwr/waterway=riverbank,river,stream,canal,ditch,drain,brook,tidal_channel",
    ]

    with tempfile.TemporaryDirectory(prefix="novostroy-osm-surfaces-") as temp_dir:
        temp = Path(temp_dir)
        filters = temp / "filters.txt"
        filters.write_text("\n".join(expressions) + "\n", encoding="utf-8")
        filtered_pbf = temp / "surfaces.osm.pbf"
        exported_seq = temp / "surfaces.geojsonl"
        subprocess.run(
            [osmium, "tags-filter", "--expressions", str(filters), "--output", str(filtered_pbf), str(source_pbf)],
            check=True,
        )
        subprocess.run(
            [
                osmium,
                "export",
                "--output-format=geojsonseq",
                "--add-unique-id=type_id",
                "--geometry-types=polygon,linestring",
                "--attributes=type,id,version,timestamp",
                "--output",
                str(exported_seq),
                str(filtered_pbf),
            ],
            check=True,
        )

        schemas = {
            "parks": schema_for(["Polygon", "MultiPolygon"]),
            "water": schema_for(["Polygon", "MultiPolygon"]),
            "waterways": schema_for(["LineString", "MultiLineString"]),
        }
        temporary_outputs = {name: path.with_name(path.name + ".partial") for name, path in outputs.items()}
        writers = {
            name: pq.ParquetWriter(temporary_outputs[name], schemas[name], compression="zstd", version="2.6")
            for name in outputs
        }
        rows: dict[str, list[dict[str, Any]]] = {name: [] for name in outputs}
        seen = Counter()
        duplicates = Counter()
        class_counts: dict[str, Counter[str]] = {name: Counter() for name in outputs}
        bboxes: dict[str, list[float] | None] = {name: None for name in outputs}
        interior_ring_counts = Counter()
        dedupe: dict[str, set[bytes]] = {name: set() for name in outputs}

        def flush(layer: str) -> None:
            batch_rows = rows[layer]
            if not batch_rows:
                return
            arrays = [
                pa.array([row[column] for row in batch_rows], type=kind)
                for column, kind in BASE_COLUMNS.items()
            ]
            writers[layer].write_batch(pa.RecordBatch.from_arrays(arrays, schema=schemas[layer]))
            batch_rows.clear()

        try:
            with exported_seq.open(encoding="utf-8") as stream:
                for raw_line in stream:
                    line = raw_line.lstrip("\x1e\r\n ")
                    if not line:
                        continue
                    feature = json.loads(line)
                    geometry_doc = feature.get("geometry")
                    if not geometry_doc:
                        continue
                    raw_id = str(feature.get("id", ""))
                    if not raw_id or raw_id[0] not in {"n", "w", "r", "a"}:
                        continue
                    raw_props = feature.get("properties") or {}
                    osm_type = str(raw_props.get("@type", ""))
                    if osm_type not in {"node", "way", "relation"} or "@id" not in raw_props:
                        # Areas use osmium's generated `a...` IDs; source
                        # type/id must come from the explicit attributes.
                        continue
                    osm_id = int(raw_props["@id"])
                    osm_version = int(raw_props["@version"]) if "@version" in raw_props else None
                    timestamp_raw = raw_props.get("@timestamp")
                    osm_timestamp = (
                        datetime.fromtimestamp(int(timestamp_raw), timezone.utc).isoformat().replace("+00:00", "Z")
                        if timestamp_raw is not None
                        else None
                    )
                    props = {str(k): str(v) for k, v in raw_props.items() if not str(k).startswith("@")}
                    geometry = shape(geometry_doc)
                    classified = feature_class(props, geometry.geom_type)
                    if classified is None:
                        continue
                    layer, feature_type = classified
                    seen[layer] += 1
                    if layer in {"parks", "water"}:
                        polygon_result = clip_polygon_geometry(geometry, boundary)
                        if polygon_result is None:
                            continue
                        geom_out, holes = polygon_result
                        interior_ring_counts[layer] += holes
                    else:
                        clipped = geometry.intersection(boundary)
                        parts = [part for part in line_parts(clipped) if not part.is_empty and len(part.coords) >= 2]
                        if not parts:
                            continue
                        geom_out = parts[0] if len(parts) == 1 else MultiLineString(parts)

                    # Multipolygon relation and tagged member way can repeat the
                    # exact same clipped geometry. Suppress exact duplicates per
                    # layer; distinct OSM IDs and non-identical geometries remain.
                    fingerprint = to_wkb(geom_out.normalize(), hex=False, output_dimension=2)
                    if fingerprint in dedupe[layer]:
                        duplicates[layer] += 1
                        continue
                    dedupe[layer].add(fingerprint)
                    minx, miny, maxx, maxy = geom_out.bounds
                    bbox = bboxes[layer]
                    bboxes[layer] = (
                        [minx, miny, maxx, maxy]
                        if bbox is None
                        else [min(bbox[0], minx), min(bbox[1], miny), max(bbox[2], maxx), max(bbox[3], maxy)]
                    )
                    class_counts[layer][feature_type] += 1
                    row = {
                        "geometry": to_wkb(geom_out, hex=False, output_dimension=2),
                        "id": f"osm:{osm_type}:{osm_id}",
                        "osm_type": osm_type,
                        "osm_id": osm_id,
                        "osm_version": osm_version,
                        "osm_timestamp": osm_timestamp,
                        "feature_class": feature_type,
                        "name": props.get("name"),
                        "tags_json": json.dumps(props, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    }
                    rows[layer].append(row)
                    if len(rows[layer]) >= 3000:
                        flush(layer)
        finally:
            for layer in outputs:
                flush(layer)
            for writer in writers.values():
                writer.close()

    layer_manifest: dict[str, Any] = {}
    for layer, path in outputs.items():
        temp_path = temporary_outputs[layer]
        if not path.exists():
            if not temp_path.is_file():
                raise RuntimeError(f"Missing temporary output for {layer}")
            os.replace(temp_path, path)
        metadata = pq.read_metadata(path)
        layer_manifest[layer] = {
            "file": str(path),
            "sha256": sha256_file(path),
            "row_count": metadata.num_rows,
            "bbox_wgs84": bboxes[layer],
            "feature_class_counts": dict(sorted(class_counts[layer].items())),
            "exact_duplicate_geometries_suppressed": duplicates[layer],
            "polygon_interior_ring_count": interior_ring_counts[layer] if layer in {"parks", "water"} else None,
            "geometry_types": json.loads(schemas[layer].metadata[b"geo"])["columns"]["geometry"]["geometry_types"],
            "crs": "EPSG:4326 / OGC:CRS84 coordinate order",
            "stable_source_id": "osm_type + osm_id",
            "source_fields": ["osm_version", "osm_timestamp"],
            "raw_tags_column": "tags_json",
        }

    manifest = {
        "dataset": "Moscow OSM park, waterbody and waterway layers",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "dataset": "Geofabrik Central Federal District OSM PBF, locally extracted to Moscow",
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
            "method": "osmium export assembles closed areas and multipolygon relations; Shapely intersects exported geometry with the pinned polygon",
            "coverage_scope": "within the supplied relation polygon; OSM mapping completeness is unknown",
        },
        "output_version_suffix": version_suffix or None,
        "tag_filters": expressions,
        "layers": layer_manifest,
        "rights": {
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "notice": "Use and redistribution must comply with ODbL database and attribution obligations.",
        },
        "limitations": [
            "These are OSM-mapped objects; incomplete coverage is not evidence of absence.",
            "No pedestrian route or travel-time results are included.",
            "The OSM relation is a community-mapped administrative geometry, not legal certification.",
            "Polygon interior rings are retained; water islands are not filled by this export/clip operation.",
        ],
    }
    if version_suffix == "v2":
        manifest["supersedes_invalid_files"] = [
            "osm_moscow_surfaces_261008.manifest.json",
            "osm_moscow_parks_261008.parquet",
            "osm_moscow_water_261008.parquet",
            "osm_moscow_waterways_261008.parquet",
        ]
    temp_manifest = manifest_path.with_name(manifest_path.name + ".partial")
    if temp_manifest.exists():
        raise FileExistsError(f"Refusing to overwrite partial output {temp_manifest}")
    temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_manifest, manifest_path)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pbf", type=Path, required=True)
    parser.add_argument("--boundary-geojson", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--osmium", default=shutil.which("osmium") or "osmium")
    parser.add_argument("--osm-snapshot", required=True)
    parser.add_argument("--boundary-relation-id", type=int, default=102269)
    parser.add_argument("--boundary-version", type=int, required=True)
    parser.add_argument("--version-suffix", default="")
    args = parser.parse_args()
    manifest = build_layers(
        args.source_pbf,
        args.boundary_geojson,
        args.output_dir,
        osmium=args.osmium,
        osm_snapshot=args.osm_snapshot,
        boundary_relation_id=args.boundary_relation_id,
        boundary_version=args.boundary_version,
        version_suffix=args.version_suffix,
    )
    print(json.dumps(manifest["layers"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
