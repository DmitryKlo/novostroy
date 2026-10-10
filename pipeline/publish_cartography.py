"""Publish a complete, independently validated set of local cartographic tiles."""

from __future__ import annotations

import json
from pathlib import Path

from backend.store import ASSET_ROOT
from pipeline.build_tiles import save_json


LAYERS = ("roads", "parks", "water")


def publish() -> dict:
    pointers = {}
    for layer in LAYERS:
        folder = ASSET_ROOT / f"{layer}-v1"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        tileset = json.loads((folder / "tileset.json").read_text(encoding="utf-8"))
        if manifest.get("partial") or not manifest.get("attribution"):
            raise ValueError(f"Incomplete or unattributed {layer} tileset")
        url = manifest.get("tileset_url")
        if url != f"/assets/{folder.name}/tileset.json":
            raise ValueError(f"Unexpected {layer} tileset URL")
        children = tileset["root"]["children"]
        details = [child for parent in children for child in parent["children"]]
        if len(children) != manifest["coarse_region_tiles"] or len(details) != manifest["detailed_tiles"]:
            raise ValueError(f"Incomplete {layer} tile graph")
        if any(not (folder / tile["content"]["uri"]).is_file()
               for tile in (*children, *details) if "content" in tile):
            raise ValueError(f"Missing {layer} tile content")
        pointers[f"{layer}_tileset_url"] = url
    path = ASSET_ROOT / "manifest.json"
    current = json.loads(path.read_text(encoding="utf-8"))
    current.update(pointers)
    save_json(path, current)
    return pointers


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False, indent=2))
