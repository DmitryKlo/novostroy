"""Audit the staged v6 city graph and provider-hidden public GLBs offline."""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from backend.store import ASSET_ROOT


def _document(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20:
            raise ValueError(f"Truncated GLB: {path}")
        magic, version, total, length, kind = struct.unpack("<4sIII4s", header)
        if magic != b"glTF" or version != 2 or kind != b"JSON" or total != path.stat().st_size:
            raise ValueError(f"Invalid GLB header: {path}")
        return json.loads(stream.read(length))


def _check_bounds(parent: dict[str, Any], child: dict[str, Any]) -> None:
    first = parent["boundingVolume"]["region"]
    second = child["boundingVolume"]["region"]
    if any(second[i] < first[i] - 1e-9 for i in (0, 1, 4)) or any(
            second[i] > first[i] + 1e-9 for i in (2, 3, 5)):
        raise ValueError("Tile child exceeds parent geodetic bounds")


def _check_alpha_png(path: Path, document: dict[str, Any]) -> tuple[int, int]:
    with path.open("rb") as stream:
        stream.seek(12)
        json_length = struct.unpack("<I", stream.read(4))[0]
        stream.seek(20 + json_length)
        binary_length, kind = struct.unpack("<I4s", stream.read(8))
        if kind != b"BIN\x00":
            raise ValueError(f"Missing GLB BIN: {path}")
        view = document["bufferViews"][document["images"][0]["bufferView"]]
        if view["byteOffset"] + view["byteLength"] > binary_length:
            raise ValueError(f"PNG view outside GLB BIN: {path}")
        stream.seek(28 + json_length + view["byteOffset"])
        png = stream.read(view["byteLength"])
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Missing embedded PNG: {path}")
    width, height, bit_depth, color_type = struct.unpack_from(">IIBB", png, 16)
    if bit_depth != 1 or color_type != 3 or not (0 < width <= 512 and 0 < height <= 512):
        raise ValueError(f"Wrong silhouette PNG format: {path}")
    offset = 8
    data = bytearray()
    palette = None
    transparency = None
    while offset < len(png):
        size = struct.unpack_from(">I", png, offset)[0]
        kind = png[offset + 4:offset + 8]
        payload = png[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", png, offset + 8 + size)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ValueError(f"PNG CRC mismatch: {path}")
        if kind == b"IDAT":
            data.extend(payload)
        if kind == b"PLTE":
            palette = payload
        if kind == b"tRNS":
            transparency = payload
        offset += size + 12
    if palette != bytes((0, 0, 0, 92, 107, 117)) or transparency != bytes((0, 255)):
        raise ValueError(f"Silhouette PNG palette/alpha changed: {path}")
    raw = zlib.decompress(data)
    row_bytes = 1 + math.ceil(width / 8)
    if len(raw) != height * row_bytes:
        raise ValueError(f"PNG dimensions mismatch: {path}")
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, row_bytes)
    if np.any(rows[:, 0] != 0):
        raise ValueError(f"Unsupported PNG row filter: {path}")
    occupancy = np.unpackbits(rows[:, 1:], axis=1, bitorder="big")[:, :width]
    if not np.any(occupancy == 0) or not np.any(occupancy == 1):
        raise ValueError(f"PNG must contain transparent and opaque pixels: {path}")
    return width, height


def audit(private: Path = ASSET_ROOT / "citywide-v6",
          public: Path = ASSET_ROOT / "public-citywide-v6",
          base_private: Path = ASSET_ROOT / "citywide-v5",
          base_public: Path = ASSET_ROOT / "public-citywide-v5") -> dict[str, Any]:
    manifest = json.loads((private / "manifest.json").read_text(encoding="utf-8"))
    public_manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["version"] != "6" or manifest["city_buildings"] != 564031
            or manifest["rendered_part_parent_count"] != 166 or manifest["rendered_part_count"] != 800
            or not public_manifest["public_redacted_source_ids"]
            or public_manifest["city_buildings"] != manifest["city_buildings"]
            or not public_manifest["attribution"]):
        raise ValueError("City manifest provenance or preservation check failed")
    for name in ("source_tile_index.jsonl", "part_tile_index.jsonl"):
        if not os.path.samefile(private / name, base_private / name) or (public / name).exists():
            raise ValueError(f"Private index changed or leaked: {name}")
    tileset = json.loads((public / "tileset.json").read_text(encoding="utf-8"))
    if tileset["asset"]["tilesetVersion"] != "citywide-progressive-silhouettes-v6":
        raise ValueError("Wrong public city tileset revision")
    root = tileset["root"]
    if root["refine"] != "REPLACE" or "content" in root or len(root["children"]) != 212:
        raise ValueError("Wrong city root")
    references = set()
    detail_count = 0
    silhouette_bytes = 0
    for silhouette in root["children"]:
        if (silhouette["refine"] != "REPLACE" or len(silhouette["children"]) != 1
                or silhouette["geometricError"] >= root["geometricError"]):
            raise ValueError("Wrong silhouette refinement")
        _check_bounds(root, silhouette)
        volume = silhouette["children"][0]
        if (volume["refine"] != "REPLACE" or volume["geometricError"] >= silhouette["geometricError"]):
            raise ValueError("Wrong volume refinement")
        _check_bounds(silhouette, volume)
        name = silhouette["content"]["uri"]
        if not name.startswith("silhouette_region_") or Path(name).name != name:
            raise ValueError("Wrong silhouette URI")
        path = public / name
        document = _document(path)
        if (document["asset"]["extras"]["geometry_type"]
                != "rasterized_flat_source_footprint_silhouettes"
                or document["materials"][0]["alphaMode"] != "MASK"
                or document["accessors"][2]["count"] != 96):
            raise ValueError("Wrong flat silhouette geometry")
        _check_alpha_png(path, document)
        silhouette_bytes += path.stat().st_size
        references.add(name)
        volume_name = volume["content"]["uri"]
        if not os.path.samefile(private / volume_name, base_private / volume_name) or not os.path.samefile(
                public / volume_name, base_public / volume_name):
            raise ValueError("v5 volume GLB was changed")
        references.add(volume_name)
        for detail in volume["children"]:
            _check_bounds(volume, detail)
            if detail["refine"] != "REPLACE" or detail["geometricError"] != 0:
                raise ValueError("Wrong detail refinement")
            detail_name = detail["content"]["uri"]
            if not os.path.samefile(private / detail_name, base_private / detail_name) or not os.path.samefile(
                    public / detail_name, base_public / detail_name):
                raise ValueError("v5 detail GLB was changed")
            references.add(detail_name)
            detail_count += 1
    if detail_count != 3396 or len(references) != 3820 or silhouette_bytes != manifest["silhouette_bytes"]:
        raise ValueError("City graph counts or silhouette bytes changed")
    if references != {path.name for path in public.glob("*.glb")}:
        raise ValueError("Public GLB reference mismatch")
    for name in references:
        document = _document(public / name)
        if "source_ids" in json.dumps(document) or "feature_id_mapping" in json.dumps(document):
            raise ValueError(f"Source crosswalk leaked into public GLB: {name}")
    return {"status": "passed", "silhouette_tiles": 212, "volume_tiles": 212,
            "detail_tiles": detail_count, "public_glbs": len(references),
            "silhouette_bytes": silhouette_bytes, "source_ids_redacted": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=ASSET_ROOT / "citywide-v6")
    parser.add_argument("--public", type=Path, default=ASSET_ROOT / "public-citywide-v6")
    args = parser.parse_args()
    print(json.dumps(audit(args.private, args.public), indent=2))


if __name__ == "__main__":
    main()
