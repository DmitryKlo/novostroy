"""Split immutable v6 city metadata into lazy per-region external tilesets.

The v7 root contains only silhouette tiles and one external reference per
region. Every GLB and private source index is hard-linked from v6 unchanged.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_progressive_city import sha256
from pipeline.build_tiles import save_json

BASE = ASSET_ROOT / "citywide-v6"
OUTPUT = ASSET_ROOT / "citywide-v7"
POLICY = "silhouette_regions_with_lazy_external_volume_detail_tilesets"
REGION_NAME = re.compile(r"region_-?[0-9]+_-?[0-9]+\.glb\Z")


def _serialized(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build(base: Path = BASE, output: Path = OUTPUT) -> dict[str, Any]:
    original = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if (original.get("partial") or original.get("version") != "6"
            or original.get("silhouette_region_tiles") != 212):
        raise ValueError("Expected complete reviewed v6 city")
    base_hash = sha256(base / "manifest.json")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("base_v6_manifest_sha256") != base_hash or saved.get("lazy_tileset_policy") != POLICY:
            raise ValueError("Existing v7 differs from reviewed v6 base or policy")
        return {"status": "no_op", **saved}
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    if tileset["asset"]["tilesetVersion"] != "citywide-progressive-silhouettes-v6":
        raise ValueError("Unexpected v6 tileset hierarchy")
    regions = tileset["root"]["children"]
    if len(regions) != 212:
        raise ValueError("Unexpected number of silhouette regions")
    with tempfile.TemporaryDirectory(prefix=".citywide-v7.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"manifest.json", "tileset.json"}:
                continue
            if not path.is_file():
                raise ValueError(f"Unexpected v6 asset: {path}")
            os.link(path, stage / path.name)
        external_raw = 0
        external_gzip5 = 0
        names = set()
        for silhouette in regions:
            if len(silhouette.get("children", [])) != 1 or silhouette.get("refine") != "REPLACE":
                raise ValueError("v6 silhouette region has unexpected descendants")
            volume = silhouette["children"][0]
            volume_glb = volume["content"]["uri"]
            if not REGION_NAME.fullmatch(volume_glb):
                raise ValueError("Unexpected v6 volume URI")
            name = volume_glb.removesuffix(".glb") + ".tileset.json"
            if name in names:
                raise ValueError("Duplicate external region tileset")
            names.add(name)
            if volume.get("transform") or volume.get("refine") != "REPLACE":
                raise ValueError("v6 volume transform/refinement changed")
            external = {
                "asset": {"version": "1.1"},
                "geometricError": silhouette["geometricError"],
                "root": volume,
                "extras": {"attribution": tileset["extras"]["attribution"],
                           "source_snapshot_date": original["silhouette_source_snapshot_date"],
                           "geometry_policy": "synthetic_massing_from_source_footprints"},
            }
            payload = _serialized(external)
            save_json(stage / name, external)
            external_raw += len(payload)
            external_gzip5 += len(gzip.compress(payload, compresslevel=5))
            silhouette["children"] = [{
                "boundingVolume": volume["boundingVolume"],
                "geometricError": volume["geometricError"],
                "refine": "REPLACE",
                "content": {"uri": name},
            }]
        tileset["asset"]["tilesetVersion"] = "citywide-progressive-lazy-v7"
        tileset["extras"]["external_region_tilesets"] = len(names)
        tileset["extras"]["lazy_metadata_policy"] = POLICY
        root_payload = _serialized(tileset)
        save_json(stage / "tileset.json", tileset)
        manifest = {
            **original,
            "version": "7",
            "tileset_url": f"/assets/{output.name}/tileset.json",
            "base_v6_manifest_sha256": base_hash,
            "lazy_tileset_policy": POLICY,
            "external_region_tilesets": len(names),
            "root_tileset_raw_bytes": len(root_payload),
            "root_tileset_gzip5_bytes": len(gzip.compress(root_payload, compresslevel=5)),
            "external_tilesets_raw_bytes": external_raw,
            "external_tilesets_gzip5_bytes": external_gzip5,
            "reused_v6_glbs": sum(1 for _ in stage.glob("*.glb")),
        }
        if manifest["reused_v6_glbs"] != 3820:
            raise ValueError("v6 GLB set changed")
        save_json(stage / "manifest.json", manifest)
        stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
