"""Stage a city tile revision for approved render-only height estimates.

Only affected detailed cells and their coarse parents are regenerated. All
other GLBs are hard-linked from the previously published immutable revision.
Source height and floor attributes are never changed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from shapely import wkb

from backend.store import ASSET_ROOT, ROOT
from pipeline.build_citywide import extrude_footprint
from pipeline.build_tiles import Mesh, glb, relative_xy, save_bytes, save_json


BASE = ASSET_ROOT / "citywide-v2"
OUTPUT = ASSET_ROOT / "citywide-v3"
OVERLAY = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.parquet"
OVERLAY_MANIFEST = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _glb_document(path: Path) -> dict[str, Any]:
    blob = path.read_bytes()
    if struct.unpack_from("<4sII", blob) != (b"glTF", 2, len(blob)):
        raise ValueError(f"Invalid GLB: {path}")
    length, kind = struct.unpack_from("<I4s", blob, 12)
    if kind != b"JSON":
        raise ValueError(f"Invalid GLB JSON: {path}")
    return json.loads(blob[20:20 + length])


def _overlay_rows(path: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if sha256(path) != manifest["overlay_sha256"]:
        raise ValueError("Reviewed height overlay hash changed")
    rows = pq.read_table(path).to_pylist()
    if len(rows) != manifest["overlay_rows"]:
        raise ValueError("Height overlay count changed")
    result = {}
    for row in rows:
        source_id, height = row["source_id"], row["estimated_render_height_m"]
        if source_id in result or not isinstance(height, (int, float)) or not math.isfinite(height) or height <= 0:
            raise ValueError("Invalid or duplicate estimated render height")
        if row["status"] != "estimated" or not row["source_height_missing"] or not row["source_floors_missing"]:
            raise ValueError("Overlay attempts to replace source height or floors")
        if height != min(row["donor_heights_m"]):
            raise ValueError("Estimated height is not the minimum accepted donor height")
        result[source_id] = row
    return result


def _base_index(base: Path, overlay: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    overlay_cells = {}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            if entry["source_id"] in overlay:
                if entry["height_policy"] != "display_fallback_15m" or entry["height_m"] != 15:
                    raise ValueError("Overlay source target is not a renderer fallback")
                overlay_cells[entry["source_id"]] = entry["tile"]
    if set(overlay_cells) != set(overlay):
        raise ValueError("Height overlay IDs do not match city source index")
    affected = set(overlay_cells.values())
    grouped: dict[str, list[dict[str, Any]]] = {cell: [] for cell in affected}
    with (base / "source_tile_index.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            if entry["tile"] in affected:
                grouped[entry["tile"]].append(entry)
    return grouped, affected


def _needed_geometries(source: Path, grouped: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    needed = {entry["source_id"] for entries in grouped.values() for entry in entries}
    records = {}
    for batch in pq.ParquetFile(source).iter_batches(batch_size=4096, columns=["id", "geometry", "bbox"]):
        for record in batch.to_pylist():
            if record["id"] in needed:
                records[record["id"]] = record
    if set(records) != needed:
        raise ValueError("Affected cell geometry is missing from original source")
    return records


def _rebuild_cell(stage: Path, uri: str, entries: list[dict[str, Any]],
                  geometries: dict[str, dict[str, Any]], overlay: dict[str, dict[str, Any]],
                  detail: dict[str, Any]) -> tuple[int, int]:
    west, south, east, north, _, _ = detail["boundingVolume"]["region"]
    origin_lon = math.degrees((west + east) / 2)
    origin_lat = math.degrees((south + north) / 2)
    mesh = Mesh()
    geometry_fallbacks = 0
    highest = 0.0
    for feature_id, entry in enumerate(entries):
        source_id = entry["source_id"]
        record = geometries[source_id]
        height = float(overlay[source_id]["estimated_render_height_m"] if source_id in overlay
                       else entry["height_m"])
        highest = max(highest, height)
        previous = mesh.groups.get(4)
        lengths = tuple(len(values) for values in previous) if previous else (0, 0, 0, 0)
        previous_ids = len(mesh.feature_ids.get(4, []))
        try:
            if extrude_footprint(mesh, wkb.loads(record["geometry"]), height, feature_id,
                                 origin_lon, origin_lat) == 0:
                raise ValueError("No roof triangles")
        except (ValueError, TypeError, RuntimeError):
            if 4 in mesh.groups:
                for values, length in zip(mesh.groups[4], lengths):
                    del values[length:]
                del mesh.feature_ids[4][previous_ids:]
            bbox = record["bbox"]
            fw, fe, fs, fn = bbox["xmin"], bbox["xmax"], bbox["ymin"], bbox["ymax"]
            cx, cy = relative_xy((fw + fe) / 2, (fs + fn) / 2, origin_lon, origin_lat)
            width = max(1, relative_xy(fe, origin_lat, fw, origin_lat)[0])
            depth = max(1, relative_xy(origin_lon, fn, origin_lon, fs)[1])
            mesh.box(cx, cy, 0, width, depth, height, 4, feature_id)
            geometry_fallbacks += 1
    if not mesh.groups or highest <= 0:
        raise ValueError(f"Empty regenerated city tile: {uri}")
    save_bytes(stage / uri, glb(mesh, {
        "geometry_type": "synthetic_massing_from_public_footprints",
        "source_ids": [entry["source_id"] for entry in entries],
        "source_count": len(entries),
        "feature_id_mapping": "_FEATURE_ID_0 indexes source_ids",
        "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
        "estimated_render_height_overlay": "v1.1",
    }))
    detail["boundingVolume"]["region"][5] = highest + 5
    detail["geometricError"] = 0
    document = _glb_document(stage / uri)
    assert document["asset"]["extras"]["source_ids"] == [entry["source_id"] for entry in entries]
    for primitive in document["meshes"][0]["primitives"]:
        position = document["accessors"][primitive["attributes"]["POSITION"]]
        normal = document["accessors"][primitive["attributes"]["NORMAL"]]
        if position["count"] != normal["count"] or position["count"] <= 0:
            raise ValueError(f"Invalid regenerated GLB topology: {uri}")
    return geometry_fallbacks, len(entries)


def _rebuild_region(stage: Path, parent: dict[str, Any], uri: str) -> None:
    west, south, east, north, _, _ = parent["boundingVolume"]["region"]
    origin_lon = math.degrees((west + east) / 2)
    origin_lat = math.degrees((south + north) / 2)
    mesh = Mesh()
    cell_ids = []
    for feature_id, child in enumerate(parent["children"]):
        cw, cs, ce, cn, _, top = child["boundingVolume"]["region"]
        cw, cs, ce, cn = (math.degrees(v) for v in (cw, cs, ce, cn))
        cell_height = top - 5
        cx, cy = relative_xy((cw + ce) / 2, (cs + cn) / 2, origin_lon, origin_lat)
        width = max(1, relative_xy(ce, origin_lat, cw, origin_lat)[0])
        depth = max(1, relative_xy(origin_lon, cn, origin_lon, cs)[1])
        mesh.box(cx, cy, 0, width, depth, min(30, cell_height), 4, feature_id)
        cell_ids.append(child["content"]["uri"].removesuffix(".glb"))
    save_bytes(stage / uri, glb(mesh, {
        "geometry_type": "coarse_synthetic_cell_aggregation", "cell_ids": cell_ids,
        "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
    }))
    parent["boundingVolume"]["region"][5] = max(
        child["boundingVolume"]["region"][5] for child in parent["children"])
    parent["geometricError"] = 500


def build(base: Path = BASE, output: Path = OUTPUT, overlay_path: Path = OVERLAY,
          overlay_manifest_path: Path = OVERLAY_MANIFEST) -> dict[str, Any]:
    manifest = json.loads(overlay_manifest_path.read_text(encoding="utf-8"))
    source_path = ROOT / manifest["input_file"]
    original = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if original["partial"] or original["source_sha256"] != manifest["input_sha256"]:
        raise ValueError("Base city tiles do not match reviewed height overlay source")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("overlay_sha256") != manifest["overlay_sha256"]:
            raise ValueError("Existing city height revision differs from overlay")
        return {"status": "no_op", **saved}
    overlay = _overlay_rows(overlay_path, manifest)
    grouped, affected = _base_index(base, overlay)
    geometries = _needed_geometries(source_path, grouped)
    tileset = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    parents = tileset["root"]["children"]
    detail_by_uri = {child["content"]["uri"]: child
                     for parent in parents for child in parent["children"]}
    if not affected <= set(detail_by_uri):
        raise ValueError("Affected cells missing from base tile graph")
    affected_parents = {parent["content"]["uri"] for parent in parents
                        if any(child["content"]["uri"] in affected for child in parent["children"])}
    with tempfile.TemporaryDirectory(prefix=".citywide-v3.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in base.iterdir():
            if path.name in {"tileset.json", "manifest.json", "source_tile_index.jsonl"}:
                continue
            if path.name not in affected and path.name not in affected_parents:
                os.link(path, stage / path.name)
        original_fallbacks = 0
        revised_fallbacks = 0
        for uri in sorted(affected):
            original_fallbacks += sum(entry["geometry_policy"] == "bbox_fallback_geometry_error"
                                      for entry in grouped[uri])
            made_fallbacks, _ = _rebuild_cell(stage, uri, grouped[uri], geometries,
                                              overlay, detail_by_uri[uri])
            revised_fallbacks += made_fallbacks
        for parent in parents:
            uri = parent["content"]["uri"]
            if uri in affected_parents:
                _rebuild_region(stage, parent, uri)
        tileset["root"]["boundingVolume"]["region"][5] = max(
            parent["boundingVolume"]["region"][5] for parent in parents)
        tileset["asset"]["tilesetVersion"] = "citywide-massing-dark-height-v3"
        tileset["extras"]["render_height_overlay"] = "estimated only for 59 missing-height-and-floor buildings"
        save_json(stage / "tileset.json", tileset)
        updated = 0
        with (base / "source_tile_index.jsonl").open(encoding="utf-8") as source, \
                (stage / "source_tile_index.jsonl").open("w", encoding="utf-8") as destination:
            for line in source:
                entry = json.loads(line)
                estimate = overlay.get(entry["source_id"])
                if estimate:
                    entry.update({"height_m": estimate["estimated_render_height_m"],
                                  "height_policy": "estimated_render_height_overlay",
                                  "render_height_status": "estimated",
                                  "render_height_source": "city_render_height_imputation_v1_1",
                                  "imputation_profile_id": estimate["profile_id"]})
                    line = json.dumps(entry, ensure_ascii=False) + "\n"
                    updated += 1
                destination.write(line)
            destination.flush()
            os.fsync(destination.fileno())
        if updated != len(overlay):
            raise ValueError("Source index revision omitted estimated heights")
        revised = {**original,
            "version": "3", "tileset_url": f"/assets/{output.name}/tileset.json",
            "material_recolor_only": False, "height_revision_only": True,
            "render_height_overlay": str(overlay_path.relative_to(ROOT)),
            "overlay_sha256": manifest["overlay_sha256"],
            "imputed_height_count": len(overlay),
            "unknown_height_count": original["unknown_height_count"] - len(overlay),
            "geometry_fallback_count": original["geometry_fallback_count"] - original_fallbacks + revised_fallbacks,
            "revised_detail_tiles": len(affected), "revised_coarse_region_tiles": len(affected_parents),
            "height_policy_priority": manifest["priority"],
            "estimated_height_validation": manifest["validation_status"]}
        if (revised["measured_height_count"] + revised["assumed_floor_height_count"]
                + revised["imputed_height_count"] + revised["unknown_height_count"]
                != revised["source_rows"]):
            raise ValueError("Render-height policy counts do not add to source count")
        save_json(stage / "manifest.json", revised)
        if any(not (stage / child["content"]["uri"]).is_file()
               for parent in parents for child in parent["children"]):
            raise ValueError("Revised tile graph has missing detailed GLBs")
        if any(not (stage / parent["content"]["uri"]).is_file() for parent in parents):
            raise ValueError("Revised tile graph has missing coarse GLBs")
        stage.replace(output)
    pointer_path = ASSET_ROOT / "manifest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.update({"city_tileset_url": revised["tileset_url"],
                    "city_render_height_overlay": "v1.1"})
    save_json(pointer_path, pointer)
    return {"status": "built", **revised}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
