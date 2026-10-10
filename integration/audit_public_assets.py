"""Audit the active public 3D Tiles release against its private build roots.

Run after a staged public release has been switched on:
``rtk .runtime/dev-env/bin/python integration/audit_public_assets.py``.
The private mapping is read locally, never served by the asset route. Every
public GLB is compared to its audited internal counterpart; HTTP checks cover
the active pointer, old raw roots, and sample content delivery.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
MAPPING_FILE = ASSETS / "public_release_audit.json"
PRIVATE_GLTF_KEYS = {"source_ids", "source_id", "source_record_id", "record_id",
                     "osm_id", "osm_ids", "provider_id", "feature_id_mapping"}
KNOWN_RAW_ROOTS = {"citywide", "citywide-v2", "citywide-v3", "roads-v1",
                   "parks-v1", "water-v1"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def glb_chunks(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    assert len(payload) >= 20, f"short GLB: {path}"
    magic, version, total_length = struct.unpack_from("<4sII", payload, 0)
    assert (magic, version, total_length) == (b"glTF", 2, len(payload)), path
    cursor = 12
    json_chunk: dict[str, Any] | None = None
    bin_chunk: bytes | None = None
    while cursor < len(payload):
        length, kind = struct.unpack_from("<I4s", payload, cursor)
        cursor += 8
        assert cursor + length <= len(payload), path
        chunk = payload[cursor:cursor + length]
        cursor += length
        if kind == b"JSON":
            assert json_chunk is None, path
            json_chunk = json.loads(chunk.decode("utf-8").rstrip(" \x00"))
        elif kind == b"BIN\x00":
            assert bin_chunk is None, path
            bin_chunk = chunk
        else:
            raise AssertionError(f"unexpected GLB chunk {kind!r}: {path}")
    assert cursor == len(payload) and json_chunk is not None and bin_chunk is not None, path
    return json_chunk, bin_chunk


def without_private_gltf_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_private_gltf_keys(item) for key, item in value.items()
                if key not in PRIVATE_GLTF_KEYS}
    if isinstance(value, list):
        return [without_private_gltf_keys(item) for item in value]
    return value


def assert_no_private_gltf_keys(value: Any, context: str) -> None:
    if isinstance(value, dict):
        leaked = PRIVATE_GLTF_KEYS.intersection(value)
        assert not leaked, f"private GLB metadata {sorted(leaked)} in {context}"
        for item in value.values():
            assert_no_private_gltf_keys(item, context)
    elif isinstance(value, list):
        for item in value:
            assert_no_private_gltf_keys(item, context)


def string_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if len(value) >= 6 else set()
    if isinstance(value, dict):
        result: set[str] = set()
        for item in value.values():
            result.update(string_values(item))
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(string_values(item))
        return result
    return set()


def private_string_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set()
        for key, item in value.items():
            result.update(string_values(item) if key in PRIVATE_GLTF_KEYS
                          else private_string_values(item))
        return result
    if isinstance(value, list):
        result = set()
        for item in value:
            result.update(private_string_values(item))
        return result
    return set()


def http_status(base: str, route: str, *, method: str = "GET") -> int:
    try:
        with urlopen(Request(f"{base}{route}", method=method), timeout=30) as response:
            return response.status
    except HTTPError as error:
        return error.code


def get_json(base: str, route: str) -> dict[str, Any]:
    with urlopen(f"{base}{route}", timeout=30) as response:
        assert response.status == 200, (route, response.status)
        return json.load(response)


def referenced_assets(tileset: dict[str, Any], public_dir: Path) -> tuple[set[Path], set[Path]]:
    """Resolve GLBs through external tileset JSONs as used by city v7."""
    references: set[Path] = set()
    external_json: set[Path] = set()
    pending = [(tileset["root"], Path("."))]
    while pending:
        tile, parent_dir = pending.pop()
        contents = tile.get("contents") or ([tile["content"]] if tile.get("content") else [])
        for content in contents:
            uri = content["uri"]
            assert isinstance(uri, str) and "?" not in uri and "#" not in uri, uri
            relative = parent_dir / uri
            assert not relative.is_absolute() and ".." not in relative.parts, uri
            if relative.suffix == ".glb":
                references.add(relative)
            elif relative.suffix == ".json":
                assert relative not in external_json, relative
                external_json.add(relative)
                nested = json.loads((public_dir / relative).read_text(encoding="utf-8"))
                assert nested["asset"]["version"] == "1.1"
                pending.append((nested["root"], relative.parent))
            else:
                raise AssertionError(f"unexpected tile content: {uri}")
        pending.extend((child, parent_dir) for child in tile.get("children", []))
    return references, external_json


def audit(base: str) -> None:
    mapping = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    assert mapping["version"] == "1"
    current_entries = mapping["mappings"]
    compatibility_entries = mapping.get("compatibility_mappings", [])
    assert {entry["kind"] for entry in current_entries} == {
        "city_volumes", "roads", "parks", "water"
    }
    current_roots = {entry["public_root"] for entry in current_entries}
    compatibility_roots = {entry["public_root"] for entry in compatibility_entries}
    assert not current_roots.intersection(compatibility_roots)
    entries = current_entries + compatibility_entries
    public_roots = current_roots | compatibility_roots
    private_roots = {entry["internal_root"] for entry in entries}
    assert len(public_roots) == len(entries)
    map_payload = get_json(base, "/v1/map")
    active = {item["url"]: item for item in map_payload["tilesets"]
              if item["kind"] != "facade"}
    mapped_urls = {f"/assets/{entry['public_root']}/tileset.json"
                   for entry in current_entries}
    compatibility_urls = {f"/assets/{entry['public_root']}/tileset.json"
                          for entry in compatibility_entries}
    assert set(mapping["active_public_urls"]) == mapped_urls
    assert set(mapping.get("compatibility_public_urls", [])) == compatibility_urls
    assert set(active) == mapped_urls, (set(active), mapped_urls)
    asset_manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    assert set(asset_manifest.get("previous_public_roots", [])) == compatibility_roots

    assert http_status(base, "/assets/manifest.json") == 404
    assert http_status(base, "/assets/public_release_audit.json") == 404
    prior_public_roots = {path.name for path in ASSETS.iterdir()
                          if path.is_dir() and path.name.startswith("public-")}
    for old_root in sorted((KNOWN_RAW_ROOTS | prior_public_roots | private_roots)
                           - public_roots):
        assert http_status(base, f"/assets/{old_root}/tileset.json") == 404, old_root
        old_dir = ASSETS / old_root
        if old_dir.is_dir():
            old_glb = next(old_dir.rglob("*.glb"), None)
            if old_glb is not None:
                old_route = f"/assets/{old_root}/{old_glb.relative_to(old_dir).as_posix()}"
                assert http_status(base, old_route, method="HEAD") == 404, old_route

    total_glbs = 0
    total_bin_bytes = 0
    for entry in entries:
        kind = entry["kind"]
        public_root = entry["public_root"]
        internal_root = entry["internal_root"]
        public_dir = ASSETS / public_root
        internal_dir = ASSETS / internal_root
        assert public_dir.is_dir() and internal_dir.is_dir(), (public_root, internal_root)
        assert sha256(internal_dir / "manifest.json") == entry["internal_manifest_sha256"]
        assert sha256(public_dir / "manifest.json") == entry["public_manifest_sha256"]
        public_manifest = json.loads((public_dir / "manifest.json").read_text(encoding="utf-8"))
        assert public_manifest.get("public_redacted_source_ids") is True, public_root
        route = f"/assets/{public_root}/tileset.json"
        public_tileset = get_json(base, route)
        internal_tileset = json.loads((internal_dir / "tileset.json").read_text(encoding="utf-8"))
        assert public_tileset == json.loads((public_dir / "tileset.json").read_text(encoding="utf-8"))
        assert public_tileset == without_private_gltf_keys(internal_tileset), kind
        credit = public_tileset.get("extras", {}).get("attribution")
        assert isinstance(credit, str) and credit.strip(), kind
        assert credit == internal_tileset.get("extras", {}).get("attribution"), kind
        if route in active:
            assert credit == active[route]["attribution"], kind
        assert_no_private_gltf_keys(public_tileset, route)
        assert http_status(base, f"/assets/{public_root}/manifest.json") == 404
        assert http_status(base, f"/assets/{public_root}/source_tile_index.jsonl") == 404

        glbs = sorted(public_dir.rglob("*.glb"))
        assert len(glbs) == entry["glb_count"] and glbs, (kind, len(glbs))
        glb_references, external_references = referenced_assets(public_tileset, public_dir)
        assert {path.relative_to(public_dir) for path in glbs} == glb_references, kind
        for relative in external_references:
            route = f"/assets/{public_root}/{relative.as_posix()}"
            external = get_json(base, route)
            assert external == json.loads((public_dir / relative).read_text(encoding="utf-8"))
            private_external = json.loads((internal_dir / relative).read_text(encoding="utf-8"))
            assert external == without_private_gltf_keys(private_external), route
            assert_no_private_gltf_keys(external, route)
        for public_glb in glbs:
            relative = public_glb.relative_to(public_dir)
            internal_glb = internal_dir / relative
            assert internal_glb.is_file(), internal_glb
            public_json, public_bin = glb_chunks(public_glb)
            internal_json, internal_bin = glb_chunks(internal_glb)
            assert_no_private_gltf_keys(public_json, str(public_glb))
            assert public_json == without_private_gltf_keys(internal_json), public_glb
            assert not private_string_values(internal_json).intersection(
                string_values(public_json)
            ), public_glb
            assert public_bin == internal_bin, public_glb
            total_bin_bytes += len(public_bin)
        for sample in (glbs[0], glbs[-1]):
            sample_route = f"/assets/{public_root}/{sample.relative_to(public_dir).as_posix()}"
            assert http_status(base, sample_route, method="HEAD") == 200, sample_route
        total_glbs += len(glbs)
        print(f"PASS {kind}: {len(glbs)} sanitized GLBs, BIN unchanged, credit retained")
    print(f"PASS public assets: {total_glbs} GLBs, {total_bin_bytes} identical BIN bytes, "
          "only active/compatibility roots public, raw URLs 404")


if __name__ == "__main__":
    audit((sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/"))
