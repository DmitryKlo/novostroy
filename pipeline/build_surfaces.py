"""Build spatial 3D Tiles for clipped OSM parks and open-water cartography.

The source GeoParquet remains authoritative. Rendered pastel surfaces float
slightly above the WGS84 ellipsoid; water islands and polygon holes stay empty.
Wetlands and wastewater are excluded from the solid open-water fill.
"""

from __future__ import annotations

import argparse
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
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box

from backend.store import ASSET_ROOT, ROOT
from pipeline.build_citywide import local_polygon, polygon_parts
from pipeline.build_roads import REGION_CELLS, grid_fragments, line_parts, ribbon, sha256
from pipeline.build_tiles import (
    CITY_CELL_DEGREES, Mesh, enu_relative_transform, enu_transform, glb,
    region, save_bytes, save_json,
)


SOURCE_MANIFEST = ROOT / "data/social/osm_moscow_surfaces_261008_v2.manifest.json"
PARKS = ROOT / "data/social/osm_moscow_parks_261008_v2.parquet"
WATER = ROOT / "data/social/osm_moscow_water_261008_v2.parquet"
WATERWAYS = ROOT / "data/social/osm_moscow_waterways_261008_v2.parquet"
PARK_OFFSET_M = 1.0
WATER_OFFSET_M = 1.3
WATERWAY_OFFSET_M = 1.35
OPEN_WATER_CLASSES = frozenset({
    "water", "river", "lake", "pond", "reservoir", "oxbow", "canal",
    "fishpond", "fountain", "reflecting_pool", "pool", "lagoon", "lock",
})
WATERWAY_WIDTHS_M = {"river": 6, "stream": 2, "canal": 4}


def grid_polygons(polygon: Polygon) -> Iterator[tuple[int, int, Polygon]]:
    west, south, east, north = polygon.bounds
    first_x = math.floor(west / CITY_CELL_DEGREES)
    last_x = max(first_x, math.floor((east - 1e-12) / CITY_CELL_DEGREES))
    first_y = math.floor(south / CITY_CELL_DEGREES)
    last_y = max(first_y, math.floor((north - 1e-12) / CITY_CELL_DEGREES))
    for ix in range(first_x, last_x + 1):
        for iy in range(first_y, last_y + 1):
            if first_x == last_x and first_y == last_y:
                clipped = polygon
            else:
                clipped = polygon.intersection(box(ix * CITY_CELL_DEGREES, iy * CITY_CELL_DEGREES,
                                                   (ix + 1) * CITY_CELL_DEGREES,
                                                   (iy + 1) * CITY_CELL_DEGREES))
            for part in polygon_parts(clipped):
                if part.area > 1e-12:
                    yield ix, iy, part


def polygon_surface(mesh: Mesh, geometry: Any, feature_id: int,
                    origin_lon: float, origin_lat: float, *, material: int,
                    z_m: float, coarse: bool = False) -> int:
    valid = make_valid(geometry) if not geometry.is_valid else geometry
    count = 0
    for source_part in polygon_parts(valid):
        local = local_polygon(source_part, origin_lon, origin_lat)
        if not local.is_valid:
            local = make_valid(local)
        for part in polygon_parts(local):
            if part.area < 0.5:
                continue
            part = orient_polygons(part.simplify(3.0 if coarse else 0.5, preserve_topology=True))
            if part.is_empty or part.area < 0.5:
                continue
            for triangle in constrained_delaunay_triangles(part).geoms:
                if not part.covers(triangle):
                    continue
                points = list(triangle.exterior.coords)[:3]
                signed = ((points[1][0] - points[0][0]) * (points[2][1] - points[0][1])
                          - (points[1][1] - points[0][1]) * (points[2][0] - points[0][0]))
                if signed < 0:
                    points[1], points[2] = points[2], points[1]
                mesh.triangle(tuple((float(x), float(y), z_m) for x, y in points), material, feature_id)
                count += 1
    return count


def _stage_input(connection: sqlite3.Connection, path: Path, *, layer: str,
                 line_geometry: bool, allowed: set[str] | frozenset[str] | None,
                 source_counts: dict[str, int]) -> tuple[int, int]:
    original_rows = 0
    inserted = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=4096, columns=["geometry", "id", "feature_class"]):
        pending = []
        for item in batch.to_pylist():
            original_rows += 1
            kind = item["feature_class"]
            if not item["geometry"] or not item["id"] or (allowed is not None and kind not in allowed):
                continue
            source_counts[kind] = source_counts.get(kind, 0) + 1
            geometry = wkb.loads(item["geometry"])
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            parts = line_parts(geometry) if line_geometry else polygon_parts(geometry)
            for part in parts:
                fragments = grid_fragments(part) if line_geometry else grid_polygons(part)
                for cx, cy, clipped in fragments:
                    west, south, east, north = clipped.bounds
                    pending.append((cx, cy, item["id"], kind, "line" if line_geometry else "polygon",
                                    wkb.dumps(clipped), west, south, east, north))
                    inserted += 1
        with connection:
            connection.executemany("INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?)", pending)
    return original_rows, inserted


def stage_layer(connection: sqlite3.Connection, layer: str, inputs: list[Path]) -> tuple[int, int, dict[str, int]]:
    connection.execute("CREATE TABLE features (cx INTEGER, cy INTEGER, source_id TEXT, feature_class TEXT, geometry_kind TEXT, geometry BLOB, west REAL, south REAL, east REAL, north REAL)")
    connection.execute("CREATE INDEX features_cell ON features(cx,cy)")
    counts: dict[str, int] = {}
    original, inserted = _stage_input(connection, inputs[0], layer=layer, line_geometry=False,
                                      allowed=OPEN_WATER_CLASSES if layer == "water" else None,
                                      source_counts=counts)
    if layer == "water":
        line_rows, line_parts_count = _stage_input(connection, inputs[1], layer=layer,
            line_geometry=True, allowed=set(WATERWAY_WIDTHS_M), source_counts=counts)
        original += line_rows
        inserted += line_parts_count
    return original, inserted, counts


def _surface_mesh(rows: Any, layer: str, origin_lon: float, origin_lat: float, *,
                  coarse: bool, index_stream: Any | None = None,
                  tile_id: str | None = None) -> tuple[Mesh, list[str], int, int]:
    mesh = Mesh()
    source_ids: list[str] = []
    triangle_count = 0
    skipped = 0
    for source_id, feature_class, geometry_kind, raw in rows:
        shape = wkb.loads(raw)
        feature_id = len(source_ids)
        if geometry_kind == "line":
            produced = sum(ribbon(mesh, line.simplify(0.00002, preserve_topology=True) if coarse else line,
                                  WATERWAY_WIDTHS_M[feature_class] * (1.4 if coarse else 1),
                                  feature_id, origin_lon, origin_lat, z_m=WATERWAY_OFFSET_M,
                                  material=7) for line in line_parts(shape))
        else:
            produced = polygon_surface(mesh, shape, feature_id, origin_lon, origin_lat,
                                       material=6 if layer == "parks" else 7,
                                       z_m=PARK_OFFSET_M if layer == "parks" else WATER_OFFSET_M,
                                       coarse=coarse)
        if produced:
            source_ids.append(source_id)
            triangle_count += produced
            if index_stream is not None and tile_id is not None:
                index_stream.write(json.dumps({"source_id": source_id, "tile": tile_id,
                                               "feature_class": feature_class,
                                               "geometry_kind": geometry_kind}, ensure_ascii=False) + "\n")
        else:
            skipped += 1
    return mesh, source_ids, triangle_count, skipped


def build(layer: str, *, manifest_path: Path = SOURCE_MANIFEST,
          input_paths: list[Path] | None = None, output: Path | None = None) -> dict[str, Any]:
    if layer not in {"parks", "water"}:
        raise ValueError("layer must be parks or water")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("output_version_suffix") != "v2":
        raise ValueError("Only the corrected v2 surface extract is accepted")
    paths = input_paths or ([PARKS] if layer == "parks" else [WATER, WATERWAYS])
    source_layers = [layer] if layer == "parks" else ["water", "waterways"]
    hashes = {kind: sha256(path) for kind, path in zip(source_layers, paths)}
    if any(hashes[kind] != manifest["layers"][kind]["sha256"] for kind in source_layers):
        raise ValueError("Surface source hash differs from the reviewed manifest")
    target = output or ASSET_ROOT / f"{layer}-v1"
    if target.exists():
        saved = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("source_sha256") != hashes or saved.get("partial"):
            raise ValueError("Existing surface tile set differs from source")
        return {"status": "no_op", **saved}
    stage = target.with_name(f".{target.name}.pending")
    stage.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"novostroy-{layer}-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "stage.sqlite3")
        source_rows, fragment_count, classes = stage_layer(connection, layer, paths)
        cells = connection.execute("SELECT cx,cy,COUNT(*),MIN(west),MIN(south),MAX(east),MAX(north) FROM features GROUP BY cx,cy ORDER BY cx,cy").fetchall()
        if not cells:
            raise ValueError("Surface extract produced no drawable geometry")
        details: dict[tuple[int, int], dict[str, Any]] = {}
        triangles = 0
        skipped = 0
        index_tmp = stage / ".source_tile_index.jsonl.tmp"
        with index_tmp.open("w", encoding="utf-8") as index:
            for position, (cx, cy, size, west, south, east, north) in enumerate(cells, 1):
                tile_id = f"cell_{cx}_{cy}.glb"
                origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
                rows = connection.execute("SELECT source_id,feature_class,geometry_kind,geometry FROM features WHERE cx=? AND cy=? ORDER BY source_id,feature_class", (cx, cy))
                mesh, ids, made, missed = _surface_mesh(rows, layer, origin_lon, origin_lat,
                    coarse=False, index_stream=index, tile_id=tile_id)
                triangles += made
                skipped += missed
                if not ids:
                    continue
                save_bytes(stage / tile_id, glb(mesh, {"geometry_type": f"cartographic_{layer}_surface",
                    "source_ids": ids, "feature_id_mapping": "_FEATURE_ID_0 indexes source_ids",
                    "ground_offset_m": PARK_OFFSET_M if layer == "parks" else WATER_OFFSET_M,
                    "source_coverage": "osm_mapped_snapshot_unknown_completeness"}))
                details[(cx, cy)] = {"boundingVolume": {"region": region(west, south, east, north, 3)},
                    "geometricError": 0, "refine": "REPLACE", "transform": enu_transform(origin_lon, origin_lat),
                    "content": {"uri": tile_id}}
                if position % 250 == 0:
                    print(f"built {layer} detailed tiles {position:,}/{len(cells):,}", flush=True)
            index.flush()
            os.fsync(index.fileno())
        index_tmp.replace(stage / "source_tile_index.jsonl")
        groups: dict[tuple[int, int], list[tuple[Any, ...]]] = defaultdict(list)
        for cell in cells:
            groups[(cell[0] // REGION_CELLS, cell[1] // REGION_CELLS)].append(cell)
        children = []
        for (rx, ry), group in sorted(groups.items()):
            west, south = min(c[3] for c in group), min(c[4] for c in group)
            east, north = max(c[5] for c in group), max(c[6] for c in group)
            origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
            rows = connection.execute("SELECT source_id,feature_class,geometry_kind,geometry FROM features WHERE cx BETWEEN ? AND ? AND cy BETWEEN ? AND ? ORDER BY source_id,feature_class",
                (rx * REGION_CELLS, rx * REGION_CELLS + REGION_CELLS - 1,
                 ry * REGION_CELLS, ry * REGION_CELLS + REGION_CELLS - 1))
            mesh, ids, _, _ = _surface_mesh(rows, layer, origin_lon, origin_lat, coarse=True)
            entry: dict[str, Any] = {"boundingVolume": {"region": region(west, south, east, north, 3)},
                "geometricError": 450, "refine": "REPLACE", "transform": enu_transform(origin_lon, origin_lat),
                "children": [{**details[(c[0], c[1])],
                    "transform": enu_relative_transform((c[3] + c[5]) / 2,
                                                        (c[4] + c[6]) / 2,
                                                        origin_lon, origin_lat)}
                    for c in group if (c[0], c[1]) in details]}
            if ids:
                tile_id = f"region_{rx}_{ry}.glb"
                save_bytes(stage / tile_id, glb(mesh, {"geometry_type": f"coarse_cartographic_{layer}",
                    "source_count": len(ids), "source_coverage": "osm_mapped_snapshot_unknown_completeness"}))
                entry["content"] = {"uri": tile_id}
            children.append(entry)
        save_json(stage / "tileset.json", {
            "asset": {"version": "1.1", "tilesetVersion": f"osm-{layer}-cartography-v1"},
            "geometricError": 10000,
            "root": {"boundingVolume": {"region": region(min(c[3] for c in cells),
                min(c[4] for c in cells), max(c[5] for c in cells), max(c[6] for c in cells), 3)},
                "geometricError": 5000, "refine": "REPLACE", "children": children},
            "extras": {"attribution": manifest["rights"]["attribution"],
                "visualization": "pastel map surface above WGS84 ellipsoid, not surveyed ground"}})
        finished = {"version": "1", "partial": False, "source_sha256": hashes,
            "source_rows": source_rows, "rendered_source_rows": sum(classes.values()),
            "partitioned_fragments": fragment_count, "detailed_tiles": len(details),
            "coarse_region_tiles": len(children), "triangles": triangles,
            "skipped_fragments": skipped, "rendered_class_counts": classes,
            "excluded_solid_water_classes": sorted(set(manifest["layers"]["water"]["feature_class_counts"]) - OPEN_WATER_CLASSES) if layer == "water" else [],
            "ground_offset_m": PARK_OFFSET_M if layer == "parks" else WATER_OFFSET_M,
            "waterway_ground_offset_m": WATERWAY_OFFSET_M if layer == "water" else None,
            "waterway_assumed_visual_widths_m": WATERWAY_WIDTHS_M if layer == "water" else None,
            "model_quality": "cartographic_surface", "attribution": manifest["rights"]["attribution"],
            "osm_snapshot_at": manifest["source"]["osm_snapshot_timestamp"],
            "tileset_url": f"/assets/{target.name}/tileset.json"}
        save_json(stage / "manifest.json", finished)
        connection.close()
    stage.replace(target)
    return {"status": "built", **finished}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=["parks", "water"])
    args = parser.parse_args()
    print(json.dumps(build(args.layer), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
