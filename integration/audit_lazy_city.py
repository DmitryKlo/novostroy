"""Audit staged or active v7 city tiles, including lazy external JSON trees.

Run ``rtk proxy .runtime/dev-env/bin/python integration/audit_lazy_city.py``
before publication; add ``--require-active`` after switching the pointer.
This validates artifacts and links, not network latency or browser rendering.
"""

from __future__ import annotations

import json
import re
import sys
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_progressive_city import (  # noqa: E402
    embedded_png,
    file_hash,
    region_contains,
)
from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    without_private_gltf_keys,
)


ASSETS = ROOT / "assets"
PUBLIC = ASSETS / "public-citywide-v7"
PRIVATE = ASSETS / "citywide-v7"
V6 = ASSETS / "public-citywide-v6"
REGION_JSON = re.compile(r"region_-?\d+_-?\d+\.tileset\.json\Z")
REGION_GLB = re.compile(r"region_-?\d+_-?\d+\.glb\Z")
SILHOUETTE_GLB = re.compile(r"silhouette_region_-?\d+_-?\d+\.glb\Z")
DETAIL_GLB = re.compile(r"cell_-?\d+_-?\d+\.glb\Z")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def gzip5_size(path: Path) -> int:
    compressor = zlib.compressobj(level=5, wbits=31)
    data = path.read_bytes()
    return len(compressor.compress(data) + compressor.flush())


def assert_reused(uri: str, seen: set[str]) -> None:
    assert uri not in seen, uri
    seen.add(uri)
    current = PUBLIC / uri
    prior = V6 / uri
    assert current.is_file() and prior.is_file(), uri
    assert current.samefile(prior) or file_hash(current) == file_hash(prior), uri


def audit(*, require_active: bool = False) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    mapping = read_json(ASSETS / "public_release_audit.json")
    active_v7 = pointer["city_tileset_url"] == "/assets/public-citywide-v7/tileset.json"
    assert active_v7 or not require_active, "v7 release pointer is not active"
    if active_v7:
        assert set(pointer["previous_public_roots"]) == {
            "public-citywide-v4", "public-citywide-v5", "public-citywide-v6"
        }
        assert {entry["public_root"] for entry in mapping["compatibility_mappings"]} >= {
            "public-citywide-v4", "public-citywide-v5", "public-citywide-v6"
        }
        assert {entry["public_root"] for entry in mapping["mappings"]} >= {
            "public-citywide-v7"
        }
        for old_root in pointer["previous_public_roots"]:
            old = ASSETS / old_root
            assert (old / "tileset.json").is_file(), old_root
            assert read_json(old / "manifest.json")["public_redacted_source_ids"] is True
    else:
        assert pointer["city_tileset_url"] == "/assets/public-citywide-v6/tileset.json"

    public_manifest = read_json(PUBLIC / "manifest.json")
    private_manifest = read_json(PRIVATE / "manifest.json")
    assert public_manifest["version"] == "7"
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == file_hash(PRIVATE / "manifest.json")
    assert public_manifest["city_buildings"] == private_manifest["city_buildings"] == 564_031
    public_root = read_json(PUBLIC / "tileset.json")
    private_root = read_json(PRIVATE / "tileset.json")
    assert public_root == without_private_gltf_keys(private_root)
    assert_no_private_gltf_keys(public_root, "v7 root tileset")
    assert public_root["asset"]["version"] == "1.1"
    root = public_root["root"]
    assert root["refine"] == "REPLACE" and root["geometricError"] == 100_000
    assert not root.get("content") and len(root["children"]) == 212
    v6_root = read_json(V6 / "tileset.json")["root"]
    assert len(v6_root["children"]) == len(root["children"])

    regions: set[str] = set()
    silhouettes: set[str] = set()
    volumes: set[str] = set()
    details: set[str] = set()
    texture_rgba = 0
    silhouette_bytes = 0
    region_json_bytes = 0
    region_json_gzip5 = 0
    for silhouette, old_silhouette in zip(root["children"], v6_root["children"], strict=True):
        assert silhouette["refine"] == "REPLACE" and silhouette["geometricError"] == 650
        assert region_contains(root, silhouette)
        assert {key: value for key, value in silhouette.items() if key != "children"} == {
            key: value for key, value in old_silhouette.items() if key != "children"
        }
        uri = silhouette["content"]["uri"]
        assert SILHOUETTE_GLB.fullmatch(uri), uri
        assert_reused(uri, silhouettes)
        silhouette_bytes += (PUBLIC / uri).stat().st_size
        glb, binary = glb_chunks(PUBLIC / uri)
        assert_no_private_gltf_keys(glb, uri)
        width, height, _ = embedded_png(glb, binary)
        texture_rgba += width * height * 4

        assert len(silhouette["children"]) == 1
        stub = silhouette["children"][0]
        assert stub["refine"] == "REPLACE" and stub["geometricError"] == 500
        assert "children" not in stub and region_contains(silhouette, stub)
        region_uri = stub["content"]["uri"]
        assert REGION_JSON.fullmatch(region_uri), region_uri
        assert region_uri not in regions
        regions.add(region_uri)
        expected_volume_uri = region_uri.removesuffix(".tileset.json") + ".glb"
        assert REGION_GLB.fullmatch(expected_volume_uri)
        assert stub["boundingVolume"] == old_silhouette["children"][0]["boundingVolume"]

        public_path = PUBLIC / region_uri
        private_path = PRIVATE / region_uri
        assert public_path.is_file() and private_path.is_file(), region_uri
        region_json_bytes += public_path.stat().st_size
        region_json_gzip5 += gzip5_size(public_path)
        external = read_json(public_path)
        internal = read_json(private_path)
        assert external == without_private_gltf_keys(internal), region_uri
        assert_no_private_gltf_keys(external, region_uri)
        assert external["asset"]["version"] == "1.1"
        volume = external["root"]
        assert volume == old_silhouette["children"][0], region_uri
        assert volume["content"]["uri"] == expected_volume_uri
        assert volume["refine"] == "REPLACE" and volume["geometricError"] == 500
        assert region_contains(stub, volume)
        assert_reused(expected_volume_uri, volumes)
        for detail in volume["children"]:
            assert detail["refine"] == "REPLACE" and detail["geometricError"] == 0
            assert region_contains(volume, detail)
            detail_uri = detail["content"]["uri"]
            assert DETAIL_GLB.fullmatch(detail_uri), detail_uri
            assert_reused(detail_uri, details)

    assert (len(regions), len(silhouettes), len(volumes), len(details)) == (212, 212, 212, 3396)
    assert {p.name for p in PUBLIC.glob("region_*.tileset.json")} == regions
    assert {p.name for p in PUBLIC.glob("*.glb")} == silhouettes | volumes | details
    assert silhouette_bytes == public_manifest["silhouette_bytes"] == 1_467_596
    assert texture_rgba == public_manifest["silhouette_texture_gpu_rgba_bytes"] == 129_796_096
    assert (PUBLIC / "tileset.json").stat().st_size == public_manifest["root_tileset_raw_bytes"]
    assert gzip5_size(PUBLIC / "tileset.json") == public_manifest["root_tileset_gzip5_bytes"]
    assert region_json_bytes == public_manifest["external_tilesets_raw_bytes"]
    assert region_json_gzip5 == public_manifest["external_tilesets_gzip5_bytes"]
    print(f"PASS {'active' if active_v7 else 'staged'} city v7: 212 indexed silhouettes, "
          f"212 lazy region JSONs, 212 reused volumes, 3396 reused details; "
          f"root {public_manifest['root_tileset_raw_bytes']} raw / "
          f"{public_manifest['root_tileset_gzip5_bytes']} gzip5 bytes; public metadata redacted")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
