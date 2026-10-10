"""Compare selected exact ground meshes with pinned source WKB, including holes.

Run with ``rtk proxy .runtime/geodata-env/bin/python
integration/audit_ground_source_samples.py`` after the ground release is staged.
This narrow independent sample does not assert whole-city geometry accuracy.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import sys

import pyarrow.parquet as pq
from shapely import wkb
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_ground_city import apply_transform  # noqa: E402
from integration.audit_public_assets import glb_chunks  # noqa: E402

GROUND = ROOT / "assets/public-ground-v2"
PRIVATE = ROOT / "assets/ground-v2"
SOURCE = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
SAMPLES = ("cell_3747_5582.glb", "cell_3700_5573.glb")


def lonlat(ecef: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = ecef
    a = 6_378_137.0
    e2 = 0.00669437999014
    radius = math.hypot(x, y)
    latitude = math.atan2(z, radius * (1 - e2))
    for _ in range(7):
        n = a / math.sqrt(1 - e2 * math.sin(latitude) ** 2)
        latitude = math.atan2(z + e2 * n * math.sin(latitude), radius)
    return math.degrees(math.atan2(y, x)), math.degrees(latitude)


def selected_source() -> dict[str, list[BaseGeometry]]:
    ids: dict[str, str] = {}
    with (PRIVATE / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["tile"] in SAMPLES:
                ids[row["source_id"]] = row["tile"]
    assert set(ids.values()) == set(SAMPLES)
    result: dict[str, list[BaseGeometry]] = {name: [] for name in SAMPLES}
    remaining = set(ids)
    for batch in pq.ParquetFile(SOURCE).iter_batches(batch_size=4096, columns=["id", "geometry"]):
        for row in batch.to_pylist():
            source_id = row["id"]
            if source_id not in remaining:
                continue
            result[ids[source_id]].append(wkb.loads(row["geometry"]))
            remaining.remove(source_id)
        if not remaining:
            break
    assert not remaining, f"missing pinned source rows: {len(remaining)}"
    return result


def cell_mesh(cell_name: str) -> BaseGeometry:
    root = json.loads((GROUND / "tileset.json").read_text(encoding="utf-8"))["root"]
    transforms = None
    for region in root["children"]:
        external_name = region["children"][0]["content"]["uri"]
        external = json.loads((GROUND / external_name).read_text(encoding="utf-8"))["root"]
        for cell in external["children"]:
            if cell["content"]["uri"] == cell_name:
                transforms = (cell["transform"], region["transform"], root["transform"])
                break
        if transforms is not None:
            break
    assert transforms is not None, cell_name

    glb, binary = glb_chunks(GROUND / cell_name)
    position_accessor, index_accessor = glb["accessors"]
    position_view = glb["bufferViews"][position_accessor["bufferView"]]
    index_view = glb["bufferViews"][index_accessor["bufferView"]]
    position_offset = position_view.get("byteOffset", 0) + position_accessor.get("byteOffset", 0)
    index_offset = index_view.get("byteOffset", 0) + index_accessor.get("byteOffset", 0)
    points = []
    for index in range(position_accessor["count"]):
        x, y, z = struct.unpack_from("<fff", binary, position_offset + index * 12)
        point = (x, -z, y)
        for transform in transforms:
            point = apply_transform(transform, point)
        points.append(lonlat(point))
    code = "H" if index_accessor["componentType"] == 5123 else "I"
    stride = 2 if code == "H" else 4
    indices = [struct.unpack_from("<" + code, binary, index_offset + index * stride)[0]
               for index in range(index_accessor["count"])]
    triangles = [Polygon((points[indices[index]], points[indices[index + 1]],
                          points[indices[index + 2]]))
                 for index in range(0, len(indices), 3)]
    assert all(triangle.is_valid and triangle.area > 0 for triangle in triangles)
    return unary_union(triangles)


def main() -> None:
    rows = selected_source()
    for cell_name in SAMPLES:
        source = unary_union(rows[cell_name])
        mesh = cell_mesh(cell_name)
        assert source.area > 0 and mesh.area > 0
        difference = source.symmetric_difference(mesh).area / source.area
        assert difference < 0.0001, (cell_name, difference)
        if cell_name == SAMPLES[0]:
            holes = [Polygon(interior) for geometry in rows[cell_name]
                     for polygon in (geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry])
                     for interior in polygon.interiors]
            assert holes and any(not mesh.covers(hole.representative_point()) for hole in holes)
        if cell_name == SAMPLES[1]:
            assert any(geometry.geom_type == "MultiPolygon" for geometry in rows[cell_name])
        print(f"PASS exact source geometry {cell_name}: {len(rows[cell_name])} features, "
              f"symmetric difference {difference:.8%}")


if __name__ == "__main__":
    main()
