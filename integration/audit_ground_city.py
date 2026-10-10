"""Independent offline audit of the permanent flat building ground and city v8.

Run after public staging; add ``--require-active`` after release. This checks
geometry structure and immutable artifacts, not Cesium's visible frame order.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import re
import struct
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_progressive_city import (  # noqa: E402
    file_hash,
    indexed_alpha_png,
    region_contains,
)
from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    referenced_assets,
    without_private_gltf_keys,
)

ASSETS = ROOT / "assets"
PRIVATE_GROUND = ASSETS / "ground-v2"
PUBLIC_GROUND = ASSETS / "public-ground-v2"
PREVIOUS_GROUND_CANDIDATE = ASSETS / "public-ground-v1"
PRIVATE_CITY = ASSETS / "citywide-v8"
PUBLIC_CITY = ASSETS / "public-citywide-v8"
PREVIOUS_CITY = ASSETS / "public-citywide-v7"
PINNED_SOURCE_SHA = "a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da"
REGION_JSON = re.compile(r"region_-?\d+_-?\d+\.tileset\.json\Z")
REGION_GLB = re.compile(r"overview_region_-?\d+_-?\d+\.glb\Z")
CELL_GLB = re.compile(r"cell_-?\d+_-?\d+\.glb\Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def source_counts(path: Path) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    total = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            assert isinstance(row["source_id"], str) and row["source_id"]
            counts[row["tile"]] += 1
            total += 1
    return counts, total


def assert_ground_glb(path: Path, *, geometry_type: str, count: int,
                      triangles: int, vertices: int) -> tuple[dict[str, Any], bytes]:
    document, binary = glb_chunks(path)
    assert_no_private_gltf_keys(document, str(path))
    extras = document["asset"]["extras"]
    assert extras["geometry_type"] == geometry_type
    assert extras["ground_offset_m"] == 0.45
    assert extras["source_count"] == count
    assert len(document["meshes"]) == len(document["materials"]) == 1
    primitive = document["meshes"][0]["primitives"]
    assert len(primitive) == 1 and primitive[0]["mode"] == 4
    assert set(primitive[0]["attributes"]) == {"POSITION"}
    positions = document["accessors"][primitive[0]["attributes"]["POSITION"]]
    indices = document["accessors"][primitive[0]["indices"]]
    assert positions["type"] == "VEC3" and positions["componentType"] == 5126
    assert indices["type"] == "SCALAR" and indices["componentType"] in (5123, 5125)
    assert positions["count"] == vertices and indices["count"] == triangles * 3
    assert document["materials"][0]["extensions"]["KHR_materials_unlit"] == {}
    return document, binary


def apply_transform(matrix: list[float], point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
            matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
            matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14])


def ellipsoid_height(point: tuple[float, float, float]) -> float:
    # WGS84 inverse; the coordinates are within a metre of the ellipsoid.
    x, y, z = point
    a = 6_378_137.0
    e2 = 0.00669437999014
    radius = math.hypot(x, y)
    latitude = math.atan2(z, radius * (1 - e2))
    for _ in range(6):
        n = a / math.sqrt(1 - e2 * math.sin(latitude) ** 2)
        latitude = math.atan2(z + e2 * n * math.sin(latitude), radius)
    n = a / math.sqrt(1 - e2 * math.sin(latitude) ** 2)
    return radius / math.cos(latitude) - n


def sample_height(document: dict[str, Any], binary: bytes,
                  transforms: tuple[list[float], ...]) -> None:
    accessor = document["accessors"][0]
    view = document["bufferViews"][accessor["bufferView"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    count = accessor["count"]
    for index in {0, count // 2, count - 1}:
        x, y, z = struct.unpack_from("<fff", binary, offset + index * 12)
        # glTF Y-up positions become (east, north, up) in the 3D Tiles ENU frame.
        point = (x, -z, y)
        for transform in transforms:
            point = apply_transform(transform, point)
        height = ellipsoid_height(point)
        assert abs(height - 0.45) < 0.06, (index, height)


def check_overview_chords(document: dict[str, Any], binary: bytes,
                          transform: list[float]) -> tuple[float, float]:
    """Check the actual triangles stay above the globe between grid vertices."""
    primitive = document["meshes"][0]["primitives"][0]
    position_accessor = document["accessors"][primitive["attributes"]["POSITION"]]
    index_accessor = document["accessors"][primitive["indices"]]
    position_view = document["bufferViews"][position_accessor["bufferView"]]
    index_view = document["bufferViews"][index_accessor["bufferView"]]
    position_offset = position_view.get("byteOffset", 0) + position_accessor.get("byteOffset", 0)
    index_offset = index_view.get("byteOffset", 0) + index_accessor.get("byteOffset", 0)
    vertices = []
    for index in range(position_accessor["count"]):
        x, y, z = struct.unpack_from("<fff", binary, position_offset + index * 12)
        vertices.append(apply_transform(transform, (x, -z, y)))
    heights = [ellipsoid_height(point) for point in vertices]
    assert max(abs(height - 0.45) for height in heights) < 0.06
    code, stride = ("H", 2) if index_accessor["componentType"] == 5123 else ("I", 4)
    indices = [struct.unpack_from("<" + code, binary, index_offset + index * stride)[0]
               for index in range(index_accessor["count"])]
    assert len(indices) % 3 == 0 and max(indices) < len(vertices)
    interior_heights = []
    for start in range(0, len(indices), 3):
        triangle = [vertices[indices[start + offset]] for offset in range(3)]
        centroid = tuple(sum(point[axis] for point in triangle) / 3 for axis in range(3))
        interior_heights.append(ellipsoid_height(centroid))
        for first, second in ((0, 1), (1, 2), (2, 0)):
            midpoint = tuple((triangle[first][axis] + triangle[second][axis]) / 2
                             for axis in range(3))
            interior_heights.append(ellipsoid_height(midpoint))
    assert min(interior_heights) > 0, min(interior_heights)
    return min(heights), min(interior_heights)


def audit_ground() -> dict[str, Any]:
    manifest = read_json(PRIVATE_GROUND / "manifest.json")
    public_manifest = read_json(PUBLIC_GROUND / "manifest.json")
    assert manifest["source_sha256"] == PINNED_SOURCE_SHA
    assert manifest["source_rows"] == 564_031
    assert manifest["detailed_tiles"] == 3396
    assert manifest["coarse_region_tiles"] == manifest["external_region_tilesets"] == 212
    assert manifest["exact_holes"] == 995 and manifest["exact_multipolygons"] == 702
    assert manifest["ground_offset_m"] == 0.45
    assert manifest["region_simplify_tolerance_m"] == 2.0
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == file_hash(PRIVATE_GROUND / "manifest.json")

    tree = read_json(PUBLIC_GROUND / "tileset.json")
    assert tree == without_private_gltf_keys(read_json(PRIVATE_GROUND / "tileset.json"))
    assert_no_private_gltf_keys(tree, "public ground tileset")
    assert tree["extras"]["model_quality"] == "cartographic_surface"
    assert "OpenStreetMap" in tree["extras"]["attribution"]
    root = tree["root"]
    assert root["refine"] == "REPLACE" and root["content"]["uri"] == "overview_city.glb"
    assert len(root["children"]) == 212
    assert (PUBLIC_GROUND / "overview_city.glb").stat().st_size == manifest["common_raster_glb_bytes"]
    overview, overview_binary = glb_chunks(PUBLIC_GROUND / "overview_city.glb")
    assert_no_private_gltf_keys(overview, "ground overview GLB")
    assert overview["asset"]["extras"]["geometry_type"] == "distant_source_footprint_coverage"
    assert overview["asset"]["extras"]["source_count"] == 564_031
    assert overview["asset"]["extras"]["ground_height_policy"] == "WGS84 ellipsoid +0.45 m"
    image = overview["images"][0]
    assert image["mimeType"] == "image/png" and "uri" not in image
    view = overview["bufferViews"][image["bufferView"]]
    start = view.get("byteOffset", 0)
    png = overview_binary[start:start + view["byteLength"]]
    width, height = indexed_alpha_png(png, palette_rgb=(170, 175, 178),
                                      max_dimension_px=2048)
    assert width * height * 4 == manifest["common_raster_gpu_rgba_bytes"]
    _, min_interior_height = check_overview_chords(overview, overview_binary,
                                                   root["transform"])

    cell_metrics = {row["cell"]: row for row in json.loads(
        (PRIVATE_GROUND / "cell_metrics.json").read_text(encoding="utf-8"))}
    region_metrics = {row["region"]: row for row in json.loads(
        (PRIVATE_GROUND / "region_metrics.json").read_text(encoding="utf-8"))}
    assert len(cell_metrics) == 3396 and len(region_metrics) == 212
    ground_counts, total = source_counts(PRIVATE_GROUND / "source_tile_index.jsonl")
    city_counts, city_total = source_counts(ASSETS / "citywide-v5/source_tile_index.jsonl")
    assert total == city_total == 564_031 and ground_counts == city_counts

    regions: set[str] = set()
    external_json: set[str] = set()
    cells: set[str] = set()
    for region in root["children"]:
        assert region["refine"] == "REPLACE" and region["geometricError"] == 2.0
        assert region_contains(root, region)
        region_uri = region["content"]["uri"]
        assert REGION_GLB.fullmatch(region_uri) and region_uri not in regions
        regions.add(region_uri)
        assert len(region["children"]) == 1
        stub = region["children"][0]
        assert "children" not in stub and region_contains(region, stub)
        external_uri = stub["content"]["uri"]
        assert REGION_JSON.fullmatch(external_uri) and external_uri not in external_json
        external_json.add(external_uri)
        public_external = read_json(PUBLIC_GROUND / external_uri)
        private_external = read_json(PRIVATE_GROUND / external_uri)
        assert public_external == without_private_gltf_keys(private_external)
        assert_no_private_gltf_keys(public_external, external_uri)
        external_root = public_external["root"]
        assert external_root["refine"] == "REPLACE"
        assert external_root["content"]["uri"] == region_uri
        assert region_contains(stub, external_root)
        assert_ground_glb(PUBLIC_GROUND / region_uri,
                          geometry_type="simplified_mapped_building_ground_surface",
                          count=region_metrics[region_uri]["source_count"],
                          triangles=region_metrics[region_uri]["triangles"],
                          vertices=region_metrics[region_uri]["vertices"])
        for cell in external_root["children"]:
            assert cell["refine"] == "REPLACE" and cell["geometricError"] == 0
            assert region_contains(external_root, cell)
            uri = cell["content"]["uri"]
            assert CELL_GLB.fullmatch(uri) and uri not in cells
            cells.add(uri)
            row = cell_metrics[uri]
            assert row["source_count"] == ground_counts[uri]
            document, binary = assert_ground_glb(
                PUBLIC_GROUND / uri, geometry_type="exact_mapped_building_ground_surface",
                count=row["source_count"], triangles=row["triangles"], vertices=row["vertices"])
            sample_height(document, binary, (cell["transform"], region["transform"], root["transform"]))

    assert len(regions) == len(external_json) == 212 and len(cells) == 3396
    assert {path.name for path in PUBLIC_GROUND.glob("*.tileset.json")} == external_json
    expected_glbs = {"overview_city.glb"} | regions | cells
    assert {path.name for path in PUBLIC_GROUND.glob("*.glb")} == expected_glbs
    for uri in regions | cells:
        current = PUBLIC_GROUND / uri
        previous_candidate = PREVIOUS_GROUND_CANDIDATE / uri
        assert previous_candidate.is_file()
        assert current.samefile(previous_candidate) or file_hash(current) == file_hash(previous_candidate)
    glb_refs, json_refs = referenced_assets(tree, PUBLIC_GROUND)
    assert {path.name for path in glb_refs} == expected_glbs
    assert {path.name for path in json_refs} == external_json
    assert sum(row["triangles"] for row in cell_metrics.values()) == manifest["exact_triangles"]
    assert sum(row["triangles"] for row in region_metrics.values()) == manifest["region_triangles"]
    assert sum(row["holes"] for row in cell_metrics.values()) == manifest["exact_holes"]
    assert sum(row["multipolygons"] for row in cell_metrics.values()) == manifest["exact_multipolygons"]
    assert sum((PUBLIC_GROUND / uri).stat().st_size for uri in cells) == manifest["exact_glb_bytes"]
    assert sum((PUBLIC_GROUND / uri).stat().st_size for uri in regions) == manifest["region_glb_bytes"]
    print(f"ground overview minimum sampled triangle interior height {min_interior_height:.3f} m")
    return manifest


def audit_city_v8(require_active: bool) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    active = pointer["city_tileset_url"] == "/assets/public-citywide-v8/tileset.json"
    assert active or not require_active
    if active:
        assert pointer["ground_tileset_url"] in {
            "/assets/public-ground-v2/tileset.json",
            "/assets/public-ground-v3/tileset.json",
        }
        assert set(pointer["previous_public_roots"]) >= {
            "public-citywide-v4", "public-citywide-v5",
            "public-citywide-v6", "public-citywide-v7"
        }
        mapping = read_json(ASSETS / "public_release_audit.json")
        active_ground_root = "public-ground-v3" if "ground-v3" in pointer["ground_tileset_url"] else "public-ground-v2"
        assert {entry["public_root"] for entry in mapping["mappings"]} >= {
            active_ground_root, "public-citywide-v8"
        }
        if active_ground_root == "public-ground-v3":
            assert "public-ground-v2" in {
                entry["public_root"] for entry in mapping["compatibility_mappings"]
            }
        assert "public-ground-v1" not in {
            entry["public_root"] for entry in
            mapping["mappings"] + mapping["compatibility_mappings"]
        }
        assert {entry["public_root"] for entry in mapping["compatibility_mappings"]} >= {
            "public-citywide-v4", "public-citywide-v5",
            "public-citywide-v6", "public-citywide-v7"
        }
    else:
        assert pointer["city_tileset_url"] == "/assets/public-citywide-v7/tileset.json"

    public_manifest = read_json(PUBLIC_CITY / "manifest.json")
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == file_hash(PRIVATE_CITY / "manifest.json")
    tree = read_json(PUBLIC_CITY / "tileset.json")
    assert tree == without_private_gltf_keys(read_json(PRIVATE_CITY / "tileset.json"))
    assert_no_private_gltf_keys(tree, "public city v8 tileset")
    glbs, external = referenced_assets(tree, PUBLIC_CITY)
    assert len(glbs) == 3608 and len(external) == 212
    assert not any(path.name.startswith("silhouette_") for path in glbs)
    assert {path.name for path in PUBLIC_CITY.glob("*.glb")} == {path.name for path in glbs}
    for relative in glbs:
        current = PUBLIC_CITY / relative
        prior = PREVIOUS_CITY / relative
        assert current.is_file() and prior.is_file()
        assert current.samefile(prior) or file_hash(current) == file_hash(prior)
    for relative in external:
        current = read_json(PUBLIC_CITY / relative)
        private = read_json(PRIVATE_CITY / relative)
        assert current == without_private_gltf_keys(private)
        assert_no_private_gltf_keys(current, str(relative))
    print(f"PASS {'active' if active else 'staged'} city v8 with ground-v2 geometry baseline: 1 common raster, "
          "212 simplified regions, 3396 exact flat cells with 995 holes; "
          "3608 unchanged city GLBs, public metadata redacted")


def main() -> None:
    audit_ground()
    audit_city_v8(require_active="--require-active" in sys.argv[1:])


if __name__ == "__main__":
    main()
