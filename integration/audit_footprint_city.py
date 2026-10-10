"""Independent offline audit of staged/active individual-footprint city v9.

Run with ``rtk proxy .runtime/dev-env/bin/python integration/audit_footprint_city.py``.
Add ``--require-active`` after publication. This checks emitted geometry and
references, not Cesium rendering or transfer performance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    referenced_assets,
    sha256,
    without_private_gltf_keys,
)

ASSETS = ROOT / "assets"
PRIVATE = ASSETS / "citywide-v9"
PUBLIC = ASSETS / "public-citywide-v9"
PRIOR = ASSETS / "public-citywide-v8"
INDEX = ASSETS / "citywide-v8/source_tile_index.jsonl"
PINNED_SOURCE_SHA = "a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da"
REGION = re.compile(r"region_(-?\d+)_(-?\d+)\.tileset\.json\Z")
MASSING = re.compile(r"massing_region_(-?\d+)_(-?\d+)\.glb\Z")
CELL = re.compile(r"cell_-?\d+_-?\d+\.glb\Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def same_bytes(first: Path, second: Path) -> bool:
    return first.samefile(second) or sha256(first) == sha256(second)


def source_heights() -> tuple[Counter[tuple[int, int]], dict[tuple[int, int], set[float]]]:
    counts: Counter[tuple[int, int]] = Counter()
    heights: dict[tuple[int, int], set[float]] = defaultdict(set)
    with INDEX.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            _, x, y = row["tile"].removesuffix(".glb").split("_")
            region = (int(x) // 5, int(y) // 5)
            height = float(row["height_m"])
            assert height > 0 and np.isfinite(height)
            counts[region] += 1
            heights[region].add(height)
    assert sum(counts.values()) == 564_031 and len(counts) == 212
    return counts, heights


def accessor_array(document: dict[str, Any], binary: bytes, index: int) -> np.ndarray:
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    assert "byteStride" not in view
    dtype = {5126: "<f4", 5125: "<u4"}[accessor["componentType"]]
    width = {"VEC3": 3, "SCALAR": 1}[accessor["type"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    values = np.frombuffer(binary, dtype=dtype, count=accessor["count"] * width,
                           offset=offset)
    return values.reshape(-1, width) if width != 1 else values


def near_each(actual: np.ndarray, expected: np.ndarray, tolerance: float = 0.08) -> None:
    """Every actual and expected wall height must have a nearby counterpart."""
    assert len(actual) and len(expected)
    for left, right in ((actual, expected), (expected, actual)):
        positions = np.searchsorted(right, left)
        lower = right[np.clip(positions - 1, 0, len(right) - 1)]
        upper = right[np.clip(positions, 0, len(right) - 1)]
        error = np.minimum(np.abs(left - lower), np.abs(left - upper))
        assert float(error.max()) < tolerance, float(error.max())


def audit_region_glb(path: Path, *, count: int, expected_heights: set[float],
                     metric: dict[str, Any]) -> float:
    document, binary = glb_chunks(path)
    assert_no_private_gltf_keys(document, path.name)
    extras = document["asset"]["extras"]
    assert extras["geometry_type"] == "simplified_individual_footprint_volumes"
    assert extras["source_count"] == count == metric["source_count"]
    assert extras["simplify_tolerance_m"] == 2.0
    assert extras["height_source"] == "reviewed_private_city_source_index"
    assert "cell_ids" not in extras
    assert len(document["meshes"]) == 1 and len(document["materials"]) == 2
    roof, walls = document["meshes"][0]["primitives"]
    assert roof["mode"] == walls["mode"] == 4
    assert roof["attributes"] == walls["attributes"] == {"POSITION": 0}
    assert document["materials"][0]["name"] == "individual_footprint_roof"
    assert document["materials"][1]["name"] == "individual_footprint_wall"
    positions = accessor_array(document, binary, 0)
    roof_indices = accessor_array(document, binary, roof["indices"])
    wall_indices = accessor_array(document, binary, walls["indices"])
    assert positions.dtype == np.dtype("<f4") and positions.shape[1] == 3
    assert np.isfinite(positions).all() and float(positions.max()) < 50_000
    assert len(roof_indices) % 3 == 0 and len(wall_indices) % 6 == 0
    assert int(max(roof_indices.max(), wall_indices.max())) < len(positions)
    roof_triangles = len(roof_indices) // 3
    wall_triangles = len(wall_indices) // 3
    assert roof_triangles == metric["roof_triangles"] >= count
    assert wall_triangles == metric["wall_triangles"] >= 6 * count
    assert len(positions) == metric["vertices"]
    assert path.stat().st_size == metric["bytes"]

    # Each six-index wall quad has two vertical edges. Their 3D lengths are
    # independent of the region's ENU orientation and equal the source heights.
    quads = wall_indices.reshape(-1, 6)
    assert np.array_equal(quads[:, 0], quads[:, 3])
    assert np.array_equal(quads[:, 2], quads[:, 4])
    vertical = positions[quads[:, 2]] - positions[quads[:, 1]]
    actual_heights = np.unique(np.round(np.linalg.norm(vertical, axis=1), 2))
    near_each(actual_heights, np.array(sorted(expected_heights), dtype=np.float64))
    assert abs(float(actual_heights.max()) - metric["max_height_m"]) < 0.08

    roofs = roof_indices.reshape(-1, 3)
    a = positions[roofs[:, 0]][:, [0, 2]]
    b = positions[roofs[:, 1]][:, [0, 2]]
    c = positions[roofs[:, 2]][:, [0, 2]]
    twice_area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) \
        - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    roof_area = float(np.abs(twice_area.astype(np.float64)).sum() / 2)
    reported = float(metric["summed_footprint_area_m2"])
    assert abs(roof_area - reported) / reported < 0.05, (path.name, roof_area, reported)
    return roof_area / float(metric["region_bbox_area_m2"])


def audit(*, require_active: bool = False) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    active = pointer["city_tileset_url"] == "/assets/public-citywide-v9/tileset.json"
    assert active or not require_active, "City v9 release pointer is not active"
    if active:
        assert pointer["ground_tileset_url"] == "/assets/public-ground-v3/tileset.json"
        assert {f"public-citywide-v{number}" for number in range(4, 9)}.issubset(
            set(pointer["previous_public_roots"]))
    private_manifest = read_json(PRIVATE / "manifest.json")
    public_manifest = read_json(PUBLIC / "manifest.json")
    assert private_manifest["version"] == public_manifest["version"] == "9"
    assert private_manifest["source_sha256"] == PINNED_SOURCE_SHA
    assert private_manifest["base_v8_manifest_sha256"] == sha256(ASSETS / "citywide-v8/manifest.json")
    assert public_manifest["private_manifest_sha256"] == sha256(PRIVATE / "manifest.json")
    assert public_manifest["public_redacted_source_ids"] is True
    assert private_manifest["region_overview_cell_box_count"] == 0
    assert private_manifest["region_overview_source_rows"] == 564_031
    assert same_bytes(INDEX, PRIVATE / "source_tile_index.jsonl")
    assert not (PUBLIC / "source_tile_index.jsonl").exists()
    assert not (PUBLIC / "region_overview_metrics.json").exists()

    tree = read_json(PUBLIC / "tileset.json")
    assert tree == without_private_gltf_keys(read_json(PRIVATE / "tileset.json"))
    assert_no_private_gltf_keys(tree, "v9 root")
    assert tree["extras"]["region_overview_has_cell_boxes"] is False
    assert "no cell aggregation" in tree["extras"]["geometry_policy"]
    assert "individual_footprint_regions" in tree["extras"]["lazy_metadata_policy"]
    prior_tree = read_json(PRIOR / "tileset.json")
    assert len(tree["root"]["children"]) == len(prior_tree["root"]["children"]) == 212
    references, externals = referenced_assets(tree, PUBLIC)
    assert len(externals) == 212 and len(references) == 3608
    assert all((PUBLIC / path).is_file() for path in references | externals)
    assert {path.name for path in PUBLIC.glob("*.glb")} == {path.name for path in references}

    counts, heights = source_heights()
    metrics = {row["region"]: row for row in
               json.loads((PRIVATE / "region_overview_metrics.json").read_text(encoding="utf-8"))}
    assert len(metrics) == 212
    seen_regions: set[tuple[int, int]] = set()
    detail_count = 0
    dense_coverage: float | None = None
    for current, previous in zip(tree["root"]["children"],
                                 prior_tree["root"]["children"], strict=True):
        external_name = current["children"][0]["content"]["uri"]
        match = REGION.fullmatch(external_name)
        assert match and external_name == previous["children"][0]["content"]["uri"]
        region = int(match.group(1)), int(match.group(2))
        assert region not in seen_regions
        seen_regions.add(region)
        massing_name = current["content"]["uri"]
        assert MASSING.fullmatch(massing_name)
        assert massing_name == f"massing_region_{region[0]}_{region[1]}.glb"
        assert {key: value for key, value in current.items() if key != "content"} == {
            key: value for key, value in previous.items() if key != "content"
        }
        assert current["refine"] == "REPLACE" and current["geometricError"] == 500
        current_external = read_json(PUBLIC / external_name)
        prior_external = read_json(PRIOR / external_name)
        assert current_external == without_private_gltf_keys(read_json(PRIVATE / external_name))
        assert_no_private_gltf_keys(current_external, external_name)
        assert {key: value for key, value in current_external["root"].items()
                if key != "content"} == {key: value for key, value in prior_external["root"].items()
                                      if key != "content"}
        assert current_external["root"]["content"]["uri"] == massing_name
        for child in current_external["root"]["children"]:
            name = child["content"]["uri"]
            assert CELL.fullmatch(name)
            assert same_bytes(PUBLIC / name, PRIOR / name)
            detail_count += 1
        public_glb = PUBLIC / massing_name
        assert same_bytes(public_glb, PRIVATE / massing_name)
        fraction = audit_region_glb(public_glb, count=counts[region],
                                    expected_heights=heights[region], metric=metrics[massing_name])
        if region == (752, 1115):
            dense_coverage = fraction
    assert seen_regions == set(counts)
    assert detail_count == private_manifest["reused_v8_detail_glbs"] == 3396
    assert dense_coverage is not None and dense_coverage < 0.5, dense_coverage
    print(f"PASS {'active' if active else 'staged'} city v9: 212 individual-footprint "
          f"volumes / 564031 sources, 3396 v8-identical details, no region cell boxes; "
          f"dense roof coverage {dense_coverage:.2%}; public refs/privacy/height sets verified")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
