"""Verify the reviewed eight-parent multilevel sample remains bounded and real."""

from __future__ import annotations

import json
import struct

from backend.store import ASSET_ROOT
from pipeline.build_citywide import extrude_footprint
from pipeline.build_tiles import Mesh
from shapely.geometry import Polygon


def _document_and_bin(path):
    blob = path.read_bytes()
    assert struct.unpack_from("<4sII", blob) == (b"glTF", 2, len(blob))
    size = struct.unpack_from("<I", blob, 12)[0]
    document = json.loads(blob[20:20 + size])
    binary = blob[28 + size:]
    return document, binary


def _values(document, binary, accessor_id):
    accessor = document["accessors"][accessor_id]
    view = document["bufferViews"][accessor["bufferView"]]
    offset = view["byteOffset"] + accessor.get("byteOffset", 0)
    if accessor["type"] == "SCALAR" and accessor["componentType"] == 5125:
        return struct.unpack_from(f"<{accessor['count']}I", binary, offset)
    assert accessor["type"] == "VEC3" and accessor["componentType"] == 5126
    return struct.unpack_from(f"<{accessor['count'] * 3}f", binary, offset)


def test_interval_extrusion_uses_top_not_top_plus_min_height() -> None:
    mesh = Mesh()
    geometry = Polygon([(0, 0), (0.0001, 0), (0.0001, 0.0001), (0, 0.0001)])
    assert extrude_footprint(mesh, geometry, 80, 0, 0, 0, bottom=76) > 0
    positions = mesh.groups[4][0]
    assert min(positions[1::3]) == 76
    assert max(positions[1::3]) == 80


def test_reviewed_parts_replace_only_eight_parent_envelopes() -> None:
    base = ASSET_ROOT / "citywide-v3"
    revised = ASSET_ROOT / "citywide-v4"
    manifest = json.loads((revised / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["city_buildings"] == manifest["source_rows"] == 564031
    assert (manifest["rendered_part_parent_count"], manifest["rendered_part_count"]) == (8, 16)
    assert (manifest["revised_detail_tiles"], manifest["revised_coarse_region_tiles"]) == (5, 4)
    assert manifest["part_sample_scope"].endswith("not citywide coverage")
    parts = [json.loads(line) for line in (revised / "part_tile_index.jsonl").open(encoding="utf-8")]
    assert len(parts) == len({part["part_id"] for part in parts}) == 16
    assert len({part["parent_building_id"] for part in parts}) == 8
    changed_parents = set()
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as previous, \
            (revised / "source_tile_index.jsonl").open(encoding="utf-8") as current:
        for before, after in zip(previous, current, strict=True):
            if before == after:
                continue
            old, new = json.loads(before), json.loads(after)
            assert old["source_id"] == new["source_id"]
            assert old["height_m"] == new["height_m"]
            assert old["height_policy"] == new["height_policy"]
            assert new["geometry_policy"] == "reviewed_multilevel_parts_parent_suppressed"
            changed_parents.add(new["source_id"])
    assert changed_parents == {part["parent_building_id"] for part in parts}
    changed_tiles = {part["tile"] for part in parts}
    assert len(changed_tiles) == 5
    for name in changed_tiles:
        document, binary = _document_and_bin(revised / name)
        extras = document["asset"]["extras"]
        source_ids = extras["source_ids"]
        assert not changed_parents.intersection(source_ids)
        primitive = document["meshes"][0]["primitives"][0]
        feature_ids = _values(document, binary, primitive["attributes"]["_FEATURE_ID_0"])
        positions = _values(document, binary, primitive["attributes"]["POSITION"])
        assert len(positions) == len(feature_ids) * 3
        for part in (part for part in parts if part["tile"] == name):
            local_id = source_ids.index(part["part_id"])
            heights = [positions[3 * index + 1] for index, feature in enumerate(feature_ids)
                       if feature == local_id]
            assert heights
            assert abs(min(heights) - part["base_m"]) < 1e-5
            assert abs(max(heights) - part["top_m"]) < 1e-5
    assert (revised / "cell_3761_5575.glb").stat().st_ino == (base / "cell_3761_5575.glb").stat().st_ino


def test_full_reviewed_166_group_revision_preserves_vertical_intervals_and_residuals() -> None:
    base = ASSET_ROOT / "citywide-v3"
    revised = ASSET_ROOT / "citywide-v5"
    manifest = json.loads((revised / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["city_buildings"], manifest["rendered_part_parent_count"],
            manifest["rendered_part_count"]) == (564031, 166, 800)
    assert (manifest["revised_detail_tiles"], manifest["revised_coarse_region_tiles"]) == (95, 42)
    assert (manifest["measured_height_count"], manifest["assumed_floor_height_count"],
            manifest["imputed_height_count"], manifest["unknown_height_count"]) == (
                6595, 117132, 59, 440245)
    parts = [json.loads(line) for line in (revised / "part_tile_index.jsonl").open(encoding="utf-8")]
    assert len(parts) == len({part["part_id"] for part in parts}) == 800
    assert len({part["parent_building_id"] for part in parts}) == 166
    assert sum(part["base_m"] > 0 for part in parts) == 230
    changed = {}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as previous, \
            (revised / "source_tile_index.jsonl").open(encoding="utf-8") as current:
        for before, after in zip(previous, current, strict=True):
            if before == after:
                continue
            old, new = json.loads(before), json.loads(after)
            assert old["source_id"] == new["source_id"]
            assert old["height_m"] == new["height_m"]
            assert old["height_policy"] == new["height_policy"]
            changed[new["source_id"]] = new
    assert len(changed) == 166
    assert {part["parent_building_id"] for part in parts} == set(changed)
    residuals = [row for row in changed.values()
                 if row["geometry_policy"] == "reviewed_multilevel_parts_with_residual_parent"]
    assert len(residuals) == 3
    assert 0 < max(row["residual_parent_area_m2"] for row in residuals) <= 5.31
    for part in (parts[0], next(part for part in parts if part["base_m"] == 52)):
        document, binary = _document_and_bin(revised / part["tile"])
        source_ids = document["asset"]["extras"]["source_ids"]
        assert part["part_id"] in source_ids
        primitive = document["meshes"][0]["primitives"][0]
        ids = _values(document, binary, primitive["attributes"]["_FEATURE_ID_0"])
        positions = _values(document, binary, primitive["attributes"]["POSITION"])
        heights = [positions[3 * index + 1] for index, feature in enumerate(ids)
                   if feature == source_ids.index(part["part_id"])]
        assert abs(min(heights) - part["base_m"]) < 1e-5
        assert abs(max(heights) - part["top_m"]) < 1e-5
    tileset = json.loads((revised / "tileset.json").read_text(encoding="utf-8"))
    assert tileset["root"]["geometricError"] == 5000
    assert tileset["root"]["boundingVolume"]["region"][5] == max(
        parent["boundingVolume"]["region"][5] for parent in tileset["root"]["children"])
    assert all(parent["geometricError"] == 500 and
               all(child["geometricError"] == 0 for child in parent["children"])
               for parent in tileset["root"]["children"])
