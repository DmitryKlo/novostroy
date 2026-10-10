"""Create a checked, source-minimal API data release without an archive.

Dry run is the default. ``current-only`` is for a new origin without old
immutable URLs; ``full-compat`` retains the pointer's previous public roots.
Only this explicit runtime allowlist is copied. The generated data and assets
directories are intended for separate read-only container mounts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.entrypoint import ROUTING_INPUT_FILES, verify_routing_inputs
MODEL_RUN_RE = re.compile(r"models-v1-[0-9a-f]{16}")
DEVELOPER_RUN_RE = re.compile(r"developer-map-v1-[0-9a-f]{16}")
DEVELOPER_SNAPSHOT_RE = re.compile(r"developer-map-snapshot-[0-9a-f]{16}")
DEVELOPER_UNITS_RUN_RE = re.compile(r"developer-units-v1-[0-9a-f]{16}")
DEVELOPER_MODELS_RUN_RE = re.compile(r"developer-models-v1-[0-9a-f]{16}")
DEVELOPER_ENRICHMENT_RUN_RE = re.compile(r"developer-enrichment-v1-[0-9a-f]{16}")
VISIBILITY_RUN_RE = re.compile(r"visibility-v1-[0-9a-f]{16}")
GENERATION_RE = re.compile(r"generation-[0-9a-f]{16}")
SNAPSHOT_RE = re.compile(r"data/property/estateline/normalized/snapshots/estate-[0-9a-f]{16}")
MODEL_ROOT_RE = re.compile(r"public-project-models-v1-[0-9a-f]{16}")
PUBLIC_ROOT_RE = re.compile(r"public-[a-z0-9-]+-v[0-9]+")
REGION_RE = re.compile(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json")
MODEL_FILE_RE = re.compile(r"model_[0-9a-f]{64}(?:\.tileset\.json|\.glb)")
MAX_RELEASE_MANIFEST_BYTES = 64_000_000
LAYER_KEYS = ("city_tileset_url", "ground_tileset_url", "roads_tileset_url",
              "parks_tileset_url", "water_tileset_url")
DATA_FILES = (
    "data/property/estateline/projects.jsonl",
    "data/property/estateline/source_manifest.json",
    "data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.jsonl",
    "data/social/osm_moscow_social_pois_261008.parquet",
    "data/social/osm_moscow_social_pois_261008.manifest.json",
    "data/social/valhalla_moscow/valhalla_pedestrian.json",
    "data/social/valhalla_moscow/build_manifest.json",
    "data/social/oms_verified_clinics_2026.json",
    "data/social/oms_verified_clinics_2026.manifest.json",
    "data/social/metro_station_access_261008.json",
    "data/social/metro_station_access_261008.manifest.json",
    "data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json",
    "data/city/environment/moscow_district_candidates_2026-09-23.1.geojson",
    "data/city/environment/moscow_district_candidates_2026-09-23.1.manifest.json",
) + ROUTING_INPUT_FILES


@dataclass(frozen=True)
class PlannedFile:
    relative: str
    group: str
    bytes: int
    mtime_ns: int


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _line_count(path: Path) -> int:
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            count += chunk.count(b"\n")
    return count


def developer_map_files(source_root: Path) -> list[str]:
    """Pin and hash the active immutable developer building registry run."""
    pointer_relative = "data/property/developer_map/active.json"
    pointer = _json(_safe_source_file(source_root, pointer_relative))
    if (pointer.get("schema_version") != "developer-map-pointer/1"
            or set(pointer) != {"schema_version", "run_id", "manifest_sha256"}):
        raise ValueError("Invalid active developer-map pointer")
    run_id = pointer.get("run_id")
    manifest_sha = pointer.get("manifest_sha256")
    if (not isinstance(run_id, str) or DEVELOPER_RUN_RE.fullmatch(run_id) is None
            or not isinstance(manifest_sha, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None):
        raise ValueError("Invalid active developer-map identity")
    prefix = f"data/property/developer_map/runs/{run_id}"
    manifest_relative = f"{prefix}/manifest.json"
    manifest_path = _safe_source_file(source_root, manifest_relative)
    if manifest_path.stat().st_size > 100_000:
        raise ValueError("Developer-units manifest exceeds its byte limit")
    if _sha256_file(manifest_path) != manifest_sha:
        raise ValueError("Developer-map manifest differs from active pointer")
    manifest = _json(manifest_path)
    if (manifest.get("schema_version") != "developer-map-snapshot/1"
            or manifest.get("run_id") != run_id
            or not isinstance(manifest.get("snapshot_id"), str)
            or DEVELOPER_SNAPSHOT_RE.fullmatch(manifest["snapshot_id"]) is None):
        raise ValueError("Invalid active developer-map manifest")
    if (type(manifest.get("buildings_total")) is not int or manifest["buildings_total"] < 0
            or type(manifest.get("map_points_total")) is not int or manifest["map_points_total"] <= 0):
        raise ValueError("Active developer-map run has no eligible points")
    relatives = [pointer_relative, manifest_relative]
    for label, max_bytes in (("points", 100_000_000), ("groups", 20_000_000),
                             ("rejections", 50_000_000)):
        relative = f"{prefix}/{label}.jsonl"
        path = _safe_source_file(source_root, relative)
        expected_sha = manifest.get(f"{label}_sha256")
        expected_count = manifest.get(f"{label}_total")
        if (not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
                or type(expected_count) is not int or expected_count < 0
                or path.stat().st_size > max_bytes):
            raise ValueError(f"Invalid developer-map {label} descriptor")
        if _sha256_file(path) != expected_sha or _line_count(path) != expected_count:
            raise ValueError(f"Developer-map {label} data differs from active manifest")
        relatives.append(relative)
    return relatives


def developer_experience_files(source_root: Path) -> list[str]:
    """Return the single-pointer map/menu/model snapshot and prepared assets."""
    pointer_relative = "data/property/developer_experience/active.json"
    pointer_path = _safe_source_file(source_root, pointer_relative)
    if pointer_path.stat().st_size > 4096:
        raise ValueError("Developer-experience pointer exceeds its byte limit")
    pointer = _json(pointer_path)
    expected_keys = {
        "schema_version", "map_run_id", "map_snapshot_id", "map_manifest_sha256",
        "unit_run_id", "unit_manifest_sha256", "model_run_id", "model_manifest_sha256",
    }
    version = pointer.get("schema_version")
    if version == "developer-experience-pointer/2":
        expected_keys |= {"enrichment_run_id", "enrichment_manifest_sha256"}
    if (set(pointer) != expected_keys
            or version not in {"developer-experience-pointer/1", "developer-experience-pointer/2"}):
        raise ValueError("Invalid active developer-experience pointer")
    identity_patterns = {
        "map_run_id": DEVELOPER_RUN_RE,
        "map_snapshot_id": DEVELOPER_SNAPSHOT_RE,
        "unit_run_id": DEVELOPER_UNITS_RUN_RE,
        "model_run_id": DEVELOPER_MODELS_RUN_RE,
    }
    for key, pattern in identity_patterns.items():
        if not isinstance(pointer.get(key), str) or pattern.fullmatch(pointer[key]) is None:
            raise ValueError(f"Invalid developer-experience {key}")
    for key in ("map_manifest_sha256", "unit_manifest_sha256", "model_manifest_sha256"):
        if not isinstance(pointer.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", pointer[key]) is None:
            raise ValueError(f"Invalid developer-experience {key}")

    # The map reader retains its own pointer. The unified pointer must pin it
    # exactly, while menu and selected models have no independent active files.
    for stale_pointer in ("data/property/developer_units/active.json",
                          "data/property/developer_models/active.json",
                          "data/property/developer_enrichment/active.json"):
        if (source_root / stale_pointer).exists() or (source_root / stale_pointer).is_symlink():
            raise ValueError("Separate unit/model active pointers are not accepted")
    developer_map_files(source_root)
    map_pointer = _json(_safe_source_file(source_root, "data/property/developer_map/active.json"))
    map_manifest_relative = f"data/property/developer_map/runs/{pointer['map_run_id']}/manifest.json"
    map_manifest_path = _safe_source_file(source_root, map_manifest_relative)
    map_manifest = _json(map_manifest_path)
    if (map_pointer.get("run_id") != pointer["map_run_id"]
            or map_pointer.get("manifest_sha256") != pointer["map_manifest_sha256"]
            or map_manifest.get("snapshot_id") != pointer["map_snapshot_id"]
            or _sha256_file(map_manifest_path) != pointer["map_manifest_sha256"]):
        raise ValueError("Developer-experience pointer differs from active map")

    relatives = [pointer_relative]
    unit_run = pointer["unit_run_id"]
    unit_prefix = f"data/property/developer_units/runs/{unit_run}"
    unit_manifest_relative = f"{unit_prefix}/manifest.json"
    unit_manifest_path = _safe_source_file(source_root, unit_manifest_relative)
    if unit_manifest_path.stat().st_size > 100_000:
        raise ValueError("Developer-units manifest exceeds its byte limit")
    unit_manifest = _json(unit_manifest_path)
    if (_sha256_file(unit_manifest_path) != pointer["unit_manifest_sha256"]
            or unit_manifest.get("schema_version") != "developer-units-snapshot/1"
            or unit_manifest.get("run_id") != unit_run
            or unit_manifest.get("map_run_id") != pointer["map_run_id"]
            or unit_manifest.get("map_snapshot_id") != pointer["map_snapshot_id"]
            or unit_manifest.get("map_manifest_sha256") != pointer["map_manifest_sha256"]):
        raise ValueError("Developer-units snapshot differs from the experience pointer")
    relatives.append(unit_manifest_relative)

    unit_assets: dict[str, str] = {}
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    for label, max_bytes in (("buildings", 10_000_000), ("units", 120_000_000),
                             ("floors", 50_000_000), ("plans", 50_000_000),
                             ("unit_aliases", 10_000_000)):
        relative = f"{unit_prefix}/{label}.jsonl"
        path = _safe_source_file(source_root, relative)
        expected_sha = unit_manifest.get(f"{label}_sha256")
        expected_count = unit_manifest.get(f"{label}_total")
        if (not isinstance(expected_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
                or type(expected_count) is not int or expected_count < 0
                or path.stat().st_size > max_bytes
                or _sha256_file(path) != expected_sha
                or _line_count(path) != expected_count):
            raise ValueError(f"Invalid or changed developer-units {label} file")
        relatives.append(relative)
        if label == "plans":
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Invalid developer-units plan row")
                    asset_sha = row.get("asset_sha256")
                    if asset_sha is None:
                        continue
                    mime = row.get("asset_mime")
                    if (not isinstance(asset_sha, str)
                            or re.fullmatch(r"[0-9a-f]{64}", asset_sha) is None
                            or mime not in extensions):
                        raise ValueError("Invalid developer-units plan asset descriptor")
                    if asset_sha in unit_assets and unit_assets[asset_sha] != mime:
                        raise ValueError("Developer-units plan asset MIME conflict")
                    unit_assets[asset_sha] = mime
    for asset_sha, mime in sorted(unit_assets.items()):
        relative = f"{unit_prefix}/assets/{asset_sha}{extensions[mime]}"
        path = _safe_source_file(source_root, relative)
        if path.stat().st_size > 10_000_000 or _sha256_file(path) != asset_sha:
            raise ValueError("Developer-units plan asset differs from its SHA")
        with path.open("rb") as stream:
            signature = stream.read(12)
        valid = ((mime == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
                 or (mime == "image/jpeg" and signature.startswith(b"\xff\xd8\xff"))
                 or (mime == "image/webp" and signature.startswith(b"RIFF")
                     and signature[8:12] == b"WEBP"))
        if not valid:
            raise ValueError("Developer-units plan asset MIME differs from content")
        relatives.append(relative)

    model_run = pointer["model_run_id"]
    model_prefix = f"data/property/developer_models/runs/{model_run}"
    model_manifest_relative = f"{model_prefix}/manifest.json"
    model_manifest_path = _safe_source_file(source_root, model_manifest_relative)
    if model_manifest_path.stat().st_size > 100_000:
        raise ValueError("Developer-models manifest exceeds its byte limit")
    model_manifest = _json(model_manifest_path)
    if (_sha256_file(model_manifest_path) != pointer["model_manifest_sha256"]
            or model_manifest.get("schema_version") != "developer-models-snapshot/1"
            or model_manifest.get("run_id") != model_run
            or model_manifest.get("map_run_id") != pointer["map_run_id"]
            or model_manifest.get("map_snapshot_id") != pointer["map_snapshot_id"]
            or model_manifest.get("map_manifest_sha256") != pointer["map_manifest_sha256"]
            or model_manifest.get("menu_run_id") != unit_run
            or model_manifest.get("menu_manifest_sha256") != pointer["unit_manifest_sha256"]):
        raise ValueError("Selected models differ from the experience pointer")
    relatives.append(model_manifest_relative)
    models_relative = f"{model_prefix}/models.jsonl"
    models_path = _safe_source_file(source_root, models_relative)
    models_sha = model_manifest.get("models_sha256")
    models_total = model_manifest.get("models_total")
    if (not isinstance(models_sha, str) or re.fullmatch(r"[0-9a-f]{64}", models_sha) is None
            or type(models_total) is not int or models_total < 0
            or models_path.stat().st_size > 30_000_000
            or _sha256_file(models_path) != models_sha
            or _line_count(models_path) != models_total):
        raise ValueError("Invalid or changed selected-model rows")
    relatives.append(models_relative)
    model_assets: set[str] = set()
    with models_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            asset_sha = row.get("asset_sha256") if isinstance(row, dict) else None
            if (not isinstance(asset_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", asset_sha) is None
                    or type(row.get("asset_bytes")) is not int
                    or not 0 < row["asset_bytes"] <= 20_000_000
                    or asset_sha in model_assets):
                raise ValueError("Invalid or duplicate selected-model asset descriptor")
            model_assets.add(asset_sha)
            relative = f"{model_prefix}/assets/{asset_sha}.glb"
            path = _safe_source_file(source_root, relative)
            if path.stat().st_size != row["asset_bytes"] or _sha256_file(path) != asset_sha:
                raise ValueError("Selected model asset differs from its row descriptor")
            with path.open("rb") as asset_stream:
                header = asset_stream.read(12)
            if (not header.startswith(b"glTF") or len(header) < 12
                    or int.from_bytes(header[8:12], "little") != row["asset_bytes"]):
                raise ValueError("Selected model asset is not a complete GLB")
            relatives.append(relative)
    if version == "developer-experience-pointer/2":
        relatives.extend(developer_enrichment_files(source_root, pointer))
    return relatives


def developer_enrichment_files(source_root: Path, pointer: dict[str, Any],
                               hashes: dict[str, str] | None = None) -> list[str]:
    """Bind optional enrichment and prepared media to the exact experience.

    Lazy media caches and producer input snapshots are not runtime release
    assets. Existing menu assets stay in their already-pinned menu run.
    """
    run_id, manifest_sha = pointer.get("enrichment_run_id"), pointer.get("enrichment_manifest_sha256")
    if (not isinstance(run_id, str) or DEVELOPER_ENRICHMENT_RUN_RE.fullmatch(run_id) is None
            or not isinstance(manifest_sha, str) or re.fullmatch(r"[a-f0-9]{64}", manifest_sha) is None):
        raise ValueError("Invalid enrichment pointer identity")
    prefix = f"data/property/developer_enrichment/runs/{run_id}"
    relatives = [f"{prefix}/manifest.json"]
    def checked(relative: str, expected_sha: Any, limit: int) -> Path:
        path = _safe_source_file(source_root, relative)
        actual = hashes.get(relative) if hashes is not None else _sha256_file(path)
        if (not isinstance(expected_sha, str) or re.fullmatch(r"[a-f0-9]{64}", expected_sha) is None
                or path.stat().st_size > limit or actual != expected_sha):
            raise ValueError(f"Enrichment file missing, oversized or hash mismatch: {relative}")
        return path
    manifest = _json(checked(relatives[0], manifest_sha, 128_000))
    identity = {key: pointer[key] for key in (
        "map_run_id", "map_snapshot_id", "map_manifest_sha256", "unit_run_id",
        "unit_manifest_sha256", "model_run_id", "model_manifest_sha256")}
    if (manifest.get("schema_version") != "developer-enrichment-snapshot/1"
            or manifest.get("run_id") != run_id
            or any(manifest.get(key) != value for key, value in identity.items())):
        raise ValueError("Enrichment snapshot differs from the pinned experience")
    if any(value is not None and (not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None)
           for key, value in manifest.items() if key.endswith("_sha256")):
        raise ValueError("Invalid enrichment source evidence SHA")
    menu_prefix = f"data/property/developer_units/runs/{pointer['unit_run_id']}"
    menu = _json(_safe_source_file(source_root, f"{menu_prefix}/manifest.json"))
    if any(manifest.get(f"{key}_total") != menu.get(f"{key}_total") for key in ("buildings", "units")):
        raise ValueError("Enrichment counts differ from the pinned menu")
    for name, limit in (("buildings", 5_000_000), ("units", 90_000_000), ("media", 110_000_000)):
        relative = f"{prefix}/{name}.jsonl"
        path = checked(relative, manifest.get(f"{name}_sha256"), limit)
        count = manifest.get(f"{name}_total")
        if type(count) is not int or count < 0 or _line_count(path) != count:
            raise ValueError(f"Enrichment {name} row count differs from manifest")
        relatives.append(relative)
    menu_assets = set()
    with _safe_source_file(source_root, f"{menu_prefix}/plans.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("asset_sha256"):
                menu_assets.add((row["asset_sha256"], row.get("asset_mime")))
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    asset_inventory = manifest.get("assets", [])
    if not isinstance(asset_inventory, list):
        raise ValueError("Invalid prepared enrichment asset inventory")
    prepared = {}
    for asset in asset_inventory:
        if (not isinstance(asset, dict) or set(asset) != {"sha256", "mime", "bytes"}
                or not isinstance(asset["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]) is None
                or asset["mime"] not in extensions or type(asset["bytes"]) is not int
                or not 0 < asset["bytes"] <= 10_000_000 or asset["sha256"] in prepared):
            raise ValueError("Invalid prepared enrichment asset descriptor")
        prepared[asset["sha256"]] = asset
    used = set()
    with _safe_source_file(source_root, f"{prefix}/media.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid enrichment media row")
            digest, mime, size, source = (row.get(f"prepared_{key}") for key in ("sha256", "mime", "bytes", "source"))
            if all(value is None for value in (digest, mime, size, source)):
                continue
            if (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                    or mime not in extensions or type(size) is not int or not 0 < size <= 10_000_000):
                raise ValueError("Invalid enrichment prepared media reference")
            if source == "menu_asset" and (digest, mime) in menu_assets:
                asset_prefix = menu_prefix
            elif source == "enrichment_asset" and prepared.get(digest) == {"sha256": digest, "mime": mime, "bytes": size}:
                asset_prefix = prefix
                used.add(digest)
            else:
                raise ValueError("Unknown or unpinned enrichment prepared media source")
            relative = f"{asset_prefix}/assets/{digest}{extensions[mime]}"
            if checked(relative, digest, 10_000_000).stat().st_size != size:
                raise ValueError("Prepared enrichment media size differs from its reference")
            if source == "enrichment_asset":
                with _safe_source_file(source_root, relative).open("rb") as image:
                    signature = image.read(12)
                if not ((mime == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
                        or (mime == "image/jpeg" and signature.startswith(b"\xff\xd8\xff"))
                        or (mime == "image/webp" and signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")):
                    raise ValueError("Prepared enrichment asset MIME differs from bytes")
    if used != set(prepared):
        raise ValueError("Prepared enrichment asset inventory has unreferenced files")
    relatives.extend(f"{prefix}/assets/{sha}{extensions[prepared[sha]['mime']]}" for sha in sorted(used))
    return relatives


def _safe_source_file(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or "\\" in relative or "\0" in relative
            or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))):
        raise ValueError(f"Unsafe release path: {relative}")
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError(f"Unsafe release path: {relative}")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"Symlink in release path: {relative}")
    if not path.is_file():
        raise FileNotFoundError(f"Missing runtime file: {relative}")
    return path


def _public_root(root: Path, name: str, *, model: bool = False) -> list[str]:
    if not isinstance(name, str) or (MODEL_ROOT_RE.fullmatch(name) if model else
                                     PUBLIC_ROOT_RE.fullmatch(name)) is None:
        raise ValueError(f"Unsafe public root: {name}")
    folder = root / "assets" / name
    if folder.is_symlink() or not folder.is_dir():
        raise FileNotFoundError(f"Missing public root: {name}")
    manifest_name = "public_manifest.json" if model else "manifest.json"
    manifest = _json(_safe_source_file(root, f"assets/{name}/{manifest_name}"))
    if manifest.get("public_redacted_source_ids") is not True:
        raise ValueError(f"Unsanitized public root: {name}")
    listed: list[str] = []
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Unexpected directory or symlink in public root: {name}")
        basename = path.name
        if model:
            allowed = basename == manifest_name or MODEL_FILE_RE.fullmatch(basename)
        else:
            allowed = (basename in {"tileset.json", manifest_name}
                       or basename.endswith(".glb")
                       or REGION_RE.fullmatch(basename))
        if not allowed:
            raise ValueError(f"Unexpected public asset: {name}/{basename}")
        listed.append(f"assets/{name}/{basename}")
    if f"assets/{name}/tileset.json" not in listed and not model:
        raise ValueError(f"Public root has no tileset: {name}")
    actual = {PurePosixPath(relative).name for relative in listed}
    if model:
        records = manifest.get("files")
        if not isinstance(records, list) or not records:
            raise ValueError("Public model root has no file inventory")
        expected: set[str] = {manifest_name}
        for record in records:
            basename = record.get("path") if isinstance(record, dict) else None
            if not isinstance(basename, str) or MODEL_FILE_RE.fullmatch(basename) is None:
                raise ValueError("Invalid model asset inventory")
            expected.add(basename)
        if len(expected) != len(records) + 1 or actual != expected:
            raise ValueError("Model asset inventory differs from public root")
    else:
        # Every copied asset must be reachable from the public root tileset.
        # This catches stray data hidden behind an innocuous .glb extension.
        expected = {"tileset.json", manifest_name}
        pending = ["tileset.json"]
        visited: set[str] = set()
        while pending:
            document_name = pending.pop()
            if document_name in visited:
                continue
            visited.add(document_name)
            document = _json(_safe_source_file(root, f"assets/{name}/{document_name}"))
            nodes: list[Any] = [document]
            while nodes:
                node = nodes.pop()
                if isinstance(node, list):
                    nodes.extend(node)
                elif isinstance(node, dict):
                    uri = node.get("uri")
                    if uri is not None:
                        if (not isinstance(uri, str) or len(PurePosixPath(uri).parts) != 1
                                or not (uri.endswith(".glb") or REGION_RE.fullmatch(uri))):
                            raise ValueError(f"Unsafe tileset reference in {name}")
                        expected.add(uri)
                        if uri.endswith(".tileset.json"):
                            pending.append(uri)
                    nodes.extend(node.values())
        if actual != expected:
            raise ValueError(f"Tileset references differ from public root: {name}")
    return sorted(listed)


def _active_roots(pointer: dict[str, Any]) -> list[str]:
    roots: list[str] = []
    for key in LAYER_KEYS:
        value = pointer.get(key)
        if not isinstance(value, str) or not value.startswith("/assets/"):
            raise ValueError(f"Missing asset pointer: {key}")
        parts = PurePosixPath(value.removeprefix("/assets/")).parts
        if len(parts) != 2 or parts[1] != "tileset.json" or PUBLIC_ROOT_RE.fullmatch(parts[0]) is None:
            raise ValueError(f"Unsafe asset pointer: {key}")
        roots.append(parts[0])
    if len(set(roots)) != len(roots):
        raise ValueError("Duplicate active public roots")
    return roots


def make_plan(source_root: Path, profile: str) -> tuple[list[PlannedFile], dict[str, Any]]:
    if profile not in {"current-only", "full-compat"}:
        raise ValueError("Unknown release profile")
    source_root = source_root.resolve(strict=True)
    verify_routing_inputs(source_root)
    pointer = _json(_safe_source_file(source_root, "assets/manifest.json"))
    if pointer.get("public_redacted_source_ids") is not True:
        raise ValueError("Asset pointer lacks public redaction gate")
    active_roots = _active_roots(pointer)
    model_root = pointer.get("project_models_root")
    if not isinstance(model_root, str) or MODEL_ROOT_RE.fullmatch(model_root) is None:
        raise ValueError("Invalid active project model root")
    previous = pointer.get("previous_public_roots", [])
    if not isinstance(previous, list) or any(not isinstance(item, str) or
                                             (PUBLIC_ROOT_RE.fullmatch(item) is None and
                                              MODEL_ROOT_RE.fullmatch(item) is None)
                                             for item in previous):
        raise ValueError("Invalid compatibility roots")
    if len(set(previous)) != len(previous):
        raise ValueError("Duplicate compatibility root")
    release_pointer = dict(pointer)
    release_pointer["previous_public_roots"] = previous if profile == "full-compat" else []
    release_pointer["facade_models"] = 0  # No fictional facade assets in real release.

    normalized_files: list[str] = []
    generation_path = source_root / "data/property/estateline/normalized/active.json"
    generation_bundle: dict[str, Any] | None = None
    if generation_path.exists() or generation_path.is_symlink():
        generation_pointer = _json(_safe_source_file(
            source_root, "data/property/estateline/normalized/active.json"))
        generation_id = generation_pointer.get("generation_id")
        bundle_sha = generation_pointer.get("bundle_sha256")
        if (generation_pointer.get("schema_version") != "estate-generation-pointer/1"
                or not isinstance(generation_id, str) or GENERATION_RE.fullmatch(generation_id) is None
                or not isinstance(bundle_sha, str) or re.fullmatch(r"[0-9a-f]{64}", bundle_sha) is None
                or generation_id != f"generation-{bundle_sha[:16]}"):
            raise ValueError("Invalid normalized generation pointer")
        bundle_relative = f"data/property/estateline/normalized/generations/{generation_id}/bundle.json"
        bundle_path = _safe_source_file(source_root, bundle_relative)
        if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != bundle_sha:
            raise ValueError("Normalized generation bundle differs from pointer")
        generation_bundle = _json(bundle_path)
        snapshot = generation_bundle.get("normalized_snapshot")
        if (generation_bundle.get("schema_version") != "estate-generation/1"
                or not isinstance(snapshot, dict) or not isinstance(snapshot.get("path"), str)
                or SNAPSHOT_RE.fullmatch(snapshot["path"]) is None):
            raise ValueError("Invalid normalized generation snapshot")
        normalized_files = ["data/property/estateline/normalized/active.json", bundle_relative,
                            *(f"{snapshot['path']}/{name}" for name in
                              ("projects.jsonl", "source_manifest.json", "review_required.jsonl", "schema.json"))]

    active = (_json(_safe_source_file(source_root, "data/models/active.json"))
              if generation_bundle is None else generation_bundle.get("model"))
    run_id = active.get("run_id") if isinstance(active, dict) else None
    if not isinstance(run_id, str) or MODEL_RUN_RE.fullmatch(run_id) is None:
        raise ValueError("Invalid active model run")
    run_files = ("manifest.json", "assessments.jsonl", "candidates.jsonl",
                 "source_evaluations.jsonl")
    model_root = pointer.get("project_models_root") if generation_bundle is None else active.get("public_root")
    if not isinstance(model_root, str) or MODEL_ROOT_RE.fullmatch(model_root) is None:
        raise ValueError("Invalid pinned project model root")
    if generation_bundle is not None:
        release_pointer["project_models_root"] = model_root
        if profile == "full-compat":
            release_pointer["previous_public_roots"] = list(dict.fromkeys(
                [*previous, pointer["project_models_root"]]))
            release_pointer["previous_public_roots"] = [name for name in
                release_pointer["previous_public_roots"] if name != model_root]
    groups: dict[str, list[str]] = {
        "registry_and_point_labels": [DATA_FILES[0], DATA_FILES[1], DATA_FILES[2]],
        "developer_map": developer_map_files(source_root),
        "developer_experience": developer_experience_files(source_root),
        "nearby": list(DATA_FILES[3:11]),
        "coverage": [DATA_FILES[11]],
        "mapped_districts": list(DATA_FILES[12:14]),
        "routing_inputs": list(ROUTING_INPUT_FILES),
        "models_private": ([] if generation_bundle else ["data/models/active.json"])
        + [f"data/models/{run_id}/{name}" for name in run_files],
    }
    # Optional, private source-backed group proof is needed by the map reader
    # when a future model run publishes verified multi-building memberships.
    group_evidence_file = f"data/models/{run_id}/map_point_groups.private.json"
    group_evidence_path = source_root / group_evidence_file
    if group_evidence_path.exists() or group_evidence_path.is_symlink():
        _safe_source_file(source_root, group_evidence_file)
        groups["models_private"].append(group_evidence_file)
    if normalized_files:
        groups["registry_generation"] = normalized_files
    visibility_pointer_path = source_root / "data/property/estateline/visibility/active.json"
    if generation_bundle is not None:
        pinned_visibility = generation_bundle.get("visibility")
        visibility_run = pinned_visibility.get("run_id") if isinstance(pinned_visibility, dict) else None
        if not isinstance(visibility_run, str) or VISIBILITY_RUN_RE.fullmatch(visibility_run) is None:
            raise ValueError("Invalid pinned registry visibility run")
        prefix = f"data/property/estateline/visibility/runs/{visibility_run}"
        groups["registry_visibility"] = [f"{prefix}/manifest.json", f"{prefix}/records.jsonl"]
    elif visibility_pointer_path.exists() or visibility_pointer_path.is_symlink():
        visibility_pointer = _json(_safe_source_file(
            source_root, "data/property/estateline/visibility/active.json"))
        visibility_run = visibility_pointer.get("run_id")
        visibility_hash = visibility_pointer.get("manifest_sha256")
        if (not isinstance(visibility_run, str) or VISIBILITY_RUN_RE.fullmatch(visibility_run) is None
                or not isinstance(visibility_hash, str) or re.fullmatch(r"[0-9a-f]{64}", visibility_hash) is None):
            raise ValueError("Invalid registry visibility pointer")
        prefix = f"data/property/estateline/visibility/runs/{visibility_run}"
        visibility_manifest = _safe_source_file(source_root, f"{prefix}/manifest.json")
        if hashlib.sha256(visibility_manifest.read_bytes()).hexdigest() != visibility_hash:
            raise ValueError("Registry visibility manifest differs from pointer")
        groups["registry_visibility"] = [
            "data/property/estateline/visibility/active.json",
            f"{prefix}/manifest.json", f"{prefix}/records.jsonl",
        ]
    tile_folder = source_root / "data/social/valhalla_moscow/tiles"
    if tile_folder.is_symlink() or not tile_folder.is_dir():
        raise FileNotFoundError("Valhalla tile directory is missing")
    tile_entries = sorted(tile_folder.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in tile_entries):
        raise ValueError("Valhalla tile tree contains unsafe entries")
    tile_paths = [path for path in tile_entries if path.is_file()]
    if not tile_paths:
        raise ValueError("Valhalla tile tree is empty or contains non-files")
    groups["nearby"].extend(str(path.relative_to(source_root)) for path in tile_paths)
    for key, name in zip(LAYER_KEYS, active_roots, strict=True):
        groups[key.removesuffix("_tileset_url")] = _public_root(source_root, name)
    groups["models_assets"] = _public_root(source_root, model_root, model=True)
    for name in release_pointer["previous_public_roots"]:
        if name in active_roots or name == model_root:
            raise ValueError("Compatibility root duplicates active root")
        is_model = MODEL_ROOT_RE.fullmatch(name) is not None
        groups[f"compat:{name}"] = _public_root(source_root, name, model=is_model)
        if is_model:
            old_run = "models-v1-" + name.removeprefix("public-project-models-v1-")
            groups[f"compat-model-meta:{name}"] = [f"data/models/{old_run}/manifest.json"]
    groups["pointer"] = ["assets/manifest.json"]

    planned: list[PlannedFile] = []
    seen: set[str] = set()
    for group, relatives in groups.items():
        for relative in relatives:
            if relative in seen:
                raise ValueError(f"Duplicate runtime path: {relative}")
            seen.add(relative)
            stat = _safe_source_file(source_root, relative).stat()
            planned.append(PlannedFile(relative, group, stat.st_size, stat.st_mtime_ns))
    if len(planned) > 100_000:
        raise ValueError("Unexpectedly large release file count")
    return sorted(planned, key=lambda item: item.relative), release_pointer


def validate_current_source(source_root: Path) -> None:
    """Run existing data gates before export; no network, server, or large copy."""
    if source_root.resolve() != ROOT:
        return  # Test fixtures use the same structural checks in make_plan.
    sys.path.insert(0, str(ROOT))
    from backend.models import load_model_run
    from backend.nearby import _graph_version
    from backend.point_labels import load_point_labels
    from backend.registry import load_registry
    from backend.developer_map import resolve_active as resolve_developer_map
    from backend.developer_experience import resolve_active as resolve_developer_experience

    registry = load_registry()
    run = load_model_run()
    load_point_labels(run)
    developer_map = resolve_developer_map()
    if developer_map is None or not developer_map["by_point"]:
        raise ValueError("Active developer building map is missing or empty")
    if resolve_developer_experience(developer_map) is None:
        raise ValueError("Active developer experience is missing or empty")
    if not registry["items"] or not run["items"]:
        raise ValueError("Registry or model release is empty")
    if _graph_version() in {"invalid", "missing"}:
        raise ValueError("Nearby graph manifest is invalid")


def _copy_hash(source: Path, target: Path, expected: PlannedFile) -> str:
    before = source.stat()
    if (before.st_size, before.st_mtime_ns) != (expected.bytes, expected.mtime_ns):
        raise ValueError(f"Source changed before copy: {expected.relative}")
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as stream, target.open("xb") as output:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    after = source.stat()
    if (after.st_size, after.st_mtime_ns) != (expected.bytes, expected.mtime_ns):
        raise ValueError(f"Source changed during copy: {expected.relative}")
    target.chmod(0o644)
    return digest.hexdigest()


def apply_plan(source_root: Path, output: Path, profile: str,
               planned: list[PlannedFile], pointer: dict[str, Any], *,
               base_release: Path | None = None,
               base_manifest_sha256: str | None = None) -> Path:
    source_root = source_root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Release output already exists: {output}")
    if output.is_relative_to(source_root / "data") or output.is_relative_to(source_root / "assets"):
        raise ValueError("Release output cannot be inside runtime inputs")
    output.parent.mkdir(parents=True, exist_ok=True)
    base_files: dict[str, dict[str, Any]] = {}
    if (base_release is None) != (base_manifest_sha256 is None):
        raise ValueError("Base release and its manifest SHA must be supplied together")
    if base_release is not None:
        from scripts.runtime_delta import checked_base
        base_release, base_manifest = checked_base(base_release, base_manifest_sha256)
        base_files = {row["path"]: row for row in base_manifest["files"]}
    staged = Path(tempfile.mkdtemp(prefix=".deploy-release-", dir=output.parent))
    try:
        records: list[dict[str, Any]] = []
        for item in planned:
            target = staged / item.relative
            if item.relative == "assets/manifest.json":
                target.parent.mkdir(parents=True, exist_ok=True)
                body = (json.dumps(pointer, ensure_ascii=False, sort_keys=True,
                                   indent=2) + "\n").encode("utf-8")
                target.write_bytes(body)
                target.chmod(0o644)
                digest = hashlib.sha256(body).hexdigest()
                size = len(body)
            else:
                source = _safe_source_file(source_root, item.relative)
                base_row = base_files.get(item.relative)
                # Verify current source bytes before reusing immutable base
                # files. Never open or chmod a linked base inode for writing.
                if base_row is not None and base_row["bytes"] == item.bytes:
                    before = source.stat()
                    digest = _sha256_file(source)
                    after = source.stat()
                    if ((before.st_size, before.st_mtime_ns) != (item.bytes, item.mtime_ns)
                            or (after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns)
                            != (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns)):
                        raise ValueError(f"Source changed before reuse: {item.relative}")
                    if digest == base_row["sha256"]:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.link(_safe_source_file(base_release, item.relative), target,
                                follow_symlinks=False)
                    else:
                        digest = _copy_hash(source, target, item)
                else:
                    digest = _copy_hash(source, target, item)
                size = item.bytes
            records.append({"path": item.relative, "sha256": digest,
                            "bytes": size, "group": item.group})
        (staged / "data/runtime").mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": "novostroy-deploy-data/1",
            "profile": profile,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_mode": "real",
            "registry_mode": "estateline",
            "previous_public_roots_excluded": [] if profile == "full-compat" else
            _json(_safe_source_file(source_root, "assets/manifest.json")).get("previous_public_roots", []),
            "file_count": len(records),
            "bytes_total": sum(record["bytes"] for record in records),
            "files": records,
        }
        (staged / "release-manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        (staged / "release-manifest.json").chmod(0o644)
        from deploy.apply_release import verify_release as verify_deploy_release
        verify_deploy_release(staged)
        from scripts.runtime_transport import _rename_noreplace
        _rename_noreplace(staged, output)
    except BaseException:
        shutil.rmtree(staged)
        raise
    return output


def verify_release(output: Path) -> dict[str, Any]:
    """Check every copied byte and reject extra files or links before upload."""
    output = output.resolve(strict=True)
    manifest_path = output / "release-manifest.json"
    if (not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_RELEASE_MANIFEST_BYTES):
        raise ValueError("Release manifest is missing or oversized")
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != "novostroy-deploy-data/1" or manifest.get(
            "profile") not in {"current-only", "full-compat"}:
        raise ValueError("Unsupported release manifest")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != manifest.get("file_count"):
        raise ValueError("Invalid release file inventory")
    expected: set[str] = {"release-manifest.json"}
    total = 0
    for row in records:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes", "group"}:
            raise ValueError("Invalid release file record")
        relative, digest, size = row["path"], row["sha256"], row["bytes"]
        if (not isinstance(relative, str) or relative in expected
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(size, int) or size < 0):
            raise ValueError("Invalid or duplicate release file")
        path = _safe_source_file(output, relative)
        if path.stat().st_size != size:
            raise ValueError(f"Release file size mismatch: {relative}")
        actual = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise ValueError(f"Release file hash mismatch: {relative}")
        expected.add(relative)
        total += size
    if total != manifest.get("bytes_total"):
        raise ValueError("Release total size mismatch")
    for path in output.rglob("*"):
        if path.is_symlink():
            raise ValueError("Release contains a symlink")
        if path.is_file() and str(path.relative_to(output)) not in expected:
            raise ValueError(f"Unlisted release file: {path.relative_to(output)}")
    pointer = _json(output / "assets/manifest.json")
    if pointer.get("public_redacted_source_ids") is not True:
        raise ValueError("Release pointer is not sanitized")
    if manifest["profile"] == "current-only" and pointer.get("previous_public_roots") != []:
        raise ValueError("Current-only release exposes compatibility roots")
    if pointer.get("facade_models") != 0:
        raise ValueError("Real release advertises fictional facades")
    required_developer_map = developer_map_files(output)
    if not set(required_developer_map).issubset(expected):
        raise ValueError("Release omits the active developer-map run")
    required_experience = developer_experience_files(output)
    if not set(required_experience).issubset(expected):
        raise ValueError("Release omits the active developer experience")
    verify_routing_inputs(output, {row["path"]: row["sha256"] for row in records})
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--profile", choices=("current-only", "full-compat"),
                        default="full-compat")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-release", type=Path,
                        help="reuse byte-identical files from a pinned immutable release")
    parser.add_argument("--base-manifest-sha256")
    parser.add_argument("--apply", action="store_true", help="copy after dry-run review")
    parser.add_argument("--verify", action="store_true", help="verify an existing release")
    args = parser.parse_args()
    if (args.apply or args.verify) and args.output is None:
        parser.error("--apply and --verify require --output")
    if args.apply and args.verify:
        parser.error("--apply and --verify are separate operations")
    if (args.base_release is None) != (args.base_manifest_sha256 is None):
        parser.error("--base-release requires --base-manifest-sha256 and vice versa")
    if args.verify and args.base_release is not None:
        parser.error("--verify does not accept a base release")
    if args.verify:
        manifest = verify_release(args.output)
        print(json.dumps({"verified": True, "profile": manifest["profile"],
                          "files": manifest["file_count"],
                          "bytes": manifest["bytes_total"]}, sort_keys=True))
        return 0
    planned, pointer = make_plan(args.source_root, args.profile)
    groups: dict[str, list[int]] = defaultdict(list)
    for item in planned:
        groups[item.group].append(item.bytes)
    print(json.dumps({"profile": args.profile, "files": len(planned),
                      "estimated_bytes": sum(item.bytes for item in planned),
                      "groups": {group: {"files": len(sizes), "bytes": sum(sizes)}
                                 for group, sizes in sorted(groups.items())},
                      "excluded_previous_public_roots": [] if args.profile == "full-compat" else
                      _json(_safe_source_file(args.source_root.resolve(), "assets/manifest.json"))
                      .get("previous_public_roots", [])},
                     ensure_ascii=False, sort_keys=True, indent=2))
    if args.apply:
        validate_current_source(args.source_root)
        path = apply_plan(args.source_root, args.output, args.profile, planned, pointer,
                          base_release=args.base_release,
                          base_manifest_sha256=args.base_manifest_sha256)
        print(f"Release ready: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
