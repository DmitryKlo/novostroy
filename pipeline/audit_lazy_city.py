"""Verify v7 lazy region metadata against the immutable v6 city offline."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.audit_progressive_city import _check_bounds
from pipeline.build_progressive_city import sha256

REGION_JSON = re.compile(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json\Z")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit(private: Path = ASSET_ROOT / "citywide-v7",
          public: Path = ASSET_ROOT / "public-citywide-v7",
          base_private: Path = ASSET_ROOT / "citywide-v6",
          base_public: Path = ASSET_ROOT / "public-citywide-v6") -> dict[str, Any]:
    manifest = _read_json(private / "manifest.json")
    public_manifest = _read_json(public / "manifest.json")
    base_manifest = _read_json(base_private / "manifest.json")
    if (manifest.get("version") != "7" or manifest.get("base_v6_manifest_sha256") != sha256(
            base_private / "manifest.json") or manifest.get("external_region_tilesets") != 212
            or manifest.get("reused_v6_glbs") != 3820
            or public_manifest.get("private_manifest_sha256") != sha256(private / "manifest.json")
            or public_manifest.get("public_redacted_source_ids") is not True
            or public_manifest.get("attribution") != _read_json(base_public / "manifest.json")["attribution"]):
        raise ValueError("v7 provenance or public redaction manifest changed")
    for key in ("city_buildings", "rendered_part_parent_count", "rendered_part_count",
                "silhouette_bytes", "silhouette_triangles", "silhouette_source_rows"):
        if manifest[key] != base_manifest[key]:
            raise ValueError(f"v7 city source count changed: {key}")
    for name in ("source_tile_index.jsonl", "part_tile_index.jsonl"):
        if not os.path.samefile(private / name, base_private / name) or (public / name).exists():
            raise ValueError(f"Private source index changed or leaked: {name}")
    old = _read_json(base_public / "tileset.json")
    new = _read_json(public / "tileset.json")
    if (new["asset"]["tilesetVersion"] != "citywide-progressive-lazy-v7"
            or new["root"]["boundingVolume"] != old["root"]["boundingVolume"]
            or new["root"]["geometricError"] != old["root"]["geometricError"]
            or new["root"]["refine"] != "REPLACE"):
        raise ValueError("v7 root LOD or bounds changed")
    before = old["root"]["children"]
    after = new["root"]["children"]
    if len(before) != 212 or len(after) != 212:
        raise ValueError("Unexpected v7 region count")
    names = set()
    glbs = set()
    external_raw = 0
    external_gzip5 = 0
    detail_count = 0
    for old_silhouette, silhouette in zip(before, after, strict=True):
        old_volume = old_silhouette["children"][0]
        if {key: value for key, value in silhouette.items() if key != "children"} != {
                key: value for key, value in old_silhouette.items() if key != "children"}:
            raise ValueError("v7 silhouette metadata changed")
        if len(silhouette.get("children", [])) != 1:
            raise ValueError("v7 silhouette must have one lazy child")
        _check_bounds(new["root"], silhouette)
        reference = silhouette["children"][0]
        if ("children" in reference or reference["refine"] != "REPLACE"
                or reference["geometricError"] != old_volume["geometricError"]
                or reference["boundingVolume"] != old_volume["boundingVolume"]):
            raise ValueError("External reference tile violates 3D Tiles contract")
        name = reference["content"]["uri"]
        expected = old_volume["content"]["uri"].removesuffix(".glb") + ".tileset.json"
        if name != expected or not REGION_JSON.fullmatch(name) or name in names:
            raise ValueError("External region URI invalid or duplicated")
        names.add(name)
        _check_bounds(silhouette, reference)
        private_external = private / name
        public_external = public / name
        if private_external.read_bytes() != public_external.read_bytes():
            raise ValueError("Private and public external tilesets differ")
        external_bytes = public_external.read_bytes()
        external_raw += len(external_bytes)
        external_gzip5 += len(gzip.compress(external_bytes, compresslevel=5))
        external = json.loads(external_bytes)
        if (external["asset"]["version"] != "1.1"
                or external["root"] != old_volume
                or external["geometricError"] < external["root"]["geometricError"]
                or external["extras"]["attribution"] != public_manifest["attribution"]
                or "source_ids" in json.dumps(external)
                or "feature_id_mapping" in json.dumps(external)):
            raise ValueError("External region subtree or privacy changed")
        _check_bounds(reference, external["root"])
        glbs.add(silhouette["content"]["uri"])
        glbs.add(external["root"]["content"]["uri"])
        for child in external["root"]["children"]:
            _check_bounds(external["root"], child)
            if child["refine"] != "REPLACE" or child["geometricError"] != 0:
                raise ValueError("Detailed cell refinement changed")
            glbs.add(child["content"]["uri"])
            detail_count += 1
    if detail_count != 3396 or len(glbs) != 3820:
        raise ValueError("External subtree did not preserve all GLBs")
    if names != {path.name for path in public.glob("region_*.tileset.json")}:
        raise ValueError("Public external JSON set differs from references")
    if glbs != {path.name for path in public.glob("*.glb")}:
        raise ValueError("Public GLB set differs from references")
    for name in glbs:
        if (not os.path.samefile(private / name, base_private / name)
                or not os.path.samefile(public / name, base_public / name)):
            raise ValueError(f"v6 GLB changed in v7: {name}")
    root_bytes = (public / "tileset.json").read_bytes()
    if (len(root_bytes) != manifest["root_tileset_raw_bytes"]
            or len(gzip.compress(root_bytes, compresslevel=5)) != manifest["root_tileset_gzip5_bytes"]
            or external_raw != manifest["external_tilesets_raw_bytes"]
            or external_gzip5 != manifest["external_tilesets_gzip5_bytes"]):
        raise ValueError("v7 metadata budget differs from manifest")
    return {"status": "passed", "root_raw_bytes": len(root_bytes),
            "root_gzip5_bytes": manifest["root_tileset_gzip5_bytes"],
            "external_tilesets": len(names), "external_gzip5_bytes": external_gzip5,
            "reused_glbs": len(glbs), "detail_cells": detail_count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=ASSET_ROOT / "citywide-v7")
    parser.add_argument("--public", type=Path, default=ASSET_ROOT / "public-citywide-v7")
    args = parser.parse_args()
    print(json.dumps(audit(args.private, args.public), indent=2))


if __name__ == "__main__":
    main()
