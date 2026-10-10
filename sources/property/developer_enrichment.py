"""Stage a map-bound, immutable developer enrichment snapshot.

This command never changes the active experience pointer. Every input is
checked against the current map/menu/model generation before it is copied.
Later geography, noise, or model-air evidence produces a new run, not an
in-place edit.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator
import re

from backend.store import ROOT


BASE = ROOT / "data/property/developer_enrichment"
EXPERIENCE_POINTER = ROOT / "data/property/developer_experience/active.json"
MAP_BASE = ROOT / "data/property/developer_map/runs"
MENU_BASE = ROOT / "data/property/developer_units/runs"
MODEL_BASE = ROOT / "data/property/developer_models/runs"
SCHEMA = "developer-enrichment-snapshot/1"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"Blank JSONL row: {path.name}:{number}")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Non-object JSONL row: {path.name}:{number}")
            yield row


def _pinned_inputs(pointer_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise ValueError("Missing or unsafe active developer experience pointer")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if pointer.get("schema_version") not in ("developer-experience-pointer/1",
                                              "developer-experience-pointer/2"):
        raise ValueError("Unsupported experience pointer")
    roots = {"map": MAP_BASE, "unit": MENU_BASE, "model": MODEL_BASE}
    manifests: dict[str, Any] = {}
    for role, base in roots.items():
        run_id = pointer[f"{role}_run_id"]
        path = base / run_id / "manifest.json"
        if path.is_symlink() or not path.is_file() or _sha(path) != pointer[f"{role}_manifest_sha256"]:
            raise ValueError(f"Pinned {role} manifest changed")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["run_id"] != run_id:
            raise ValueError(f"Pinned {role} run ID changed")
        manifests[role] = manifest
    if manifests["map"]["snapshot_id"] != pointer["map_snapshot_id"]:
        raise ValueError("Pinned map snapshot changed")
    if (manifests["unit"]["map_run_id"] != pointer["map_run_id"] or
            manifests["model"]["menu_run_id"] != pointer["unit_run_id"]):
        raise ValueError("Pinned menu/model lineage mismatch")
    return pointer, manifests


def _load_exact_input(path: Path, manifest_path: Path, *, expected_sha_key: str,
                      expected_count_key: str, expected_run_key: str,
                      expected_run_id: str) -> tuple[str, int]:
    if path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Input symlinks are forbidden")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest[expected_run_key] != expected_run_id:
        raise ValueError("Input does not pin active menu")
    digest = _sha(path)
    if digest != manifest[expected_sha_key]:
        raise ValueError(f"Input SHA mismatch: {path.name}")
    count = sum(1 for _ in _rows(path))
    if count != manifest[expected_count_key]:
        raise ValueError(f"Input row count mismatch: {path.name}")
    return digest, count


def _load_optional(path: Path | None, *, id_key: str,
                   valid_ids: set[str]) -> tuple[dict[str, dict[str, Any]], str | None]:
    if path is None:
        return {}, None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Unsafe optional input: {path}")
    result: dict[str, dict[str, Any]] = {}
    for row in _rows(path):
        key = row.get(id_key)
        if key not in valid_ids or key in result:
            raise ValueError(f"Unknown or duplicate {id_key} in {path.name}: {key}")
        result[key] = row
    return result, _sha(path)


def _building_overlay(point: dict[str, Any], geography: dict[str, Any] | None,
                      noise: dict[str, Any] | None,
                      air: dict[str, Any] | None = None) -> dict[str, Any]:
    registry_id = point["registry_id"]
    located = bool(point["map_eligible"])
    scope = "inside" if located else "uncertain"
    geography_basis = "pinned_moscow_boundary_point_inside" if located else None
    okrug_id = okrug_name = district_id = district_name = None
    geography_confidence = geography_polygon_id = geography_polygon_source = None
    geography_edge_distance_m_approx = None
    if geography is not None:
        candidate_scope = geography.get("municipality_scope")
        if candidate_scope not in ("inside", "outside", "uncertain"):
            raise ValueError(f"Invalid municipality scope for {registry_id}")
        if candidate_scope == "outside" and geography.get("evidence_basis") not in (
                "verified_boundary_polygon", "verified_source_address_boundary"):
            raise ValueError(f"Outside claim lacks exact evidence for {registry_id}")
        if located and candidate_scope == "outside":
            raise ValueError(f"Outside claim contradicts pinned Moscow point: {registry_id}")
        scope = candidate_scope
        geography_basis = geography.get("evidence_basis")
        okrug_id, okrug_name = geography.get("okrug_id"), geography.get("okrug_name")
        district_id, district_name = geography.get("district_id"), geography.get("district_name")
        geography_confidence = geography.get("confidence")
        geography_polygon_id = geography.get("polygon_id")
        geography_polygon_source = geography.get("polygon_source")
        geography_edge_distance_m_approx = geography.get("edge_distance_m_approx")
    score: int | None = None
    noise_basis: str | None = None
    noise_coverage = "unlocated" if not located else "unknown"
    noise_inputs: dict[str, Any] | None = None
    noise_as_of_date = noise_source_snapshot_sha256 = noise_formula_version = None
    if noise is not None:
        score = noise.get("noise_index")
        if score is not None and (type(score) is not int or not 0 <= score <= 100):
            raise ValueError(f"Invalid noise index for {registry_id}")
        noise_basis = noise.get("noise_basis")
        noise_coverage = noise.get("noise_coverage", "unknown")
        if noise_coverage not in ("covered", "unknown", "unlocated",
                                  "boundary_incomplete", "source_error"):
            raise ValueError(f"Invalid noise coverage for {registry_id}")
        if (score is None) == (noise_coverage == "covered"):
            raise ValueError(f"Noise score/coverage mismatch for {registry_id}")
        noise_inputs = _compact_noise_inputs(noise.get("noise_inputs"))
        noise_as_of_date = noise.get("as_of_date")
        noise_source_snapshot_sha256 = noise.get("source_snapshot_sha256")
        noise_formula_version = noise.get("formula_version")
    air_model = _air_model(air) if district_id is not None else None
    return {
        "schema_version": "developer-building-enrichment/1",
        "registry_id": registry_id,
        "municipality_scope": scope,
        "visibility_eligible": scope != "outside",
        "geography_basis": geography_basis,
        "okrug_id": okrug_id, "okrug_name": okrug_name,
        "district_id": district_id, "district_name": district_name,
        "geography_confidence": geography_confidence,
        "geography_polygon_id": geography_polygon_id,
        "geography_polygon_source": geography_polygon_source,
        "geography_edge_distance_m_approx": geography_edge_distance_m_approx,
        "noise_index": score, "noise_basis": noise_basis,
        "noise_coverage": noise_coverage, "noise_inputs": noise_inputs,
        "noise_as_of_date": noise_as_of_date,
        "noise_source_snapshot_sha256": noise_source_snapshot_sha256,
        "noise_formula_version": noise_formula_version,
        "air_value": None,
        "air_model": air_model,
        "air_reason": (None if air_model is not None else
                       "unlocated_or_model_cell_unavailable"),
    }


def _aware_time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing air model timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Air model timestamp has no timezone")
    return value


def _air_model(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None or row.get("value_status") == "unavailable":
        return None
    if (row.get("value_status") != "modeled_forecast" or
            row.get("model_basis") != "open_meteo_cams_europe_grid_cell_forecast" or
            row.get("measured_district_average") is not False):
        raise ValueError("Air row is not a labeled grid-cell forecast")
    values = {}
    for name in ("pm2_5_ug_m3", "pm10_ug_m3", "no2_ug_m3"):
        value = row.get(name)
        if value is not None and (isinstance(value, bool) or
                                  not isinstance(value, (int, float)) or
                                  not 0 <= value < 1000):
            raise ValueError("Air model pollutant value is invalid")
        values[name] = value
    if all(value is None for value in values.values()):
        return None
    center = row.get("returned_grid_center_lon_lat")
    requested = row.get("requested_point_lon_lat")
    if (not isinstance(center, list) or len(center) != 2 or
            not isinstance(requested, list) or len(requested) != 2 or
            any(isinstance(value, bool) or not isinstance(value, (int, float))
                for value in center + requested)):
        raise ValueError("Air model lacks source grid/request coordinates")
    response_sha = row.get("source_response_sha256")
    if not isinstance(response_sha, str) or SHA_RE.fullmatch(response_sha) is None:
        raise ValueError("Air model response SHA is missing")
    attribution = row.get("attribution")
    if not isinstance(attribution, str) or not attribution:
        raise ValueError("Air model attribution is missing")
    refresh_after = row.get("request_refresh_after_utc", row.get("expires_at_utc"))
    stale_after = row.get("data_stale_after_utc")
    return {"model_basis": row["model_basis"],
            "sample_scope": row["sample_scope"],
            "requested_point_lon_lat": requested,
            "returned_grid_center_lon_lat": center,
            "forecast_at": _aware_time(row.get("forecast_at")),
            "retrieved_at": _aware_time(row.get("retrieved_at")),
            "request_refresh_after_utc": _aware_time(refresh_after),
            "data_stale_after_utc": (_aware_time(stale_after)
                                     if stale_after is not None else None),
            **values,
            "measured_district_average": False,
            "attribution": attribution,
            "source_response_sha256": response_sha}


def _compact_noise_inputs(value: Any) -> dict[str, Any] | None:
    """Keep useful distances and object IDs; full raw tags remain in source run."""
    if not isinstance(value, dict):
        return None
    components: dict[str, Any] = {}
    raw_components = value.get("components", {})
    if not isinstance(raw_components, dict):
        raise ValueError("Invalid noise component evidence")
    for name in ("major_road", "railway", "industrial", "airport"):
        item = raw_components.get(name)
        if not isinstance(item, dict):
            continue
        nearest = item.get("nearest_source")
        summary = None
        if isinstance(nearest, dict):
            summary = {key: nearest.get(key) for key in
                       ("source_id", "osm_version", "osm_timestamp", "geometry_type")}
        components[name] = {
            "distance_m": item.get("distance_m"),
            "distance_status": item.get("distance_status"),
            "component_points": item.get("component_points"),
            "nearest_source": summary,
            "source_layer_sha256": item.get("source_layer_sha256"),
        }
    return {"components": components,
            "search_halo_m": value.get("search_halo_m"),
            "distance_crs": value.get("distance_crs"),
            "activity_status": value.get("activity_status"),
            "not_an_acoustic_measurement": value.get("not_an_acoustic_measurement")}


def _check_geography_identity(points: dict[str, dict[str, Any]],
                              rows: dict[str, dict[str, Any]]) -> None:
    """A polygon join must refer to the exact published point, not a nearby one."""
    for registry_id, candidate in rows.items():
        point = points[registry_id]
        if point["map_eligible"] and (
                candidate.get("point_id") != point["point_id"] or
                [candidate.get("lon"), candidate.get("lat")] != point["location"] or
                candidate.get("publisher_project_id") != point.get("publisher_project_id") or
                candidate.get("publisher_building_id") != point.get("publisher_building_id")):
            raise ValueError(f"Geography evidence differs from exact map point: {registry_id}")


def _check_noise_identity(points: dict[str, dict[str, Any]],
                          rows: dict[str, dict[str, Any]]) -> None:
    for registry_id, candidate in rows.items():
        point = points[registry_id]
        inputs = candidate.get("noise_inputs")
        if point["map_eligible"]:
            if (candidate.get("noise_coverage") != "covered" or
                    not isinstance(inputs, dict) or
                    inputs.get("location_lon_lat") != point["location"]):
                raise ValueError(f"Noise evidence differs from exact map point: {registry_id}")
        elif candidate.get("noise_index") is not None:
            raise ValueError(f"Unlocated building has a noise score: {registry_id}")


def _check_air_identity(geography_rows: dict[str, dict[str, Any]],
                        air_rows: dict[str, dict[str, Any]]) -> None:
    """Bind modeled samples to the exact district and okrug joined to points."""
    district_identity: dict[str, tuple[Any, Any]] = {}
    for row in geography_rows.values():
        district_id = row.get("district_id")
        if district_id is None:
            continue
        identity = (row.get("district_name"), row.get("okrug_id"))
        if district_id in district_identity and district_identity[district_id] != identity:
            raise ValueError(f"Geography rows disagree on district identity: {district_id}")
        district_identity[district_id] = identity
    for district_id, identity in district_identity.items():
        candidate = air_rows.get(district_id)
        if candidate is None:
            raise ValueError("Air input omits an active district candidate")
        if (candidate.get("district_name"), candidate.get("okrug_id")) != identity:
            raise ValueError(f"Air input differs from exact district identity: {district_id}")


def build(*, unit_details: Path, unit_details_manifest: Path, media: Path,
          media_manifest: Path, geography: Path | None = None,
          geography_evidence_summary: Path | None = None,
          geography_source_manifest: Path | None = None,
          noise: Path | None = None, noise_evidence_manifest: Path | None = None,
          district_air: Path | None = None,
          district_air_manifest: Path | None = None,
          base: Path = BASE,
          pointer_path: Path = EXPERIENCE_POINTER) -> dict[str, Any]:
    """Copy and validate exact inputs, then publish only an immutable run dir."""
    pointer, manifests = _pinned_inputs(pointer_path)
    points_path = MAP_BASE / pointer["map_run_id"] / "points.jsonl"
    menu_units_path = MENU_BASE / pointer["unit_run_id"] / "units.jsonl"
    menu_floors_path = MENU_BASE / pointer["unit_run_id"] / "floors.jsonl"
    if _sha(points_path) != manifests["map"]["points_sha256"]:
        raise ValueError("Map point rows changed")
    if _sha(menu_units_path) != manifests["unit"]["units_sha256"]:
        raise ValueError("Menu unit rows changed")
    if _sha(menu_floors_path) != manifests["unit"]["floors_sha256"]:
        raise ValueError("Menu floor rows changed")
    points = {row["registry_id"]: row for row in _rows(points_path)}
    menu_units = {row["unit_id"]: row for row in _rows(menu_units_path)}
    menu_floors = {row["floor_id"]: row for row in _rows(menu_floors_path)}
    if len(points) != manifests["map"]["buildings_total"] or len(menu_units) != manifests["unit"]["units_total"]:
        raise ValueError("Pinned map/menu identity count mismatch")
    detail_sha, detail_count = _load_exact_input(
        unit_details, unit_details_manifest, expected_sha_key="sha256",
        expected_count_key="records", expected_run_key="unit_run_id",
        expected_run_id=pointer["unit_run_id"])
    media_sha, media_count = _load_exact_input(
        media, media_manifest, expected_sha_key="media_sha256",
        expected_count_key="media_total", expected_run_key="unit_run_id",
        expected_run_id=pointer["unit_run_id"])
    detail_source_manifest = json.loads(unit_details_manifest.read_text(encoding="utf-8"))
    media_source_manifest = json.loads(media_manifest.read_text(encoding="utf-8"))
    if (detail_source_manifest["unit_manifest_sha256"] != pointer["unit_manifest_sha256"] or
            media_source_manifest["unit_manifest_sha256"] != pointer["unit_manifest_sha256"] or
            media_source_manifest["map_manifest_sha256"] != pointer["map_manifest_sha256"]):
        raise ValueError("Source overlay manifest does not match pinned map/menu")
    if detail_count != len(menu_units):
        raise ValueError("Unit enrichment does not cover exact active menu")
    detail_ids: set[str] = set()
    for row in _rows(unit_details):
        unit_id = row["unit_id"]
        if unit_id in detail_ids or unit_id not in menu_units:
            raise ValueError(f"Duplicate or unknown unit detail: {unit_id}")
        if row["registry_id"] != menu_units[unit_id]["registry_id"]:
            raise ValueError(f"Unit building mismatch: {unit_id}")
        detail_ids.add(unit_id)
    media_ids: set[str] = set()
    for row in _rows(media):
        media_id, registry_id = row["media_id"], row["registry_id"]
        if media_id in media_ids or registry_id not in points:
            raise ValueError(f"Duplicate media or unknown building: {media_id}")
        media_ids.add(media_id)
        unit_id, floor_id = row.get("unit_id"), row.get("floor_id")
        if unit_id is not None and (unit_id not in menu_units or
                                    menu_units[unit_id]["registry_id"] != registry_id):
            raise ValueError(f"Media unit membership mismatch: {media_id}")
        if floor_id is not None and (floor_id not in menu_floors or
                                     menu_floors[floor_id]["registry_id"] != registry_id):
            raise ValueError(f"Media floor membership mismatch: {media_id}")
        if row["allowed_host"] not in ("cdn-dataout.trendagent.ru", "cdn.a101.ru", "cdn.fsk.ru"):
            raise ValueError(f"Media host not approved: {media_id}")
    geography_rows, geography_sha = _load_optional(geography, id_key="registry_id",
                                                   valid_ids=set(points))
    noise_rows, noise_sha = _load_optional(noise, id_key="registry_id",
                                           valid_ids=set(points))
    if (district_air is None) != (district_air_manifest is None):
        raise ValueError("Air rows and source manifest must be staged together")
    air_rows: dict[str, dict[str, Any]] = {}
    air_sha: str | None = None
    if district_air is not None:
        if district_air.is_symlink() or not district_air.is_file():
            raise ValueError("Unsafe district air input")
        for row in _rows(district_air):
            key = row.get("district_id")
            if not isinstance(key, str) or key in air_rows:
                raise ValueError("Invalid or duplicate district air ID")
            if row.get("value_status") not in ("modeled_forecast", "unavailable"):
                raise ValueError("Invalid district air status")
            _air_model(row)
            air_rows[key] = row
        air_sha = _sha(district_air)
        _check_air_identity(geography_rows, air_rows)
    _check_geography_identity(points, geography_rows)
    _check_noise_identity(points, noise_rows)
    geography_evidence_sha: str | None = None
    if geography_evidence_summary is not None:
        if geography is None or geography_evidence_summary.is_symlink():
            raise ValueError("Geography evidence needs a safe geography input")
        summary = json.loads(geography_evidence_summary.read_text(encoding="utf-8"))
        if (summary.get("outputs", {}).get(str(geography)) != geography_sha or
                summary.get("input_sha256", {}).get("points") !=
                manifests["map"]["points_sha256"]):
            raise ValueError("Geography evidence does not pin exact output/map")
        geography_evidence_sha = _sha(geography_evidence_summary)
    geography_source_sha: str | None = None
    if geography_source_manifest is not None:
        if geography is None or geography_source_manifest.is_symlink():
            raise ValueError("Geography source manifest needs a safe geography input")
        source_manifest = json.loads(geography_source_manifest.read_text(encoding="utf-8"))
        files = source_manifest.get("files", {})
        if (source_manifest.get("active_run_id") != pointer["map_run_id"] or
                files.get("active_points", {}).get("sha256") !=
                manifests["map"]["points_sha256"] or
                files.get("point_admin_candidates", {}).get("sha256") != geography_sha):
            raise ValueError("Geography source manifest does not pin map/output")
        geography_source_sha = _sha(geography_source_manifest)
    noise_evidence_sha: str | None = None
    if noise_evidence_manifest is not None:
        if noise is None or noise_evidence_manifest.is_symlink():
            raise ValueError("Noise evidence needs a safe noise input")
        source_manifest = json.loads(noise_evidence_manifest.read_text(encoding="utf-8"))
        if (source_manifest.get("rows_sha256") != noise_sha or
                source_manifest.get("map_snapshot", {}).get("manifest_sha256") !=
                pointer["map_manifest_sha256"] or
                source_manifest.get("map_snapshot", {}).get("points_sha256") !=
                manifests["map"]["points_sha256"]):
            raise ValueError("Noise evidence does not pin exact output")
        noise_evidence_sha = _sha(noise_evidence_manifest)
    air_source_manifest_sha: str | None = None
    if district_air_manifest is not None:
        if district_air is None or district_air_manifest.is_symlink():
            raise ValueError("Air source manifest needs safe district air input")
        air_source_manifest = json.loads(district_air_manifest.read_text(encoding="utf-8"))
        air_geography_sha = air_source_manifest.get("ata_geography_manifest_sha256")
        if air_geography_sha is None:
            air_geography_sha = air_source_manifest.get("geography", {}).get(
                "geography_manifest_sha256")
        if (air_source_manifest.get("rows_sha256") != air_sha or
                air_source_manifest.get("rows_total") != len(air_rows) or
                air_geography_sha != geography_source_sha):
            raise ValueError("Air source manifest does not pin exact rows/geography")
        response_sha = air_source_manifest.get("source_response_sha256")
        if not isinstance(response_sha, str) or SHA_RE.fullmatch(response_sha) is None:
            raise ValueError("Air source manifest lacks response hash")
        if any(row.get("source_response_sha256") != response_sha
               for row in air_rows.values()):
            raise ValueError("Air row differs from pinned source response")
        air_source_manifest_sha = _sha(district_air_manifest)
    observed_at = max(row["observed_at"] for row in points.values())
    runs_root = base / "runs"
    runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".enrichment-stage-", dir=runs_root) as temporary:
        staging = Path(temporary)
        os.chmod(staging, 0o700)
        buildings_path = staging / "buildings.jsonl"
        with buildings_path.open("wb") as stream:
            for registry_id in sorted(points):
                stream.write(_canonical(_building_overlay(
                    points[registry_id], geography_rows.get(registry_id),
                    noise_rows.get(registry_id),
                    air_rows.get(geography_rows.get(registry_id, {}).get("district_id")))))
            stream.flush()
            os.fsync(stream.fileno())
        for source, name in ((unit_details, "units.jsonl"), (media, "media.jsonl")):
            target = staging / name
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
        if (_sha(staging / "units.jsonl") != detail_sha or
                _sha(staging / "media.jsonl") != media_sha):
            raise ValueError("Source overlay changed while staging")
        buildings_sha = _sha(buildings_path)
        with buildings_path.open(encoding="utf-8") as stream:
            air_model_buildings_total = sum(
                json.loads(line)["air_model"] is not None for line in stream)
        identity = {
            "schema_version": SCHEMA,
            "map_run_id": pointer["map_run_id"],
            "map_snapshot_id": pointer["map_snapshot_id"],
            "map_manifest_sha256": pointer["map_manifest_sha256"],
            "unit_run_id": pointer["unit_run_id"],
            "unit_manifest_sha256": pointer["unit_manifest_sha256"],
            "model_run_id": pointer["model_run_id"],
            "model_manifest_sha256": pointer["model_manifest_sha256"],
            "buildings_sha256": buildings_sha,
            "buildings_total": len(points),
            "units_sha256": detail_sha, "units_total": detail_count,
            "unit_details_source_manifest_sha256": _sha(unit_details_manifest),
            "media_sha256": media_sha, "media_total": media_count,
            "media_source_manifest_sha256": _sha(media_manifest),
            "geography_input_sha256": geography_sha,
            "geography_evidence_summary_sha256": geography_evidence_sha,
            "geography_source_manifest_sha256": geography_source_sha,
            "geography_rows_total": len(geography_rows),
            "noise_input_sha256": noise_sha,
            "noise_evidence_manifest_sha256": noise_evidence_sha,
            "noise_rows_total": len(noise_rows),
            "air_district_input_sha256": air_sha,
            "air_district_rows_total": len(air_rows),
            "air_source_manifest_sha256": air_source_manifest_sha,
            "air_model_buildings_total": air_model_buildings_total,
            "observed_at": observed_at,
            "air_data_status": ("modeled_grid_cell_samples" if air_model_buildings_total else
                                "district_measurement_unavailable"),
            "display_policy": "user_authorized_source_bound_preview/1",
        }
        run_id = "developer-enrichment-v1-" + hashlib.sha256(_canonical(identity)).hexdigest()[:16]
        manifest = {"run_id": run_id, **identity}
        manifest_path = staging / "manifest.json"
        manifest_path.write_bytes(_canonical(manifest))
        for path in (buildings_path, manifest_path):
            os.chmod(path, 0o600)
        with manifest_path.open("rb") as stream:
            os.fsync(stream.fileno())
        final = runs_root / run_id
        if final.exists():
            if _sha(final / "manifest.json") != _sha(manifest_path):
                raise ValueError("Immutable enrichment run ID collision")
            for name in ("buildings.jsonl", "units.jsonl", "media.jsonl"):
                if _sha(final / name) != _sha(staging / name):
                    raise ValueError("Immutable enrichment run content mismatch")
        else:
            os.replace(staging, final)
            fd = os.open(runs_root, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return {"run_id": run_id, "manifest_sha256": _sha(final / "manifest.json"),
                "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-details", type=Path, required=True)
    parser.add_argument("--unit-details-manifest", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--media-manifest", type=Path, required=True)
    parser.add_argument("--geography", type=Path)
    parser.add_argument("--geography-evidence-summary", type=Path)
    parser.add_argument("--geography-source-manifest", type=Path)
    parser.add_argument("--noise", type=Path)
    parser.add_argument("--noise-evidence-manifest", type=Path)
    parser.add_argument("--district-air", type=Path)
    parser.add_argument("--district-air-manifest", type=Path)
    args = parser.parse_args()
    result = build(unit_details=args.unit_details,
                   unit_details_manifest=args.unit_details_manifest,
                   media=args.media, media_manifest=args.media_manifest,
                   geography=args.geography,
                   geography_evidence_summary=args.geography_evidence_summary,
                   geography_source_manifest=args.geography_source_manifest,
                   noise=args.noise, noise_evidence_manifest=args.noise_evidence_manifest,
                   district_air=args.district_air,
                   district_air_manifest=args.district_air_manifest)
    print(json.dumps({key: result[key] for key in ("run_id", "manifest_sha256")},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
