"""Stream a clipped Overture GeoParquet export into hierarchical local 3D Tiles.

This is an offline build. It stages one Parquet batch at a time in SQLite,
groups nearby footprints, and emits coarse region tiles plus detailed cell
tiles. Detailed roofs use constrained triangulation, preserving holes and
MultiPolygons; sides follow every exterior and interior ring. Height and
ground uncertainties remain explicit in the source index and manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq
from shapely import constrained_delaunay_triangles, make_valid, orient_polygons, wkb
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

from backend.store import ASSET_ROOT, ROOT
from pipeline.build_tiles import (
    CITY_CELL_DEGREES, FALLBACK_HEIGHT_M, Mesh, enu_transform, enu_relative_transform, glb, region,
    relative_xy, save_json, save_bytes,
)

DEFAULT_INPUT = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
OUTPUT = ASSET_ROOT / "citywide-v2"
REGION_CELLS = 5
ASSUMED_FLOOR_HEIGHT_M = 3.0


def may_publish_city_revision(current_url: str | None, candidate_url: str) -> bool:
    """Keep an active reviewed overlay revision when the base builder reruns."""
    return current_url in {None, "/assets/city/tileset.json", "/assets/citywide/tileset.json", candidate_url}


def polygon_parts(geometry: Any) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon):
        if not geometry.is_empty:
            yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def local_polygon(polygon: Polygon, origin_lon: float, origin_lat: float) -> Polygon:
    def ring(coords: Any) -> list[tuple[float, float]]:
        return [relative_xy(float(x), float(y), origin_lon, origin_lat) for x, y, *_ in coords]

    return Polygon(ring(polygon.exterior.coords), [ring(interior.coords) for interior in polygon.interiors])


def extrude_footprint(mesh: Mesh, source_geometry: Any, height: float, feature_id: int,
                      origin_lon: float, origin_lat: float, *, bottom: float = 0.0,
                      simplify_tolerance_m: float = 0.35,
                      min_area_m2: float = 0.5) -> int:
    """Return roof triangle count; caller decides whether a bbox fallback is needed."""
    geometry = make_valid(source_geometry) if not source_geometry.is_valid else source_geometry
    triangle_count = 0
    for part in polygon_parts(geometry):
        local = local_polygon(part, origin_lon, origin_lat)
        if not local.is_valid:
            local = make_valid(local)
        for polygon in polygon_parts(local):
            if polygon.area < min_area_m2:
                continue
            polygon = orient_polygons(polygon.simplify(simplify_tolerance_m, preserve_topology=True))
            if polygon.is_empty or polygon.area < min_area_m2:
                continue
            triangles = constrained_delaunay_triangles(polygon)
            for triangle in triangles.geoms:
                # The constrained Delaunay result respects concavity and holes.
                if not polygon.covers(triangle):
                    continue
                coords = list(triangle.exterior.coords)[:3]
                signed = (coords[1][0] - coords[0][0]) * (coords[2][1] - coords[0][1]) - (coords[1][1] - coords[0][1]) * (coords[2][0] - coords[0][0])
                if signed < 0:
                    coords[1], coords[2] = coords[2], coords[1]
                top = tuple((float(x), float(y), height) for x, y in coords)
                lower = tuple((float(x), float(y), bottom) for x, y in reversed(coords))
                mesh.triangle(top, 4, feature_id)
                mesh.triangle(lower, 4, feature_id)
                triangle_count += 1
            for boundary in (polygon.exterior, *polygon.interiors):
                ring = list(boundary.coords)
                for first, second in zip(ring, ring[1:]):
                    x0, y0 = first[:2]
                    x1, y1 = second[:2]
                    mesh.triangle(((x0, y0, bottom), (x1, y1, bottom), (x1, y1, height)), 4, feature_id)
                    mesh.triangle(((x0, y0, bottom), (x1, y1, height), (x0, y0, height)), 4, feature_id)
    return triangle_count


def stage_parquet(path: Path, connection: sqlite3.Connection, limit_features: int | None = None) -> tuple[int, str]:
    connection.execute("CREATE TABLE buildings (cell_x INTEGER NOT NULL, cell_y INTEGER NOT NULL, source_id TEXT NOT NULL, geometry BLOB NOT NULL, height REAL, height_policy TEXT NOT NULL, west REAL NOT NULL, south REAL NOT NULL, east REAL NOT NULL, north REAL NOT NULL)")
    connection.execute("CREATE INDEX buildings_cell_idx ON buildings(cell_x,cell_y)")
    parquet = pq.ParquetFile(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    total = 0
    columns = ["id", "geometry", "height", "num_floors", "bbox"]
    for batch_no, batch in enumerate(parquet.iter_batches(batch_size=4096, columns=columns)):
        rows = []
        for record in batch.to_pylist():
            if limit_features is not None and total >= limit_features:
                break
            if not record["geometry"] or not record["id"]:
                continue
            bbox = record.get("bbox")
            if bbox:
                west, east, south, north = (bbox.get("xmin"), bbox.get("xmax"), bbox.get("ymin"), bbox.get("ymax"))
            else:
                west, south, east, north = wkb.loads(record["geometry"]).bounds
            if any(value is None or not math.isfinite(value) for value in (west, south, east, north)):
                continue
            x = math.floor(((west + east) / 2) / CITY_CELL_DEGREES)
            y = math.floor(((south + north) / 2) / CITY_CELL_DEGREES)
            height = record.get("height")
            if height is not None and (not math.isfinite(height) or height <= 0):
                height = None
            if height is not None:
                policy = "source_height_m"
            else:
                floors = record.get("num_floors")
                if isinstance(floors, int) and 1 <= floors <= 100:
                    height = floors * ASSUMED_FLOOR_HEIGHT_M
                    policy = "assumed_3m_per_floor"
                else:
                    height = FALLBACK_HEIGHT_M
                    policy = "display_fallback_15m"
            rows.append((x, y, record["id"], record["geometry"], height, policy, west, south, east, north))
            total += 1
        with connection:
            connection.executemany("INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        if batch_no % 10 == 0:
            print(f"staged {total:,} buildings", flush=True)
        if limit_features is not None and total >= limit_features:
            break
    return total, digest.hexdigest()


def build(input_path: Path, output: Path = OUTPUT, limit_features: int | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novostroy-city-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "stage.sqlite3")
        total, source_sha256 = stage_parquet(input_path, connection, limit_features)
        cells = connection.execute("SELECT cell_x, cell_y, COUNT(*), MIN(west), MIN(south), MAX(east), MAX(north), MAX(height) FROM buildings GROUP BY cell_x,cell_y ORDER BY cell_x,cell_y").fetchall()
        print(f"spatial cells: {len(cells):,}; building detailed GLBs", flush=True)
        regions: dict[tuple[int, int], list[tuple[Any, ...]]] = defaultdict(list)
        for cell in cells:
            regions[(cell[0] // REGION_CELLS, cell[1] // REGION_CELLS)].append(cell)
        index_path = output / "source_tile_index.jsonl"
        temporary_index = output / ".source_tile_index.jsonl.tmp"
        detail_tiles = {}
        fallback_count = 0
        unknown_height_count = 0
        assumed_floor_height_count = 0
        with temporary_index.open("w", encoding="utf-8") as index_stream:
            for cell_no, (ix, iy, size, west, south, east, north, highest) in enumerate(cells, 1):
                tile_id = f"cell_{ix}_{iy}"
                origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
                mesh = Mesh()
                source_ids = []
                rows = connection.execute("SELECT source_id,geometry,height,height_policy,west,south,east,north FROM buildings WHERE cell_x=? AND cell_y=? ORDER BY source_id", (ix, iy))
                for feature_id, (source_id, geom_bytes, measured, height_policy, fw, fs, fe, fn) in enumerate(rows):
                    height = float(measured)
                    if height_policy == "display_fallback_15m":
                        unknown_height_count += 1
                    elif height_policy == "assumed_3m_per_floor":
                        assumed_floor_height_count += 1
                    policy = "footprint_extrusion"
                    previous = mesh.groups.get(4)
                    previous_lengths = tuple(len(values) for values in previous) if previous else (0, 0, 0, 0)
                    previous_feature_ids = len(mesh.feature_ids.get(4, []))
                    try:
                        roof_count = extrude_footprint(mesh, wkb.loads(geom_bytes), height, feature_id, origin_lon, origin_lat)
                        if roof_count == 0:
                            raise ValueError("No roof triangles")
                    except (ValueError, TypeError, RuntimeError):
                        # Discard a partially triangulated feature before the fallback box.
                        if 4 in mesh.groups:
                            for values, length in zip(mesh.groups[4], previous_lengths):
                                del values[length:]
                            del mesh.feature_ids[4][previous_feature_ids:]
                        cx, cy = relative_xy((fw + fe) / 2, (fs + fn) / 2, origin_lon, origin_lat)
                        width = max(1, relative_xy(fe, origin_lat, fw, origin_lat)[0])
                        depth = max(1, relative_xy(origin_lon, fn, origin_lon, fs)[1])
                        mesh.box(cx, cy, 0, width, depth, height, 4, feature_id)
                        fallback_count += 1
                        policy = "bbox_fallback_geometry_error"
                    source_ids.append(source_id)
                    index_stream.write(json.dumps({"source_id": source_id, "tile": tile_id + ".glb", "height_m": height,
                                                   "height_policy": height_policy,
                                                   "geometry_policy": policy}, ensure_ascii=False) + "\n")
                save_bytes(output / (tile_id + ".glb"), glb(mesh, {
                    "geometry_type": "synthetic_massing_from_public_footprints",
                    "source_ids": source_ids, "source_count": len(source_ids),
                    "feature_id_mapping": "_FEATURE_ID_0 indexes source_ids",
                    "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
                }))
                detail_tiles[(ix, iy)] = {
                    "boundingVolume": {"region": region(west, south, east, north, highest + 5)},
                    "geometricError": 0, "refine": "REPLACE",
                    "transform": enu_transform(origin_lon, origin_lat),
                    "content": {"uri": tile_id + ".glb"},
                }
                if cell_no % 100 == 0:
                    print(f"built detailed cells {cell_no:,}/{len(cells):,}", flush=True)
            index_stream.flush()
            os.fsync(index_stream.fileno())
        temporary_index.replace(index_path)
        children = []
        for (rx, ry), group in sorted(regions.items()):
            west = min(cell[3] for cell in group)
            south = min(cell[4] for cell in group)
            east = max(cell[5] for cell in group)
            north = max(cell[6] for cell in group)
            highest = max(cell[7] for cell in group)
            origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
            mesh = Mesh()
            for feature_id, (ix, iy, size, cw, cs, ce, cn, cell_height) in enumerate(group):
                cx, cy = relative_xy((cw + ce) / 2, (cs + cn) / 2, origin_lon, origin_lat)
                width = max(1, relative_xy(ce, origin_lat, cw, origin_lat)[0])
                depth = max(1, relative_xy(origin_lon, cn, origin_lon, cs)[1])
                mesh.box(cx, cy, 0, width, depth, min(30, cell_height), 4, feature_id)
            name = f"region_{rx}_{ry}.glb"
            save_bytes(output / name, glb(mesh, {
                "geometry_type": "coarse_synthetic_cell_aggregation", "cell_ids": [f"cell_{c[0]}_{c[1]}" for c in group],
                "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
            }))
            children.append({
                "boundingVolume": {"region": region(west, south, east, north, highest + 5)},
                "geometricError": 500, "refine": "REPLACE",
                "transform": enu_transform(origin_lon, origin_lat),
                "content": {"uri": name},
                "children": [
                    {**detail_tiles[(cell[0], cell[1])],
                     "transform": enu_relative_transform((cell[3] + cell[5]) / 2, (cell[4] + cell[6]) / 2, origin_lon, origin_lat)}
                    for cell in group
                ],
            })
        if not cells:
            raise RuntimeError("No valid building rows in city input")
        save_json(output / "tileset.json", {
            "asset": {"version": "1.1", "tilesetVersion": "citywide-massing-dark-v2"},
            "geometricError": 10000,
            "root": {
                "boundingVolume": {"region": region(min(c[3] for c in cells), min(c[4] for c in cells), max(c[5] for c in cells), max(c[6] for c in cells), max(c[7] for c in cells) + 5)},
                "geometricError": 5000, "refine": "REPLACE", "children": children,
            },
            "extras": {"attribution": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps",
                       "geometry_policy": "constrained footprint extrusion with coarse cell aggregation",
                       "unknown_height_policy": "display_fallback_15m", "ground_height_policy": "WGS84 ellipsoid 0 m; approximate"},
        })
        manifest = {
            "version": "2", "input": str(input_path.resolve()), "source_sha256": source_sha256,
            "render_palette": {"name": "dark_city_massing_v2", "base_color_factor": [0.36, 0.42, 0.46, 1]},
            "source_rows": total, "city_buildings": total, "detailed_tiles": len(cells),
            "coarse_region_tiles": len(regions), "unknown_height_count": unknown_height_count,
            "assumed_floor_height_count": assumed_floor_height_count,
            "measured_height_count": total - unknown_height_count - assumed_floor_height_count,
            "assumed_floor_height_m": ASSUMED_FLOOR_HEIGHT_M,
            "geometry_fallback_count": fallback_count, "tileset_url": f"/assets/{output.name}/tileset.json",
            "model_quality": "synthetic_massing", "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
            "partial": limit_features is not None,
        }
        save_json(output / "manifest.json", manifest)
        if limit_features is None and output.resolve() == OUTPUT.resolve():
            # An older builder must never silently roll a newer reviewed height
            # revision back to the plain v2 massing when rerun from bootstrap.
            current = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8")) if (ASSET_ROOT / "manifest.json").is_file() else {"version": "1", "facade_models": 3}
            if may_publish_city_revision(current.get("city_tileset_url"), manifest["tileset_url"]):
                current.update({"city_buildings": total, "city_tileset_url": manifest["tileset_url"]})
                save_json(ASSET_ROOT / "manifest.json", current)
        connection.close()
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit-features", type=int)
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.output, args.limit_features), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
