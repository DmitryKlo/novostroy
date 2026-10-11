"""Assemble a reviewed, all-LOD city tile replacement for developer overlays.

The detail and overview GLB edits are made by separate evidence-producing jobs.
This module only verifies their frozen outputs and builds an immutable tileset
which uses the replacements at *both* levels of detail. It never edits the
existing public city tiles or infers a building identity from proximity.
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any

from sources.property.developer_map import ROOT, _canonical, _file_sha, _sha


CITY_ROOT = ROOT / "assets/public-citywide-v12"
CITY_MANIFEST = CITY_ROOT / "manifest.json"
CITY_TILESET = CITY_ROOT / "tileset.json"


def _json_bytes(value: Any) -> bytes:
    return _canonical(value) + b"\n"


def _checked_bytes(path: Path, expected_sha: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or linked city patch input: {path}")
    data = path.read_bytes()
    if _sha(data) != expected_sha:
        raise ValueError(f"City patch input SHA mismatch: {path}")
    return data


def _proof(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing city patch proof manifest")
    data = path.read_bytes()
    proof = json.loads(data)
    if not isinstance(proof, dict):
        raise ValueError("Invalid city patch proof manifest")
    return proof, _sha(data)


def _replace_content_uri(tile: dict[str, Any], uri: str) -> None:
    if not isinstance(tile.get("content"), dict) or not isinstance(tile["content"].get("uri"), str):
        raise ValueError("City tile content URI missing")
    tile["content"]["uri"] = uri


def assemble(
    detail_manifest_path: Path,
    overview_manifest_path: Path,
    expected_registry_ids: set[str],
    source_geometry: dict[str, dict[str, str]],
    *,
    city_root: Path = CITY_ROOT,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return a pinned city-patch descriptor and run-relative file bytes.

    Every targeted registry ID must occur in detail proof and in an impacted
    overview region. Unchanged base refs remain absolute, while patched refs
    are relative to their containing JSON document.
    """
    if not expected_registry_ids or set(source_geometry) != expected_registry_ids:
        raise ValueError("Empty reviewed city patch target set")
    detail, detail_sha = _proof(detail_manifest_path)
    overview, overview_sha = _proof(overview_manifest_path)
    base_manifest_path = city_root / "manifest.json"
    base_root_path = city_root / "tileset.json"
    base_manifest_sha = _file_sha(base_manifest_path)
    base_root_sha = _file_sha(base_root_path)
    if detail.get("inputs", {}).get("public_city_manifest_sha256") != base_manifest_sha:
        raise ValueError("Detail proof uses a different base city manifest")
    if overview.get("base_city_manifest_sha256") != base_manifest_sha:
        raise ValueError("Overview proof uses a different base city manifest")
    mapping = detail.get("target_city_feature_mapping")
    if not isinstance(mapping, list) or len(mapping) != len(expected_registry_ids):
        raise ValueError("Detail proof target count differs from reviewed targets")
    mapped_ids = [row.get("registry_id") for row in mapping]
    if len(set(mapped_ids)) != len(mapped_ids) or set(mapped_ids) != expected_registry_ids:
        raise ValueError("Detail proof registry IDs differ from reviewed targets")
    city_by_registry = {row["registry_id"]: row.get("city_feature_id_private") for row in mapping}
    if (any(not isinstance(value, str) or not value for value in city_by_registry.values()) or
            len(set(city_by_registry.values())) != len(city_by_registry)):
        raise ValueError("Reviewed city source IDs must be exact and unique")

    patched: dict[str, bytes] = {}
    tile_inventory: list[dict[str, Any]] = []
    detail_by_region: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    ids_by_region: dict[str, set[str]] = defaultdict(set)
    for row in detail.get("tiles", []):
        source_name = row.get("tile")
        region_name = (row.get("overview_lod") or {}).get("external_tileset")
        if not (isinstance(source_name, str) and source_name.startswith("cell_") and
                source_name.endswith(".glb") and isinstance(region_name, str) and
                region_name.startswith("region_") and region_name.endswith(".tileset.json")):
            raise ValueError("Invalid detail tile/region name")
        if source_name in detail_by_region[region_name]:
            raise ValueError("Duplicate detail tile proof")
        source_sha = row.get("original_public_sha256")
        _checked_bytes(city_root / source_name, source_sha)
        blob = _checked_bytes(detail_manifest_path.parent / "patched-detail" / source_name,
                              row.get("patched_private_sha256"))
        digest = _sha(blob)
        path = f"city-patch/tiles/{digest}.glb"
        previous = patched.setdefault(path, blob)
        if previous != blob:
            raise ValueError("Content-addressed detail collision")
        ids = sorted({item["registry_id"] for item in row.get("target_features", [])})
        if not ids or not set(ids) <= expected_registry_ids:
            raise ValueError("Detail tile has missing or unreviewed target")
        ids_by_region[region_name].update(ids)
        detail_by_region[region_name][source_name] = {"path": path, "sha256": digest}
        tile_inventory.append({"path": path, "sha256": digest, "bytes": len(blob),
                               "source_tile": source_name, "source_sha256": source_sha,
                               "lod": "detail", "suppressed_registry_ids": ids})
    if not detail_by_region or set().union(*ids_by_region.values()) != expected_registry_ids:
        raise ValueError("Detail replacement does not cover all reviewed IDs")

    overview_rows = overview.get("regions")
    if not isinstance(overview_rows, list) or len(overview_rows) != len(detail_by_region):
        raise ValueError("Overview proof must cover exactly the impacted regions")
    overview_by_region: dict[str, dict[str, Any]] = {}
    reviewed_pairs: list[dict[str, str]] = []
    for row in overview_rows:
        name = row.get("name")
        if not isinstance(name, str) or not name.startswith("massing_region_") or not name.endswith(".glb"):
            raise ValueError("Invalid overview tile name")
        region_name = name.replace("massing_region_", "region_", 1).replace(".glb", ".tileset.json")
        if region_name in overview_by_region or region_name not in detail_by_region:
            raise ValueError("Overview proof has unexpected/duplicate region")
        expected_pairs = sorted(
            ({"city_feature_id": city_by_registry[registry_id],
              "registry_id": registry_id} for registry_id in ids_by_region[region_name]),
            key=lambda pair: (pair["city_feature_id"], pair["registry_id"]),
        )
        if row.get("removed_pairs") != expected_pairs:
            raise ValueError("Overview city/registry pair differs from exact alignment")
        reviewed_pairs.extend(expected_pairs)
        if set(row.get("removed_registry_ids", [])) != ids_by_region[region_name]:
            raise ValueError("Overview removed IDs differ from detail targets")
        if set(row.get("removed_source_ids", [])) != {
                city_by_registry[registry_id] for registry_id in ids_by_region[region_name]}:
            raise ValueError("Overview removed source IDs differ from detail targets")
        if row.get("reproduction_check") is not True:
            raise ValueError("Overview region lacks byte reproduction proof")
        if row.get("source_sha256") == row.get("patched_sha256"):
            raise ValueError("Overview target was not removed")
        _checked_bytes(city_root / name, row.get("source_sha256"))
        blob = _checked_bytes(overview_manifest_path.parent / "patched" / name,
                              row.get("patched_sha256"))
        digest = _sha(blob)
        path = f"city-patch/tiles/{digest}.glb"
        previous = patched.setdefault(path, blob)
        if previous != blob:
            raise ValueError("Content-addressed overview collision")
        overview_by_region[region_name] = {"path": path, "sha256": digest,
                                            "source_tile": name, "source_sha256": row["source_sha256"]}
        tile_inventory.append({"path": path, "sha256": digest, "bytes": len(blob),
                               "source_tile": name, "source_sha256": row["source_sha256"],
                               "lod": "overview",
                               "suppressed_registry_ids": sorted(ids_by_region[region_name])})
    reviewed_pairs.sort(key=lambda pair: (pair["city_feature_id"], pair["registry_id"]))
    if (len(reviewed_pairs) != len(expected_registry_ids) or
            overview.get("pair_count") != len(reviewed_pairs) or
            overview.get("pair_mapping_sha256") != _sha(_json_bytes(reviewed_pairs))):
        raise ValueError("Overview exact pair mapping digest/count differs")
    if set(overview_by_region) != set(detail_by_region):
        raise ValueError("Overview/detail region coverage differs")

    base_tileset = json.loads(_checked_bytes(base_root_path, base_root_sha))
    root_children = base_tileset.get("root", {}).get("children", [])
    if not isinstance(root_children, list):
        raise ValueError("Base city root children missing")
    touched: set[str] = set()
    region_inventory: list[dict[str, Any]] = []
    for child in root_children:
        nested = child.get("children") or []
        if len(nested) != 1:
            raise ValueError("Unexpected base city region structure")
        region_name = nested[0].get("content", {}).get("uri")
        if not isinstance(region_name, str) or not region_name.startswith("region_"):
            raise ValueError("Unexpected base city region URI")
        overview_name = child.get("content", {}).get("uri")
        if not isinstance(overview_name, str) or not overview_name.startswith("massing_region_"):
            raise ValueError("Unexpected base city overview URI")
        if region_name not in overview_by_region:
            _replace_content_uri(child, f"/assets/public-citywide-v12/{overview_name}")
            _replace_content_uri(nested[0], f"/assets/public-citywide-v12/{region_name}")
            continue
        touched.add(region_name)
        overview_info = overview_by_region[region_name]
        if overview_info["source_tile"] != overview_name:
            raise ValueError("Base city region/overview link differs from proof")
        _replace_content_uri(child, f"tiles/{overview_info['sha256']}.glb")
        source_region_path = city_root / region_name
        source_region_sha = _file_sha(source_region_path)
        region_json = json.loads(_checked_bytes(source_region_path, source_region_sha))
        region_root = region_json.get("root")
        if not isinstance(region_root, dict) or region_root.get("content", {}).get("uri") != overview_name:
            raise ValueError("External region root is not the pinned overview")
        _replace_content_uri(region_root, f"../tiles/{overview_info['sha256']}.glb")
        matched_detail: set[str] = set()
        for leaf in region_root.get("children", []):
            source_tile = leaf.get("content", {}).get("uri")
            if source_tile in detail_by_region[region_name]:
                info = detail_by_region[region_name][source_tile]
                _replace_content_uri(leaf, f"../tiles/{info['sha256']}.glb")
                matched_detail.add(source_tile)
            else:
                if not isinstance(source_tile, str) or not source_tile.startswith("cell_"):
                    raise ValueError("Unexpected external region detail URI")
                _replace_content_uri(leaf, f"/assets/public-citywide-v12/{source_tile}")
        if matched_detail != set(detail_by_region[region_name]):
            raise ValueError("Not every detail replacement is in the base region")
        region_bytes = _json_bytes(region_json)
        region_sha = _sha(region_bytes)
        region_path = f"city-patch/regions/{region_sha}.json"
        patched[region_path] = region_bytes
        _replace_content_uri(nested[0], f"regions/{region_sha}.json")
        region_inventory.append({"path": region_path, "sha256": region_sha,
                                 "bytes": len(region_bytes), "source_region": region_name,
                                 "source_sha256": source_region_sha})
    if touched != set(overview_by_region):
        raise ValueError("Base root does not contain all affected regions")

    sorted_ids = sorted(expected_registry_ids)
    sidecar = _json_bytes(sorted_ids)
    pair_rows: list[dict[str, str]] = []
    for registry_id in sorted_ids:
        source = source_geometry[registry_id]
        if (source.get("source_group") not in {"proplanirovki", "fsk"} or
                not source.get("publisher_project_id") or
                not source.get("publisher_building_id") or
                not source.get("source_polygon_sha256")):
            raise ValueError("Reviewed source geometry identity is incomplete")
        if source["source_group"] == "fsk":
            if (registry_id != "registry-b1feab9d36bced349dfa4b1d" or
                    source["publisher_project_id"] != "636ce8f7f459625c9efe72ad" or
                    source["publisher_building_id"] != "639b34e2b176d8bf3ad3d6b1"):
                raise ValueError("Only the exact The Lake source way may enter the FSK patch")
            geometry_source_id = "osm:way/1317814900@2"
        else:
            geometry_source_id = "proplanirovki:" + source["publisher_project_id"] + ":" + source["publisher_building_id"]
        pair_rows.append({
            "registry_id": registry_id,
            "city_source_id": city_by_registry[registry_id],
            "geometry_source_id": geometry_source_id,
            "source_polygon_sha256": source["source_polygon_sha256"],
        })
    pair_rows.sort(key=lambda row: (row["city_source_id"], row["registry_id"]))
    pair_bytes = b"".join(_json_bytes(row) for row in pair_rows)
    tile_inventory.sort(key=lambda row: (row["lod"], row["source_tile"]))
    region_inventory.sort(key=lambda row: row["source_region"])
    root_bytes = _json_bytes(base_tileset)
    region_inventory_bytes = b"".join(_json_bytes(row) for row in region_inventory)
    tile_inventory_bytes = b"".join(_json_bytes(row) for row in tile_inventory)
    patched["city-patch/tileset.json"] = root_bytes
    patched["city-patch/region-inventory.jsonl"] = region_inventory_bytes
    patched["city-patch/tile-inventory.jsonl"] = tile_inventory_bytes
    patched["city-patch/suppressed-registry-ids.json"] = sidecar
    patched["city-patch/pair-proof.jsonl"] = pair_bytes
    descriptor = {"base_city_manifest_sha256": base_manifest_sha,
                  "base_city_tileset_sha256": base_root_sha,
                  "tileset_sha256": _sha(root_bytes),
                  "region_inventory_sha256": _sha(region_inventory_bytes),
                  "tile_inventory_sha256": _sha(tile_inventory_bytes),
                  "suppressed_registry_ids_sha256": _sha(sidecar),
                  "pair_proof_sha256": _sha(pair_bytes),
                  "pair_proof_total": len(pair_rows),
                  "regions_total": len(region_inventory),
                  "replacement_tiles_total": len(tile_inventory),
                  "suppressed_total": len(sorted_ids),
                  "all_visible_lods_covered": True,
                  "detail_proof_manifest_sha256": detail_sha,
                  "overview_proof_manifest_sha256": overview_sha}
    return descriptor, patched
