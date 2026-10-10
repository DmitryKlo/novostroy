"""Audit every v11 city GLB as a material-only v10 revision, offline."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_city_palette_revision import (
    NEW_DETAIL_BYTES, NEW_REGION_BYTES, OLD_DETAIL_BYTES, OLD_REGION_BYTES,
)
from pipeline.publish_public_assets import sha256


def _chunks(path: Path) -> tuple[bytes, bytes]:
    blob = path.read_bytes()
    if len(blob) < 28 or struct.unpack_from("<4sII", blob) != (b"glTF", 2, len(blob)):
        raise ValueError(f"Invalid city GLB: {path.name}")
    json_length, json_kind = struct.unpack_from("<I4s", blob, 12)
    offset = 20 + json_length
    bin_length, bin_kind = struct.unpack_from("<I4s", blob, offset)
    if json_kind != b"JSON" or bin_kind != b"BIN\x00" or offset + 8 + bin_length != len(blob):
        raise ValueError(f"Invalid city GLB chunks: {path.name}")
    return blob[20:offset], blob[offset + 8:]


def _audit_root(old: Path, new: Path) -> None:
    previous = json.loads((old / "tileset.json").read_text(encoding="utf-8"))
    revised = json.loads((new / "tileset.json").read_text(encoding="utf-8"))
    if previous["root"] != revised["root"] or previous["geometricError"] != revised["geometricError"]:
        raise ValueError("City tile hierarchy or bounds changed")
    for key in ("attribution", "geometry_policy", "ground_first_city_policy",
                "region_overview_has_cell_boxes", "region_simplify_tolerance_m"):
        if previous["extras"].get(key) != revised["extras"].get(key):
            raise ValueError(f"City tileset provenance or geometry policy changed: {key}")


def audit(base: Path, revised: Path, public_base: Path, public_revised: Path) -> dict[str, Any]:
    old_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    new_manifest = json.loads((revised / "manifest.json").read_text(encoding="utf-8"))
    public_manifest = json.loads((public_revised / "manifest.json").read_text(encoding="utf-8"))
    if (new_manifest.get("base_v10_manifest_sha256") != sha256(base / "manifest.json")
            or new_manifest.get("version") != "11" or new_manifest.get("recolored_city_glbs") != 3608
            or new_manifest.get("bin_identical_to_v10_glbs") is not True
            or public_manifest.get("private_manifest_sha256") != sha256(revised / "manifest.json")
            or public_manifest.get("public_redacted_source_ids") is not True):
        raise ValueError("City v11 manifest or public privacy gate failed")
    for key in ("source_rows", "source_sha256", "city_buildings", "detailed_tiles",
                "region_overview_glbs", "region_overview_cell_box_count",
                "region_overview_roof_triangles", "region_overview_wall_triangles"):
        if old_manifest.get(key) != new_manifest.get(key):
            raise ValueError(f"City {key} changed in material revision")
    old_files = sorted(path.name for path in base.glob("*.glb"))
    if (len(old_files) != 3608
            or old_files != sorted(path.name for path in revised.glob("*.glb"))
            or old_files != sorted(path.name for path in public_base.glob("*.glb"))
            or old_files != sorted(path.name for path in public_revised.glob("*.glb"))):
        raise ValueError("City GLB set changed")
    _audit_root(base, revised)
    _audit_root(public_base, public_revised)
    for path in base.glob("region_*.tileset.json"):
        for root in (revised, public_base, public_revised):
            if (root / path.name).read_bytes() != path.read_bytes():
                raise ValueError("External region JSON changed")
    total_bytes = 0
    for name in old_files:
        overview = name.startswith("massing_region_")
        old_color, new_color = ((OLD_REGION_BYTES, NEW_REGION_BYTES) if overview
                                else (OLD_DETAIL_BYTES, NEW_DETAIL_BYTES))
        for old_root, new_root in ((base, revised), (public_base, public_revised)):
            old_json, old_bin = _chunks(old_root / name)
            new_json, new_bin = _chunks(new_root / name)
            if old_root == base:
                json_matches = (old_json.count(old_color) == (2 if overview else 1)
                                and new_json == old_json.replace(old_color, new_color))
            else:
                # Redaction reserializes private JSON and normalizes 0.40 to
                # 0.4; compare the complete document after this one factor.
                previous = json.loads(old_json)
                current = json.loads(new_json)
                for material_index in ((0, 1) if overview else (4,)):
                    previous["materials"][material_index]["pbrMetallicRoughness"]["baseColorFactor"] = [0.40, 0.46, 0.50, 1]
                json_matches = previous == current
            if not json_matches or new_bin != old_bin:
                raise ValueError(f"City GLB changed outside its material factor: {name}")
            if old_root == public_base:
                total_bytes += (new_root / name).stat().st_size
    return {"glbs": len(old_files), "overview": sum(name.startswith("massing_region_") for name in old_files),
            "detail": sum(name.startswith("cell_") for name in old_files),
            "public_glb_bytes": total_bytes,
            "private_manifest_sha256": sha256(revised / "manifest.json"),
            "public_manifest_sha256": sha256(public_revised / "manifest.json")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=ASSET_ROOT / "citywide-v10")
    parser.add_argument("--revised", type=Path, default=ASSET_ROOT / "citywide-v11")
    parser.add_argument("--public-base", type=Path, default=ASSET_ROOT / "public-citywide-v10")
    parser.add_argument("--public-revised", type=Path, default=ASSET_ROOT / "public-citywide-v11")
    arguments = parser.parse_args()
    print(json.dumps(audit(arguments.base, arguments.revised,
                           arguments.public_base, arguments.public_revised),
                     ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
