"""Release color-correct unlit ground vectors without rebuilding source geometry."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_progressive_city import sha256
from pipeline.build_tiles import save_bytes, save_json

BASE = ASSET_ROOT / "ground-v2"
OUTPUT = ASSET_ROOT / "ground-v3"
DISPLAY_SRGB = (170, 175, 178)
DISPLAY_HEX = "#AAAFB2"
OLD_FACTOR = [0.667, 0.686, 0.698, 1.0]
COLOR_SPACE_POLICY = "unlit_vectors_linear_factor_from_srgb_AAAFB2_overview_png_srgb"


def srgb_to_linear(value: int) -> float:
    channel = value / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def linear_to_srgb(value: float) -> int:
    channel = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return round(max(0.0, min(1.0, channel)) * 255)


LINEAR_FACTOR = [round(srgb_to_linear(value), 9) for value in DISPLAY_SRGB] + [1.0]
if tuple(linear_to_srgb(value) for value in LINEAR_FACTOR[:3]) != DISPLAY_SRGB:
    raise ValueError("Ground material sRGB round trip changed")


def _serialized(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _recolor_glb(payload: bytes) -> bytes:
    if len(payload) < 28 or struct.unpack_from("<4sII", payload) != (b"glTF", 2, len(payload)):
        raise ValueError("Invalid base ground GLB")
    json_length, kind = struct.unpack_from("<I4s", payload, 12)
    if kind != b"JSON":
        raise ValueError("Missing ground GLB JSON")
    binary_start = 20 + json_length
    binary_length, binary_kind = struct.unpack_from("<I4s", payload, binary_start)
    if binary_kind != b"BIN\x00" or binary_start + 8 + binary_length != len(payload):
        raise ValueError("Invalid ground GLB BIN")
    document = json.loads(payload[20:binary_start])
    materials = document.get("materials", [])
    if (len(materials) != 1 or materials[0].get("extensions", {}).get("KHR_materials_unlit") != {}
            or materials[0]["pbrMetallicRoughness"]["baseColorFactor"] != OLD_FACTOR):
        raise ValueError("Base vector material changed")
    materials[0]["pbrMetallicRoughness"]["baseColorFactor"] = LINEAR_FACTOR
    document["asset"]["extras"].update({"display_color_srgb": DISPLAY_HEX,
                                          "base_color_factor_color_space": "linear"})
    json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_part += b" " * (-len(json_part) % 4)
    binary_chunk = payload[binary_start:]
    revised = (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + len(binary_chunk))
               + struct.pack("<I4s", len(json_part), b"JSON") + json_part + binary_chunk)
    if revised[20 + len(json_part):] != binary_chunk:
        raise ValueError("Color revision altered geometry BIN")
    return revised


def build(base: Path = BASE, output: Path = OUTPUT) -> dict[str, Any]:
    original = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if original.get("version") != "2" or original.get("partial") or original.get("exact_holes") != 995:
        raise ValueError("Expected reviewed ground v2 base")
    base_hash = sha256(base / "manifest.json")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if (saved.get("base_v2_manifest_sha256") != base_hash
                or saved.get("color_space_policy") != COLOR_SPACE_POLICY):
            raise ValueError("Existing ground v3 does not match reviewed v2 base")
        return {"status": "no_op", **saved}
    with tempfile.TemporaryDirectory(prefix=".ground-v3.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"manifest.json", "tileset.json", "cell_metrics.json", "region_metrics.json"}:
                continue
            if path.suffix == ".glb" and path.name != "overview_city.glb":
                continue
            if not path.is_file():
                raise ValueError(f"Unexpected v2 asset: {path}")
            os.link(path, stage / path.name)
        exact_bytes = 0
        region_bytes = 0
        recolored = 0
        for kind, metric_name, key in (("cell", "cell_metrics.json", "exact_glb_bytes"),
                                       ("overview_region", "region_metrics.json", "region_glb_bytes")):
            metrics = json.loads((base / metric_name).read_text(encoding="utf-8"))
            total = 0
            for row in metrics:
                name = row["cell"] if kind == "cell" else row["region"]
                if not name.startswith(kind + "_") or not name.endswith(".glb"):
                    raise ValueError("Ground metrics no longer match vector filenames")
                before = (base / name).read_bytes()
                after = _recolor_glb(before)
                save_bytes(stage / name, after)
                row["bytes"] = len(after)
                total += len(after)
                recolored += 1
            save_json(stage / metric_name, metrics)
            if kind == "cell":
                exact_bytes = total
            else:
                region_bytes = total
        if recolored != 3608 or not (stage / "overview_city.glb").is_file():
            raise ValueError("Ground color revision lost geometry tiles")
        root = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
        root["asset"]["tilesetVersion"] = "mapped-ground-footprints-v3"
        root["extras"].update({"display_color_srgb": DISPLAY_HEX,
                               "vector_base_color_factor_color_space": "linear",
                               "overview_png_color_space": "sRGB"})
        root_blob = _serialized(root)
        save_json(stage / "tileset.json", root)
        revised = {**original, "version": "3", "tileset_url": f"/assets/{output.name}/tileset.json",
                   "base_v2_manifest_sha256": base_hash, "color_space_policy": COLOR_SPACE_POLICY,
                   "display_color_srgb": DISPLAY_HEX, "vector_linear_base_color_factor": LINEAR_FACTOR,
                   "recolored_vector_glbs": recolored, "bin_identical_to_v2_glbs": recolored,
                   "common_raster_reused_from_v2": True,
                   "exact_glb_bytes": exact_bytes, "region_glb_bytes": region_bytes,
                   "root_tileset_raw_bytes": len(root_blob),
                   "root_tileset_gzip5_bytes": len(gzip.compress(root_blob, compresslevel=5))}
        save_json(stage / "manifest.json", revised)
        stage.replace(output)
    return {"status": "built", **revised}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.base, arguments.output), ensure_ascii=False, indent=2))
