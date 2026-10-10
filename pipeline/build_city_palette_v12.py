"""Stage a neutral city material revision without rebuilding any geometry.

The only GLB edits are the city massing baseColorFactor values in JSON chunks.
All BIN chunks, region documents, source indexes, transforms and bounds are
preserved from immutable v11. Publication is a separate audited step.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_city_palette_revision import _check_material, _json_chunk
from pipeline.build_tiles import save_json
from pipeline.publish_public_assets import sha256


BASE = ASSET_ROOT / "citywide-v11"
OUTPUT = ASSET_ROOT / "citywide-v12"
OLD_FACTOR = [0.40, 0.46, 0.50, 1]
NEW_FACTOR = [0.42, 0.44, 0.46, 1]
OLD_DETAIL_BYTES = b"[0.40,0.46,0.50,1]"
NEW_DETAIL_BYTES = b"[0.42,0.44,0.46,1]"
OLD_REGION_BYTES = b"[0.40,0.46,0.50,1.0]"
NEW_REGION_BYTES = b"[0.42,0.44,0.46,1.0]"
PALETTE_NAME = "subdued_neutral_city_massing_v12"


def recolor_glb(source: Path, target: Path, *, overview: bool) -> bool:
    old_bytes = OLD_REGION_BYTES if overview else OLD_DETAIL_BYTES
    new_bytes = NEW_REGION_BYTES if overview else NEW_DETAIL_BYTES
    if len(old_bytes) != len(new_bytes):
        raise ValueError("Material replacement would move the BIN chunk")
    original, length = _json_chunk(source)
    if original.count(old_bytes) != (2 if overview else 1):
        raise ValueError(f"Unexpected v11 massing material in {source.name}")
    _check_material(original, overview=overview, factor=OLD_FACTOR)
    revised = original.replace(old_bytes, new_bytes)
    _check_material(revised, overview=overview, factor=NEW_FACTOR)
    if target.exists():
        existing, existing_length = _json_chunk(target)
        if (target.stat().st_size != source.stat().st_size or existing_length != length
                or existing != revised):
            raise ValueError(f"Staged v12 tile differs from expected recolor: {target.name}")
        return False
    temporary = target.with_name(f".{target.name}.pending")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as stream:
            stream.seek(20)
            stream.write(revised)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            raise ValueError("Color revision changed GLB length")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def build(base: Path = BASE, output: Path = OUTPUT) -> dict[str, Any]:
    base_manifest_path = base / "manifest.json"
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    if (base_manifest.get("version") != "11" or base_manifest.get("partial")
            or base_manifest.get("region_overview_glbs") != 212
            or base_manifest.get("detailed_tiles") != 3396
            or base_manifest.get("region_overview_cell_box_count") != 0
            or base_manifest.get("render_palette", {}).get("base_color_factor") != OLD_FACTOR):
        raise ValueError("Expected complete immutable city v11 release")
    base_hash = sha256(base_manifest_path)
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if (saved.get("version") != "12" or saved.get("base_v11_manifest_sha256") != base_hash
                or saved.get("render_palette", {}).get("base_color_factor") != NEW_FACTOR):
            raise ValueError("Existing immutable v12 is a different revision")
        return {"status": "no_op", **saved}
    overview_paths = sorted(base.glob("massing_region_*.glb"))
    detail_paths = sorted(base.glob("cell_*.glb"))
    external_paths = sorted(base.glob("region_*.tileset.json"))
    if (len(overview_paths), len(detail_paths), len(external_paths)) != (212, 3396, 212):
        raise ValueError("City v11 tile tree is incomplete")
    stage = output.with_name(f".{output.name}.pending")
    stage.mkdir(mode=0o755, exist_ok=True)
    for position, (path, overview) in enumerate(([(p, True) for p in overview_paths]
                                                 + [(p, False) for p in detail_paths]), 1):
        recolor_glb(path, stage / path.name, overview=overview)
        if position % 400 == 0:
            print(f"recolored city GLBs {position}/{len(overview_paths) + len(detail_paths)}", flush=True)
    for path in (*external_paths, base / "source_tile_index.jsonl", base / "part_tile_index.jsonl",
                 base / "region_overview_metrics.json"):
        target = stage / path.name
        if target.exists():
            if not os.path.samefile(path, target):
                raise ValueError(f"Staged non-material artifact differs: {path.name}")
        else:
            os.link(path, target)
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    tileset["asset"]["tilesetVersion"] = "citywide-individual-footprint-massing-v12"
    tileset["extras"].update({"region_overview_revision": "citywide-v12",
                              "detail_revision": "citywide-v12_material_only",
                              "render_palette": PALETTE_NAME,
                              "massing_base_color_factor": NEW_FACTOR,
                              "palette_revision_only": True})
    save_json(stage / "tileset.json", tileset)
    manifest = dict(base_manifest)
    manifest.update({"version": "12", "base_v11_manifest_sha256": base_hash,
                     "render_palette": {"name": PALETTE_NAME, "base_color_factor": NEW_FACTOR},
                     "region_overview_roof_base_color_factor": NEW_FACTOR,
                     "region_overview_wall_base_color_factor": NEW_FACTOR,
                     "detail_material_base_color_factor": NEW_FACTOR,
                     "palette_revision_only": True, "bin_identical_to_v11_glbs": True,
                     "recolored_city_glbs": 3608, "reused_v11_external_region_json": 212,
                     "tileset_url": f"/assets/{output.name}/tileset.json"})
    save_json(stage / "manifest.json", manifest)
    stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    arguments = parser.parse_args()
    result = build(arguments.base, arguments.output)
    print(json.dumps({key: result[key] for key in ("status", "version", "tileset_url",
                                                     "recolored_city_glbs", "base_v11_manifest_sha256")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
