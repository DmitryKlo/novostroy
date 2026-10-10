"""Offline audit of the active city v6 silhouette -> volume -> detail tree.

Run `rtk proxy .runtime/dev-env/bin/python integration/audit_progressive_city.py`.
It checks the small new silhouette GLBs and metadata, plus references to the
previously audited v5 volume/detail files. It does not measure browser speed.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    without_private_gltf_keys,
)


PRIVATE = ROOT / "assets/citywide-v6"
PUBLIC = ROOT / "assets/public-citywide-v6"
PREVIOUS_PUBLIC = ROOT / "assets/public-citywide-v5"


def region_contains(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    outer = parent["boundingVolume"]["region"]
    inner = child["boundingVolume"]["region"]
    tolerance = 1e-9
    return all((outer[index] - tolerance <= inner[index] if index in (0, 1, 4)
                else outer[index] + tolerance >= inner[index]) for index in range(6))


def indexed_alpha_png(
    png: bytes, *, palette_rgb: tuple[int, int, int] = (92, 107, 117),
    max_dimension_px: int = 512,
) -> tuple[int, int]:
    """Verify that an embedded 1-bit image contains both mask values."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    cursor = 8
    chunks: dict[bytes, list[bytes]] = {}
    while cursor < len(png):
        length, kind = struct.unpack_from(">I4s", png, cursor)
        start = cursor + 8
        end = start + length
        assert end + 4 <= len(png)
        payload = png[start:end]
        crc = struct.unpack_from(">I", png, end)[0]
        assert zlib.crc32(kind + payload) == crc
        chunks.setdefault(kind, []).append(payload)
        cursor = end + 4
    assert cursor == len(png)
    assert len(chunks.get(b"IHDR", [])) == len(chunks.get(b"PLTE", [])) == \
        len(chunks.get(b"tRNS", [])) == len(chunks.get(b"IEND", [])) == 1
    width, height, bit_depth, color_type, compression, filtering, interlace = \
        struct.unpack(">IIBBBBB", chunks[b"IHDR"][0])
    assert 1 <= width <= max_dimension_px and 1 <= height <= max_dimension_px
    assert (bit_depth, color_type, compression, filtering, interlace) == (1, 3, 0, 0, 0)
    assert chunks[b"PLTE"][0] == bytes((0, 0, 0, *palette_rgb))
    assert chunks[b"tRNS"][0] == bytes((0, 255))
    assert chunks[b"IEND"][0] == b""

    raw = zlib.decompress(b"".join(chunks.get(b"IDAT", [])))
    stride = (width + 7) // 8
    assert len(raw) == height * (stride + 1)
    prior = bytearray(stride)
    opaque = transparent = False
    for index in range(height):
        offset = index * (stride + 1)
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        assert filter_type in range(5)
        if filter_type:
            for column in range(stride):
                left = row[column - 1] if column else 0
                above = prior[column]
                upper_left = prior[column - 1] if column else 0
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = above
                elif filter_type == 3:
                    predictor = (left + above) // 2
                else:
                    estimate = left + above - upper_left
                    distances = (abs(estimate - left), abs(estimate - above),
                                 abs(estimate - upper_left))
                    predictor = (left, above, upper_left)[distances.index(min(distances))]
                row[column] = (row[column] + predictor) & 255
        full_bytes, tail_bits = divmod(width, 8)
        opaque |= any(value != 0 for value in row[:full_bytes])
        transparent |= any(value != 255 for value in row[:full_bytes])
        if tail_bits:
            mask = (255 << (8 - tail_bits)) & 255
            opaque |= bool(row[full_bytes] & mask)
            transparent |= (row[full_bytes] & mask) != mask
        prior = row
    assert opaque and transparent
    return width, height


def embedded_png(glb: dict[str, Any], binary: bytes) -> tuple[int, int, int]:
    assert glb.get("images") and len(glb["images"]) == 1
    image = glb["images"][0]
    assert image.get("mimeType") == "image/png" and "uri" not in image
    view = glb["bufferViews"][image["bufferView"]]
    start = view.get("byteOffset", 0)
    png = binary[start:start + view["byteLength"]]
    width, height = indexed_alpha_png(png)
    materials = glb.get("materials", [])
    assert len(materials) == 1 and materials[0].get("alphaMode") == "MASK"
    assert materials[0].get("doubleSided") is True
    assert "KHR_materials_unlit" in materials[0].get("extensions", {})
    return width, height, len(png)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit() -> None:
    active = json.loads((ROOT / "assets/manifest.json").read_text(encoding="utf-8"))
    mapping = json.loads((ROOT / "assets/public_release_audit.json").read_text(encoding="utf-8"))
    assert active["city_tileset_url"] == "/assets/public-citywide-v6/tileset.json"
    assert set(active["previous_public_roots"]) == {"public-citywide-v4", "public-citywide-v5"}
    assert {item["public_root"] for item in mapping["mappings"]} == {
        "public-citywide-v6", "public-roads-v1", "public-parks-v1", "public-water-v1"
    }
    assert {item["public_root"] for item in mapping["compatibility_mappings"]} == \
        {"public-citywide-v4", "public-citywide-v5"}
    for previous in active["previous_public_roots"]:
        previous_root = ROOT / "assets" / previous
        manifest = json.loads((previous_root / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["public_redacted_source_ids"] is True
        assert (previous_root / "tileset.json").is_file()

    private_manifest = json.loads((PRIVATE / "manifest.json").read_text(encoding="utf-8"))
    public_manifest = json.loads((PUBLIC / "manifest.json").read_text(encoding="utf-8"))
    private_tree = json.loads((PRIVATE / "tileset.json").read_text(encoding="utf-8"))
    public_tree = json.loads((PUBLIC / "tileset.json").read_text(encoding="utf-8"))
    assert public_tree == without_private_gltf_keys(private_tree)
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == file_hash(PRIVATE / "manifest.json")
    assert_no_private_gltf_keys(public_tree, "public city v6 tileset")

    root = public_tree["root"]
    assert root["refine"] == "REPLACE" and root["geometricError"] == 100000
    assert not root.get("content") and len(root["children"]) == 212
    silhouettes: set[str] = set()
    volumes: set[str] = set()
    details: set[str] = set()
    raw_bytes = 0
    triangles = 0
    gpu_rgba_bytes = 0
    for silhouette in root["children"]:
        assert silhouette["refine"] == "REPLACE" and silhouette["geometricError"] == 650
        assert region_contains(root, silhouette)
        silhouette_uri = silhouette["content"]["uri"]
        assert silhouette_uri.startswith("silhouette_region_") and silhouette_uri.endswith(".glb")
        assert silhouette_uri not in silhouettes
        silhouettes.add(silhouette_uri)
        path = PUBLIC / silhouette_uri
        assert path.is_file()
        raw_bytes += path.stat().st_size
        glb, binary = glb_chunks(path)
        assert_no_private_gltf_keys(glb, silhouette_uri)
        width, height, _ = embedded_png(glb, binary)
        gpu_rgba_bytes += width * height * 4
        assert len(glb["meshes"]) == 1
        for primitive in glb["meshes"][0]["primitives"]:
            count = glb["accessors"][primitive["indices"]]["count"]
            assert count % 3 == 0
            triangles += count // 3

        assert len(silhouette["children"]) == 1
        volume = silhouette["children"][0]
        assert volume["refine"] == "REPLACE" and volume["geometricError"] == 500
        assert region_contains(silhouette, volume)
        volume_uri = volume["content"]["uri"]
        assert volume_uri.startswith("region_") and volume_uri.endswith(".glb")
        assert volume_uri not in volumes
        volumes.add(volume_uri)
        for detail in volume["children"]:
            assert detail["refine"] == "REPLACE" and detail["geometricError"] == 0
            assert region_contains(volume, detail)
            detail_uri = detail["content"]["uri"]
            assert detail_uri.startswith("cell_") and detail_uri.endswith(".glb")
            assert detail_uri not in details
            details.add(detail_uri)

    assert (len(silhouettes), len(volumes), len(details)) == (212, 212, 3396)
    assert raw_bytes == public_manifest["silhouette_bytes"]
    assert triangles == public_manifest["silhouette_triangles"] == 6_784
    assert gpu_rgba_bytes == public_manifest["silhouette_texture_gpu_rgba_bytes"] == 129_796_096
    assert public_manifest["city_buildings"] == private_manifest["city_buildings"] == 564_031
    for uri in volumes | details:
        assert (PUBLIC / uri).is_file() and (PREVIOUS_PUBLIC / uri).is_file(), uri
        assert ((PUBLIC / uri).samefile(PREVIOUS_PUBLIC / uri) or
                file_hash(PUBLIC / uri) == file_hash(PREVIOUS_PUBLIC / uri)), uri
    print(f"PASS active city v6: {len(silhouettes)} indexed alpha-mask silhouettes, "
          f"{len(volumes)} reused volumes, {len(details)} reused details, "
          f"{raw_bytes} silhouette bytes, {triangles} triangles, public metadata redacted")


if __name__ == "__main__":
    audit()
