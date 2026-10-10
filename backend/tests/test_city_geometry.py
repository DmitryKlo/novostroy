"""Roof/hole and nested transform regressions for public-footprint massing."""

from __future__ import annotations

import math
import json
import struct
from pathlib import Path

import pytest

from shapely.geometry import MultiPolygon, Polygon

from pipeline.build_citywide import extrude_footprint, local_polygon, may_publish_city_revision
from pipeline.build_tiles import Mesh, ecef, enu_relative_transform, enu_transform
from pipeline.recolor_citywide import NEW_COLOR, _has_color, recolor_glb
from backend.store import ASSET_ROOT


LON, LAT = 37.6, 55.75


def geographic(polygon: Polygon) -> Polygon:
    def convert(points):
        return [(LON + x / (111320 * math.cos(math.radians(LAT))), LAT + y / 111132) for x, y in points]

    return Polygon(convert(polygon.exterior.coords), [convert(ring.coords) for ring in polygon.interiors])


def top_triangles(mesh: Mesh, height: float):
    positions, _, _, indices = mesh.groups[4]
    for a, b, c in zip(indices[::3], indices[1::3], indices[2::3]):
        points = [(positions[3 * i], -positions[3 * i + 2], positions[3 * i + 1]) for i in (a, b, c)]
        if all(abs(point[2] - height) < 1e-7 for point in points):
            yield Polygon((point[0], point[1]) for point in points)


def test_constrained_roofs_preserve_concavity_hole_and_multipolygon() -> None:
    l_shape = Polygon([(0, 0), (20, 0), (20, 5), (5, 5), (5, 20), (0, 20)])
    courtyard = Polygon([(0, 0), (25, 0), (25, 25), (0, 25)], [[(8, 8), (17, 8), (17, 17), (8, 17)]])
    multi = MultiPolygon([Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                          Polygon([(15, 0), (25, 0), (25, 10), (15, 10)])])
    for geometry in (l_shape, courtyard, multi):
        geographic_geometry = geographic(geometry) if isinstance(geometry, Polygon) else MultiPolygon([geographic(part) for part in geometry.geoms])
        mesh = Mesh()
        assert extrude_footprint(mesh, geographic_geometry, 12, 7, LON, LAT) > 0
        pieces = list(top_triangles(mesh, 12))
        assert pieces
        projected = local_polygon(geographic_geometry, LON, LAT) if isinstance(geographic_geometry, Polygon) else MultiPolygon([local_polygon(part, LON, LAT) for part in geographic_geometry.geoms])
        assert abs(sum(piece.area for piece in pieces) - projected.area) / projected.area < 0.02
        assert all(projected.buffer(1e-5).covers(piece) for piece in pieces)
        assert set(mesh.feature_ids[4]) == {7}


def test_nested_enu_transform_returns_child_ecef_origin() -> None:
    parent = enu_transform(37.6, 55.75)
    child = enu_relative_transform(37.61, 55.755, 37.6, 55.75)
    computed = [parent[12 + i] + sum(parent[4 * axis + i] * child[12 + axis] for axis in range(3)) for i in range(3)]
    assert math.dist(computed, ecef(37.61, 55.755)) < 0.001


def test_dark_palette_changes_only_glb_material_not_geometry(tmp_path: Path) -> None:
    old_tiles = sorted((ASSET_ROOT / "citywide").glob("*.glb"))
    if not old_tiles:
        pytest.skip("v1 citywide tile set not present")
    source = old_tiles[0]
    target = tmp_path / source.name
    recolor_glb(source, target)
    assert _has_color(target, NEW_COLOR)
    original = source.read_bytes()
    revised = target.read_bytes()
    original_json_length = struct.unpack_from("<I", original, 12)[0]
    revised_json_length = struct.unpack_from("<I", revised, 12)[0]
    assert original_json_length == revised_json_length
    assert original[20 + original_json_length:] == revised[20 + revised_json_length:]
    assert original != revised


def test_render_height_revision_is_incremental_and_source_attributes_unchanged() -> None:
    base = ASSET_ROOT / "citywide-v2"
    revised = ASSET_ROOT / "citywide-v3"
    if not revised.is_dir():
        pytest.skip("reviewed render-height revision not built")
    manifest = json.loads((revised / "manifest.json").read_text(encoding="utf-8"))
    tileset = json.loads((revised / "tileset.json").read_text(encoding="utf-8"))
    assert (manifest["source_rows"], manifest["imputed_height_count"]) == (564031, 59)
    assert (manifest["measured_height_count"], manifest["assumed_floor_height_count"]) == (6595, 117132)
    assert manifest["unknown_height_count"] == 440245
    assert manifest["material_recolor_only"] is False and manifest["height_revision_only"] is True
    assert manifest["revised_detail_tiles"] == 29
    assert manifest["revised_coarse_region_tiles"] == 11
    assert manifest["overlay_sha256"] == "e4aef2a5791cefede521a0a3e5ade0713933cee085f2b3fe1b3d8c001b99330c"
    assert (base / "tileset.json").is_file()  # the previous release is preserved
    assert tileset["root"]["geometricError"] == 5000
    updated_ids = set()
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as previous, \
            (revised / "source_tile_index.jsonl").open(encoding="utf-8") as current:
        for old_line, new_line in zip(previous, current, strict=True):
            if old_line == new_line:
                continue
            old, new = json.loads(old_line), json.loads(new_line)
            assert old["source_id"] == new["source_id"]
            assert old["height_policy"] == "display_fallback_15m"
            assert new["height_policy"] == "estimated_render_height_overlay"
            assert new["render_height_status"] == "estimated"
            assert 0 < new["height_m"] <= 300
            updated_ids.add(new["source_id"])
    assert len(updated_ids) == 59
    affected = set()
    for parent in tileset["root"]["children"]:
        assert parent["geometricError"] == 500
        assert parent["boundingVolume"]["region"][5] == max(
            child["boundingVolume"]["region"][5] for child in parent["children"])
        for child in parent["children"]:
            assert child["geometricError"] == 0
            path = revised / child["content"]["uri"]
            if path.stat().st_ino == (base / path.name).stat().st_ino:
                continue
            affected.add(path.name)
            blob = path.read_bytes()
            assert struct.unpack_from("<4sII", blob) == (b"glTF", 2, len(blob))
            json_length = struct.unpack_from("<I", blob, 12)[0]
            document = json.loads(blob[20:20 + json_length])
            assert len(document["asset"]["extras"]["source_ids"]) > 0
            for primitive in document["meshes"][0]["primitives"]:
                positions = document["accessors"][primitive["attributes"]["POSITION"]]
                normals = document["accessors"][primitive["attributes"]["NORMAL"]]
                indices = document["accessors"][primitive["indices"]]
                assert positions["count"] == normals["count"] > 0
                assert indices["count"] % 3 == 0
                assert positions["max"][1] + 5 <= child["boundingVolume"]["region"][5] + 1e-3
                assert max(abs(value) for value in normals["min"] + normals["max"]) <= 1.000001
    assert len(affected) == 29
    assert tileset["root"]["boundingVolume"]["region"][5] == max(
        parent["boundingVolume"]["region"][5] for parent in tileset["root"]["children"])


def test_base_city_builder_cannot_roll_back_reviewed_height_revision() -> None:
    candidate = "/assets/citywide-v2/tileset.json"
    assert may_publish_city_revision(None, candidate)
    assert may_publish_city_revision("/assets/citywide/tileset.json", candidate)
    assert may_publish_city_revision(candidate, candidate)
    assert not may_publish_city_revision("/assets/citywide-v3/tileset.json", candidate)
