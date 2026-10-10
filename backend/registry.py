"""Strict, offer-free projection of the supplied Moscow building registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend import districts
from backend.generation import current_generation
from backend.public import EXPOSE_SOURCES
from backend.store import ROOT
from backend.visibility import load_visibility


REGISTRY_DIR = ROOT / "data/property/estateline"
PROJECTS_PATH = REGISTRY_DIR / "projects.jsonl"
MANIFEST_PATH = REGISTRY_DIR / "source_manifest.json"
DATASET_ID = "building_registry"
ALLOWED_KINDS = frozenset({"multifamily", "apartment_complex", "low_rise"})


def registry_mode() -> str:
    mode = os.environ.get("NOVOSTROY_REGISTRY_MODE", "auto")
    if mode not in {"auto", "off", "estateline"}:
        raise RuntimeError("NOVOSTROY_REGISTRY_MODE must be auto, off or estateline")
    return mode


def registry_enabled() -> bool:
    mode = registry_mode()
    if mode == "off":
        return False
    if current_generation() is not None:
        load_registry()
        return True
    present = (PROJECTS_PATH.is_file(), MANIFEST_PATH.is_file())
    if present == (False, False):
        if mode == "estateline":
            raise RuntimeError("Configured registry import is missing")
        return False
    if present != (True, True):
        raise RuntimeError("Registry import is incomplete")
    load_registry()
    return True


def _optional_string(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Registry {field} must be a string or null")
    return value.strip() or None


def _optional_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Registry {field} must be a non-negative integer or null")
    return value


def _public_record(row: dict[str, Any]) -> dict[str, Any]:
    source_id = row.get("registry_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("Registry identity is missing")
    name = _optional_string(row.get("name"), "name")
    if not name:
        raise ValueError("Registry name is missing")
    if row.get("city") not in {"Москва", "Moscow"}:
        raise ValueError("Registry includes a non-Moscow or unresolved city")
    kind = row.get("record_kind")
    if kind not in ALLOWED_KINDS:
        raise ValueError("Registry includes a non-residential or unresolved kind")
    known_date = row.get("latest_known_date")
    if not isinstance(known_date, str) or date.fromisoformat(known_date).isoformat() != known_date:
        raise ValueError("Registry latest_known_date must be ISO YYYY-MM-DD")
    location = row.get("location")
    location_quality = row.get("location_quality")
    if location is not None:
        if not (location_quality == "verified" and isinstance(location, list) and len(location) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in location)
                and 36.7 <= location[0] <= 38.4 and 55.0 <= location[1] <= 56.4):
            raise ValueError("Registry location lacks a verified Moscow point")
        location_source = (row.get("provenance") or {}).get("location_source")
        if (row.get("location_method") != "strict_osm_address_tuple"
                or not isinstance(location_source, dict)
                or not all(location_source.get(key) for key in
                           ("osm_type", "osm_id", "osm_version", "osm_timestamp",
                            "members", "representative_point_method", "source_attribution"))):
            raise ValueError("Registry location lacks exact-match source evidence")
    elif location_quality not in {None, "unknown"}:
        raise ValueError("Registry location quality claims a missing point")
    public_id = "registry-" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:24]
    record = {
        "registry_id": public_id,
        "name": name,
        "record_kind": kind,
        "city": "Москва",
        "address": _optional_string(row.get("address"), "address"),
        "location": location,
        "location_quality": "verified" if location is not None else None,
        "latest_known_date": known_date,
        "status": _optional_string(row.get("status"), "status"),
        "handover": _optional_string(row.get("handover"), "handover"),
        "handover_year": _optional_integer(row.get("handover_year"), "handover_year"),
        "handover_quarter": _optional_integer(row.get("handover_quarter"), "handover_quarter"),
        "handover_precision": _optional_string(row.get("handover_precision"), "handover_precision"),
        "floors": row.get("floors"),
        "building_type": _optional_string(row.get("building_type"), "building_type"),
        "has_apartment_data": False,
        "offer_count": 0,
        "price": None,
    }
    if record["handover_quarter"] is not None and not 1 <= record["handover_quarter"] <= 4:
        raise ValueError("Registry handover_quarter must be 1..4")
    if record["floors"] is not None and (
        isinstance(record["floors"], bool) or not isinstance(record["floors"], (str, int))
        or isinstance(record["floors"], int) and record["floors"] <= 0
    ):
        raise ValueError("Registry floors must be a positive integer, string or null")
    if EXPOSE_SOURCES:
        record["source"] = {key: row[key] for key in
                            ("registry_id", "source_row_id", "source_category", "provenance", "location_method")
                            if key in row}
    return record


@lru_cache(maxsize=4)
def _load_registry(projects_path: Path, manifest_path: Path,
                   projects_mtime_ns: int, projects_size: int,
                   manifest_mtime_ns: int, manifest_size: int,
                   district_geo_mtime_ns: int, district_geo_size: int,
                   district_manifest_mtime_ns: int, district_manifest_size: int) -> dict[str, Any]:
    manifest_payload = manifest_path.read_bytes()
    manifest = json.loads(manifest_payload)
    if not isinstance(manifest, dict):
        raise ValueError("Registry manifest must be an object")
    if manifest.get("schema_version") != "estateline-projects-v1":
        raise ValueError("Unsupported registry import schema")
    if manifest.get("rights_status") != "user_supplied_internal_use":
        raise ValueError("Registry internal-use rights marker is missing")
    expected_hash = manifest.get("projects_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise ValueError("Registry manifest projects_sha256 is invalid")
    expected_count = manifest.get("records_total")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise ValueError("Registry manifest records_total is invalid")
    payload = projects_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("Registry import hash does not match its manifest")
    raw_rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
    if len(raw_rows) != expected_count:
        raise ValueError("Registry import count does not match its manifest")
    if not all(isinstance(row, dict) for row in raw_rows):
        raise ValueError("Registry records must be JSON objects")
    if any(row.get("schema_version") != "estateline-projects-v1" for row in raw_rows):
        raise ValueError("Registry record schema does not match manifest")
    source_ids = [row.get("registry_id") for row in raw_rows]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("Registry identity is duplicated")
    items = [_public_record(row) for row in raw_rows]
    if len({row["registry_id"] for row in items}) != len(items):
        raise ValueError("Public registry identity collision")
    source_to_public = {row["registry_id"]: item["registry_id"]
                        for row, item in zip(raw_rows, items, strict=True)}
    items.sort(key=lambda row: (row["name"].casefold(), row["registry_id"]))
    district_release, district_names = districts.classify_verified_points(items)
    newest = max(row["latest_known_date"] for row in items)
    digest = hashlib.sha256(payload).hexdigest()
    source_dates = {row["registry_id"]: row["latest_known_date"] for row in raw_rows}
    return {"dataset_id": DATASET_ID, "snapshot_id": f"building-registry-{digest[:16]}",
            "snapshot_at": newest, "items": items,
            "projects_sha256": digest,
            "source_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "source_to_public": source_to_public, "source_dates": source_dates,
            "district_names": district_names,
            "district_basis": {"source": "Overture Maps / OpenStreetMap",
                               "release": district_release,
                               "quality": "mapped_candidate_unique_point"}}


def load_registry() -> dict[str, Any]:
    generation = current_generation()
    projects_path = generation["paths"]["projects"] if generation else PROJECTS_PATH
    manifest_path = generation["paths"]["source_manifest"] if generation else MANIFEST_PATH
    if not projects_path.is_file() or not manifest_path.is_file():
        raise RuntimeError("Registry import is missing")
    projects, manifest = projects_path.stat(), manifest_path.stat()
    district_geo, district_manifest = districts.GEOJSON_PATH.stat(), districts.MANIFEST_PATH.stat()
    base = _load_registry(projects_path, manifest_path,
                          projects.st_mtime_ns, projects.st_size,
                          manifest.st_mtime_ns, manifest.st_size,
                          district_geo.st_mtime_ns, district_geo.st_size,
                          district_manifest.st_mtime_ns, district_manifest.st_size)
    policy = load_visibility(base["projects_sha256"], base["source_manifest_sha256"],
                             base["source_dates"],
                             pinned=generation["visibility"] if generation else None)
    visible_ids = (set(base["source_to_public"].values()) if policy is None else
                   {base["source_to_public"][source_id] for source_id, row in policy["records"].items()
                    if row["visibility"] == "visible"})
    policy_hash = policy["manifest_sha256"] if policy else "no-visibility-policy"
    district_hash = hashlib.sha256(districts.MANIFEST_PATH.read_bytes()).hexdigest()
    eligibility_hash = hashlib.sha256(
        f"{base['snapshot_id']}|{policy_hash}|{district_hash}".encode("ascii")
    ).hexdigest()
    public_base = {key: value for key, value in base.items()
                   if key not in {"source_to_public", "source_dates"}}
    return {**public_base, "source_snapshot_id": base["snapshot_id"],
            "snapshot_id": f"building-eligibility-{eligibility_hash[:16]}",
            "visible_ids": {registry_id: True for registry_id in visible_ids},
            "visibility": {"source_total": len(base["items"]), "visible_total": len(visible_ids),
                           "archived_total": len(base["items"]) - len(visible_ids),
                           "policy_run_id": policy["manifest"]["run_id"] if policy else None,
                           "policy_as_of": policy["manifest"]["as_of_date"] if policy else None}}


def select_records(registry: dict[str, Any], *, query: str | None = None,
                   status: str | None = None, kind: str | None = None,
                   district: str | None = None,
                   handover_year: int | None = None, handover_quarter: int | None = None,
                   floors_min: int | None = None, floors_max: int | None = None,
                   walk_metro_max_min: int | None = None,
                   walk_school_max_min: int | None = None,
                   walk_kindergarten_max_min: int | None = None,
                   walk_medical_max_min: int | None = None,
                   walking_minutes: dict[str, dict[str, int | None]] | None = None) -> list[dict[str, Any]]:
    if kind is not None and kind not in ALLOWED_KINDS:
        raise ValueError("Unknown registry kind")
    if district is not None and (not isinstance(district, str) or not district.strip()
                                 or len(district) > 160):
        raise ValueError("Invalid mapped district label")
    if handover_year is not None and (type(handover_year) is not int or not 1900 <= handover_year <= 2100):
        raise ValueError("Invalid handover year")
    if handover_quarter is not None and (type(handover_quarter) is not int or not 1 <= handover_quarter <= 4):
        raise ValueError("Invalid handover quarter")
    if any(value is not None and (type(value) is not int or not 1 <= value <= 300)
           for value in (floors_min, floors_max)):
        raise ValueError("Invalid building floor range")
    if floors_min is not None and floors_max is not None and floors_min > floors_max:
        raise ValueError("Building floor minimum exceeds maximum")
    walk_limits = {"metro": walk_metro_max_min, "school": walk_school_max_min,
                   "kindergarten": walk_kindergarten_max_min, "medical": walk_medical_max_min}
    if any(value is not None and (type(value) is not int or not 1 <= value <= 180)
           for value in walk_limits.values()):
        raise ValueError("Invalid walking-minute limit")
    if any(value is not None for value in walk_limits.values()) and walking_minutes is None:
        raise ValueError("Walking filter facts are unavailable")
    needle = query.strip().casefold() if query else None
    return [row for row in registry["items"]
            if row["registry_id"] in registry["visible_ids"]
            and (not needle or needle in row["name"].casefold() or
                needle in (row["address"] or "").casefold())
            and (status is None or row["status"] == status)
            and (kind is None or row["record_kind"] == kind)
            and (district is None or registry["district_names"].get(row["registry_id"]) == district)
            and (handover_year is None or row["handover_year"] == handover_year)
            and (handover_quarter is None or row["handover_quarter"] == handover_quarter)
            and (floors_min is None or type(row["floors"]) is int and row["floors"] >= floors_min)
            and (floors_max is None or type(row["floors"]) is int and row["floors"] <= floors_max)
            and all(limit is None or (
                (minutes := (walking_minutes or {}).get(row["registry_id"], {}).get(category)) is not None
                and minutes <= limit)
                for category, limit in walk_limits.items())]
