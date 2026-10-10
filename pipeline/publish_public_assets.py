"""Stage provider-hidden public GLBs and atomically publish active tile roots.

Private build folders retain lossless source-to-feature indexes and GLB extras.
Public GLBs keep identical BIN geometry and local feature IDs, while source-ID
crosswalks remain only in private artifacts. Previously released roots remain
available through explicit compatibility mappings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_tiles import save_bytes, save_json


ROOTS = (
    ("city_volumes", "citywide-v12", "public-citywide-v12", "city_tileset_url"),
    ("ground_buildings", "ground-v3", "public-ground-v3", "ground_tileset_url"),
    ("roads", "roads-v2", "public-roads-v2", "roads_tileset_url"),
    ("parks", "parks-v1", "public-parks-v1", "parks_tileset_url"),
    ("water", "water-v1", "public-water-v1", "water_tileset_url"),
)
REDACT_KEYS = frozenset({"source_ids", "feature_id_mapping"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()
                if key not in REDACT_KEYS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def sanitize_glb(payload: bytes) -> tuple[bytes, bool]:
    if len(payload) < 28 or struct.unpack_from("<4sII", payload) != (b"glTF", 2, len(payload)):
        raise ValueError("Invalid GLB header")
    json_length, json_kind = struct.unpack_from("<I4s", payload, 12)
    if json_kind != b"JSON":
        raise ValueError("GLB lacks a JSON chunk")
    binary_header = 20 + json_length
    binary_length, binary_kind = struct.unpack_from("<I4s", payload, binary_header)
    if binary_kind != b"BIN\x00" or binary_header + 8 + binary_length != len(payload):
        raise ValueError("GLB has an invalid binary chunk")
    original = json.loads(payload[20:binary_header])
    document = _redact(original)
    if document == original:
        return payload, False
    json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_part += b" " * (-len(json_part) % 4)
    binary_chunk = payload[binary_header:]
    total = 12 + 8 + len(json_part) + len(binary_chunk)
    return (struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<I4s", len(json_part), b"JSON")
            + json_part + binary_chunk), True


def _public_manifest(private: dict[str, Any], *, url: str,
                     private_manifest_sha256: str, attribution: str) -> dict[str, Any]:
    fields = ("version", "partial", "source_rows", "city_buildings", "detailed_tiles",
              "coarse_region_tiles", "measured_height_count", "assumed_floor_height_count",
              "imputed_height_count", "unknown_height_count", "model_quality",
              "render_palette", "geometry_fallback_count", "rendered_source_rows",
              "partitioned_fragments", "triangles", "skipped_fragments",
              "silhouette_region_tiles", "silhouette_source_rows", "silhouette_triangles",
              "silhouette_bytes", "silhouette_max_region_bytes",
              "silhouette_texture_gpu_rgba_bytes", "silhouette_max_region_texture_gpu_rgba_bytes",
              "silhouette_max_raster_pixel_m", "silhouette_subpixel_features",
              "silhouette_geometry_fallback_count", "silhouette_policy",
              "silhouette_source_snapshot_date",
              "reused_v5_volume_detail_glbs", "external_region_tilesets",
              "root_tileset_raw_bytes", "root_tileset_gzip5_bytes",
              "external_tilesets_raw_bytes", "external_tilesets_gzip5_bytes",
              "lazy_tileset_policy", "reused_v6_glbs", "ground_first_city_policy",
              "ground_is_separate_tileset", "reused_v7_volume_detail_glbs",
              "geometry_policy", "rights", "ground_offset_m", "region_simplify_tolerance_m",
              "source_snapshot_date", "common_raster_max_dimension_px", "common_raster_pixel_m",
              "common_raster_subpixel_omitted", "common_raster_glb_bytes",
              "common_raster_gpu_rgba_bytes", "exact_triangles", "exact_glb_bytes",
              "exact_holes", "exact_multipolygons", "region_triangles", "region_glb_bytes",
              "source_ids_omitted_from_glb", "color_space_policy", "display_color_srgb",
              "vector_linear_base_color_factor", "recolored_vector_glbs",
              "bin_identical_to_v2_glbs", "common_raster_reused_from_v2",
              "overview_policy", "region_simplify_tolerance_m", "region_overview_glbs",
              "region_overview_source_rows", "region_overview_roof_triangles",
              "region_overview_wall_triangles", "region_overview_bytes",
              "region_overview_max_bytes", "region_overview_cell_box_count",
              "region_overview_roof_base_color_factor",
              "region_overview_wall_base_color_factor", "reused_v8_detail_glbs",
              "base_v9_manifest_sha256", "region_overview_lighting_policy",
              "region_overview_normal_bytes", "region_overview_degenerate_wall_faces",
              "coarse_triangle_coordinates_identical_to_v9", "region_overview_split_vertices",
              "reused_v9_detail_glbs", "base_v10_manifest_sha256",
              "detail_material_base_color_factor", "palette_revision_only",
              "bin_identical_to_v10_glbs", "recolored_city_glbs",
              "reused_v10_external_region_json", "base_v11_manifest_sha256",
              "bin_identical_to_v11_glbs", "reused_v11_external_region_json")
    fields += ("bounds_margin_detail_m", "bounds_margin_coarse_m")
    output = {key: private[key] for key in fields if key in private}
    output.update({"tileset_url": url, "attribution": attribution,
                   "public_redacted_source_ids": True,
                   "private_manifest_sha256": private_manifest_sha256})
    return output


def stage_public_root(kind: str, internal_root: str, public_root: str) -> dict[str, Any]:
    source = ASSET_ROOT / internal_root
    target = ASSET_ROOT / public_root
    private_manifest_path = source / "manifest.json"
    private_manifest = json.loads(private_manifest_path.read_text(encoding="utf-8"))
    if private_manifest.get("partial"):
        raise ValueError(f"Incomplete private {kind} tile build")
    if internal_root == "citywide-v6":
        previous_private_manifest = ASSET_ROOT / "citywide-v5" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v5" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")):
            raise ValueError("v6 cannot reuse an unverified v5 public release")
    if internal_root == "citywide-v7":
        previous_private_manifest = ASSET_ROOT / "citywide-v6" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v6" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v6_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")):
            raise ValueError("v7 cannot reuse an unverified v6 public release")
    if internal_root == "citywide-v8":
        previous_private_manifest = ASSET_ROOT / "citywide-v7" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v7" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v7_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("reused_v7_volume_detail_glbs") != 3608):
            raise ValueError("v8 cannot reuse an unverified v7 public release")
    if internal_root == "citywide-v9":
        previous_private_manifest = ASSET_ROOT / "citywide-v8" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v8" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v8_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("reused_v8_detail_glbs") != 3396
                or private_manifest.get("region_overview_cell_box_count") != 0):
            raise ValueError("v9 cannot reuse an unverified v8 detail release")
    if internal_root == "citywide-v10":
        previous_private_manifest = ASSET_ROOT / "citywide-v9" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v9" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v9_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("reused_v9_detail_glbs") != 3396
                or private_manifest.get("region_overview_glbs") != 212
                or not private_manifest.get("coarse_triangle_coordinates_identical_to_v9")):
            raise ValueError("v10 cannot reuse an unverified v9 footprint release")
    if internal_root == "citywide-v11":
        previous_private_manifest = ASSET_ROOT / "citywide-v10" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v10" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v10_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("version") != "11"
                or private_manifest.get("recolored_city_glbs") != 3608
                or private_manifest.get("reused_v10_external_region_json") != 212
                or private_manifest.get("bin_identical_to_v10_glbs") is not True
                or private_manifest.get("region_overview_cell_box_count") != 0):
            raise ValueError("v11 cannot recolor an unverified v10 footprint release")
    if internal_root == "citywide-v12":
        previous_private_manifest = ASSET_ROOT / "citywide-v11" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-citywide-v11" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v11_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("version") != "12"
                or private_manifest.get("recolored_city_glbs") != 3608
                or private_manifest.get("reused_v11_external_region_json") != 212
                or private_manifest.get("bin_identical_to_v11_glbs") is not True
                or private_manifest.get("region_overview_cell_box_count") != 0):
            raise ValueError("v12 cannot recolor an unverified v11 city release")
    if internal_root == "ground-v3":
        previous_private_manifest = ASSET_ROOT / "ground-v2" / "manifest.json"
        previous_public_manifest = json.loads(
            (ASSET_ROOT / "public-ground-v2" / "manifest.json").read_text(encoding="utf-8"))
        if (private_manifest.get("base_v2_manifest_sha256") != sha256(previous_private_manifest)
                or previous_public_manifest.get("private_manifest_sha256") != sha256(previous_private_manifest)
                or not previous_public_manifest.get("public_redacted_source_ids")
                or private_manifest.get("recolored_vector_glbs") != 3608):
            raise ValueError("v3 cannot recolor an unverified v2 ground release")
    if internal_root == "ground-v3" and (private_manifest.get("source_rows") != 564031
                                          or private_manifest.get("exact_holes") != 995
                                          or not private_manifest.get("source_ids_omitted_from_glb")):
        raise ValueError("Ground revision is incomplete or includes source IDs")
    if internal_root == "roads-v2":
        previous_private = ASSET_ROOT / "roads-v1" / "manifest.json"
        previous_public = json.loads(
            (ASSET_ROOT / "public-roads-v1" / "manifest.json").read_text(encoding="utf-8"))
        previous = json.loads(previous_private.read_text(encoding="utf-8"))
        if (previous_public.get("private_manifest_sha256") != sha256(previous_private)
                or not previous_public.get("public_redacted_source_ids")
                or private_manifest.get("version") != "2"
                or private_manifest.get("source_sha256") != previous.get("source_sha256")
                or private_manifest.get("source_rows") != previous.get("source_rows")
                or private_manifest.get("partitioned_fragments") != previous.get("partitioned_fragments")
                or private_manifest.get("geometry_policy") !=
                "exact_WGS84_centerline_projection_with_conservative_ribbon_bounds"):
            raise ValueError("v2 roads do not match the reviewed v1 source and seam policy")
    private_hash = sha256(private_manifest_path)
    if target.exists():
        saved = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("private_manifest_sha256") != private_hash or not saved.get("public_redacted_source_ids"):
            raise ValueError(f"Existing public {kind} root differs from internal build")
        return {"kind": kind, "internal_root": internal_root, "public_root": public_root,
                "internal_manifest_sha256": private_hash,
                "public_manifest_sha256": sha256(target / "manifest.json"),
                "glb_count": sum(1 for _ in target.glob("*.glb"))}
    tileset = json.loads((source / "tileset.json").read_text(encoding="utf-8"))
    attribution = tileset.get("extras", {}).get("attribution")
    if not isinstance(attribution, str) or not attribution.strip():
        raise ValueError(f"Private {kind} tileset lacks required attribution")
    with tempfile.TemporaryDirectory(prefix=f".{public_root}.", dir=ASSET_ROOT) as temp:
        stage = Path(temp)
        save_bytes(stage / "tileset.json", (source / "tileset.json").read_bytes())
        if internal_root in {"citywide-v7", "citywide-v8", "citywide-v9", "citywide-v10", "citywide-v11", "citywide-v12", "ground-v3"}:
            external = list(source.glob("region_*.tileset.json"))
            if len(external) != private_manifest["external_region_tilesets"]:
                raise ValueError("External region tileset count changed")
            for path in external:
                if not re.fullmatch(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json", path.name):
                    raise ValueError("Unexpected external region tileset filename")
                save_bytes(stage / path.name, path.read_bytes())
        count = 0
        redacted_count = 0
        reused_redacted_count = 0
        for path in source.glob("*.glb"):
            previous_root = ("citywide-v5" if internal_root == "citywide-v6" and not path.name.startswith("silhouette_")
                             else "citywide-v6" if internal_root == "citywide-v7"
                             else "citywide-v7" if internal_root == "citywide-v8"
                             else "citywide-v8" if internal_root == "citywide-v9" and path.name.startswith("cell_")
                             else "citywide-v9" if internal_root == "citywide-v10" and path.name.startswith("cell_")
                             else None)
            if previous_root is not None:
                previous_private = ASSET_ROOT / previous_root / path.name
                previous_public = ASSET_ROOT / ("public-" + previous_root) / path.name
                if (previous_private.is_file() and previous_public.is_file()
                        and os.path.samefile(path, previous_private)):
                    os.link(previous_public, stage / path.name)
                    reused_redacted_count += 1
                    count += 1
                    continue
            content, changed = sanitize_glb(path.read_bytes())
            if internal_root == "ground-v3" and changed:
                raise ValueError("Ground GLB unexpectedly contains source IDs")
            if changed:
                save_bytes(stage / path.name, content)
                redacted_count += 1
            else:
                os.link(path, stage / path.name)
            count += 1
        if not count or (internal_root != "ground-v3" and not (redacted_count or reused_redacted_count)):
            raise ValueError(f"No source IDs redacted from {kind} GLBs")
        seen_external: set[str] = set()

        def referenced_uris(tile: dict[str, Any]) -> set[str]:
            result: set[str] = set()
            if "content" in tile:
                name = tile["content"]["uri"]
                if Path(name).name != name:
                    raise ValueError("Tile URI escapes public root")
                result.add(name)
                if name.endswith(".json"):
                    if (internal_root not in {"citywide-v7", "citywide-v8", "citywide-v9", "citywide-v10", "citywide-v11", "citywide-v12", "ground-v3"} or not re.fullmatch(
                            r"region_-?[0-9]+_-?[0-9]+\.tileset\.json", name)
                            or tile.get("children") or name in seen_external):
                        raise ValueError("Invalid external region tileset reference")
                    seen_external.add(name)
                    external_doc = json.loads((stage / name).read_text(encoding="utf-8"))
                    result.update(referenced_uris(external_doc["root"]))
            for child in tile.get("children", []):
                result.update(referenced_uris(child))
            return result

        referenced = referenced_uris(tileset["root"])
        if any(Path(name).name != name or not (stage / name).is_file() for name in referenced):
            raise ValueError(f"Missing public {kind} GLB")
        if internal_root in {"citywide-v7", "citywide-v8", "citywide-v9", "citywide-v10", "citywide-v11", "citywide-v12", "ground-v3"} and (
                len(seen_external) != 212 or len(referenced) != count + 212):
            raise ValueError("External references do not cover every staged tile")
        url = f"/assets/{public_root}/tileset.json"
        save_json(stage / "manifest.json", _public_manifest(private_manifest, url=url,
                  private_manifest_sha256=private_hash, attribution=attribution))
        stage.replace(target)
    return {"kind": kind, "internal_root": internal_root, "public_root": public_root,
            "internal_manifest_sha256": private_hash,
            "public_manifest_sha256": sha256(target / "manifest.json"),
            "glb_count": count}


def publish() -> dict[str, Any]:
    mappings = [stage_public_root(kind, internal, public)
                for kind, internal, public, _ in ROOTS]
    urls = [f"/assets/{public}/tileset.json" for _, _, public, _ in ROOTS]
    pointer_path = ASSET_ROOT / "manifest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    old_urls = [pointer.get(key) for _, _, _, key in ROOTS]
    old_audit_path = ASSET_ROOT / "public_release_audit.json"
    old_audit = json.loads(old_audit_path.read_text(encoding="utf-8")) if old_audit_path.is_file() else {}
    compatibility_by_root = {
        entry["public_root"]: entry
        for entry in old_audit.get("compatibility_mappings", []) + old_audit.get("mappings", [])
        if f"/assets/{entry['public_root']}/tileset.json" not in urls
    }
    compatibility = [compatibility_by_root[root] for root in sorted(compatibility_by_root)]
    audit = {"version": "1", "mappings": mappings, "active_public_urls": urls,
             "compatibility_mappings": compatibility,
             "compatibility_public_urls": [f"/assets/{entry['public_root']}/tileset.json"
                                           for entry in compatibility]}
    save_json(old_audit_path, audit)
    if old_urls != urls:
        pointer["previous_public_roots"] = sorted(set(pointer.get("previous_public_roots", [])) | {
            Path(url.removeprefix("/assets/")).parts[0]
            for url in old_urls if isinstance(url, str) and url.startswith("/assets/public-")
            and url not in urls
        })
    pointer.update({key: url for (_, _, _, key), url in zip(ROOTS, urls, strict=True)})
    city_mapping = next(entry for entry in mappings if entry["kind"] == "city_volumes")
    city_manifest = json.loads((ASSET_ROOT / city_mapping["internal_root"] / "manifest.json")
                               .read_text(encoding="utf-8"))
    palette = city_manifest.get("render_palette", {}).get("name")
    if not isinstance(palette, str) or not palette:
        raise ValueError("Active city release lacks a render palette")
    pointer["city_render_palette"] = palette
    pointer["public_redacted_source_ids"] = True
    save_json(pointer_path, pointer)
    return audit


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False, indent=2))
