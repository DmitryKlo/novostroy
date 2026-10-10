"""Build permanent source-backed ground footprints independently of 3D massing.

The far overview is one bounded coverage raster. Regional overviews use
topology-preserving source polygons simplified by at most two metres. Exact
nearby cell meshes use the original rings, including every courtyard hole.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
from array import array
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from shapely import constrained_delaunay_triangles, make_valid, orient_polygons, wkb
from shapely.geometry import Polygon

from backend.store import ASSET_ROOT
from pipeline.build_citywide import REGION_CELLS, polygon_parts
from pipeline.build_progressive_city import RasterSilhouette, SOURCE, sha256
from pipeline.build_tiles import (
    CITY_CELL_DEGREES, ecef, enu_relative_transform, enu_transform, region, save_bytes, save_json,
)

BASE_CITY = ASSET_ROOT / "citywide-v5"
OUTPUT = ASSET_ROOT / "ground-v2"
REVISION = "2"
GROUND_OFFSET_M = 0.45
GROUND_BOUND_TOP_M = 2.0
REGION_SIMPLIFY_M = 2.0
REGION_GEOMETRIC_ERROR_M = 2.0
COMMON_RASTER_DIMENSION_PX = 2048
COMMON_SURFACE_GRID_CELLS = 32
PALETTE_RGB = (170, 175, 178)
MATERIAL_COLOR = [0.667, 0.686, 0.698, 1.0]


class GroundMesh:
    """One indexed unlit primitive per tile, without feature IDs or walls."""

    def __init__(self, origin_lon: float, origin_lat: float) -> None:
        self.origin_lon = origin_lon
        self.origin_lat = origin_lat
        parent = enu_transform(origin_lon, origin_lat)
        self.origin = parent[12:15]
        self.axes = (parent[0:3], parent[4:7], parent[8:11])
        self.positions = array("f")
        self.indices = array("I")
        self.vertices: dict[tuple[float, float], int] = {}
        self.triangles = 0
        self.features = 0
        self.holes = 0
        self.multipolygons = 0

    def _vertex(self, lon: float, lat: float) -> int:
        key = (lon, lat)
        found = self.vertices.get(key)
        if found is not None:
            return found
        point = ecef(lon, lat, GROUND_OFFSET_M)
        delta = [point[i] - self.origin[i] for i in range(3)]
        east, north, up = (sum(axis[i] * delta[i] for i in range(3)) for axis in self.axes)
        position = len(self.positions) // 3
        self.positions.extend((east, up, -north))
        self.vertices[key] = position
        return position

    def _surface(self, polygon: Polygon) -> int:
        if polygon.is_empty or polygon.area <= 0:
            raise ValueError("Empty source footprint surface")
        polygon = orient_polygons(polygon)
        made = 0
        triangle_area = 0.0
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            if not polygon.covers(triangle):
                continue
            coords = [(float(x), float(y)) for x, y, *_ in list(triangle.exterior.coords)[:3]]
            signed = ((coords[1][0] - coords[0][0]) * (coords[2][1] - coords[0][1])
                      - (coords[1][1] - coords[0][1]) * (coords[2][0] - coords[0][0]))
            if abs(signed) <= 1e-20:
                continue
            if signed < 0:
                coords[1], coords[2] = coords[2], coords[1]
            self.indices.extend(self._vertex(*corner) for corner in coords)
            made += 1
            triangle_area += triangle.area
        if not made or abs(triangle_area - polygon.area) > max(1e-13, polygon.area * 1e-4):
            raise ValueError("Triangulated surface does not cover its source polygon")
        self.triangles += made
        self.holes += len(polygon.interiors)
        return made

    def add(self, geometry: Any, *, simplify_m: float = 0) -> int:
        valid = make_valid(geometry) if not geometry.is_valid else geometry
        parts = list(polygon_parts(valid))
        if not parts:
            raise ValueError("Source footprint has no polygon part")
        if len(parts) > 1:
            self.multipolygons += 1
        made = 0
        for part in parts:
            drawable = part
            if simplify_m:
                # The latitude degree bound is conservative in the east-west
                # direction at Moscow latitude, so displacement stays <= 2 m.
                drawable = part.simplify(simplify_m / 111132, preserve_topology=True)
                if drawable.is_empty or drawable.area <= 0:
                    drawable = part
            for polygon in polygon_parts(drawable):
                made += self._surface(polygon)
        if not made:
            raise ValueError("Source feature produced no surface triangles")
        self.features += 1
        return made

    def to_glb(self, *, geometry_type: str) -> bytes:
        if sys.byteorder != "little" or not self.indices:
            raise ValueError("Ground mesh is empty or host is not little-endian")
        positions = self.positions.tobytes()
        vertex_count = len(self.positions) // 3
        if vertex_count <= 65535:
            index_blob = array("H", self.indices).tobytes()
            index_type = 5123
        else:
            index_blob = self.indices.tobytes()
            index_type = 5125
        binary = positions + index_blob
        binary += b"\x00" * (-len(binary) % 4)
        document = {
            "asset": {"version": "2.0", "generator": "Novostroy source ground footprint builder",
                      "extras": {"geometry_type": geometry_type,
                                 "source_count": self.features,
                                 "ground_offset_m": GROUND_OFFSET_M,
                                 "source_snapshot_date": "2026-09-23"}},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0},
                                        "indices": 1, "material": 0, "mode": 4}]}],
            "materials": [{"name": "mapped_building_ground", "doubleSided": True,
                           "extensions": {"KHR_materials_unlit": {}},
                           "pbrMetallicRoughness": {"baseColorFactor": MATERIAL_COLOR,
                                                    "metallicFactor": 0, "roughnessFactor": 1}}],
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962},
                            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(index_blob),
                             "target": 34963}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": vertex_count,
                           "type": "VEC3", "min": [min(self.positions[i::3]) for i in range(3)],
                           "max": [max(self.positions[i::3]) for i in range(3)]},
                          {"bufferView": 1, "componentType": index_type,
                           "count": len(self.indices), "type": "SCALAR"}],
        }
        json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_part += b" " * (-len(json_part) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + 8 + len(binary))
                + struct.pack("<I4s", len(json_part), b"JSON") + json_part
                + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def _reference_counts(path: Path) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = defaultdict(int)
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            _, ix, iy = json.loads(line)["tile"].removesuffix(".glb").split("_")
            counts[int(ix), int(iy)] += 1
    return counts


def _stage_source(source: Path, connection: sqlite3.Connection) -> tuple[int, dict[tuple[int, int], int]]:
    connection.execute("CREATE TABLE footprint (cx INTEGER, cy INTEGER, source_id TEXT, geometry BLOB, west REAL, south REAL, east REAL, north REAL)")
    connection.execute("CREATE INDEX footprint_cell ON footprint(cx,cy,source_id)")
    total = 0
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for batch in pq.ParquetFile(source).iter_batches(batch_size=4096, columns=["id", "geometry", "bbox"]):
        rows = []
        for item in batch.to_pylist():
            bbox = item["bbox"]
            if not item["id"] or not item["geometry"] or not bbox:
                raise ValueError("Pinned source has missing footprint or ID")
            west, south, east, north = bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"]
            if any(value is None or not math.isfinite(value) for value in (west, south, east, north)):
                raise ValueError("Pinned source has invalid footprint bounds")
            cx = math.floor(((west + east) / 2) / CITY_CELL_DEGREES)
            cy = math.floor(((south + north) / 2) / CITY_CELL_DEGREES)
            rows.append((cx, cy, item["id"], item["geometry"], west, south, east, north))
            counts[cx, cy] += 1
            total += 1
        with connection:
            connection.executemany("INSERT INTO footprint VALUES (?,?,?,?,?,?,?,?)", rows)
    return total, counts


def build(source: Path = SOURCE, base_city: Path = BASE_CITY,
          output: Path = OUTPUT) -> dict[str, Any]:
    city_manifest = json.loads((base_city / "manifest.json").read_text(encoding="utf-8"))
    if city_manifest["source_sha256"] != sha256(source) or city_manifest["source_rows"] != 564031:
        raise ValueError("Ground source differs from reviewed city snapshot")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("source_sha256") != city_manifest["source_sha256"] or saved.get("version") != REVISION:
            raise ValueError("Existing ground revision differs from pinned source")
        return {"status": "no_op", **saved}
    city = json.loads((base_city / "tileset.json").read_text(encoding="utf-8"))
    base_regions = city["root"]["children"]
    with tempfile.TemporaryDirectory(prefix=".ground-v1.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        connection = sqlite3.connect(stage / ".footprint_stage.sqlite3")
        total, counts = _stage_source(source, connection)
        if total != 564031 or counts != _reference_counts(base_city / "source_tile_index.jsonl"):
            raise ValueError("Ground cells differ from reviewed volume source index")
        cells = connection.execute("SELECT cx,cy,COUNT(*),MIN(west),MIN(south),MAX(east),MAX(north) FROM footprint GROUP BY cx,cy ORDER BY cx,cy").fetchall()
        if len(cells) != 3396 or len(base_regions) != 212:
            raise ValueError("Ground grid differs from reviewed city hierarchy")
        detail: dict[tuple[int, int], dict[str, Any]] = {}
        cell_metrics = []
        with (stage / "source_tile_index.jsonl").open("w", encoding="utf-8") as index:
            for position, (cx, cy, size, west, south, east, north) in enumerate(cells, 1):
                origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
                mesh = GroundMesh(origin_lon, origin_lat)
                uri = f"cell_{cx}_{cy}.glb"
                rows = connection.execute("SELECT source_id,geometry FROM footprint WHERE cx=? AND cy=? ORDER BY source_id", (cx, cy))
                for source_id, geometry in rows:
                    mesh.add(wkb.loads(geometry))
                    index.write(json.dumps({"source_id": source_id, "tile": uri}, ensure_ascii=False) + "\n")
                if mesh.features != size:
                    raise ValueError("Ground cell lost a source footprint")
                payload = mesh.to_glb(geometry_type="exact_mapped_building_ground_surface")
                save_bytes(stage / uri, payload)
                detail[cx, cy] = {"boundingVolume": {"region": region(west, south, east, north, GROUND_BOUND_TOP_M)},
                                  "geometricError": 0, "refine": "REPLACE",
                                  "transform": enu_transform(origin_lon, origin_lat),
                                  "content": {"uri": uri}}
                cell_metrics.append({"cell": uri, "source_count": size, "triangles": mesh.triangles,
                                     "vertices": len(mesh.positions) // 3, "bytes": len(payload),
                                     "holes": mesh.holes, "multipolygons": mesh.multipolygons})
                if position % 250 == 0:
                    print(f"built exact ground cells {position}/{len(cells)}", flush=True)
            index.flush()
            os.fsync(index.fileno())
        bounds = city["root"]["boundingVolume"]["region"]
        common = RasterSilhouette(bounds, max_dimension_px=COMMON_RASTER_DIMENSION_PX,
                                  palette_rgb=PALETTE_RGB, surface_height_m=GROUND_OFFSET_M,
                                  surface_grid_cells=COMMON_SURFACE_GRID_CELLS, mark_subpixel=False,
                                  geometry_type="distant_source_footprint_coverage")
        regions = []
        region_metrics = []
        external_raw = 0
        external_gzip5 = 0
        for position, base_region in enumerate(base_regions, 1):
            volume_uri = base_region["content"]["uri"]
            _, rx, ry = volume_uri.removesuffix(".glb").split("_")
            rx, ry = int(rx), int(ry)
            matching = [cell for cell in cells if cell[0] // REGION_CELLS == rx and cell[1] // REGION_CELLS == ry]
            if not matching:
                raise ValueError("Ground region has no source cells")
            west = min(cell[3] for cell in matching)
            south = min(cell[4] for cell in matching)
            east = max(cell[5] for cell in matching)
            north = max(cell[6] for cell in matching)
            origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
            mesh = GroundMesh(origin_lon, origin_lat)
            rows = connection.execute("SELECT geometry FROM footprint WHERE cx BETWEEN ? AND ? AND cy BETWEEN ? AND ? ORDER BY cx,cy,source_id",
                                      (rx * REGION_CELLS, rx * REGION_CELLS + REGION_CELLS - 1,
                                       ry * REGION_CELLS, ry * REGION_CELLS + REGION_CELLS - 1))
            for (geometry,) in rows:
                shape = wkb.loads(geometry)
                mesh.add(shape, simplify_m=REGION_SIMPLIFY_M)
                for polygon in polygon_parts(shape):
                    common.polygon(polygon)
                common.footprints += 1
            expected = sum(cell[2] for cell in matching)
            if mesh.features != expected:
                raise ValueError("Ground region omitted source footprints")
            overview_uri = f"overview_region_{rx}_{ry}.glb"
            payload = mesh.to_glb(geometry_type="simplified_mapped_building_ground_surface")
            save_bytes(stage / overview_uri, payload)
            cell_children = []
            for cx, cy, _, cw, cs, ce, cn in matching:
                child = detail[cx, cy].copy()
                child["transform"] = enu_relative_transform((cw + ce) / 2, (cs + cn) / 2,
                                                              origin_lon, origin_lat)
                cell_children.append(child)
            regional_bounds = {"region": region(west, south, east, north, GROUND_BOUND_TOP_M)}
            external_name = f"region_{rx}_{ry}.tileset.json"
            external = {"asset": {"version": "1.1"}, "geometricError": REGION_GEOMETRIC_ERROR_M,
                        "root": {"boundingVolume": regional_bounds,
                                 "geometricError": REGION_GEOMETRIC_ERROR_M, "refine": "REPLACE",
                                 "content": {"uri": overview_uri}, "children": cell_children},
                        "extras": {"attribution": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps",
                                   "model_quality": "cartographic_surface",
                                   "source_snapshot_date": "2026-09-23"}}
            save_json(stage / external_name, external)
            external_blob = (json.dumps(external, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            external_raw += len(external_blob)
            external_gzip5 += len(gzip.compress(external_blob, compresslevel=5))
            regions.append({"boundingVolume": regional_bounds,
                            "geometricError": REGION_GEOMETRIC_ERROR_M, "refine": "REPLACE",
                            "transform": enu_relative_transform(origin_lon, origin_lat,
                                                                (math.degrees(bounds[0]) + math.degrees(bounds[2])) / 2,
                                                                (math.degrees(bounds[1]) + math.degrees(bounds[3])) / 2),
                            "content": {"uri": overview_uri},
                            "children": [{"boundingVolume": regional_bounds,
                                          "geometricError": REGION_GEOMETRIC_ERROR_M,
                                          "refine": "REPLACE", "content": {"uri": external_name}}]})
            region_metrics.append({"region": overview_uri, "source_count": mesh.features,
                                   "triangles": mesh.triangles, "vertices": len(mesh.positions) // 3,
                                   "bytes": len(payload), "holes": mesh.holes,
                                   "multipolygons": mesh.multipolygons})
            if position % 20 == 0:
                print(f"built ground regions {position}/{len(base_regions)}", flush=True)
        if common.footprints != total:
            raise ValueError("Distant ground overview missed source rows")
        connection.close()
        (stage / ".footprint_stage.sqlite3").unlink()
        common_payload = common.to_glb()
        save_bytes(stage / "overview_city.glb", common_payload)
        common_error = math.ceil(common.pixel_m)
        tileset = {"asset": {"version": "1.1", "tilesetVersion": "mapped-ground-footprints-v2"},
                   "geometricError": common_error * 2,
                   "root": {"boundingVolume": {"region": region(math.degrees(bounds[0]), math.degrees(bounds[1]),
                                                                  math.degrees(bounds[2]), math.degrees(bounds[3]),
                                                                  GROUND_BOUND_TOP_M)},
                            "geometricError": common_error, "refine": "REPLACE",
                            "transform": enu_transform((math.degrees(bounds[0]) + math.degrees(bounds[2])) / 2,
                                                       (math.degrees(bounds[1]) + math.degrees(bounds[3])) / 2),
                            "content": {"uri": "overview_city.glb"}, "children": regions},
                   "extras": {"attribution": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps",
                              "model_quality": "cartographic_surface", "source_snapshot_date": "2026-09-23",
                              "geometry_policy": "source_polygons_exact_cells_2m_simplified_regions_distant_coverage",
                              "ground_offset_m": GROUND_OFFSET_M,
                              "common_pixel_m": common.pixel_m}}
        root_blob = (json.dumps(tileset, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        save_json(stage / "tileset.json", tileset)
        save_json(stage / "cell_metrics.json", cell_metrics)
        save_json(stage / "region_metrics.json", region_metrics)
        manifest = {"version": REVISION, "source_sha256": city_manifest["source_sha256"],
                    "source_snapshot_date": "2026-09-23", "source_rows": total,
                    "detailed_tiles": len(cell_metrics), "coarse_region_tiles": len(region_metrics),
                    "external_region_tilesets": len(region_metrics),
                    "model_quality": "cartographic_surface",
                    "geometry_policy": "exact_mapped_source_polygons_with_holes_no_simplification_at_cell_level",
                    "rights": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps",
                    "ground_offset_m": GROUND_OFFSET_M, "region_simplify_tolerance_m": REGION_SIMPLIFY_M,
                    "common_raster_max_dimension_px": COMMON_RASTER_DIMENSION_PX,
                    "common_raster_pixel_m": round(common.pixel_m, 3),
                    "common_raster_subpixel_omitted": common.subpixel_features,
                    "common_raster_glb_bytes": len(common_payload),
                    "common_raster_gpu_rgba_bytes": common.width * common.height * 4,
                    "exact_triangles": sum(row["triangles"] for row in cell_metrics),
                    "exact_glb_bytes": sum(row["bytes"] for row in cell_metrics),
                    "exact_holes": sum(row["holes"] for row in cell_metrics),
                    "exact_multipolygons": sum(row["multipolygons"] for row in cell_metrics),
                    "region_triangles": sum(row["triangles"] for row in region_metrics),
                    "region_glb_bytes": sum(row["bytes"] for row in region_metrics),
                    "root_tileset_raw_bytes": len(root_blob),
                    "root_tileset_gzip5_bytes": len(gzip.compress(root_blob, compresslevel=5)),
                    "external_tilesets_raw_bytes": external_raw,
                    "external_tilesets_gzip5_bytes": external_gzip5,
                    "source_ids_omitted_from_glb": True,
                    "tileset_url": f"/assets/{output.name}/tileset.json",
                    "partial": False}
        save_json(stage / "manifest.json", manifest)
        stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--base-city", type=Path, default=BASE_CITY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.base_city, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
