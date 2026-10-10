"""Offline integrity/privacy/LOD audit for a staged model-candidate run."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import struct
from collections import Counter
from pathlib import Path
from typing import Any

from shapely.geometry import shape

from sources.models.build import ASSETS, INPUTS, OUTPUT, file_sha, jsonl

MODEL_FILE = re.compile(r"model_[a-f0-9]{64}(?:\.tileset\.json|\.glb)\Z")


def _glb_document(path: Path) -> dict[str, Any]:
    blob = path.read_bytes()
    if len(blob) < 28 or struct.unpack_from("<4sII", blob) != (b"glTF", 2, len(blob)):
        raise ValueError(f"Malformed GLB: {path.name}")
    json_length, chunk_type = struct.unpack_from("<I4s", blob, 12)
    if chunk_type != b"JSON":
        raise ValueError(f"Missing GLB JSON: {path.name}")
    document = json.loads(blob[20:20 + json_length])
    if document["asset"]["extras"].get("source_ids_redacted") is not True:
        raise ValueError("Model GLB lacks privacy declaration")
    if any(secret in blob[20:20 + json_length].lower() for secret in
           (b"source_record_id", b"source_url", b"osm_id", b"estateline:")):
        raise ValueError("Model GLB embeds a source identifier")
    if not document["meshes"][0]["primitives"]:
        raise ValueError("Model GLB is empty")
    return document


def audit(run_dir: Path, asset_root: Path = ASSETS) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != "model-candidates/1":
        raise ValueError("Unexpected run schema")
    for key, path in INPUTS.items():
        if file_sha(path) != manifest["input_hashes"][key]:
            raise ValueError(f"Pinned input drift: {key}")
    lines: dict[str, list[dict[str, Any]]] = {}
    for name in ("assessments", "candidates", "source_evaluations"):
        path = run_dir / f"{name}.jsonl"
        if file_sha(path) != manifest[f"{name}_sha256"]:
            raise ValueError(f"Output drift: {name}")
        lines[name] = jsonl(path)
        if len(lines[name]) != manifest[f"{name}_count"]:
            raise ValueError(f"Output count drift: {name}")
    rows = lines["assessments"]
    if len(rows) != 2317 or len({row["candidate_id"] for row in rows}) != len(rows):
        raise ValueError("Missing or duplicated candidate assessment")
    if {row["candidate_id"] for row in lines["source_evaluations"]} != {row["candidate_id"] for row in rows}:
        raise ValueError("Private audit does not cover exactly the assessment IDs")
    if any(not row.get("attempt_log") for row in rows):
        raise ValueError("At least one source row has no recorded gate attempts")
    counts = dict(Counter(row["decision"] for row in rows))
    if counts != manifest["decision_counts"]:
        raise ValueError("Decision counts disagree with assessments")
    public = asset_root / manifest["public_root"]
    public_manifest_path = public / "public_manifest.json"
    if file_sha(public_manifest_path) != manifest["public_manifest_sha256"]:
        raise ValueError("Public model manifest drift")
    public_manifest = json.loads(public_manifest_path.read_text(encoding="utf-8"))
    if public_manifest.get("public_redacted_source_ids") is not True:
        raise ValueError("Public model root is not sanitized")
    file_index = {}
    raw_bytes = gzip_bytes = 0
    for record in public_manifest["files"]:
        name = record["path"]
        if not MODEL_FILE.fullmatch(name) or name in file_index:
            raise ValueError("Unsafe or repeated public model filename")
        path = public / name
        blob = path.read_bytes()
        if file_sha(path) != record["sha256"] or len(blob) != record["bytes"]:
            raise ValueError(f"Public model asset drift: {name}")
        file_index[name] = path
        raw_bytes += len(blob)
        gzip_bytes += len(gzip.compress(blob, compresslevel=5))
    if {path.name for path in public.iterdir()} != {*file_index, "public_manifest.json"}:
        raise ValueError("Unlisted public model asset")
    ready = 0
    volume = 0
    flat = 0
    for row in rows:
        selected = row["selected_model"]
        if row["decision"] != "ready":
            if selected is not None or row["asset_readiness"] == "ready_to_render":
                raise ValueError("Non-ready assessment exposes a model")
            continue
        ready += 1
        if row["entity_type"] != "mapped_construction" or row["project_link_status"] != "unlinked":
            raise ValueError("Mapped outline claims a project/corpus identity")
        if selected["kind"] != "mapped_construction" or selected["rights_evidence"] != {
            "license": "ODbL-1.0", "display_cleared": True, "attribution_required": True,
        }:
            raise ValueError("Ready model lacks mapped source rights")
        expected = f"/assets/{manifest['public_root']}/model_{selected['content_hash']}.tileset.json"
        if selected["tileset_url"] != expected:
            raise ValueError("Selected model URL is not content addressed")
        path = file_index.get(Path(expected).name)
        if path is None:
            raise ValueError("Selected model tileset not listed")
        tile = json.loads(path.read_text(encoding="utf-8"))
        root = tile["root"]
        bbox = row["bbox"]
        preview_geometry = row.get("preview_geometry")
        if not isinstance(preview_geometry, dict) or preview_geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Ready source geometry lacks exact selected preview")
        preview_shape = shape(preview_geometry)
        if not preview_shape.is_valid or not preview_shape.bounds == tuple(bbox):
            raise ValueError("Selected preview topology or bounds differ from source")
        polygons = [preview_shape] if preview_shape.geom_type == "Polygon" else list(preview_shape.geoms)
        vertices = sum(len(ring.coords) for polygon in polygons
                       for ring in (polygon.exterior, *polygon.interiors))
        if vertices > 512:
            raise ValueError("Selected preview exceeds coordinate budget")
        bounds = root["boundingVolume"]["region"]
        if any(abs(math.radians(bbox[index]) - bounds[index]) > 1e-10 for index in range(4)):
            raise ValueError("Selected model bounds differ from source polygon")
        flat_name = root["content"]["uri"]
        if flat_name not in file_index or len(_glb_document(file_index[flat_name])["meshes"][0]["primitives"]) != 1:
            raise ValueError("Flat exact-footprint parent is missing")
        height = row["height_evidence"]["render_height_m"]
        if height is None:
            flat += 1
            if root["children"] or row["depiction"] != "construction_outline":
                raise ValueError("Unknown height was converted into a volume")
        else:
            volume += 1
            if (not math.isfinite(height) or height <= 0 or len(root["children"]) != 1
                    or row["depiction"] != "schematic_massing"
                    or bounds[5] < height + 0.65):
                raise ValueError("Schematic massing has invalid height or LOD")
            volume_name = root["children"][0]["content"]["uri"]
            if volume_name not in file_index or len(_glb_document(file_index[volume_name])["meshes"][0]["primitives"]) != 2:
                raise ValueError("Schematic massing is missing roof/wall geometry")
    if (ready, volume, flat) != (756, 273, 483):
        raise ValueError("Pinned mapped geometry coverage changed")
    return {"run_id": manifest["run_id"], "records": len(rows), "decisions": counts,
            "ready_assets": ready, "schematic_volumes": volume, "flat_only": flat,
            "public_files": len(file_index), "public_raw_bytes": raw_bytes,
            "public_gzip5_bytes_sum": gzip_bytes,
            "manifest_sha256": file_sha(run_dir / "manifest.json")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--asset-root", type=Path, default=ASSETS)
    arguments = parser.parse_args()
    print(json.dumps(audit(arguments.run_dir, arguments.asset_root), ensure_ascii=False,
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
