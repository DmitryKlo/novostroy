"""Audit v9 regional footprint volumes and immutable detailed city cells."""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.audit_progressive_city import _check_bounds, _document
from pipeline.build_footprint_city import OVERVIEW_POLICY, SIMPLIFY_M
from pipeline.build_progressive_city import sha256
from pipeline.publish_public_assets import sanitize_glb

REGION_JSON = re.compile(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json\Z")
OVERVIEW_GLB = re.compile(r"massing_region_-?[0-9]+_-?[0-9]+\.glb\Z")
CELL_GLB = re.compile(r"cell_-?[0-9]+_-?[0-9]+\.glb\Z")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit(private: Path = ASSET_ROOT / "citywide-v9",
          public: Path = ASSET_ROOT / "public-citywide-v9",
          base_private: Path = ASSET_ROOT / "citywide-v8",
          base_public: Path = ASSET_ROOT / "public-citywide-v8",
          *, require_active: bool = False) -> dict[str, Any]:
    manifest = _read(private / "manifest.json")
    released = _read(public / "manifest.json")
    if (manifest.get("version") != "9" or manifest.get("base_v8_manifest_sha256") != sha256(
            base_private / "manifest.json") or manifest.get("overview_policy") != OVERVIEW_POLICY
            or manifest.get("region_simplify_tolerance_m") != SIMPLIFY_M
            or manifest.get("region_overview_cell_box_count") != 0
            or manifest.get("region_overview_source_rows") != 564031
            or manifest.get("reused_v8_detail_glbs") != 3396
            or released.get("private_manifest_sha256") != sha256(private / "manifest.json")
            or released.get("public_redacted_source_ids") is not True):
        raise ValueError("v9 provenance, source count or public redaction changed")
    for name in ("source_tile_index.jsonl", "part_tile_index.jsonl"):
        if not os.path.samefile(private / name, base_private / name) or (public / name).exists():
            raise ValueError("Reviewed private source/part index changed or leaked")
    old = _read(base_public / "tileset.json")
    new = _read(public / "tileset.json")
    if (new["asset"]["tilesetVersion"] != "citywide-individual-footprint-massing-v9"
            or new["root"]["boundingVolume"] != old["root"]["boundingVolume"]
            or new["root"]["geometricError"] != old["root"]["geometricError"]
            or new["root"]["refine"] != "REPLACE"
            or len(new["root"]["children"]) != 212):
        raise ValueError("v9 root traversal or conservative bounds changed")
    metrics = {row["region"]: row for row in _read(private / "region_overview_metrics.json")}
    if len(metrics) != 212 or (public / "region_overview_metrics.json").exists():
        raise ValueError("v9 overview metrics missing or public")
    externals: set[str] = set()
    overview: set[str] = set()
    details: set[str] = set()
    source_rows = 0
    roof_triangles = 0
    wall_triangles = 0
    overview_bytes = 0
    external_raw = 0
    external_gzip5 = 0
    for old_parent, parent in zip(old["root"]["children"], new["root"]["children"], strict=True):
        _check_bounds(new["root"], parent)
        if (parent["boundingVolume"] != old_parent["boundingVolume"]
                or parent["transform"] != old_parent["transform"]
                or parent["geometricError"] != old_parent["geometricError"]
                or parent["refine"] != "REPLACE" or len(parent.get("children", [])) != 1):
            raise ValueError("v9 region bounds, transform or REPLACE chain changed")
        name = parent["content"]["uri"]
        if not OVERVIEW_GLB.fullmatch(name) or name in overview:
            raise ValueError("v9 region still has a cell aggregation box")
        overview.add(name)
        reference = parent["children"][0]
        external_name = reference["content"]["uri"]
        if (not REGION_JSON.fullmatch(external_name) or external_name in externals
                or reference.get("children") or reference["refine"] != "REPLACE"):
            raise ValueError("Invalid v9 lazy external region")
        _check_bounds(parent, reference)
        externals.add(external_name)
        payload = (public / external_name).read_bytes()
        if payload != (private / external_name).read_bytes():
            raise ValueError("Public external tree differs from private")
        external_raw += len(payload)
        external_gzip5 += len(gzip.compress(payload, compresslevel=5))
        subtree = json.loads(payload)
        old_subtree = _read(base_public / external_name)
        _check_bounds(reference, subtree["root"])
        if (subtree["asset"]["version"] != "1.1"
                or subtree["root"]["content"]["uri"] != name
                or subtree["root"]["boundingVolume"] != old_subtree["root"]["boundingVolume"]
                or subtree["root"]["geometricError"] != old_subtree["root"]["geometricError"]
                or subtree["root"]["refine"] != "REPLACE"
                or subtree["root"]["children"] != old_subtree["root"]["children"]):
            raise ValueError("v9 changed detailed region subtree")
        for child in subtree["root"]["children"]:
            _check_bounds(subtree["root"], child)
            cell = child["content"]["uri"]
            if not CELL_GLB.fullmatch(cell) or child["geometricError"] != 0:
                raise ValueError("v9 exact city cell changed")
            details.add(cell)
        document = _document(public / name)
        extras = document["asset"]["extras"]
        primitives = document["meshes"][0]["primitives"]
        if (extras.get("geometry_type") != "simplified_individual_footprint_volumes"
                or extras.get("simplify_tolerance_m") != SIMPLIFY_M
                or len(primitives) != 2 or len(document["materials"]) != 2
                or document["extensionsUsed"] != ["KHR_materials_unlit"]
                or any("_FEATURE_ID_0" in primitive["attributes"] for primitive in primitives)
                or primitives[0]["attributes"] != {"POSITION": 0}
                or primitives[1]["attributes"] != {"POSITION": 0}):
            raise ValueError("v9 overview is not source footprint roof+walls")
        row = metrics[name]
        if (row["source_count"] != extras["source_count"]
                or row["roof_triangles"] != document["accessors"][1]["count"] // 3
                or row["wall_triangles"] != document["accessors"][2]["count"] // 3
                or row["bytes"] != (public / name).stat().st_size
                or row["max_height_m"] > parent["boundingVolume"]["region"][5] + 1e-6):
            raise ValueError("v9 overview geometry metrics or bounds changed")
        if row["source_count"] > 100 and row["summed_coverage_fraction"] >= 0.9:
            raise ValueError("Dense overview still fills a whole region")
        _, changed = sanitize_glb((public / name).read_bytes())
        if changed or not os.path.samefile(private / name, public / name):
            raise ValueError("v9 overview leaked source IDs or changed after publication")
        source_rows += row["source_count"]
        roof_triangles += row["roof_triangles"]
        wall_triangles += row["wall_triangles"]
        overview_bytes += row["bytes"]
    if (len(externals) != 212 or len(overview) != 212 or len(details) != 3396
            or source_rows != manifest["region_overview_source_rows"]
            or roof_triangles != manifest["region_overview_roof_triangles"]
            or wall_triangles != manifest["region_overview_wall_triangles"]
            or overview_bytes != manifest["region_overview_bytes"]
            or overview | details != {path.name for path in public.glob("*.glb")}
            or externals != {path.name for path in public.glob("region_*.tileset.json")}):
        raise ValueError("v9 region or detailed tile coverage changed")
    for name in details:
        if (not os.path.samefile(private / name, base_private / name)
                or not os.path.samefile(public / name, base_public / name)):
            raise ValueError(f"v9 changed immutable detailed geometry: {name}")
    root_payload = (public / "tileset.json").read_bytes()
    if (len(root_payload) != manifest["root_tileset_raw_bytes"]
            or len(gzip.compress(root_payload, compresslevel=5)) != manifest["root_tileset_gzip5_bytes"]
            or external_raw != manifest["external_tilesets_raw_bytes"]
            or external_gzip5 != manifest["external_tilesets_gzip5_bytes"]):
        raise ValueError("v9 hierarchy metadata budget changed")
    if require_active:
        pointer = _read(ASSET_ROOT / "manifest.json")
        if (pointer.get("city_tileset_url") != "/assets/public-citywide-v9/tileset.json"
                or pointer.get("ground_tileset_url") != "/assets/public-ground-v3/tileset.json"
                or not {f"public-citywide-v{number}" for number in range(4, 9)}.issubset(
                    set(pointer.get("previous_public_roots", [])))):
            raise ValueError("v9 active pointer or older immutable city roots changed")
    return {"status": "passed", "overviews": len(overview), "source_buildings": source_rows,
            "roof_triangles": roof_triangles, "wall_triangles": wall_triangles,
            "detail_cells_reused": len(details), "active_pointer_checked": require_active}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-active", action="store_true")
    print(json.dumps(audit(require_active=parser.parse_args().require_active), indent=2))
