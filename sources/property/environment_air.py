"""Capture a short-lived, clearly modelled CAMS air-quality candidate.

This provider emits one model-grid sample per official Moscow district name,
using a representative point from the pinned mapped polygon candidate set.
The value is a forecast for a returned CAMS grid cell, never a measurement or
district average. It writes immutable private captures only and does not
publish a product pointer.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import unicodedata
from zoneinfo import ZoneInfo

from shapely.geometry import Point, shape


ROOT = Path(__file__).resolve().parents[2]
API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
API_DOCS_URL = "https://open-meteo.com/en/docs/air-quality-api"
TERMS_URL = "https://open-meteo.com/en/terms"
SCHEMA_VERSION = "environment-air-grid-run/1"
ROW_SCHEMA_VERSION = "district-air-grid-value/1"
MODEL_BASIS = "open_meteo_cams_europe_grid_cell_forecast"
SAMPLE_SCOPE = "requested_district_candidate_point_to_returned_grid_cell"
SOURCE_MODEL = "CAMS European Air Quality Forecast"
ATTRIBUTION = "Open-Meteo.com (CC BY 4.0) / CAMS European Air Quality Forecast"
GRID_RESOLUTION_KM_APPROX = 11
TTL_SECONDS = 3600
STALE_AFTER_HOURS = 24
MAX_LOCATIONS = 150
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
POLLUTANT_FIELDS = {
    "pm2_5": "pm2_5_ug_m3",
    "pm10": "pm10_ug_m3",
    "nitrogen_dioxide": "no2_ug_m3",
}
INPUTS = {
    "base_district_candidate_layer": "data/city/environment/moscow_district_candidates_2026-09-23.1.geojson",
    "supplemental_polygons": ".runtime/research/geography-20261011/supplemental_district_polygons.geojson",
    "official_hierarchy": ".runtime/research/geography-20261011/official_district_hierarchy.json",
    "point_admin_candidates": ".runtime/research/geography-20261011/point_admin_candidates.jsonl",
    "geography_manifest": ".runtime/research/geography-20261011/manifest_final.json",
}
EXPECTED_INPUT_SHA256 = {
    "base_district_candidate_layer": "113724900f2e90e9210255c4857c0eb1c30f030c68ba9d43c5477697e5beb060",
    "supplemental_polygons": "98c4a83791585c9e35ccd8d6feff1cd5e5bc416fc7e79569146c32d3a3c3aef0",
    "official_hierarchy": "7c57690cecc4aafd40b440557395206bd9577213bd20ad71c1b5c76f42f75a7e",
    "point_admin_candidates": "017a3617b5ce25cbda067dc646c7fcd8380056ee82df610e70629e1b778790d0",
    "geography_manifest": "87a44421b1702ab649b938c7cf564da74c79000636d12628af8eacb18ad8e2c6",
}


class AirBuildError(RuntimeError):
    """Pinned geography, API capture, or model rows failed validation."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if partial.exists() or partial.is_symlink():
        raise FileExistsError(f"Refusing to overwrite partial file: {partial}")
    try:
        with partial.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    _atomic_write(path, b"".join(_canonical(row) + b"\n" for row in rows))


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise AirBuildError(f"Required input missing or symlink: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AirBuildError(f"Invalid JSON input: {path}") from exc


def _normalized_key(value: str) -> str:
    value = unicodedata.normalize("NFC", value).casefold().replace("ё", "е").strip()
    return " ".join(value.split())


def _polygon_name_key(value: str) -> str:
    key = _normalized_key(value)
    for prefix in ("район ", "муниципальный округ "):
        if key.startswith(prefix):
            key = key[len(prefix):]
    if key.endswith(" район"):
        key = key[:-len(" район")]
    return key.strip()


def load_district_request_points(root: Path = ROOT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the frozen 132-district polygon/hierarchy inputs and emit points.

    The one extra mapped name ("микрорайон Петровская Слобода") is not an
    official district and is ignored. All 132 official hierarchy entries must
    have exactly one matching candidate polygon; no fuzzy fallback is used.
    """
    input_paths = {key: root / relpath for key, relpath in INPUTS.items()}
    actual_hashes = {key: sha256_file(path) for key, path in input_paths.items()}
    if actual_hashes != EXPECTED_INPUT_SHA256:
        mismatches = {key: {"expected": EXPECTED_INPUT_SHA256[key], "actual": actual_hashes[key]}
                      for key in actual_hashes if actual_hashes[key] != EXPECTED_INPUT_SHA256[key]}
        raise AirBuildError(f"Geography evidence pin mismatch: {mismatches}")

    manifest = _read_json(input_paths["geography_manifest"])
    if manifest.get("join_contract", {}).get("district_id_scheme") != "district:<normalized official district name> (generated, not governmental code)":
        raise AirBuildError("Geography audit uses an unexpected district ID scheme")
    hierarchy = _read_json(input_paths["official_hierarchy"])
    mapping = hierarchy.get("mapping")
    if not isinstance(mapping, list) or len(mapping) != 132:
        raise AirBuildError("Pinned official district hierarchy must contain 132 districts")
    official: dict[str, dict[str, Any]] = {}
    for row in mapping:
        key = row.get("normalized_key")
        name = row.get("district_name")
        okrug = row.get("okrug_name")
        if not all(isinstance(v, str) and v.strip() for v in (key, name, okrug)):
            raise AirBuildError("District hierarchy contains an incomplete entry")
        key = _normalized_key(key)
        if key in official:
            raise AirBuildError(f"Duplicate official district key: {key}")
        official[key] = {**row, "normalized_key": key}

    features: list[dict[str, Any]] = []
    for key in ("base_district_candidate_layer", "supplemental_polygons"):
        document = _read_json(input_paths[key])
        if document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
            raise AirBuildError(f"{key} is not a GeoJSON FeatureCollection")
        features.extend(document["features"])
    by_district: dict[str, list[dict[str, Any]]] = {}
    ignored_names: list[str] = []
    for feature in features:
        props = feature.get("properties") or {}
        name = props.get("name")
        if not isinstance(name, str):
            raise AirBuildError("District polygon has no source name")
        key = _polygon_name_key(name)
        if key not in official:
            ignored_names.append(name)
            continue
        geometry_doc = feature.get("geometry")
        if not isinstance(geometry_doc, dict):
            raise AirBuildError(f"District polygon has no geometry: {name}")
        geometry = shape(geometry_doc)
        if (geometry.geom_type not in {"Polygon", "MultiPolygon"}
                or geometry.is_empty or not geometry.is_valid):
            raise AirBuildError(f"District polygon is invalid: {name}")
        by_district.setdefault(key, []).append({"properties": props, "geometry": geometry})
    missing = sorted(set(official) - set(by_district))
    duplicates = {key: len(items) for key, items in by_district.items() if len(items) != 1}
    if missing or duplicates or len(by_district) != 132:
        raise AirBuildError(f"Polygon-to-hierarchy join is not one-to-one; missing={missing}, duplicates={duplicates}")

    points: list[dict[str, Any]] = []
    for key, district in sorted(official.items()):
        item = by_district[key][0]
        point = item["geometry"].representative_point()
        if point.is_empty or not item["geometry"].covers(point):
            raise AirBuildError(f"Representative point is not covered by candidate polygon: {district['district_name']}")
        district_name = district["district_name"]
        district_key = district["normalized_key"]
        okrug_name = district["okrug_name"]
        okrug_key = _normalized_key(okrug_name)
        suffix = " округ"
        if okrug_key.endswith(suffix):
            okrug_key = okrug_key[:-len(suffix)]
        points.append({
            "district_id": f"district:{district_key}",
            "district_name": district_name,
            "okrug_id": f"okrug:{okrug_key}",
            "okrug_name": okrug_name,
            "requested_point_lon_lat": [round(point.x, 7), round(point.y, 7)],
            "point_method": "representative_point_inside_pinned_mapped_district_polygon_candidate",
            "polygon_source_name": item["properties"].get("name"),
            "polygon_source_id": item["properties"].get("source_id"),
            "polygon_upstream_osm_record_id": item["properties"].get("upstream_osm_record_id"),
        })
    if len({row["district_id"] for row in points}) != 132:
        raise AirBuildError("District IDs are not unique")
    if len({tuple(row["requested_point_lon_lat"]) for row in points}) != 132:
        raise AirBuildError("Two official districts have identical query points; refusing implicit point alias")
    return points, {
        "input_sha256": actual_hashes,
        "geography_manifest_sha256": actual_hashes["geography_manifest"],
        "official_district_count": 132,
        "polygon_feature_count": len(features),
        "matched_district_polygon_count": len(by_district),
        "ignored_non_district_names": sorted(ignored_names),
        "district_geometry_authority": "mapped Overture/OSM polygon candidates crosswalked to official hierarchy names; not legal/cadastral boundaries",
        "point_method": "Shapely representative_point inside each mapped candidate polygon",
    }


def build_request_url(points: list[dict[str, Any]]) -> str:
    if not 1 <= len(points) <= MAX_LOCATIONS:
        raise AirBuildError(f"Request location count must be 1..{MAX_LOCATIONS}")
    latitudes = ",".join(f"{row['requested_point_lon_lat'][1]:.7f}" for row in points)
    longitudes = ",".join(f"{row['requested_point_lon_lat'][0]:.7f}" for row in points)
    query = urlencode({
        "latitude": latitudes,
        "longitude": longitudes,
        "hourly": ",".join(POLLUTANT_FIELDS),
        "domains": "cams_europe",
        "forecast_days": "2",
        "timezone": "Europe/Moscow",
    })
    url = f"{API_URL}?{query}"
    if len(url.encode("ascii")) > 12_000:
        raise AirBuildError("Bounded Open-Meteo request URL exceeded 12KB")
    return url


def _request_json(url: str) -> tuple[bytes, dict[str, str], int, float]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "novostroy-environment-candidate/1"})
    started = time.monotonic()
    try:
        with urlopen(request, timeout=45) as response:
            status = int(response.status)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            if status in {401, 403, 429}:
                raise AirBuildError(f"Open-Meteo denied the request with HTTP {status}; no retry is allowed")
            if status != 200:
                raise AirBuildError(f"Unexpected Open-Meteo HTTP status: {status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AirBuildError("Open-Meteo response exceeded the 4 MiB capture limit")
            return raw, headers, status, time.monotonic() - started
    except HTTPError as exc:
        # A denial is a terminal source status. The caller records the failure
        # in the local ledger and will not retry through another route.
        if exc.code in {401, 403, 429}:
            raise AirBuildError(f"Open-Meteo denied the request with HTTP {exc.code}; no retry is allowed") from exc
        raise AirBuildError(f"Open-Meteo HTTP error {exc.code}") from exc
    except URLError as exc:
        raise AirBuildError(f"Open-Meteo request failed: {exc.reason}") from exc


def _parse_local_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    return parsed


def _choose_forecast_at(responses: list[dict[str, Any]], retrieved_at: datetime) -> tuple[str, int]:
    common_times: set[str] | None = None
    for location in responses:
        if location.get("timezone") != "Europe/Moscow":
            raise AirBuildError("Open-Meteo response timezone is not Europe/Moscow")
        hourly = location.get("hourly")
        times = hourly.get("time") if isinstance(hourly, dict) else None
        if not isinstance(times, list) or not all(isinstance(value, str) for value in times):
            raise AirBuildError("Open-Meteo response has no hourly time series")
        common_times = set(times) if common_times is None else common_times.intersection(times)
    if not common_times:
        raise AirBuildError("Open-Meteo locations have no common forecast timestamps")
    future = sorted((time for time in common_times if _parse_local_time(time).astimezone(timezone.utc) >= retrieved_at),
                    key=lambda value: _parse_local_time(value).astimezone(timezone.utc))
    if not future:
        raise AirBuildError("Open-Meteo response contains no future forecast hour")
    selected = future[0]
    first_times = responses[0]["hourly"]["time"]
    return selected, first_times.index(selected)


def _finite_pollutant(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0:
        return float(value)
    return None


def parse_model_response(
    raw: bytes,
    request_points: list[dict[str, Any]],
    *,
    retrieved_at: datetime,
    request_url_sha256: str,
    response_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AirBuildError("Open-Meteo response is not valid JSON") from exc
    responses = payload if isinstance(payload, list) else [payload]
    if len(responses) != len(request_points):
        raise AirBuildError(f"Response location count {len(responses)} != request point count {len(request_points)}")
    if not all(isinstance(item, dict) for item in responses):
        raise AirBuildError("Open-Meteo returned a non-object location result")
    valid_at_local, time_index = _choose_forecast_at(responses, retrieved_at)
    valid_at = _parse_local_time(valid_at_local)
    retrieved_text = retrieved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    refresh_after_text = (retrieved_at + timedelta(seconds=TTL_SECONDS)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    stale_after_text = (valid_at.astimezone(timezone.utc) + timedelta(hours=STALE_AFTER_HOURS)).isoformat().replace("+00:00", "Z")
    rows: list[dict[str, Any]] = []
    cell_use: Counter[str] = Counter()
    for point, location in zip(request_points, responses, strict=True):
        units = location.get("hourly_units") or {}
        if any(units.get(field) not in {"μg/m³", "µg/m³", "ug/m³"} for field in POLLUTANT_FIELDS):
            raise AirBuildError("Open-Meteo returned unexpected pollutant units")
        hourly = location.get("hourly") or {}
        values: dict[str, float | None] = {}
        for source_field, output_field in POLLUTANT_FIELDS.items():
            series = hourly.get(source_field)
            if not isinstance(series, list) or len(series) <= time_index:
                raise AirBuildError(f"Open-Meteo pollutant series is incomplete: {source_field}")
            values[output_field] = _finite_pollutant(series[time_index])
        center_lat, center_lon = location.get("latitude"), location.get("longitude")
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (center_lat, center_lon)):
            raise AirBuildError("Open-Meteo response omitted grid-cell coordinates")
        center = [float(center_lon), float(center_lat)]
        cell_key = f"{center[0]:.6f},{center[1]:.6f}"
        cell_use[cell_key] += 1
        status = "modeled_forecast" if all(value is not None for value in values.values()) else "unavailable"
        reason = None if status == "modeled_forecast" else "One or more pollutant values were null or invalid in the source response."
        rows.append({
            "schema_version": ROW_SCHEMA_VERSION,
            "district_id": point["district_id"],
            "district_name": point["district_name"],
            "okrug_id": point["okrug_id"],
            "okrug_name": point["okrug_name"],
            "model_basis": MODEL_BASIS,
            "basis": "CAMS_grid_via_Open_Meteo",
            "sample_scope": SAMPLE_SCOPE,
            "spatial_scope": "district_representative_grid_cell",
            "requested_point_lon_lat": point["requested_point_lon_lat"],
            "returned_grid_center_lon_lat": center,
            "cell_center": center,
            "grid_cell_key": cell_key,
            "cell_resolution_km_approx": GRID_RESOLUTION_KM_APPROX,
            "forecast_at": valid_at.isoformat(),
            "valid_at": valid_at.isoformat(),
            "forecast_at_utc": valid_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "retrieved_at": retrieved_text,
            "request_refresh_after_utc": refresh_after_text,
            "data_stale_after_utc": stale_after_text,
            **values,
            "value_status": status,
            "status": status,
            "reason": reason,
            "measured_district_average": False,
            "source_model": SOURCE_MODEL,
            "source_resolution": "CAMS Europe 0.1 degree, approximately 11 km (Open-Meteo documentation)",
            "source_url_sha256": request_url_sha256,
            "source_response_sha256": response_sha256,
            "attribution": ATTRIBUTION,
            "terms_url": TERMS_URL,
            "candidate_geometry_basis": point["point_method"],
            "candidate_polygon_source": point["polygon_source_name"],
            "candidate_polygon_source_id": point["polygon_source_id"],
        })
    metadata = {
        "forecast_at": valid_at.isoformat(),
        "forecast_at_utc": valid_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retrieved_at": retrieved_text,
        "request_refresh_after_utc": refresh_after_text,
        "data_stale_after_utc": stale_after_text,
        "ttl_seconds": TTL_SECONDS,
        "model_grid_centers_unique": len(cell_use),
        "district_rows_per_grid_center": dict(sorted(Counter(cell_use.values()).items())),
        "available_rows": sum(row["value_status"] == "modeled_forecast" for row in rows),
        "unavailable_rows": sum(row["value_status"] == "unavailable" for row in rows),
    }
    return rows, metadata


def _write_request_ledger(ledger_root: Path, row: dict[str, Any]) -> Path:
    ledger_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    name = f"request-{stamp}-{row['request_url_sha256'][:12]}.json"
    path = ledger_root / name
    _write_json(path, row)
    return path


def _fresh_cached_run(output_root: Path, url_sha256: str, now: datetime) -> dict[str, Any] | None:
    """Reuse a fully hash-checked prior response until its explicit TTL expires."""
    if not output_root.exists():
        return None
    now_utc = now.astimezone(timezone.utc)
    for run_dir in sorted(output_root.iterdir(), reverse=True):
        if run_dir.is_symlink() or not run_dir.is_dir() or run_dir.name.endswith(".partial"):
            continue
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            request = manifest.get("request") or {}
            refresh_after = manifest.get("request_refresh_after_utc", manifest.get("expires_at_utc"))
            refresh_due = datetime.fromisoformat(refresh_after.replace("Z", "+00:00"))
            rows_path = run_dir / manifest["rows_path"]
            if (manifest.get("schema_version") != SCHEMA_VERSION
                    or request.get("request_url_sha256") != url_sha256
                    or refresh_due <= now_utc
                    or rows_path.is_symlink()
                    or not rows_path.is_file()
                    or sha256_file(rows_path) != manifest.get("rows_sha256")):
                continue
            return {
                **manifest,
                "ata_geography_manifest_sha256": manifest.get(
                    "ata_geography_manifest_sha256",
                    (manifest.get("geography") or {}).get("geography_manifest_sha256"),
                ),
                "request_refresh_after_utc": refresh_after,
                "run_dir": str(run_dir),
                "manifest_sha256": sha256_bytes(manifest_bytes),
                "cache_hit": True,
            }
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def fetch_candidate(
    *,
    root: Path = ROOT,
    output_root: Path | None = None,
    live: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch one bounded multi-location request and stage an immutable run."""
    if not live:
        raise AirBuildError("Network access requires explicit --live")
    request_points, geography = load_district_request_points(root)
    url = build_request_url(request_points)
    url_sha = sha256_bytes(url.encode("utf-8"))
    check_time = now or datetime.now(timezone.utc)
    if check_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    output_root = output_root or root / ".runtime/research/environment-20261011/air/runs"
    cached = _fresh_cached_run(output_root, url_sha, check_time)
    if cached is not None:
        handoff_path, handoff_sha = _write_enrichment_handoff(root, cached)
        cached["enrichment_manifest_path"] = str(handoff_path)
        cached["enrichment_manifest_sha256"] = handoff_sha
        return cached
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        raw, headers, status, elapsed = _request_json(url)
    except AirBuildError as exc:
        ledger_path = _write_request_ledger(root / ".runtime/research/environment-20261011/air/ledger", {
            "schema_version": "environment-air-request-attempt/1",
            "started_at": started_at,
            "request_url": url,
            "request_url_sha256": url_sha,
            "location_count": len(request_points),
            "status": "failed_no_retry",
            "error": str(exc),
            "response_status": getattr(exc.__cause__, "code", None),
        })
        raise AirBuildError(f"{exc}; ledger={ledger_path}") from exc
    retrieved_at = datetime.now(timezone.utc)
    response_sha = sha256_bytes(raw)
    ledger_path = _write_request_ledger(root / ".runtime/research/environment-20261011/air/ledger", {
        "schema_version": "environment-air-request-attempt/1",
        "started_at": started_at,
        "retrieved_at": retrieved_at.isoformat().replace("+00:00", "Z"),
        "request_url": url,
        "request_url_sha256": url_sha,
        "location_count": len(request_points),
        "response_status": status,
        "response_bytes": len(raw),
        "response_sha256": response_sha,
        "elapsed_seconds": round(elapsed, 3),
        "http_date": headers.get("date"),
        "retry_count": 0,
    })
    try:
        rows, forecast = parse_model_response(
            raw,
            request_points,
            retrieved_at=retrieved_at,
            request_url_sha256=url_sha,
            response_sha256=response_sha,
        )
    except BaseException as exc:
        failed_raw_root = root / ".runtime/research/environment-20261011/air/failed-captures"
        failed_raw_root.mkdir(parents=True, exist_ok=True)
        failed_raw = failed_raw_root / f"{response_sha}.json"
        if not failed_raw.exists():
            _atomic_write(failed_raw, raw)
        _write_request_ledger(root / ".runtime/research/environment-20261011/air/ledger", {
            "schema_version": "environment-air-response-validation/1",
            "request_ledger_path": str(ledger_path),
            "response_sha256": response_sha,
            "failed_raw_path": str(failed_raw),
            "status": "response_validation_failed_no_retry",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        raise
    stamp = retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"air-cams-v1-{stamp}-{response_sha[:12]}"
    run_dir = output_root / run_id
    if run_dir.exists() or run_dir.is_symlink():
        raise FileExistsError(f"Refusing to overwrite air capture: {run_dir}")
    stage = run_dir.with_name(run_dir.name + ".partial")
    if stage.exists() or stage.is_symlink():
        raise FileExistsError(f"Refusing to overwrite partial air capture: {stage}")
    stage.mkdir(parents=True)
    try:
        raw_path = stage / "raw-response.json"
        _atomic_write(raw_path, raw)
        headers_path = stage / "response-headers.json"
        _write_json(headers_path, headers)
        url_path = stage / "request-url.txt"
        _atomic_write(url_path, (url + "\n").encode("utf-8"))
        points_path = stage / "request-points.jsonl"
        _write_jsonl(points_path, request_points)
        rows_path = stage / "district-air-values.jsonl"
        _write_jsonl(rows_path, rows)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at_utc": forecast["retrieved_at"],
            "forecast_at": forecast["forecast_at"],
            "forecast_at_utc": forecast["forecast_at_utc"],
            "retrieved_at": forecast["retrieved_at"],
            "request_refresh_after_utc": forecast["request_refresh_after_utc"],
            "data_stale_after_utc": forecast["data_stale_after_utc"],
            "ttl_seconds": TTL_SECONDS,
            "consumer_stale_after_hours": STALE_AFTER_HOURS,
            "ata_geography_manifest_sha256": geography["geography_manifest_sha256"],
            "model": {
                "name": SOURCE_MODEL,
                "basis": MODEL_BASIS,
                "provider": "Open-Meteo Air Quality API",
                "api_url": API_URL,
                "docs_url": API_DOCS_URL,
                "terms_url": TERMS_URL,
                "domains_parameter": "cams_europe",
                "variables": list(POLLUTANT_FIELDS),
                "units": "μg/m³",
                "resolution": "0.1 degree, approximately 11 km",
                "data_kind": "modeled forecast, not measured observation",
                "attribution": ATTRIBUTION,
                "commercial_use": "not authorized by this capture; free API use is limited to non-commercial use under provider terms",
            },
            "geography": geography,
            "request": {
                "method": "GET",
                "locations": len(request_points),
                "request_url_file": "request-url.txt",
                "request_url_sha256": url_sha,
                "request_ledger_path": str(ledger_path),
                "request_ledger_sha256": sha256_file(ledger_path),
                "http_status": status,
                "http_date": headers.get("date"),
                "elapsed_seconds": round(elapsed, 3),
                "retry_count": 0,
            },
            "response": {
                "raw_file": raw_path.name,
                "raw_sha256": response_sha,
                "raw_bytes": len(raw),
                "headers_file": headers_path.name,
                "headers_sha256": sha256_file(headers_path),
                "returned_locations": len(request_points),
            },
            "forecast": forecast,
            "rows_path": rows_path.name,
            "rows_sha256": sha256_file(rows_path),
            "rows_total": len(rows),
            "source_url_sha256": url_sha,
            "source_response_sha256": response_sha,
            "coverage": {
                "districts_requested": len(request_points),
                "modelled_forecast_rows": forecast["available_rows"],
                "unavailable_rows": forecast["unavailable_rows"],
                "distinct_returned_grid_centers": forecast["model_grid_centers_unique"],
                "district_rows_per_grid_center": forecast["district_rows_per_grid_center"],
                "district_average": False,
            },
            "publication": {
                "status": "staged_private_candidate",
                "active_pointer_changed": False,
                "product_activation_performed": False,
            },
            "limitations": [
                "Each row is one point lookup at the returned CAMS grid cell near a mapped district polygon candidate representative point, not a district average.",
                "The 132 district names follow a separately pinned official hierarchy; polygon geometries are Overture/OSM mapped candidates and are not legal/cadastral boundaries.",
                "Multiple districts can share one approximately 11 km model cell; shared returned grid centers are preserved and counted.",
                "Values are model forecasts, not monitoring-station measurements; no local station validation or bias correction was applied.",
                "The 1-hour TTL controls local request reuse only. Consumers may display the dated forecast with a stale marker after 24 hours from forecast_at.",
            ],
        }
        _write_json(stage / "manifest.json", manifest)
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        os.replace(stage, run_dir)
        manifest["manifest_sha256"] = sha256_file(run_dir / "manifest.json")
        result = {**manifest, "run_dir": str(run_dir)}
        handoff_path, handoff_sha = _write_enrichment_handoff(root, result)
        result["enrichment_manifest_path"] = str(handoff_path)
        result["enrichment_manifest_sha256"] = handoff_sha
        return result
    except BaseException:
        # Keep no half-published run. A successful HTTP request remains recorded
        # in the append-only request ledger, including its raw response hash.
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _write_enrichment_handoff(root: Path, run: dict[str, Any]) -> tuple[Path, str]:
    """Write the small exact manifest contract consumed by enrichment."""
    run_dir = Path(run["run_dir"])
    run_manifest_path = run_dir / "manifest.json"
    run_manifest_sha = run.get("manifest_sha256") or sha256_file(run_manifest_path)
    geography = run.get("geography") or {}
    geography_sha = run.get("ata_geography_manifest_sha256") or geography.get("geography_manifest_sha256")
    response_sha = run.get("source_response_sha256")
    if not all(isinstance(value, str) and HASH_RE.fullmatch(value)
               for value in (run.get("rows_sha256"), geography_sha, response_sha, run_manifest_sha)):
        raise AirBuildError("Cannot build enrichment handoff without pinned hashes")
    rows_total = run.get("rows_total")
    if type(rows_total) is not int or rows_total != 132:
        raise AirBuildError("Enrichment handoff requires exactly 132 district rows")
    source_rows_path = run_dir / run["rows_path"]
    if source_rows_path.is_symlink() or not source_rows_path.is_file():
        raise AirBuildError("Pinned source rows are missing or a symlink")
    if sha256_file(source_rows_path) != run["rows_sha256"]:
        raise AirBuildError("Pinned source rows changed")
    forecast_utc = datetime.fromisoformat(run["forecast_at_utc"].replace("Z", "+00:00"))
    data_stale_after = (forecast_utc + timedelta(hours=STALE_AFTER_HOURS)).astimezone(timezone.utc)
    refresh_after = run.get("request_refresh_after_utc", run.get("expires_at_utc"))
    projected_rows: list[dict[str, Any]] = []
    with source_rows_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            # The original immutable capture used a one-hour field name that
            # suggested source-data expiry. The derived handoff clarifies it
            # as request reuse only and separately pins forecast staleness.
            old_expiry = row.pop("expires_at_utc", None)
            row["request_refresh_after_utc"] = row.get("request_refresh_after_utc", old_expiry or refresh_after)
            row["data_stale_after_utc"] = row.get("data_stale_after_utc", data_stale_after.isoformat().replace("+00:00", "Z"))
            projected_rows.append(row)
    if len(projected_rows) != rows_total:
        raise AirBuildError("Handoff row count differs from source manifest")
    handoff_rows_path = root / ".runtime/research/environment-20261011/air/handoffs" / run["run_id"] / "district-air-values.jsonl"
    if handoff_rows_path.exists() or handoff_rows_path.is_symlink():
        if handoff_rows_path.is_symlink() or sha256_file(handoff_rows_path) != sha256_bytes(b"".join(_canonical(row) + b"\n" for row in projected_rows)):
            raise FileExistsError(f"Conflicting handoff rows already exist: {handoff_rows_path}")
    else:
        _write_jsonl(handoff_rows_path, projected_rows)
    rows_sha256 = sha256_file(handoff_rows_path)
    folder = handoff_rows_path.parent
    path = folder / "manifest.json"
    handoff = {
        "schema_version": "environment-air-enrichment-evidence/1",
        "run_id": run["run_id"],
        "source_run_manifest_path": str(run_manifest_path),
        "source_run_manifest_sha256": run_manifest_sha,
        "source_rows_original_sha256": run.get("rows_sha256"),
        "source_rows_reconstructed_from_pinned_raw": False,
        "rows_path": handoff_rows_path.name,
        "rows_sha256": rows_sha256,
        "rows_total": rows_total,
        "ata_geography_manifest_sha256": geography_sha,
        "source_response_sha256": response_sha,
        "source_url_sha256": run.get("source_url_sha256"),
        "forecast_at": run.get("forecast_at"),
        "retrieved_at": run.get("retrieved_at"),
        "request_refresh_after_utc": refresh_after,
        "data_stale_after_utc": data_stale_after.isoformat().replace("+00:00", "Z"),
        "consumer_stale_after_hours": STALE_AFTER_HOURS,
        "model_grid_centers_unique": (run.get("coverage") or {}).get("distinct_returned_grid_centers"),
        "coverage": run.get("coverage"),
        "attribution": ATTRIBUTION,
        "terms_url": TERMS_URL,
        "commercial_use": "not authorized by this capture; provider free API is limited to non-commercial use under terms",
        "measured_district_average": False,
        "publication_status": "staged_private_candidate",
    }
    if path.exists() or path.is_symlink():
        if path.is_symlink() or json.loads(path.read_text(encoding="utf-8")) != handoff:
            raise FileExistsError(f"Conflicting enrichment handoff already exists: {path}")
    else:
        _write_json(path, handoff)
    return path, sha256_file(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make one ordinary public Open-Meteo GET")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    manifest = fetch_candidate(root=args.root, output_root=args.output_root,
                               live=args.live)
    print(json.dumps({
        "run_id": manifest["run_id"],
        "run_dir": manifest["run_dir"],
        "manifest_sha256": manifest["manifest_sha256"],
        "rows_path": str(Path(manifest["run_dir"]) / manifest["rows_path"]),
        "rows_sha256": manifest["rows_sha256"],
        "coverage": manifest["coverage"],
        "forecast_at": manifest["forecast_at"],
        "retrieved_at": manifest["retrieved_at"],
        "request_refresh_after_utc": manifest.get("request_refresh_after_utc", manifest.get("expires_at_utc")),
        "data_stale_after_utc": manifest.get("data_stale_after_utc"),
        "enrichment_manifest_path": manifest.get("enrichment_manifest_path"),
        "enrichment_manifest_sha256": manifest.get("enrichment_manifest_sha256"),
        "active_pointer_changed": False,
        "cache_hit": manifest.get("cache_hit", False),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
