"""Create immutable v8 massing without the old silhouette intermediate stage."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_progressive_city import sha256
from pipeline.build_tiles import save_json

BASE = ASSET_ROOT / "citywide-v7"
OUTPUT = ASSET_ROOT / "citywide-v8"
POLICY = "independent_ground_first_volume_detail_without_city_silhouettes"


def _serialized(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build(base: Path = BASE, output: Path = OUTPUT) -> dict[str, Any]:
    original = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if original.get("version") != "7" or original.get("partial") or original["reused_v6_glbs"] != 3820:
        raise ValueError("Expected complete reviewed v7 city")
    base_hash = sha256(base / "manifest.json")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("base_v7_manifest_sha256") != base_hash or saved.get("ground_first_city_policy") != POLICY:
            raise ValueError("Existing v8 does not match reviewed v7 base and policy")
        return {"status": "no_op", **saved}
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    if tileset["asset"]["tilesetVersion"] != "citywide-progressive-lazy-v7":
        raise ValueError("Unexpected v7 city tree")
    with tempfile.TemporaryDirectory(prefix=".citywide-v8.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"tileset.json", "manifest.json"} or path.name.startswith("silhouette_"):
                continue
            if path.name.endswith(".tileset.json"):
                continue
            if not path.is_file():
                raise ValueError(f"Unexpected v7 asset: {path}")
            os.link(path, stage / path.name)
        external_raw = 0
        external_gzip5 = 0
        for parent in tileset["root"]["children"]:
            if len(parent.get("children", [])) != 1 or not parent["content"]["uri"].startswith("silhouette_"):
                raise ValueError("v7 silhouette hierarchy changed")
            reference = parent["children"][0]
            external_name = reference["content"]["uri"]
            volume_name = external_name.removesuffix(".tileset.json") + ".glb"
            if not (stage / volume_name).is_file():
                raise ValueError("v7 volume GLB missing")
            external = json.loads((base / external_name).read_text(encoding="utf-8"))
            if external["root"]["content"]["uri"] != volume_name:
                raise ValueError("External volume reference changed")
            external["geometricError"] = external["root"]["geometricError"]
            save_json(stage / external_name, external)
            payload = _serialized(external)
            external_raw += len(payload)
            external_gzip5 += len(gzip.compress(payload, compresslevel=5))
            parent["content"] = {"uri": volume_name}
            parent["geometricError"] = external["root"]["geometricError"]
            reference["geometricError"] = external["root"]["geometricError"]
        tileset["asset"]["tilesetVersion"] = "citywide-ground-first-volumes-v8"
        tileset["extras"].pop("first_lod_geometry", None)
        tileset["extras"].pop("silhouette_raster_max_dimension_px", None)
        tileset["extras"].pop("silhouette_surface_grid_cells", None)
        tileset["extras"].update({"ground_first_city_policy": POLICY,
                                   "ground_is_separate_tileset": True,
                                   "region_overview_geometry": "coarse_synthetic_cell_aggregation"})
        root_payload = _serialized(tileset)
        save_json(stage / "tileset.json", tileset)
        revised = {key: value for key, value in original.items()
                   if not key.startswith("silhouette_") and key not in {
                       "base_v6_manifest_sha256", "lazy_tileset_policy", "reused_v6_glbs",
                       "reused_v5_volume_detail_glbs"}}
        revised.update({"version": "8", "tileset_url": f"/assets/{output.name}/tileset.json",
                        "base_v7_manifest_sha256": base_hash,
                        "ground_first_city_policy": POLICY,
                        "ground_is_separate_tileset": True,
                        "reused_v7_volume_detail_glbs": sum(1 for _ in stage.glob("*.glb")),
                        "root_tileset_raw_bytes": len(root_payload),
                        "root_tileset_gzip5_bytes": len(gzip.compress(root_payload, compresslevel=5)),
                        "external_tilesets_raw_bytes": external_raw,
                        "external_tilesets_gzip5_bytes": external_gzip5})
        if revised["reused_v7_volume_detail_glbs"] != 3608:
            raise ValueError("v8 must reuse exactly 212 volume and 3396 detail GLBs")
        save_json(stage / "manifest.json", revised)
        stage.replace(output)
    return {"status": "built", **revised}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
