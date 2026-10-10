"""Focused audit of the additive ground-v3 color correction.

Run staged, then with ``--require-active`` after pointer publication. The
earlier v2 geometry/source audit remains valid only if all BIN chunks and
geometry-bearing JSON fields are unchanged, as checked here.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_progressive_city import file_hash  # noqa: E402
from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    referenced_assets,
    without_private_gltf_keys,
)

ASSETS = ROOT / "assets"
OLD = ASSETS / "public-ground-v2"
NEW = ASSETS / "public-ground-v3"
PRIVATE = ASSETS / "ground-v3"
TARGET_SRGB = (170, 175, 178)
def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def linear_to_srgb(value: float) -> float:
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def audit(*, require_active: bool = False) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    active = pointer.get("ground_tileset_url") == "/assets/public-ground-v3/tileset.json"
    assert active or not require_active, "ground-v3 pointer is not active"
    if active:
        assert pointer["city_tileset_url"] == "/assets/public-citywide-v8/tileset.json"
        mapping = read_json(ASSETS / "public_release_audit.json")
        assert "public-ground-v3" in {entry["public_root"] for entry in mapping["mappings"]}
        assert "public-ground-v2" in {entry["public_root"] for entry in mapping["compatibility_mappings"]}
        assert "public-ground-v1" not in {
            entry["public_root"] for entry in
            mapping["mappings"] + mapping["compatibility_mappings"]
        }
    else:
        assert pointer["ground_tileset_url"] == "/assets/public-ground-v2/tileset.json"

    manifest = read_json(NEW / "manifest.json")
    previous_manifest = read_json(OLD / "manifest.json")
    assert manifest["public_redacted_source_ids"] is True
    assert manifest["private_manifest_sha256"] == file_hash(PRIVATE / "manifest.json")
    for key in ("source_rows", "exact_holes", "exact_multipolygons", "exact_triangles",
                "region_triangles", "detailed_tiles", "coarse_region_tiles"):
        assert manifest[key] == previous_manifest[key], key
    assert (NEW / "overview_city.glb").read_bytes() == (OLD / "overview_city.glb").read_bytes()
    tree = read_json(NEW / "tileset.json")
    previous_tree = read_json(OLD / "tileset.json")
    assert tree["asset"]["tilesetVersion"] == "mapped-ground-footprints-v3"
    assert tree["extras"]["display_color_srgb"] == "#AAAFB2"
    assert tree["extras"]["overview_png_color_space"] == "sRGB"
    assert tree["extras"]["vector_base_color_factor_color_space"] == "linear"
    normalized_tree = deepcopy(tree)
    normalized_tree["asset"]["tilesetVersion"] = previous_tree["asset"]["tilesetVersion"]
    for key in ("display_color_srgb", "overview_png_color_space",
                "vector_base_color_factor_color_space"):
        normalized_tree["extras"].pop(key)
    assert normalized_tree == previous_tree
    assert tree == without_private_gltf_keys(read_json(PRIVATE / "tileset.json"))
    assert_no_private_gltf_keys(tree, "ground-v3 root")
    glbs, externals = referenced_assets(tree, NEW)
    assert len(glbs) == 3609 and len(externals) == 212
    assert {path.name for path in NEW.glob("*.glb")} == {path.name for path in glbs}
    for relative in externals:
        assert (NEW / relative).read_bytes() == (OLD / relative).read_bytes()
        external = read_json(NEW / relative)
        assert external == without_private_gltf_keys(read_json(PRIVATE / relative))
        assert_no_private_gltf_keys(external, str(relative))

    corrected = 0
    for relative in sorted(glbs):
        if relative.name == "overview_city.glb":
            continue
        old_json, old_bin = glb_chunks(OLD / relative)
        new_json, new_bin = glb_chunks(NEW / relative)
        assert old_bin == new_bin, relative
        assert_no_private_gltf_keys(new_json, str(relative))
        old_extras = old_json["asset"].get("extras", {})
        new_extras = new_json["asset"].get("extras", {})
        assert new_extras.get("display_color_srgb") == "#AAAFB2"
        assert new_extras.get("base_color_factor_color_space") == "linear"
        assert {key: value for key, value in new_extras.items()
                if key not in ("display_color_srgb", "base_color_factor_color_space")} == old_extras
        old_material = old_json["materials"][0]
        new_material = new_json["materials"][0]
        assert {key: value for key, value in old_material.items()
                if key != "pbrMetallicRoughness"} == {
                    key: value for key, value in new_material.items()
                    if key != "pbrMetallicRoughness"}, relative
        old_pbr = old_material["pbrMetallicRoughness"]
        new_pbr = new_material["pbrMetallicRoughness"]
        assert {key: value for key, value in old_pbr.items()
                if key != "baseColorFactor"} == {
                    key: value for key, value in new_pbr.items()
                    if key != "baseColorFactor"}, relative
        rgba = new_pbr["baseColorFactor"]
        assert len(rgba) == 4 and rgba[3] == 1
        assert tuple(round(linear_to_srgb(channel) * 255) for channel in rgba[:3]) == TARGET_SRGB
        assert tuple(round(channel * 255) for channel in old_pbr["baseColorFactor"][:3]) == TARGET_SRGB
        normalized_json = deepcopy(new_json)
        normalized_json["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] = old_pbr["baseColorFactor"]
        normalized_json["asset"]["extras"].pop("display_color_srgb")
        normalized_json["asset"]["extras"].pop("base_color_factor_color_space")
        assert normalized_json == old_json, relative
        corrected += 1
    assert corrected == 3608
    print(f"PASS {'active' if active else 'staged'} ground-v3 color: "
          f"{corrected} vector GLBs linear→sRGB {TARGET_SRGB}; BIN/geometry identical v2, "
          "raster/external JSON unchanged, root metadata versioned, public refs redacted")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
