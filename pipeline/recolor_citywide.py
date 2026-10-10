"""Version the citywide material without rebuilding or changing source geometry.

The completed v1 tile set stays available. A staged v2 folder is published only
after every GLB and index is present; --publish separately switches the API
asset pointer. No source download or footprint processing occurs here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_tiles import save_json


SOURCE = ASSET_ROOT / "citywide"
DESTINATION = ASSET_ROOT / "citywide-v2"
OLD_COLOR = [0.64, 0.69, 0.72, 1]
NEW_COLOR = [0.36, 0.42, 0.46, 1]
OLD_BYTES = b"[0.64,0.69,0.72,1]"
NEW_BYTES = b"[0.36,0.42,0.46,1]"
PALETTE_NAME = "dark_city_massing_v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_chunk(path: Path) -> tuple[int, bytes]:
    with path.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20:
            raise ValueError("GLB header truncated")
        magic, version, total, length, kind = struct.unpack("<4sIII4s", header)
        if magic != b"glTF" or version != 2 or total != path.stat().st_size or kind != b"JSON":
            raise ValueError("Invalid GLB header")
        payload = stream.read(length)
    if len(payload) != length:
        raise ValueError("GLB JSON chunk truncated")
    return length, payload


def _has_color(path: Path, color: list[float]) -> bool:
    _, payload = _json_chunk(path)
    document = json.loads(payload)
    materials = document.get("materials") or []
    return len(materials) > 4 and materials[4].get("name") == "city_massing" \
        and materials[4].get("pbrMetallicRoughness", {}).get("baseColorFactor") == color


def recolor_glb(source: Path, destination: Path) -> None:
    length, payload = _json_chunk(source)
    if payload.count(OLD_BYTES) != 1 or not _has_color(source, OLD_COLOR):
        raise ValueError(f"Unexpected city material in {source.name}")
    if len(OLD_BYTES) != len(NEW_BYTES):
        raise RuntimeError("Material edit must preserve GLB JSON chunk length")
    if destination.is_file() and destination.stat().st_size == source.stat().st_size \
            and _has_color(destination, NEW_COLOR):
        return
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    with temporary.open("r+b") as stream:
        stream.seek(20)
        stream.write(payload.replace(OLD_BYTES, NEW_BYTES))
        stream.flush()
        os.fsync(stream.fileno())
    if not _has_color(temporary, NEW_COLOR):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Recolored GLB did not validate")
    temporary.replace(destination)


def publish(destination: Path, manifest: dict[str, Any]) -> None:
    if not (destination / "tileset.json").is_file() or manifest.get("partial"):
        raise ValueError("Only a complete tile set can be published")
    pointer = ASSET_ROOT / "manifest.json"
    current = json.loads(pointer.read_text(encoding="utf-8"))
    if current.get("city_buildings") != manifest.get("city_buildings"):
        raise ValueError("City building count changed during palette update")
    current["city_tileset_url"] = manifest["tileset_url"]
    current["city_render_palette"] = PALETTE_NAME
    save_json(pointer, current)


def build(source: Path = SOURCE, destination: Path = DESTINATION, *, switch_pointer: bool = False) -> dict[str, Any]:
    original = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if original.get("partial") or original.get("model_quality") != "synthetic_massing":
        raise ValueError("Source must be a complete citywide massing tile set")
    expected = original["detailed_tiles"] + original["coarse_region_tiles"]
    glbs = sorted(source.glob("*.glb"))
    if len(glbs) != expected:
        raise ValueError("Source tile count does not match the manifest")
    target_manifest = {**original, "version": "2",
        "tileset_url": f"/assets/{destination.name}/tileset.json",
        "render_palette": {"name": PALETTE_NAME, "base_color_factor": NEW_COLOR},
        "material_recolor_only": True}
    if destination.exists():
        saved = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        if saved != target_manifest:
            raise ValueError("Existing destination has a different manifest")
        finished = sorted(destination.glob("*.glb"))
        if len(finished) != expected or not all(_has_color(tile, NEW_COLOR) for tile in finished):
            raise ValueError("Existing destination has incomplete or invalid tile materials")
        if _sha256(destination / "source_tile_index.jsonl") != _sha256(source / "source_tile_index.jsonl"):
            raise ValueError("Existing destination source index differs")
        if switch_pointer:
            publish(destination, saved)
        return {"status": "no_op", "tiles": expected, "tileset_url": saved["tileset_url"]}
    stage = destination.with_name(f".{destination.name}.pending")
    stage.mkdir(mode=0o755, exist_ok=True)
    for position, tile in enumerate(glbs, 1):
        recolor_glb(tile, stage / tile.name)
        if position % 500 == 0:
            print(f"recolored {position}/{expected} city tiles", flush=True)
    index = source / "source_tile_index.jsonl"
    target_index = stage / index.name
    if not target_index.is_file() or _sha256(target_index) != _sha256(index):
        shutil.copyfile(index, target_index)
    tileset = json.loads((source / "tileset.json").read_text(encoding="utf-8"))
    tileset["asset"]["tilesetVersion"] = "citywide-massing-dark-v2"
    tileset.setdefault("extras", {})["render_palette"] = PALETTE_NAME
    save_json(stage / "tileset.json", tileset)
    save_json(stage / "manifest.json", target_manifest)
    stage.replace(destination)
    if switch_pointer:
        publish(destination, target_manifest)
    return {"status": "built", "tiles": expected, "tileset_url": target_manifest["tileset_url"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=DESTINATION)
    parser.add_argument("--publish", action="store_true", help="Switch the API asset pointer after validation")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, switch_pointer=args.publish), ensure_ascii=False))


if __name__ == "__main__":
    main()
