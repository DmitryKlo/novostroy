"""Export only hash-pinned procedural style descriptors and generated PNGs."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct
from typing import Any

from scripts.runtime_overlay import SHA_RE, _json, _regular_file
from sources.property.developer_facade_styles import validate_material_record


STYLE_KEYS = {"style_batch_manifest_sha256", "styles_materials_sha256",
              "styles_materials_total", "styles_texture_inventory_sha256",
              "styles_textures_total", "styled_models_total"}
STYLE_RE = re.compile(r"style_[0-9a-f]{24}\Z")
TEXTURE_RE = re.compile(r"styles/textures/([0-9a-f]{64})\.png\Z")
MODEL_RUN_RE = re.compile(r"developer-models-v1-[0-9a-f]{16}\Z")


def developer_model_style_files(root: Path, prefix: str, manifest: dict[str, Any],
                                hashes: dict[str, str] | None = None) -> list[str]:
    if not STYLE_KEYS.intersection(manifest):
        return []
    if (not STYLE_KEYS.issubset(manifest)
            or any(not isinstance(manifest[key], str) or SHA_RE.fullmatch(manifest[key]) is None
                   for key in STYLE_KEYS if key.endswith("_sha256"))
            or any(type(manifest[key]) is not int or not 0 < manifest[key] <= 10_000
                   for key in STYLE_KEYS if key.endswith("_total"))):
        raise ValueError("Invalid selected-model style manifest")
    files: list[str] = []

    def checked(name: str, expected_sha: str, limit: int) -> Path:
        relative = prefix.rstrip("/") + "/" + name
        path = _regular_file(root, relative)
        if path.stat().st_size > limit:
            raise ValueError("Oversized selected-model style file")
        digest = hashes.get(relative) if hashes is not None else hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha:
            raise ValueError("Selected-model style file hash mismatch")
        if relative not in files:
            files.append(relative)
        return path

    materials = [_json(line) for line in checked("styles/materials.jsonl",
        manifest["styles_materials_sha256"], 10_000_000).read_bytes().splitlines()]
    inventory = [_json(line) for line in checked("styles/texture-inventory.jsonl",
        manifest["styles_texture_inventory_sha256"], 10_000_000).read_bytes().splitlines()]
    if (len(materials) != manifest["styles_materials_total"]
            or len(inventory) != manifest["styles_textures_total"]):
        raise ValueError("Selected-model style inventory count differs")
    by_style: dict[str, dict[str, Any]] = {}
    project_ids: set[str] = set()
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for material in materials:
        if not isinstance(material, dict) or set(material) != {
                "schema_version", "renderer", "style_id", "publisher_project_id", "basis", "label",
                "geometry_scope", "rights_status", "lods", "source_evidence_sha256"}:
            raise ValueError("Selected style descriptor contains unsupported or private fields")
        validate_material_record(material)
        style_id = material["style_id"]
        evidence = material["source_evidence_sha256"]
        if (not isinstance(style_id, str) or STYLE_RE.fullmatch(style_id) is None
                or style_id in by_style or material["publisher_project_id"] in project_ids
                or not isinstance(evidence, str) or SHA_RE.fullmatch(evidence) is None
                or material["rights_status"] != "unknown"):
            raise ValueError("Invalid or duplicate selected style identity/evidence")
        by_style[style_id] = material
        project_ids.add(material["publisher_project_id"])
        for lod in material["lods"]:
            if set(lod) != {"level", "width", "height", "mime_type", "sha256", "asset_uri", "asset_path"}:
                raise ValueError("Unsupported selected style LOD fields")
            expected[(style_id, lod["level"])] = lod
    seen = set()
    descriptors: dict[str, tuple[str, int]] = {}
    for row in inventory:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes", "style_id", "level"}:
            raise ValueError("Invalid selected style texture row")
        key = (row["style_id"], row["level"])
        lod = expected.get(key)
        match = TEXTURE_RE.fullmatch(row["path"]) if isinstance(row["path"], str) else None
        if (match is None or row["sha256"] != match[1] or key in seen or lod is None
                or type(row["bytes"]) is not int or not 0 < row["bytes"] <= 8_000_000
                or row["sha256"] != lod["sha256"] or row["path"] != "styles/" + lod["asset_path"]):
            raise ValueError("Selected style texture differs from its material LOD")
        seen.add(key)
        descriptor = (row["sha256"], row["bytes"])
        if row["path"] in descriptors and descriptors[row["path"]] != descriptor:
            raise ValueError("Conflicting selected style texture references")
        descriptors[row["path"]] = descriptor
        path = checked(row["path"], row["sha256"], 8_000_000)
        with path.open("rb") as stream:
            header = stream.read(33)
        if (path.stat().st_size != row["bytes"] or len(header) != 33
                or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                or struct.unpack(">II", header[16:24]) != (lod["width"], lod["height"])):
            raise ValueError("Selected style PNG dimensions or bytes differ")
    if seen != set(expected):
        raise ValueError("Selected style inventory omits a material LOD")
    models = [_json(line) for line in _regular_file(root, prefix.rstrip("/") + "/models.jsonl")
              .read_bytes().splitlines()]
    styled = [row for row in models if row.get("style_id") is not None]
    if len(styled) != manifest["styled_models_total"]:
        raise ValueError("Styled model count differs from its manifest")
    for row in styled:
        material = by_style.get(row["style_id"])
        if (material is None or row.get("source_project_id") != material["publisher_project_id"]
                or row.get("facade_style_basis") != "project_style_inference"
                or row.get("texture_basis") != "generated_project_style_repeat_unoriented"
                or row.get("facade_style_evidence_sha256") != material["source_evidence_sha256"]
                or row.get("texture_sha256") != expected[(row["style_id"], "closer")]["sha256"]):
            raise ValueError("Selected model style differs from its exact project material")
    files.extend(_baseline_files(root, manifest, models, hashes))
    return files


def _baseline_files(root: Path, manifest: dict[str, Any], models: list[dict[str, Any]],
                    hashes: dict[str, str] | None) -> list[str]:
    """One explicit unstyled baseline dependency; never recursively copy old runs."""
    run_id, digest = manifest.get("base_model_run_id"), manifest.get("base_model_manifest_sha256")
    if (not isinstance(run_id, str) or MODEL_RUN_RE.fullmatch(run_id) is None
            or run_id == manifest.get("run_id") or not isinstance(digest, str)
            or SHA_RE.fullmatch(digest) is None):
        raise ValueError("Styled selected models lack a pinned immutable baseline")
    prefix = f"data/property/developer_models/runs/{run_id}"
    files = []
    def checked(name: str, expected: str, limit: int) -> Path:
        relative = prefix + "/" + name
        path = _regular_file(root, relative)
        actual = hashes.get(relative) if hashes is not None else hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size > limit or actual != expected:
            raise ValueError("Styled model baseline hash or size differs")
        files.append(relative)
        return path
    baseline = _json(checked("manifest.json", digest, 100_000).read_bytes())
    if (not isinstance(baseline, dict) or baseline.get("run_id") != run_id
            or baseline.get("schema_version") != "developer-models-snapshot/1"
            or baseline.get("styled_models_total", 0) != 0
            or baseline.get("base_model_run_id") is not None
            or baseline.get("base_model_manifest_sha256") is not None
            or any(baseline.get(key) != manifest.get(key) for key in (
                "map_run_id", "map_snapshot_id", "map_manifest_sha256", "menu_run_id",
                "menu_manifest_sha256", "delivery_manifest_sha256"))):
        raise ValueError("Styled model baseline differs from pinned map/menu/source or is not unstyled")
    rows = [_json(line) for line in checked("models.jsonl", baseline.get("models_sha256"),
                                          30_000_000).read_bytes().splitlines()]
    by_registry = {row.get("registry_id"): row for row in rows if isinstance(row, dict)}
    if (len(by_registry) != len(rows) or baseline.get("models_total") != len(rows)
            or set(by_registry) != {row.get("registry_id") for row in models}):
        raise ValueError("Styled model baseline changes the selected building set")
    invariant = ("model_transform", "height_m", "height_basis", "source_total_floors",
                 "source_section_total_floors", "source_geometry_basis", "source_geometry_evidence_sha256",
                 "source_observed_at", "depiction", "render_policy", "triangles", "courtyards")
    for row in models:
        if any(row.get(key) != by_registry[row["registry_id"]].get(key) for key in invariant):
            raise ValueError("Styled model changed source geometry or height")
    asset_ids = set()
    byte_total = 0
    for row in rows:
        sha, size = row.get("asset_sha256"), row.get("asset_bytes")
        if (not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None or sha in asset_ids
                or type(size) is not int or not 0 < size <= 20_000_000):
            raise ValueError("Invalid styled-model baseline asset descriptor")
        path = checked("assets/" + sha + ".glb", sha, 20_000_000)
        with path.open("rb") as stream:
            header = stream.read(12)
        if (path.stat().st_size != size or len(header) != 12 or header[:4] != b"glTF"
                or int.from_bytes(header[8:12], "little") != size):
            raise ValueError("Invalid styled-model baseline GLB")
        asset_ids.add(sha)
        byte_total += size
    if baseline.get("assets_total") != len(asset_ids) or baseline.get("assets_bytes") != byte_total:
        raise ValueError("Styled model baseline asset totals differ")
    return files
