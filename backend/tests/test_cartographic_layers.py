"""Check tiled OSM cartography and topology before publishing map pointers."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

from fastapi.testclient import TestClient
from shapely.geometry import MultiPolygon, Polygon

from backend.app import app
from backend.store import ASSET_ROOT
from pipeline.build_roads import ROAD_OFFSET_M, ribbon
from pipeline.build_surfaces import (
    PARK_OFFSET_M, WATER_OFFSET_M, WATERWAY_OFFSET_M, polygon_surface,
)
from pipeline.build_tiles import Mesh
from shapely.geometry import LineString


def _glb_document(path: Path) -> dict:
    payload = path.read_bytes()
    assert struct.unpack_from("<4sII", payload) == (b"glTF", 2, len(payload))
    size, tag = struct.unpack_from("<I4s", payload, 12)
    assert tag == b"JSON"
    document = json.loads(payload[20:20 + size])
    assert document["meshes"][0]["primitives"]
    return document


def _roof_triangles(mesh: Mesh, material: int, z: float) -> list[Polygon]:
    positions, normals, _, indices = mesh.groups[material]
    assert all(math.isfinite(value) for value in positions + normals)
    assert len(positions) == len(normals)
    output = []
    for a, b, c in zip(indices[::3], indices[1::3], indices[2::3]):
        points = [(positions[3 * i], -positions[3 * i + 2], positions[3 * i + 1])
                  for i in (a, b, c)]
        assert all(abs(point[2] - z) < 1e-6 for point in points)
        output.append(Polygon((point[0], point[1]) for point in points))
    return output


def test_pastel_surface_triangulation_preserves_holes_and_disjoint_parts() -> None:
    shape_m = MultiPolygon([
        Polygon([(0, 0), (30, 0), (30, 30), (0, 30)],
                [[(8, 8), (22, 8), (22, 22), (8, 22)]]),
        Polygon([(40, 0), (50, 0), (50, 10), (40, 10)]),
    ])
    def geographic(part: Polygon) -> Polygon:
        def coords(ring):
            return [(x / 111320, y / 111132) for x, y in ring.coords]
        return Polygon(coords(part.exterior), [coords(ring) for ring in part.interiors])
    shape = MultiPolygon([geographic(part) for part in shape_m.geoms])
    for material, height in ((6, PARK_OFFSET_M), (7, WATER_OFFSET_M)):
        mesh = Mesh()
        assert polygon_surface(mesh, shape, 0, 0, 0, material=material,
                               z_m=height) > 0
        triangles = _roof_triangles(mesh, material, height)
        assert all(shape_m.buffer(1e-6).covers(piece) for piece in triangles)
        assert abs(sum(piece.area for piece in triangles) - shape_m.area) < 0.01


def test_layer_offsets_and_ribbon_are_distinct() -> None:
    assert 0 < PARK_OFFSET_M < WATER_OFFSET_M < WATERWAY_OFFSET_M < ROAD_OFFSET_M
    mesh = Mesh()
    assert ribbon(mesh, LineString([(0, 0), (0.001, 0.001)]), 6, 0, 0, 0) == 2
    assert len(_roof_triangles(mesh, 5, ROAD_OFFSET_M)) == 2


def test_spatial_tilesets_have_renderable_content_and_public_credit() -> None:
    for layer, quality, minimum in (("roads", "cartographic_ribbon", 3000),
                                    ("parks", "cartographic_surface", 1000),
                                    ("water", "cartographic_surface", 2500)):
        folder = ASSET_ROOT / f"{layer}-v1"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        tileset = json.loads((folder / "tileset.json").read_text(encoding="utf-8"))
        assert manifest["partial"] is False
        assert manifest["model_quality"] == quality
        assert manifest["detailed_tiles"] >= minimum
        assert "OpenStreetMap" in manifest["attribution"]
        assert tileset["asset"]["version"] == "1.1"
        regions = tileset["root"]["children"]
        details = [child for parent in regions for child in parent["children"]]
        assert len(regions) == manifest["coarse_region_tiles"]
        assert len(details) == manifest["detailed_tiles"]
        assert all((folder / tile["content"]["uri"]).is_file()
                   for tile in regions + details if "content" in tile)
        for tile in (regions[0], details[0]):
            assert tile["boundingVolume"]["region"][-1] > 0
            assert len(tile["transform"]) == 16
            assert _glb_document(folder / tile["content"]["uri"])
    water = json.loads((ASSET_ROOT / "water-v1/manifest.json").read_text(encoding="utf-8"))
    assert "wetland" in water["excluded_solid_water_classes"]


def test_map_publishes_all_manifested_layers_with_credit() -> None:
    manifest = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    response = TestClient(app).get("/v1/map")
    assert response.status_code == 200
    tiles = {tile["kind"]: tile for tile in response.json()["tilesets"]}
    for layer in ("roads", "parks", "water"):
        assert tiles[layer]["url"] == manifest[f"{layer}_tileset_url"]
        assert "OpenStreetMap" in tiles[layer]["attribution"]
