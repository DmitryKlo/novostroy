"""Validate one immutable, experience-pinned building enrichment snapshot.

The enrichment is an overlay on exact active map and apartment identities. It
cannot make a project marker into a building point or attach a source media URL
to a different apartment merely because the names or coordinates are close.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.store import ROOT


BASE = ROOT / "data/property/developer_enrichment"
RUN_RE = re.compile(r"developer-enrichment-v1-[a-f0-9]{16}\Z")
SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
MEDIA_RE = re.compile(r"media_[a-f0-9]{24}\Z")
ALLOWED_MEDIA_HOSTS = frozenset({"cdn-dataout.trendagent.ru", "cdn.a101.ru", "cdn.fsk.ru"})
MAX_FILE_BYTES = {"buildings": 5_000_000, "units": 90_000_000,
                  "media": 110_000_000}


def _safe_file(path: Path, limit: int) -> Path:
    if not path.is_relative_to(BASE):
        raise ValueError("Developer enrichment file escapes its root")
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        if part in {"", ".", ".."} or part.startswith("."):
            raise ValueError("Unsafe developer enrichment path")
        current /= part
        if current.is_symlink():
            raise ValueError("Developer enrichment path contains a symlink")
    if not current.is_file() or current.stat().st_size > limit:
        raise ValueError("Developer enrichment file is missing or too large")
    return current


def _sha(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    with _safe_file(path, limit).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pinned_manifest_sha(path: Path) -> str:
    # These three paths have already passed their owners' immutable-run
    # validators. Rehash them here to bind the enrichment to their exact bytes.
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
        raise ValueError("Pinned experience manifest is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, limit: int) -> list[dict[str, Any]]:
    output = []
    with _safe_file(path, limit).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid developer enrichment row")
            output.append(row)
    return output


def _file_signature(path: Path, limit: int) -> tuple[int, int]:
    file = _safe_file(path, limit)
    stat = file.stat()
    return stat.st_size, stat.st_mtime_ns


def _valid_media_url(row: dict[str, Any]) -> bool:
    url = row.get("source_url")
    if not isinstance(url, str) or len(url) > 2048:
        return False
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in ALLOWED_MEDIA_HOSTS
            and parsed.hostname == row.get("allowed_host")
            and parsed.username is None and parsed.password is None
            and parsed.port is None and not parsed.fragment)


def _valid_air_model(model: Any) -> bool:
    """A district sample is a dated grid forecast, never a sensor reading."""
    if model is None:
        return True
    if (not isinstance(model, dict) or
            model.get("model_basis") != "open_meteo_cams_europe_grid_cell_forecast" or
            model.get("sample_scope") !=
            "requested_district_candidate_point_to_returned_grid_cell" or
            model.get("measured_district_average") is not False or
            not isinstance(model.get("attribution"), str) or
            not model["attribution"] or
            not isinstance(model.get("source_response_sha256"), str) or
            SHA_RE.fullmatch(model["source_response_sha256"]) is None):
        return False
    for key in ("forecast_at", "retrieved_at"):
        try:
            stamp = datetime.fromisoformat(model[key].replace("Z", "+00:00"))
        except (KeyError, AttributeError, TypeError, ValueError):
            return False
        if stamp.tzinfo is None:
            return False
    for key in ("requested_point_lon_lat", "returned_grid_center_lon_lat"):
        pair = model.get(key)
        if (not isinstance(pair, list) or len(pair) != 2 or
                any(type(value) not in {int, float} or not math.isfinite(value)
                    for value in pair) or
                not -180 <= pair[0] <= 180 or not -90 <= pair[1] <= 90):
            return False
    return all(type(model.get(key)) in {int, float} and
               math.isfinite(model[key]) and 0 <= model[key] <= 1000
               for key in ("pm2_5_ug_m3", "pm10_ug_m3", "no2_ug_m3"))


@lru_cache(maxsize=3)
def _validated(run_id: str, expected_manifest_sha256: str,
               signatures: tuple[tuple[int, int], ...],
               map_id: str, map_snapshot_id: str, map_manifest_sha256: str,
               unit_id: str, unit_manifest_sha256: str,
               model_id: str, model_manifest_sha256: str,
               registry_ids: frozenset[str],
               unit_bindings: frozenset[tuple[str, str]],
               floor_bindings: frozenset[tuple[str, str]]) -> dict[str, Any]:
    del signatures  # included in the cache key so changed immutable files revalidate
    run = BASE / "runs" / run_id
    manifest_path = run / "manifest.json"
    if _sha(manifest_path, 128_000) != expected_manifest_sha256:
        raise ValueError("Developer enrichment manifest hash differs from pointer")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "developer-enrichment-snapshot/1":
        raise ValueError("Invalid developer enrichment manifest")
    pinned = {"run_id": run_id, "map_run_id": map_id,
              "map_snapshot_id": map_snapshot_id,
              "map_manifest_sha256": map_manifest_sha256,
              "unit_run_id": unit_id,
              "unit_manifest_sha256": unit_manifest_sha256,
              "model_run_id": model_id,
              "model_manifest_sha256": model_manifest_sha256}
    if any(manifest.get(key) != value for key, value in pinned.items()):
        raise ValueError("Developer enrichment pins a different experience")
    data = {}
    for name in ("buildings", "units", "media"):
        path = run / f"{name}.jsonl"
        expected = manifest.get(f"{name}_sha256")
        if not isinstance(expected, str) or SHA_RE.fullmatch(expected) is None:
            raise ValueError("Invalid developer enrichment file hash")
        if _sha(path, MAX_FILE_BYTES[name]) != expected:
            raise ValueError("Developer enrichment file hash differs from manifest")
        rows = _rows(path, MAX_FILE_BYTES[name])
        if manifest.get(f"{name}_total") != len(rows):
            raise ValueError("Developer enrichment row count differs from manifest")
        data[name] = rows
    buildings: dict[str, dict[str, Any]] = {}
    for row in data["buildings"]:
        key = row.get("registry_id")
        if key not in registry_ids or key in buildings:
            raise ValueError("Developer enrichment building identity mismatch")
        scope = row.get("municipality_scope")
        if (scope not in {"inside", "outside", "uncertain"}
                or type(row.get("visibility_eligible")) is not bool
                or (row["visibility_eligible"] is False and
                    (scope != "outside" or not row.get("geography_basis")))):
            raise ValueError("Developer enrichment visibility has no boundary basis")
        noise = row.get("noise_index")
        if noise is not None and (type(noise) is not int or not 0 <= noise <= 100):
            raise ValueError("Developer enrichment noise index is invalid")
        if not _valid_air_model(row.get("air_model")):
            raise ValueError("Developer enrichment air model is invalid")
        buildings[key] = row
    if set(buildings) != registry_ids:
        raise ValueError("Developer enrichment omits active buildings")
    unit_ids = {unit_id for unit_id, _ in unit_bindings}
    units: dict[str, dict[str, Any]] = {}
    for row in data["units"]:
        key = row.get("unit_id")
        if ((key, row.get("registry_id")) not in unit_bindings or key in units):
            raise ValueError("Developer enrichment unit identity mismatch")
        units[key] = row
    if set(units) != unit_ids:
        raise ValueError("Developer enrichment omits active apartments")
    media: dict[str, dict[str, Any]] = {}
    by_registry: dict[str, list[dict[str, Any]]] = {}
    by_unit: dict[str, list[dict[str, Any]]] = {}
    by_floor: dict[str, list[dict[str, Any]]] = {}
    for row in data["media"]:
        key = row.get("media_id")
        registry_id = row.get("registry_id")
        unit = row.get("unit_id")
        floor = row.get("floor_id")
        scope = row.get("scope")
        if (not isinstance(key, str) or MEDIA_RE.fullmatch(key) is None
                or key in media or registry_id not in buildings
                or not _valid_media_url(row)
                or row.get("display_policy") != "user_authorized_source_bound_preview/1"
                or row.get("rights_status") not in {
                    "unknown", "source_reuse_unconfirmed_private_research_only"}):
            raise ValueError("Developer enrichment media identity or source is unsafe")
        if scope == "unit_plan":
            if (unit, registry_id) not in unit_bindings or floor is not None:
                raise ValueError("Unit media has no exact apartment binding")
            by_unit.setdefault(unit, []).append(row)
        elif scope == "floor_plan":
            if (floor, registry_id) not in floor_bindings or unit is not None:
                raise ValueError("Floor media has no exact floor binding")
            by_floor.setdefault(floor, []).append(row)
        elif scope in {"project_gallery", "project_gallery_single_corpus"}:
            if unit is not None or floor is not None:
                raise ValueError("Project gallery cannot be a unit/floor asset")
            by_registry.setdefault(registry_id, []).append(row)
        else:
            raise ValueError("Unknown developer enrichment media scope")
        media[key] = row
    return {"manifest": manifest, "buildings": buildings, "units": units,
            "media": media, "media_by_registry": by_registry,
            "media_by_unit": by_unit, "media_by_floor": by_floor,
            "run_path": run}


def load_staged(run_id: str, map_run: dict[str, Any], menu: dict[str, Any],
                models: dict[str, Any], *,
                expected_manifest_sha256: str) -> dict[str, Any]:
    if (not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None
            or not isinstance(expected_manifest_sha256, str)
            or SHA_RE.fullmatch(expected_manifest_sha256) is None):
        raise ValueError("Invalid developer enrichment pointer identity")
    run = BASE / "runs" / run_id
    files = ((run / "manifest.json", 128_000),
             *((run / f"{name}.jsonl", MAX_FILE_BYTES[name])
               for name in ("buildings", "units", "media")))
    signatures = tuple(_file_signature(path, limit) for path, limit in files)
    map_manifest = map_run["manifest"]
    unit_manifest = menu["manifest"]
    model_manifest = models["manifest"]
    return _validated(
        run_id, expected_manifest_sha256, signatures,
        map_manifest["run_id"], map_manifest["snapshot_id"],
        _pinned_manifest_sha(ROOT / "data/property/developer_map/runs" /
                             map_manifest["run_id"] / "manifest.json"),
        unit_manifest["run_id"],
        _pinned_manifest_sha(ROOT / "data/property/developer_units/runs" /
                             unit_manifest["run_id"] / "manifest.json"),
        model_manifest["run_id"],
        _pinned_manifest_sha(ROOT / "data/property/developer_models/runs" /
                             model_manifest["run_id"] / "manifest.json"),
        frozenset(map_run["by_registry"]),
        frozenset((key, row["registry_id"]) for key, row in menu["units"].items()),
        frozenset((key, row["registry_id"]) for key, row in menu["floors"].items()),
    )
