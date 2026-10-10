"""Audit city v11 as a color-only revision of v10 without a browser.

Run ``rtk proxy .runtime/dev-env/bin/python integration/audit_city_brightness.py``
for the staged candidate and append ``--require-active`` after publication.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any


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
OLD = ASSETS / "public-citywide-v10"
NEW = ASSETS / "public-citywide-v11"
PRIVATE = ASSETS / "citywide-v11"
OLD_COLOR = [0.36, 0.42, 0.46, 1]
NEW_COLOR = [0.4, 0.46, 0.5, 1]
PUBLIC_MANIFEST_SHA = "85c413c99ddb2b9819876227a44af19a59d97cfc42f6028bbc5efa6718cdf348"
PRIVATE_MANIFEST_SHA = "c887dbb9fd323bab859c661a62d01a15f66fde10bf68eb354870d1eb1db4f58a"


def read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), path
    return document


def check_pointer(*, require_active: bool) -> str:
    pointer = read_json(ASSETS / "manifest.json")
    expected = "/assets/public-citywide-v11/tileset.json" if require_active else (
        "/assets/public-citywide-v10/tileset.json")
    assert pointer["city_tileset_url"] == expected
    assert pointer["ground_tileset_url"] == "/assets/public-ground-v3/tileset.json"
    assert pointer["roads_tileset_url"] == "/assets/public-roads-v2/tileset.json"
    active_models = read_json(ROOT / "data/models/active.json")
    model_manifest = read_json(ROOT / "data/models" / active_models["run_id"] / "manifest.json")
    assert model_manifest["public_root"] == pointer["project_models_root"]
    if require_active:
        assert "public-citywide-v10" in pointer["previous_public_roots"]
    return active_models["run_id"]


def check_root() -> tuple[set[Path], set[Path]]:
    old = read_json(OLD / "tileset.json")
    new = read_json(NEW / "tileset.json")
    assert_no_private_gltf_keys(new, "city v11 root")
    assert new == without_private_gltf_keys(read_json(PRIVATE / "tileset.json"))
    normalized = copy.deepcopy(new)
    assert normalized["asset"]["tilesetVersion"] == "citywide-individual-footprint-massing-v11"
    normalized["asset"]["tilesetVersion"] = old["asset"]["tilesetVersion"]
    extras = normalized["extras"]
    assert extras.pop("massing_base_color_factor") == NEW_COLOR
    assert extras.pop("palette_revision_only") is True
    assert extras["region_overview_revision"] == "citywide-v11"
    assert extras["detail_revision"] == "citywide-v11_material_only"
    assert extras["render_palette"] == "slightly_lighter_city_massing_v11"
    for key in ("region_overview_revision", "detail_revision", "render_palette"):
        extras[key] = old["extras"][key]
    assert normalized == old, "tileset tree changed beyond color metadata"
    refs, externals = referenced_assets(new, NEW)
    assert len(refs) == 3608 and len(externals) == 212
    assert len([path for path in refs if path.name.startswith("massing_region_")]) == 212
    assert len([path for path in refs if path.name.startswith("cell_")]) == 3396
    assert {path.name for path in NEW.glob("*.glb")} == {path.name for path in refs}
    assert all((NEW / path).is_file() for path in refs | externals)
    for path in externals:
        assert sha256(NEW / path) == sha256(OLD / path), path
    return refs, externals


def check_glbs(refs: set[Path]) -> None:
    for path in sorted(refs):
        name = path.name
        old_document, old_binary = glb_chunks(OLD / name)
        new_document, new_binary = glb_chunks(NEW / name)
        assert old_binary == new_binary, f"geometry/BIN changed: {name}"
        assert_no_private_gltf_keys(new_document, name)
        normalized = copy.deepcopy(new_document)
        indexes = (0, 1) if name.startswith("massing_region_") else (4,)
        for index in indexes:
            old_material = old_document["materials"][index]
            new_material = normalized["materials"][index]
            assert old_material["pbrMetallicRoughness"]["baseColorFactor"] == OLD_COLOR
            assert new_material["pbrMetallicRoughness"]["baseColorFactor"] == NEW_COLOR
            assert "KHR_materials_unlit" not in new_material.get("extensions", {})
            new_material["pbrMetallicRoughness"]["baseColorFactor"] = OLD_COLOR
        assert normalized == old_document, f"GLB JSON changed beyond material: {name}"


def audit(*, require_active: bool = False) -> None:
    model_run_id = check_pointer(require_active=require_active)
    assert sha256(NEW / "manifest.json") == PUBLIC_MANIFEST_SHA
    assert sha256(PRIVATE / "manifest.json") == PRIVATE_MANIFEST_SHA
    public_manifest = read_json(NEW / "manifest.json")
    private_manifest = read_json(PRIVATE / "manifest.json")
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == PRIVATE_MANIFEST_SHA
    assert public_manifest["palette_revision_only"] is True
    assert public_manifest["bin_identical_to_v10_glbs"] is True
    assert public_manifest["recolored_city_glbs"] == 3608
    assert private_manifest["palette_revision_only"] is True
    assert private_manifest["base_v10_manifest_sha256"] == sha256(
        ASSETS / "citywide-v10/manifest.json")
    refs, externals = check_root()
    check_glbs(refs)
    for filename in ("source_tile_index.jsonl", "cell_metrics.jsonl"):
        new_path = PRIVATE / filename
        old_path = ASSETS / "citywide-v10" / filename
        if new_path.is_file() or old_path.is_file():
            assert new_path.is_file() and old_path.is_file()
            assert sha256(new_path) == sha256(old_path)
    print(f"PASS {'active' if require_active else 'staged'} city v11 color-only: "
          f"{len(refs)} GLB JSON factors, all BIN unchanged, {len(externals)} external "
          f"JSON unchanged, refs/redaction intact; model run {model_run_id} retained")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
