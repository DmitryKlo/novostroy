"""Stream clipped OSM road lines into spatially culled 3D Tiles ribbons.

Widths and ellipsoid offsets are cartographic assumptions, not surveyed
carriageway widths or pedestrian-routing evidence. No remote fetch occurs.
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
from shapely import wkb
from shapely.geometry import GeometryCollection, LineString, MultiLineString, box

from backend.store import ASSET_ROOT, ROOT
from pipeline.build_tiles import (
    CITY_CELL_DEGREES, Mesh, ecef, enu_relative_transform, enu_transform, glb,
    region, relative_xy, save_bytes, save_json,
)


INPUT = ROOT / "data/social/osm_moscow_roads_261008.parquet"
SOURCE_MANIFEST = ROOT / "data/social/osm_moscow_roads_261008.manifest.json"
OUTPUT = ASSET_ROOT / "roads-v1"
REGION_CELLS = 5
ROAD_OFFSET_M = 1.6
WIDTHS_M = {
    "motorway": 14, "trunk": 12, "primary": 10, "secondary": 8,
    "tertiary": 7, "residential": 6, "living_street": 5,
    "unclassified": 5, "service": 4,
    "motorway_link": 6, "trunk_link": 6, "primary_link": 5,
    "secondary_link": 5, "tertiary_link": 4,
}
COARSE_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary",
                            "motorway_link", "trunk_link", "primary_link", "secondary_link"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def line_parts(geometry: Any) -> Iterator[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty and len(geometry.coords) >= 2 and geometry.length > 0:
            yield geometry
    elif isinstance(geometry, (MultiLineString, GeometryCollection)):
        for part in geometry.geoms:
            yield from line_parts(part)


def grid_fragments(line: LineString) -> Iterator[tuple[int, int, LineString]]:
    west, south, east, north = line.bounds
    first_x = math.floor(west / CITY_CELL_DEGREES)
    last_x = max(first_x, math.floor((east - 1e-12) / CITY_CELL_DEGREES))
    first_y = math.floor(south / CITY_CELL_DEGREES)
    last_y = max(first_y, math.floor((north - 1e-12) / CITY_CELL_DEGREES))
    for ix in range(first_x, last_x + 1):
        for iy in range(first_y, last_y + 1):
            if first_x == last_x and first_y == last_y:
                clipped = line
            else:
                clipped = line.intersection(box(ix * CITY_CELL_DEGREES, iy * CITY_CELL_DEGREES,
                                                (ix + 1) * CITY_CELL_DEGREES,
                                                (iy + 1) * CITY_CELL_DEGREES))
            for part in line_parts(clipped):
                yield ix, iy, part


def ribbon(mesh: Mesh, line: LineString, width_m: float, feature_id: int,
           origin_lon: float, origin_lat: float, *, z_m: float = ROAD_OFFSET_M,
           material: int = 5) -> int:
    points = [relative_xy(float(lon), float(lat), origin_lon, origin_lat)
              for lon, lat, *_ in line.coords]
    triangles = 0
    half_width = width_m / 2
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 0.05:
            continue
        side_x, side_y = -dy * half_width / length, dx * half_width / length
        a = (x0 + side_x, y0 + side_y, z_m)
        b = (x0 - side_x, y0 - side_y, z_m)
        c = (x1 - side_x, y1 - side_y, z_m)
        d = (x1 + side_x, y1 + side_y, z_m)
        mesh.triangle((a, b, c), material, feature_id)
        mesh.triangle((a, c, d), material, feature_id)
        triangles += 2
    return triangles


def exact_local_point(lon: float, lat: float, origin_lon: float,
                      origin_lat: float) -> tuple[float, float, float]:
    """Project one WGS84 road point into its tile ENU frame without scale drift."""
    frame = enu_transform(origin_lon, origin_lat)
    return _point_in_frame(lon, lat, frame)


def _point_in_frame(lon: float, lat: float, frame: list[float]) -> tuple[float, float, float]:
    point = ecef(lon, lat, ROAD_OFFSET_M)
    delta = [point[i] - frame[12 + i] for i in range(3)]
    return tuple(sum(frame[axis * 4 + i] * delta[i] for i in range(3))
                 for axis in range(3))


def ribbon_exact(mesh: Mesh, line: LineString, width_m: float, feature_id: int,
                 origin_lon: float, origin_lat: float, *, material: int = 5) -> int:
    """Emit road ribbons from geodetically identical points in adjacent tiles."""
    frame = enu_transform(origin_lon, origin_lat)
    points = [_point_in_frame(float(lon), float(lat), frame)
              for lon, lat, *_ in line.coords]
    triangles = 0
    half_width = width_m / 2
    for (x0, y0, z0), (x1, y1, z1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 0.05:
            continue
        side_x, side_y = -dy * half_width / length, dx * half_width / length
        a = (x0 + side_x, y0 + side_y, z0)
        b = (x0 - side_x, y0 - side_y, z0)
        c = (x1 - side_x, y1 - side_y, z1)
        d = (x1 + side_x, y1 + side_y, z1)
        mesh.triangle((a, b, c), material, feature_id)
        mesh.triangle((a, c, d), material, feature_id)
        triangles += 2
    return triangles


def padded_road_region(west: float, south: float, east: float, north: float,
                       *, coarse: bool) -> list[float]:
    """Conservatively enclose the actual ribbon, including lateral width."""
    half_width_m = max(WIDTHS_M.values()) * (0.8 if coarse else 0.5)
    margin_m = half_width_m + 1.0
    longitude_m_per_degree = 111320 * math.cos(math.radians(max(abs(south), abs(north))))
    longitude_pad = margin_m / longitude_m_per_degree
    latitude_pad = margin_m / 110500
    return region(west - longitude_pad, south - latitude_pad,
                  east + longitude_pad, north + latitude_pad, 3)


def stage_roads(input_path: Path, connection: sqlite3.Connection) -> tuple[int, int, dict[str, int]]:
    connection.execute("CREATE TABLE road_fragments (cx INTEGER, cy INTEGER, source_id TEXT, road_class TEXT, geometry BLOB, west REAL, south REAL, east REAL, north REAL)")
    connection.execute("CREATE INDEX road_fragments_cell ON road_fragments(cx,cy)")
    source_rows = 0
    fragments = 0
    classes: dict[str, int] = defaultdict(int)
    parquet = pq.ParquetFile(input_path)
    for batch in parquet.iter_batches(batch_size=8192, columns=["geometry", "id", "highway"]):
        pending = []
        for record in batch.to_pylist():
            source_rows += 1
            road_class = record["highway"]
            if road_class not in WIDTHS_M or not record["geometry"] or not record["id"]:
                continue
            classes[road_class] += 1
            for line in line_parts(wkb.loads(record["geometry"])):
                for cx, cy, part in grid_fragments(line):
                    west, south, east, north = part.bounds
                    pending.append((cx, cy, record["id"], road_class, wkb.dumps(part),
                                    west, south, east, north))
                    fragments += 1
        with connection:
            connection.executemany("INSERT INTO road_fragments VALUES (?,?,?,?,?,?,?,?,?)", pending)
        if source_rows % 81920 < 8192:
            print(f"staged {source_rows:,} road source rows", flush=True)
    return source_rows, fragments, dict(classes)


def _mesh_for_rows(rows: Any, origin_lon: float, origin_lat: float, *, coarse: bool,
                   exact_projection: bool = False, index_stream: Any | None = None,
                   tile_id: str | None = None) -> tuple[Mesh, list[str], int]:
    mesh = Mesh()
    source_ids = []
    triangles = 0
    for source_id, road_class, geometry in rows:
        line = wkb.loads(geometry)
        if coarse:
            line = line.simplify(0.00002, preserve_topology=True)
        draw = ribbon_exact if exact_projection else ribbon
        produced = draw(mesh, line, WIDTHS_M[road_class] * (1.6 if coarse else 1),
                        len(source_ids), origin_lon, origin_lat)
        if produced:
            source_ids.append(source_id)
            triangles += produced
            if index_stream is not None and tile_id is not None:
                index_stream.write(json.dumps({"source_id": source_id, "tile": tile_id,
                                               "road_class": road_class}, ensure_ascii=False) + "\n")
    return mesh, source_ids, triangles


def build(input_path: Path = INPUT, output: Path = OUTPUT,
          source_manifest_path: Path = SOURCE_MANIFEST, *, revision: str = "1") -> dict[str, Any]:
    if revision not in {"1", "2"}:
        raise ValueError("Unsupported road revision")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_hash = sha256(input_path)
    if source_hash != source_manifest["output"]["sha256"]:
        raise ValueError("Road source hash differs from the reviewed manifest")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("source_sha256") != source_hash or saved.get("partial") or saved.get("version") != revision:
            raise ValueError("Existing road tile set differs from source")
        return {"status": "no_op", **saved}
    stage = output.with_name(f".{output.name}.pending")
    stage.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novostroy-roads-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "stage.sqlite3")
        source_rows, fragments, classes = stage_roads(input_path, connection)
        cells = connection.execute("SELECT cx,cy,COUNT(*),MIN(west),MIN(south),MAX(east),MAX(north) FROM road_fragments GROUP BY cx,cy ORDER BY cx,cy").fetchall()
        if not cells:
            raise ValueError("Road source produced no drawable lines")
        detail_tiles: dict[tuple[int, int], dict[str, Any]] = {}
        total_triangles = 0
        index_tmp = stage / ".source_tile_index.jsonl.tmp"
        with index_tmp.open("w", encoding="utf-8") as index:
            for position, (cx, cy, count, west, south, east, north) in enumerate(cells, 1):
                tile_id = f"cell_{cx}_{cy}.glb"
                origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
                rows = connection.execute("SELECT source_id,road_class,geometry FROM road_fragments WHERE cx=? AND cy=? ORDER BY source_id,road_class", (cx, cy))
                mesh, ids, triangles = _mesh_for_rows(rows, origin_lon, origin_lat,
                                                       coarse=False, exact_projection=revision == "2",
                                                       index_stream=index, tile_id=tile_id)
                total_triangles += triangles
                if not ids:
                    continue
                save_bytes(stage / tile_id, glb(mesh, {"geometry_type": "cartographic_road_ribbons",
                    "source_ids": ids, "feature_id_mapping": "_FEATURE_ID_0 indexes source_ids",
                    "width_policy": "assumed_visual_widths_not_surveyed", "ground_offset_m": ROAD_OFFSET_M}))
                detail_tiles[(cx, cy)] = {"boundingVolume": {"region":
                    padded_road_region(west, south, east, north, coarse=False)
                    if revision == "2" else region(west, south, east, north, 3)},
                    "geometricError": 0, "refine": "REPLACE", "transform": enu_transform(origin_lon, origin_lat),
                    "content": {"uri": tile_id}}
                if position % 500 == 0:
                    print(f"built detailed road tiles {position:,}/{len(cells):,}", flush=True)
            index.flush()
            os.fsync(index.fileno())
        index_tmp.replace(stage / "source_tile_index.jsonl")
        groups: dict[tuple[int, int], list[tuple[Any, ...]]] = defaultdict(list)
        for cell in cells:
            groups[(cell[0] // REGION_CELLS, cell[1] // REGION_CELLS)].append(cell)
        children = []
        for (rx, ry), group in sorted(groups.items()):
            west, south = min(cell[3] for cell in group), min(cell[4] for cell in group)
            east, north = max(cell[5] for cell in group), max(cell[6] for cell in group)
            origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
            rows = connection.execute("SELECT source_id,road_class,geometry FROM road_fragments WHERE cx BETWEEN ? AND ? AND cy BETWEEN ? AND ? AND road_class IN (?,?,?,?,?,?,?,?,?) ORDER BY source_id,road_class",
                (rx * REGION_CELLS, rx * REGION_CELLS + REGION_CELLS - 1,
                 ry * REGION_CELLS, ry * REGION_CELLS + REGION_CELLS - 1,
                 *sorted(COARSE_CLASSES)))
            mesh, ids, _ = _mesh_for_rows(rows, origin_lon, origin_lat, coarse=True,
                                          exact_projection=revision == "2")
            region_tile: dict[str, Any] = {
                "boundingVolume": {"region": padded_road_region(west, south, east, north, coarse=True)
                                   if revision == "2" else region(west, south, east, north, 3)},
                "geometricError": 450, "refine": "REPLACE",
                "transform": enu_transform(origin_lon, origin_lat),
                "children": [
                    {**detail_tiles[(cell[0], cell[1])],
                     "transform": enu_relative_transform((cell[3] + cell[5]) / 2,
                                                          (cell[4] + cell[6]) / 2,
                                                          origin_lon, origin_lat)}
                    for cell in group if (cell[0], cell[1]) in detail_tiles],
            }
            if ids:
                name = f"region_{rx}_{ry}.glb"
                save_bytes(stage / name, glb(mesh, {"geometry_type": "coarse_major_road_ribbons",
                    "source_count": len(ids), "width_policy": "assumed_visual_widths_not_surveyed",
                    "ground_offset_m": ROAD_OFFSET_M}))
                region_tile["content"] = {"uri": name}
            children.append(region_tile)
        tileset = {"asset": {"version": "1.1", "tilesetVersion": f"osm-roads-cartography-v{revision}"},
            "geometricError": 10000,
            "root": {"boundingVolume": {"region":
                padded_road_region(min(c[3] for c in cells), min(c[4] for c in cells),
                                   max(c[5] for c in cells), max(c[6] for c in cells), coarse=True)
                if revision == "2" else region(min(c[3] for c in cells), min(c[4] for c in cells),
                                               max(c[5] for c in cells), max(c[6] for c in cells), 3)},
                "geometricError": 5000, "refine": "REPLACE", "children": children},
            "extras": {"attribution": source_manifest["rights"]["attribution"],
                       "visualization": "assumed-width ribbons; no routing or measured road width",
                       **({"geometry_policy": "exact_WGS84_centerline_projection_with_conservative_ribbon_bounds"}
                          if revision == "2" else {})}}
        save_json(stage / "tileset.json", tileset)
        manifest = {"version": revision, "partial": False, "source_sha256": source_hash,
            "source_rows": source_rows, "rendered_source_rows": sum(classes.values()),
            "partitioned_fragments": fragments, "detailed_tiles": len(detail_tiles),
            "coarse_region_tiles": len(children), "triangles": total_triangles,
            "rendered_class_counts": classes, "assumed_visual_widths_m": WIDTHS_M,
            "ground_offset_m": ROAD_OFFSET_M, "model_quality": "cartographic_ribbon",
            "osm_snapshot_at": source_manifest["source"]["osm_snapshot_timestamp"],
            "attribution": source_manifest["rights"]["attribution"],
            "tileset_url": f"/assets/{output.name}/tileset.json",
            **({"geometry_policy": "exact_WGS84_centerline_projection_with_conservative_ribbon_bounds",
                "bounds_margin_detail_m": 8.0, "bounds_margin_coarse_m": 12.2}
               if revision == "2" else {})}
        save_json(stage / "manifest.json", manifest)
        connection.close()
    stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--source-manifest", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--revision", choices=("1", "2"), default="1")
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.output, args.source_manifest, revision=args.revision),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
