"""Audit the staged ground-first release before and after pointer publication."""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.audit_progressive_city import _check_bounds, _document
from pipeline.build_ground_color_revision import (
    COLOR_SPACE_POLICY, DISPLAY_HEX, DISPLAY_SRGB, LINEAR_FACTOR, linear_to_srgb,
)
from pipeline.build_progressive_city import sha256
from pipeline.publish_public_assets import sanitize_glb

REGION = re.compile(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json\Z")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _metadata_budget(root: Path, manifest: dict[str, Any], externals: set[str]) -> None:
    blob = (root / "tileset.json").read_bytes()
    others = [(root / name).read_bytes() for name in sorted(externals)]
    actual = (len(blob), len(gzip.compress(blob, compresslevel=5)),
              sum(map(len, others)), sum(len(gzip.compress(item, compresslevel=5)) for item in others))
    expected = tuple(manifest[key] for key in (
        "root_tileset_raw_bytes", "root_tileset_gzip5_bytes",
        "external_tilesets_raw_bytes", "external_tilesets_gzip5_bytes"))
    if actual != expected:
        raise ValueError(f"Metadata budget changed in {root.name}: {actual} != {expected}")


def _audit_ground(private: Path, public: Path) -> dict[str, int]:
    manifest, released = _json(private / "manifest.json"), _json(public / "manifest.json")
    base = ASSET_ROOT / "ground-v2"
    if (manifest.get("version") != "3" or manifest.get("source_rows") != 564031
            or manifest.get("detailed_tiles") != 3396 or manifest.get("coarse_region_tiles") != 212
            or manifest.get("exact_holes") != 995 or manifest.get("exact_multipolygons") != 702
            or manifest.get("ground_offset_m") != 0.45
            or manifest.get("base_v2_manifest_sha256") != sha256(base / "manifest.json")
            or manifest.get("color_space_policy") != COLOR_SPACE_POLICY
            or manifest.get("display_color_srgb") != DISPLAY_HEX
            or manifest.get("vector_linear_base_color_factor") != LINEAR_FACTOR
            or tuple(linear_to_srgb(value) for value in LINEAR_FACTOR[:3]) != DISPLAY_SRGB
            or released.get("private_manifest_sha256") != sha256(private / "manifest.json")
            or released.get("public_redacted_source_ids") is not True):
        raise ValueError("Ground provenance or source coverage changed")
    root = _json(public / "tileset.json")
    tile = root["root"]
    if (root["asset"]["tilesetVersion"] != "mapped-ground-footprints-v3"
            or root["geometricError"] != 94 or tile["geometricError"] != 47
            or tile["refine"] != "REPLACE" or tile["content"]["uri"] != "overview_city.glb"
            or root["extras"].get("display_color_srgb") != DISPLAY_HEX
            or len(tile["children"]) != 212):
        raise ValueError("Ground root hierarchy changed")
    if _document(public / "overview_city.glb")["asset"]["extras"]["ground_height_policy"] != \
            "WGS84 ellipsoid +0.45 m":
        raise ValueError("Ground raster height description changed")
    externals: set[str] = set()
    glbs: set[str] = {"overview_city.glb"}
    cell_count = 0
    for parent in tile["children"]:
        _check_bounds(tile, parent)
        name = parent["content"]["uri"]
        if (parent["geometricError"] != 2 or parent["refine"] != "REPLACE"
                or not name.startswith("overview_region_") or not name.endswith(".glb")
                or len(parent.get("children", [])) != 1):
            raise ValueError("Ground region overview changed")
        glbs.add(name)
        reference = parent["children"][0]
        _check_bounds(parent, reference)
        external_name = reference["content"]["uri"]
        if (not REGION.fullmatch(external_name) or external_name in externals
                or reference.get("children") or reference["geometricError"] != 2):
            raise ValueError("Invalid ground lazy region reference")
        externals.add(external_name)
        subtree = _json(public / external_name)
        external_root = subtree["root"]
        _check_bounds(reference, external_root)
        if (subtree["asset"]["version"] != "1.1" or subtree["geometricError"] != 2
                or external_root["geometricError"] != 2 or external_root["refine"] != "REPLACE"
                or external_root["content"]["uri"] != name):
            raise ValueError("Invalid ground external subtree")
        for child in external_root["children"]:
            _check_bounds(external_root, child)
            cell = child["content"]["uri"]
            if (not re.fullmatch(r"cell_-?[0-9]+_-?[0-9]+\.glb", cell)
                    or child["geometricError"] != 0 or child["refine"] != "REPLACE"):
                raise ValueError("Invalid exact ground cell")
            glbs.add(cell)
            cell_count += 1
    if (cell_count != 3396 or len(glbs) != 3609 or len(externals) != 212
            or glbs != {path.name for path in public.glob("*.glb")}
            or externals != {path.name for path in public.glob("region_*.tileset.json")}):
        raise ValueError("Ground tile references do not match staged files")
    for name in glbs:
        if not os.path.samefile(private / name, public / name):
            raise ValueError(f"Ground geometry differs between private/public: {name}")
        payload = (public / name).read_bytes()
        _, changed = sanitize_glb(payload)
        if changed:
            raise ValueError(f"Source-ID metadata found in public ground GLB: {name}")
        if name == "overview_city.glb":
            if not os.path.samefile(public / name, ASSET_ROOT / "public-ground-v2" / name):
                raise ValueError("Distant overview PNG changed during color revision")
        else:
            previous = (base / name).read_bytes()
            prior_length = int.from_bytes(previous[12:16], "little")
            new_length = int.from_bytes(payload[12:16], "little")
            if previous[20 + prior_length:] != payload[20 + new_length:]:
                raise ValueError(f"Ground geometry BIN changed during color revision: {name}")
            document = _document(public / name)
            if (document["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] != LINEAR_FACTOR
                    or document["asset"]["extras"].get("display_color_srgb") != DISPLAY_HEX
                    or document["asset"]["extras"].get("base_color_factor_color_space") != "linear"):
                raise ValueError(f"Ground material color space changed: {name}")
    if any((public / name).exists() for name in ("source_tile_index.jsonl", "cell_metrics.json",
                                                   "region_metrics.json")):
        raise ValueError("Private ground indexes reached public root")
    metrics = _json(private / "cell_metrics.json")
    if (sum(row["source_count"] for row in metrics) != 564031
            or sum(row["holes"] for row in metrics) != 995
            or sum(row["multipolygons"] for row in metrics) != 702
            or sum(row["triangles"] for row in metrics) != manifest["exact_triangles"]):
        raise ValueError("Exact source geometry metrics changed")
    _metadata_budget(public, manifest, externals)
    return {"exact_cells": cell_count, "regions": len(externals), "exact_holes": 995}


def _audit_city(private: Path, public: Path) -> dict[str, int]:
    manifest, released = _json(private / "manifest.json"), _json(public / "manifest.json")
    base_private, base_public = ASSET_ROOT / "citywide-v7", ASSET_ROOT / "public-citywide-v7"
    if (manifest.get("version") != "8" or manifest.get("reused_v7_volume_detail_glbs") != 3608
            or manifest.get("base_v7_manifest_sha256") != sha256(base_private / "manifest.json")
            or released.get("private_manifest_sha256") != sha256(private / "manifest.json")
            or released.get("public_redacted_source_ids") is not True):
        raise ValueError("City v8 provenance changed")
    root = _json(public / "tileset.json")["root"]
    if root.get("content") or root["refine"] != "REPLACE" or len(root["children"]) != 212:
        raise ValueError("City v8 root hierarchy changed")
    externals: set[str] = set()
    glbs: set[str] = set()
    details = 0
    for parent in root["children"]:
        _check_bounds(root, parent)
        name = parent["content"]["uri"]
        if name.startswith("silhouette_") or len(parent.get("children", [])) != 1:
            raise ValueError("Old city silhouette remains in v8")
        glbs.add(name)
        ref = parent["children"][0]
        external_name = ref["content"]["uri"]
        if not REGION.fullmatch(external_name) or external_name in externals or ref.get("children"):
            raise ValueError("Invalid city v8 external reference")
        externals.add(external_name)
        external = _json(public / external_name)
        _check_bounds(ref, external["root"])
        if external["root"]["content"]["uri"] != name:
            raise ValueError("City volume parent changed")
        for child in external["root"]["children"]:
            _check_bounds(external["root"], child)
            glbs.add(child["content"]["uri"])
            details += 1
    if details != 3396 or len(glbs) != 3608 or glbs != {path.name for path in public.glob("*.glb")}:
        raise ValueError("City v8 GLB coverage changed")
    for name in glbs:
        if (not os.path.samefile(private / name, base_private / name)
                or not os.path.samefile(public / name, base_public / name)):
            raise ValueError(f"v8 changed prior volume/detail bytes: {name}")
    _metadata_budget(public, manifest, externals)
    return {"volumes": 212, "details": details}


def audit(*, require_active: bool = False) -> dict[str, Any]:
    ground = _audit_ground(ASSET_ROOT / "ground-v3", ASSET_ROOT / "public-ground-v3")
    city = _audit_city(ASSET_ROOT / "citywide-v8", ASSET_ROOT / "public-citywide-v8")
    if require_active:
        pointer = _json(ASSET_ROOT / "manifest.json")
        if (pointer.get("ground_tileset_url") != "/assets/public-ground-v3/tileset.json"
                or pointer.get("city_tileset_url") != "/assets/public-citywide-v9/tileset.json"
                or not {f"public-citywide-v{number}" for number in range(4, 9)}.issubset(
                    set(pointer.get("previous_public_roots", [])))
                or "public-ground-v2" not in pointer.get("previous_public_roots", [])):
            raise ValueError("Active pointer or immutable city compatibility changed")
    return {"status": "passed", "ground": ground, "city": city,
            "active_pointer_checked": require_active}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-active", action="store_true")
    print(json.dumps(audit(require_active=parser.parse_args().require_active), indent=2))
