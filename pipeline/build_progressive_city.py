"""Build immutable, source-backed flat silhouettes above the reviewed v5 city.

The original volume and detail GLBs are hard-linked byte for byte. Geometry is
read from the pinned GeoParquet in batches and rendered one region at a time;
source IDs and crosswalks stay in the private revision only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
import zlib
import tempfile
from array import array
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import numpy as np
from shapely import make_valid, wkb
from shapely.geometry import Polygon
from shapely.errors import GEOSException

from backend.store import ASSET_ROOT, ROOT
from pipeline.build_citywide import REGION_CELLS, polygon_parts
from pipeline.build_tiles import CITY_CELL_DEGREES, ecef, enu_transform, relative_xy, save_bytes, save_json

BASE = ASSET_ROOT / "citywide-v5"
OUTPUT = ASSET_ROOT / "citywide-v6"
SOURCE = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
SILHOUETTE_HEIGHT_M = 0.05
MAX_REGION_GLB_BYTES = 64 * 1024
MAX_TOTAL_GLB_BYTES = 3 * 1024 * 1024
MAX_TOTAL_GPU_RGBA_BYTES = 140 * 1024 * 1024
SILHOUETTE_GEOMETRIC_ERROR_M = 650
ROOT_GEOMETRIC_ERROR_M = 100000
RASTER_MAX_DIMENSION_PX = 512
SURFACE_GRID_CELLS = 4


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _xy_bounds(record: dict[str, Any]) -> tuple[float, float, float, float]:
    bbox = record.get("bbox")
    if bbox:
        values = (bbox.get("xmin"), bbox.get("ymin"), bbox.get("xmax"), bbox.get("ymax"))
    else:
        values = wkb.loads(record["geometry"]).bounds
    if len(values) != 4 or any(value is None or not math.isfinite(value) for value in values):
        raise ValueError("Source building has invalid bounds")
    return tuple(float(value) for value in values)


def _stage_source(source: Path, connection: sqlite3.Connection,
                  expected_regions: set[tuple[int, int]]) -> tuple[int, dict[tuple[int, int], int]]:
    connection.execute("CREATE TABLE footprint (rx INTEGER NOT NULL, ry INTEGER NOT NULL, source_id TEXT NOT NULL, geometry BLOB NOT NULL, west REAL NOT NULL, south REAL NOT NULL, east REAL NOT NULL, north REAL NOT NULL)")
    connection.execute("CREATE INDEX footprint_region ON footprint(rx, ry, source_id)")
    counts: dict[tuple[int, int], int] = {}
    total = 0
    for batch in pq.ParquetFile(source).iter_batches(batch_size=4096, columns=["id", "geometry", "bbox"]):
        staged = []
        for record in batch.to_pylist():
            if not record["id"] or not record["geometry"]:
                continue
            west, south, east, north = _xy_bounds(record)
            ix = math.floor(((west + east) / 2) / CITY_CELL_DEGREES)
            iy = math.floor(((south + north) / 2) / CITY_CELL_DEGREES)
            region_key = (ix // REGION_CELLS, iy // REGION_CELLS)
            if region_key not in expected_regions:
                raise ValueError(f"Source region absent from reviewed city: {region_key}")
            staged.append((*region_key, record["id"], record["geometry"], west, south, east, north))
            counts[region_key] = counts.get(region_key, 0) + 1
            total += 1
        with connection:
            connection.executemany("INSERT INTO footprint VALUES (?,?,?,?,?,?,?,?)", staged)
    return total, counts


class RasterSilhouette:
    """One server-baked alpha mask on an ellipsoid-following surface grid."""

    def __init__(self, bounds_radians: list[float], *,
                 max_dimension_px: int = RASTER_MAX_DIMENSION_PX,
                 palette_rgb: tuple[int, int, int] = (92, 107, 117),
                 surface_height_m: float = SILHOUETTE_HEIGHT_M,
                 surface_grid_cells: int = SURFACE_GRID_CELLS,
                 mark_subpixel: bool = True,
                 geometry_type: str = "rasterized_flat_source_footprint_silhouettes") -> None:
        west, south, east, north = (math.degrees(value) for value in bounds_radians[:4])
        self.west, self.south, self.east, self.north = west, south, east, north
        self.max_dimension_px = max_dimension_px
        self.palette_rgb = palette_rgb
        self.surface_height_m = surface_height_m
        self.surface_grid_cells = surface_grid_cells
        self.mark_subpixel = mark_subpixel
        self.geometry_type = geometry_type
        width_m = relative_xy(east, south, west, south)[0]
        height_m = relative_xy(west, north, west, south)[1]
        self.pixel_m = max(width_m, height_m) / max_dimension_px
        self.width = max(2, math.ceil(width_m / self.pixel_m))
        self.height = max(2, math.ceil(height_m / self.pixel_m))
        self.mask = bytearray(self.width * self.height)
        self.footprints = 0
        self.fallbacks = 0
        self.subpixel_features = 0

    def _pixel(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.west) / (self.east - self.west) * self.width,
                (self.north - lat) / (self.north - self.south) * self.height)

    @staticmethod
    def _fill_ring(mask: bytearray, width: int, height: int,
                   ring: list[tuple[float, float]], value: int,
                   offset_x: int = 0, offset_y: int = 0) -> bool:
        if len(ring) < 4:
            return False
        row_start = max(0, math.floor(min(point[1] for point in ring) - offset_y))
        row_stop = min(height, math.ceil(max(point[1] for point in ring) - offset_y))
        made = False
        for row in range(row_start, row_stop):
            y = row + offset_y + 0.5
            crossings = []
            for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
                if (y0 <= y < y1) or (y1 <= y < y0):
                    crossings.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
            crossings.sort()
            for left, right in zip(crossings[::2], crossings[1::2]):
                first = max(0, math.ceil(left - offset_x - 0.5))
                last = min(width, math.ceil(right - offset_x - 0.5))
                if last > first:
                    start = row * width + first
                    mask[start:start + last - first] = bytes([value]) * (last - first)
                    made = True
        return made

    def polygon(self, polygon: Polygon) -> bool:
        exterior = [self._pixel(float(x), float(y)) for x, y, *_ in polygon.exterior.coords]
        if not polygon.interiors:
            made = self._fill_ring(self.mask, self.width, self.height, exterior, 255)
        else:
            x0 = max(0, math.floor(min(x for x, _ in exterior)))
            y0 = max(0, math.floor(min(y for _, y in exterior)))
            x1 = min(self.width, math.ceil(max(x for x, _ in exterior)))
            y1 = min(self.height, math.ceil(max(y for _, y in exterior)))
            if x1 <= x0 or y1 <= y0:
                made = False
            else:
                patch_width = x1 - x0
                patch_height = y1 - y0
                patch = bytearray(patch_width * patch_height)
                made = self._fill_ring(patch, patch_width, patch_height, exterior, 255, x0, y0)
                for interior in polygon.interiors:
                    ring = [self._pixel(float(x), float(y)) for x, y, *_ in interior.coords]
                    self._fill_ring(patch, patch_width, patch_height, ring, 0, x0, y0)
                for row in range(patch_height):
                    source = row * patch_width
                    destination = (row + y0) * self.width + x0
                    for column, present in enumerate(patch[source:source + patch_width]):
                        if present:
                            self.mask[destination + column] = 255
        if not made:
            if self.mark_subpixel:
                center = polygon.representative_point()
                px, py = self._pixel(center.x, center.y)
                ix = min(self.width - 1, max(0, math.floor(px)))
                iy = min(self.height - 1, max(0, math.floor(py)))
                self.mask[iy * self.width + ix] = 255
            self.subpixel_features += 1
        return made or self.mark_subpixel

    def bbox(self, west: float, south: float, east: float, north: float) -> None:
        ring = [self._pixel(west, south), self._pixel(east, south),
                self._pixel(east, north), self._pixel(west, north), self._pixel(west, south)]
        if not self._fill_ring(self.mask, self.width, self.height, ring, 255):
            ix, iy = self._pixel((west + east) / 2, (south + north) / 2)
            self.mask[min(self.height - 1, max(0, math.floor(iy))) * self.width
                      + min(self.width - 1, max(0, math.floor(ix)))] = 255
            self.subpixel_features += 1
        self.fallbacks += 1

    def _png(self) -> bytes:
        opaque = np.frombuffer(self.mask, dtype=np.uint8).reshape(self.height, self.width) != 0
        packed = np.packbits(opaque, axis=1, bitorder="big")
        raw = b"".join(b"\x00" + row.tobytes() for row in packed)

        def chunk(kind: bytes, content: bytes) -> bytes:
            return (struct.pack(">I", len(content)) + kind + content
                    + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 1, 3, 0, 0, 0))
                + chunk(b"PLTE", bytes((0, 0, 0, *self.palette_rgb)))
                + chunk(b"tRNS", bytes((0, 255)))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

    def to_glb(self) -> bytes:
        if not any(self.mask):
            raise ValueError("Empty silhouette region")
        origin_lon = (self.west + self.east) / 2
        origin_lat = (self.south + self.north) / 2
        parent = enu_transform(origin_lon, origin_lat)
        origin = parent[12:15]
        axes = (parent[0:3], parent[4:7], parent[8:11])
        positions = array("f")
        uv = array("f")
        for row in range(self.surface_grid_cells + 1):
            v = row / self.surface_grid_cells
            lat = self.north + (self.south - self.north) * v
            for column in range(self.surface_grid_cells + 1):
                u = column / self.surface_grid_cells
                lon = self.west + (self.east - self.west) * u
                point = ecef(lon, lat, self.surface_height_m)
                delta = [point[i] - origin[i] for i in range(3)]
                east, north, up = (sum(axis[i] * delta[i] for i in range(3)) for axis in axes)
                positions.extend((east, up, -north))
                uv.extend((u, v))
        indices = array("H")
        stride = self.surface_grid_cells + 1
        for row in range(self.surface_grid_cells):
            for column in range(self.surface_grid_cells):
                a = row * stride + column
                b = a + 1
                c = a + stride
                d = c + 1
                indices.extend((a, c, b, b, c, d))
        image = self._png()
        if sys.byteorder != "little":
            raise RuntimeError("Raster GLB builder requires a little-endian host")
        pos = positions.tobytes()
        tex = uv.tobytes()
        idx = indices.tobytes()
        binary = pos + tex + idx + image
        binary += b"\x00" * (-len(binary) % 4)
        document = {
            "asset": {"version": "2.0", "generator": "Novostroy raster silhouette builder",
                      "extras": {"geometry_type": self.geometry_type,
                                 "source_count": self.footprints, "raster_max_dimension_px": self.max_dimension_px,
                                 "ground_height_policy":
                                     f"WGS84 ellipsoid {self.surface_height_m:+g} m"}},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1},
                                        "indices": 2, "material": 0, "mode": 4}]}],
            "materials": [{"name": "building_silhouette", "doubleSided": True,
                           "alphaMode": "MASK", "alphaCutoff": 0.5,
                           "extensions": {"KHR_materials_unlit": {}},
                           "pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                                    "metallicFactor": 0, "roughnessFactor": 1}}],
            "textures": [{"source": 0, "sampler": 0}],
            "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 33071, "wrapT": 33071}],
            "images": [{"bufferView": 3, "mimeType": "image/png"}],
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos), "target": 34962},
                            {"buffer": 0, "byteOffset": len(pos), "byteLength": len(tex), "target": 34962},
                            {"buffer": 0, "byteOffset": len(pos) + len(tex), "byteLength": len(idx), "target": 34963},
                            {"buffer": 0, "byteOffset": len(pos) + len(tex) + len(idx),
                             "byteLength": len(image)}],
            "accessors": [{"bufferView": 0, "componentType": 5126,
                           "count": (self.surface_grid_cells + 1) ** 2, "type": "VEC3",
                           "min": [min(positions[i::3]) for i in range(3)],
                           "max": [max(positions[i::3]) for i in range(3)]},
                          {"bufferView": 1, "componentType": 5126,
                           "count": (self.surface_grid_cells + 1) ** 2, "type": "VEC2"},
                          {"bufferView": 2, "componentType": 5123,
                           "count": self.surface_grid_cells ** 2 * 6, "type": "SCALAR"}],
        }
        json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_part += b" " * (-len(json_part) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + 8 + len(binary))
                + struct.pack("<I4s", len(json_part), b"JSON") + json_part
                + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def _reference_counts(base: Path) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            name = json.loads(line)["tile"].removesuffix(".glb")
            _, ix, iy = name.split("_")
            key = (int(ix) // REGION_CELLS, int(iy) // REGION_CELLS)
            counts[key] = counts.get(key, 0) + 1
    return counts


def build(base: Path = BASE, output: Path = OUTPUT, source: Path = SOURCE) -> dict[str, Any]:
    base_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if base_manifest.get("partial") or base_manifest.get("version") != "5":
        raise ValueError("Expected complete reviewed v5 city")
    if sha256(source) != base_manifest["source_sha256"]:
        raise ValueError("GeoParquet does not match reviewed v5 source")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if (saved.get("base_manifest_sha256") != sha256(base / "manifest.json")
                or saved.get("silhouette_policy")
                != "source_footprints_rasterized_512px_flat_unlit_palette_alpha_mask"):
            raise ValueError("Existing v6 differs from this reviewed build policy or v5 revision")
        return {"status": "no_op", **saved}
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    regions = tileset["root"]["children"]
    names = {(int(tile["content"]["uri"].split("_")[1]),
              int(tile["content"]["uri"].split("_")[2].removesuffix(".glb"))) for tile in regions}
    if len(names) != base_manifest["coarse_region_tiles"]:
        raise ValueError("Reviewed region hierarchy has changed")
    with tempfile.TemporaryDirectory(prefix=".citywide-v6.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"tileset.json", "manifest.json"}:
                continue
            os.link(path, stage / path.name)
        connection = sqlite3.connect(stage / ".silhouette_stage.sqlite3")
        total, counts = _stage_source(source, connection, names)
        expected = _reference_counts(base)
        if total != base_manifest["city_buildings"] or counts != expected:
            raise ValueError("Silhouette source groups differ from reviewed v5 index")
        metrics = []
        for number, parent in enumerate(regions, 1):
            name = parent["content"]["uri"]
            _, rx, ry = name.removesuffix(".glb").split("_")
            key = int(rx), int(ry)
            west, south, east, north, _, _ = parent["boundingVolume"]["region"]
            origin_lon, origin_lat = math.degrees((west + east) / 2), math.degrees((south + north) / 2)
            mesh = RasterSilhouette(parent["boundingVolume"]["region"])
            rows = connection.execute("SELECT geometry,west,south,east,north FROM footprint WHERE rx=? AND ry=? ORDER BY source_id", key)
            for geometry, fw, fs, fe, fn in rows:
                try:
                    valid = wkb.loads(geometry)
                    if not valid.is_valid:
                        valid = make_valid(valid)
                    made = False
                    for part in polygon_parts(valid):
                        made = mesh.polygon(part) or made
                    if not made:
                        raise ValueError("No silhouette polygons")
                except (ValueError, TypeError, RuntimeError, GEOSException):
                    mesh.bbox(fw, fs, fe, fn)
                mesh.footprints += 1
            if mesh.footprints != counts[key]:
                raise ValueError(f"Region source count changed: {key}")
            payload = mesh.to_glb()
            if len(payload) > MAX_REGION_GLB_BYTES:
                raise ValueError(f"Silhouette region {key} exceeds {MAX_REGION_GLB_BYTES} byte budget")
            silhouette_uri = f"silhouette_{name}"
            save_bytes(stage / silhouette_uri, payload)
            metrics.append({"region": name, "source_count": mesh.footprints,
                            "triangles": SURFACE_GRID_CELLS ** 2 * 2, "bytes": len(payload),
                            "texture_width_px": mesh.width, "texture_height_px": mesh.height,
                            "texture_gpu_rgba_bytes": mesh.width * mesh.height * 4,
                            "raster_pixel_m": round(mesh.pixel_m, 3),
                            "subpixel_features": mesh.subpixel_features,
                            "geometry_fallbacks": mesh.fallbacks})
            volume = dict(parent)
            volume.pop("transform", None)
            parent["content"] = {"uri": silhouette_uri}
            parent["geometricError"] = SILHOUETTE_GEOMETRIC_ERROR_M
            parent["children"] = [volume]
            if number % 20 == 0:
                print(f"built silhouette regions {number}/{len(regions)}", flush=True)
        connection.close()
        (stage / ".silhouette_stage.sqlite3").unlink()
        if sum(row["bytes"] for row in metrics) > MAX_TOTAL_GLB_BYTES:
            raise ValueError("First-tier GLB total exceeds build budget")
        if sum(row["texture_gpu_rgba_bytes"] for row in metrics) > MAX_TOTAL_GPU_RGBA_BYTES:
            raise ValueError("First-tier RGBA texture total exceeds build budget")
        tileset["asset"]["tilesetVersion"] = "citywide-progressive-silhouettes-v6"
        tileset["root"]["geometricError"] = ROOT_GEOMETRIC_ERROR_M
        tileset["geometricError"] = ROOT_GEOMETRIC_ERROR_M * 2
        tileset["extras"].update({"first_lod_geometry": "rasterized flat source footprint silhouettes",
                                   "silhouette_raster_max_dimension_px": RASTER_MAX_DIMENSION_PX,
                                   "silhouette_surface_grid_cells": SURFACE_GRID_CELLS,
                                   "volume_and_detail_revision": "citywide-v5"})
        save_json(stage / "tileset.json", tileset)
        manifest = {**base_manifest, "version": "6", "tileset_url": f"/assets/{output.name}/tileset.json",
                    "base_manifest_sha256": sha256(base / "manifest.json"),
                    "silhouette_region_tiles": len(metrics), "silhouette_source_rows": total,
                    "silhouette_triangles": sum(row["triangles"] for row in metrics),
                    "silhouette_bytes": sum(row["bytes"] for row in metrics),
                    "silhouette_max_region_bytes": max(row["bytes"] for row in metrics),
                    "silhouette_texture_gpu_rgba_bytes": sum(row["texture_gpu_rgba_bytes"] for row in metrics),
                    "silhouette_max_region_texture_gpu_rgba_bytes": max(row["texture_gpu_rgba_bytes"] for row in metrics),
                    "silhouette_max_raster_pixel_m": max(row["raster_pixel_m"] for row in metrics),
                    "silhouette_subpixel_features": sum(row["subpixel_features"] for row in metrics),
                    "silhouette_geometry_fallback_count": sum(row["geometry_fallbacks"] for row in metrics),
                    "silhouette_policy": "source_footprints_rasterized_512px_flat_unlit_palette_alpha_mask",
                    "silhouette_source_snapshot_date": "2026-09-23",
                    "reused_v5_volume_detail_glbs": base_manifest["coarse_region_tiles"] + base_manifest["detailed_tiles"]}
        save_json(stage / "silhouette_metrics.json", metrics)
        save_json(stage / "manifest.json", manifest)
        stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.output, args.source), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
