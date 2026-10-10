"""Validated, map-generation-bound apartment and floor menu snapshots.

Raw publisher IDs, source URLs and unprepared media stay in this private module.
The public projection is an explicit allowlist and never implies current stock.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend import developer_map
from backend.store import ROOT


BASE = ROOT / "data/property/developer_units"
ACTIVE_PATH = BASE / "active.json"
RUN_RE = re.compile(r"developer-units-v1-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
UNIT_RE = re.compile(r"unit_[0-9a-f]{24}\Z")
FLOOR_RE = re.compile(r"floor_[0-9a-f]{24}\Z")
ASSET_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
REGION_RE = re.compile(r"region_[0-9a-f]{24}\Z")
_FULL_CACHE: tuple[tuple[Any, ...], dict[str, Any]] | None = None
UNIT_FILTER_NAMES = ("price_min_rub", "price_max_rub", "price_per_m2_min_rub",
                     "price_per_m2_max_rub", "rooms", "area_min_m2", "area_max_m2",
                     "unit_floor_min", "unit_floor_max")
ROOM_LABELS = {"Студия": 0, "Однокомнатная": 1, "Двухкомнатная": 2,
               "Трехкомнатная": 3, "Трёхкомнатная": 3,
               "Четырёхкомнатная": 4, "Пятикомнатная": 5}


def _safe_file(path: Path) -> Path:
    if not path.is_relative_to(BASE):
        raise ValueError("Unit menu path escapes its root")
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        if not part or part in {".", ".."} or part.startswith("."):
            raise ValueError("Unsafe unit menu path")
        current /= part
        if current.is_symlink():
            raise ValueError("Unit menu path contains a symlink")
    if not current.is_file():
        raise FileNotFoundError("Unit menu file is missing")
    return current


def _digest(path: Path, limit: int) -> str:
    path = _safe_file(path)
    if path.stat().st_size > limit:
        raise ValueError("Unit menu file exceeds its byte limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, limit: int) -> dict[str, Any]:
    if _safe_file(path).stat().st_size > limit:
        raise ValueError("Unit menu JSON exceeds its byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Unit menu JSON must be an object")
    return value


def _rows(path: Path, limit: int) -> list[dict[str, Any]]:
    path = _safe_file(path)
    if path.stat().st_size > limit:
        raise ValueError("Unit menu rows exceed their byte limit")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Invalid unit menu JSONL row")
            rows.append(row)
    return rows


def _text(value: Any, *, nullable: bool = False, limit: int = 400) -> bool:
    return ((nullable and value is None) or
            (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()))


def _timestamp(value: Any, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    if not _text(value, limit=40):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _unit_id(provider: str, publisher_unit_id: str) -> str:
    return "unit_" + hashlib.sha256(f"{provider}\0{publisher_unit_id}".encode()).hexdigest()[:24]


def _floor_id(map_run_id: str, registry_id: str, section: str | None,
              floor: int) -> str:
    identity = f"{map_run_id}\0{registry_id}\0{section or ''}\0{floor}"
    return "floor_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def section_id(map_run_id: str, registry_id: str, section: str | None) -> str | None:
    if section is None:
        return None
    identity = f"{map_run_id}\0{registry_id}\0{section}"
    return "section_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _unit(row: dict[str, Any], buildings: dict[str, dict[str, Any]]) -> None:
    building = buildings.get(row.get("registry_id"))
    if building is None or row.get("source_group") != building["source_group"]:
        raise ValueError("Unit has no exact current building identity")
    for key in ("publisher_unit_id", "source_group", "publisher_project_id",
                "publisher_building_id", "source_corpus_label", "section_label",
                "unit_number", "rooms", "price_basis"):
        if not _text(row.get(key), nullable=True):
            raise ValueError(f"Invalid unit {key}")
    if (row["publisher_project_id"] != building["publisher_project_id"] or
            row["publisher_building_id"] != building["publisher_building_id"] or
            row["source_corpus_label"] != building["source_corpus_label"]):
        raise ValueError("Unit crosses publisher building identity")
    if (row["publisher_unit_id"] is None or
            row.get("unit_id") != _unit_id(row["source_group"], row["publisher_unit_id"])):
        raise ValueError("Invalid opaque unit ID")
    floor = row.get("floor_number")
    if floor is not None and (type(floor) is not int or not -10 <= floor <= 300):
        raise ValueError("Invalid unit floor")
    area = row.get("area_m2")
    if area is not None and (type(area) not in {int, float} or
                             not math.isfinite(area) or not 0 < area <= 10_000):
        raise ValueError("Invalid unit area")
    price = row.get("price_rub")
    if price is not None and (type(price) is not int or not 0 <= price <= 10**12):
        raise ValueError("Invalid observed unit amount")
    if (not _timestamp(row.get("observed_at")) or
            not _timestamp(row.get("source_updated_at"), nullable=True) or
            not isinstance(row.get("source_evidence_sha256"), str) or
            SHA_RE.fullmatch(row["source_evidence_sha256"]) is None):
        raise ValueError("Invalid unit observation evidence")


def _building(row: dict[str, Any], map_rows: dict[str, dict[str, Any]]) -> None:
    if row.get("registry_id") not in map_rows:
        raise ValueError("Menu building is absent from current map")
    total = row.get("total_floors")
    observed = row.get("max_observed_unit_floor")
    if total is not None and (type(total) is not int or not 1 <= total <= 300):
        raise ValueError("Invalid building total floors")
    if observed is not None and (type(observed) is not int or not -10 <= observed <= 300):
        raise ValueError("Invalid observed floor bound")
    if total is not None and observed is not None and observed > total:
        raise ValueError("Observed floor exceeds confirmed total")
    height = row.get("estimated_height_m")
    if height is not None and (type(height) not in {int, float} or
                               not math.isfinite(height) or not 0 < height <= 1500):
        raise ValueError("Invalid estimated building height")
    for key in ("total_floors_basis", "height_basis"):
        if not _text(row.get(key), nullable=True):
            raise ValueError(f"Invalid building {key}")
    for key, upper in (("source_section_total_floors", 300),
                       ("source_section_estimated_height_m", 1500)):
        values = row.get(key, {})
        if not isinstance(values, dict):
            raise ValueError("Invalid section-level building evidence")
        for section, candidates in values.items():
            if (not _text(section, limit=100) or not isinstance(candidates, list) or
                    not 1 <= len(candidates) <= 20 or
                    any(type(value) not in {int, float} or not math.isfinite(value) or
                        not 0 < value <= upper for value in candidates)):
                raise ValueError("Invalid section-level building evidence")


def section_total_floors(building: dict[str, Any], section: str | None,
                         observed_max: int | None) -> tuple[int | None, str | None]:
    if section is None:
        return None, None
    candidates = building.get("source_section_total_floors", {}).get(section, [])
    if (len(candidates) != 1 or type(candidates[0]) is not int or
            observed_max is not None and observed_max > candidates[0]):
        return None, None
    return candidates[0], "source_section_floor_count"


def _plan_regions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    regions = plan.get("regions", [])
    if not isinstance(regions, list) or len(regions) > 100:
        raise ValueError("Invalid floor plan regions")
    if not regions:
        return []
    if (plan.get("role") != "floor_plan" or plan.get("floor_id") is None or
            plan.get("asset_sha256") is None or
            plan.get("regions_asset_sha256") != plan["asset_sha256"]):
        raise ValueError("Plan regions are not bound to a prepared floor image")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("Invalid floor plan region")
        region_id = region.get("region_id")
        polygon = region.get("polygon")
        area = region.get("source_area_m2")
        if (not isinstance(region_id, str) or REGION_RE.fullmatch(region_id) is None or
                region_id in seen or not isinstance(polygon, list) or
                not 3 <= len(polygon) <= 100 or
                any(not isinstance(vertex, list) or len(vertex) != 2 or
                    any(type(value) not in {int, float} or not math.isfinite(value) or
                        not 0 <= value <= 1 for value in vertex) for vertex in polygon) or
                not _text(region.get("source_label"), nullable=True, limit=160) or
                not _text(region.get("source_rooms"), nullable=True, limit=80) or
                area is not None and (type(area) not in {int, float} or
                                      not math.isfinite(area) or not 0 < area <= 10_000) or
                region.get("unit_id") is not None or
                region.get("match_status") != "unconfirmed" or
                region.get("basis") != "raster_traced_floor_plan" or
                region.get("reconstruction") is not True):
            raise ValueError("Unsafe or unproven floor plan region")
        seen.add(region_id)
        output.append({"region_id": region_id, "polygon": polygon,
                       "coordinate_frame": "image_normalized_top_left",
                       "source_label": region.get("source_label"),
                       "source_rooms": region.get("source_rooms"),
                       "source_area_m2": area, "unit_id": None,
                       "match_status": "unconfirmed",
                       "basis": "raster_traced_floor_plan",
                       "reconstruction": True, "unit_action": False})
    return output


def _price_context(basis: str | None) -> str:
    if basis and "100%" in basis:
        return "payment_100_percent"
    if basis and ("marketing" in basis or "promo" in basis):
        return "promotion_or_conditions"
    if basis == "source base asking price":
        return "source_base_amount"
    if basis == "asking_total":
        return "source_displayed_total"
    return "conditions_unverified"


def normalize_unit_filters(**raw: Any) -> dict[str, int | float]:
    """Validate source-observation filters identically for all map projections."""
    if set(raw) - set(UNIT_FILTER_NAMES):
        raise ValueError("Unknown apartment filter")
    checked: dict[str, int | float] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key in {"area_min_m2", "area_max_m2"}:
            if (type(value) not in {int, float} or not math.isfinite(value) or
                    not 0 < value <= 10_000):
                raise ValueError(f"Invalid apartment filter {key}")
            checked[key] = value
        elif (type(value) is not int or
              (not -10 <= value <= 300 if key.startswith("unit_floor_") else
               not 0 <= value <= 20 if key == "rooms" else
               not 0 <= value <= 1_000_000_000 if key.startswith("price_per_m2_") else
               not 0 <= value <= 10**12)):
            raise ValueError(f"Invalid apartment filter {key}")
        else:
            checked[key] = value
    for low, high in (("price_min_rub", "price_max_rub"),
                      ("price_per_m2_min_rub", "price_per_m2_max_rub"),
                      ("area_min_m2", "area_max_m2"),
                      ("unit_floor_min", "unit_floor_max")):
        if low in checked and high in checked and checked[low] > checked[high]:
            raise ValueError(f"Apartment filter range {low}/{high} is reversed")
    return checked


def _room_count(label: str | None) -> int | None:
    if label is None:
        return None
    if label in ROOM_LABELS:
        return ROOM_LABELS[label]
    if re.fullmatch(r"\d+", label):
        return int(label)
    match = re.fullmatch(r"(\d+)[ЕE]?-комн\.", label)
    return int(match.group(1)) if match else None


def _matches_unit(row: dict[str, Any], filters: dict[str, int | float]) -> bool:
    price = row["price_rub"]
    area = row["area_m2"]
    floor = row["floor_number"]
    for key in ("price_min_rub", "price_max_rub"):
        if key in filters and (price is None or
                (price < filters[key] if key.endswith("min_rub") else price > filters[key])):
            return False
    for key in ("price_per_m2_min_rub", "price_per_m2_max_rub"):
        if key in filters:
            if price is None or area is None or area <= 0:
                return False
            actual = Decimal(price) / Decimal(str(area))
            if (actual < filters[key] if "_min_" in key else actual > filters[key]):
                return False
    if "rooms" in filters and _room_count(row["rooms"]) != filters["rooms"]:
        return False
    for key in ("area_min_m2", "area_max_m2"):
        if key in filters and (area is None or
                (area < filters[key] if "_min_" in key else area > filters[key])):
            return False
    for key in ("unit_floor_min", "unit_floor_max"):
        if key in filters and (floor is None or
                (floor < filters[key] if key.endswith("min") else floor > filters[key])):
            return False
    return True


def matching_unit_counts(menu: dict[str, Any], registry_ids: set[str],
                         filters: dict[str, int | float]) -> dict[str, int]:
    """Every returned building has one unit satisfying all requested facts."""
    if not filters:
        return {registry_id: len(menu["units_by_building"][registry_id])
                for registry_id in registry_ids}
    return {registry_id: sum(_matches_unit(row, filters)
                             for row in menu["units_by_building"][registry_id])
            for registry_id in registry_ids}


def filter_units(rows: list[dict[str, Any]],
                 filters: dict[str, int | float]) -> list[dict[str, Any]]:
    return [row for row in rows if _matches_unit(row, filters)] if filters else rows


@lru_cache(maxsize=2)
def _validated(run_id: str, manifest_sha256: str,
               signatures: tuple[tuple[str, int, int, int, int], ...],
               map_snapshot: str, map_run_id: str, map_manifest_sha256: str) -> dict[str, Any]:
    directory = BASE / "runs" / run_id
    manifest_path = directory / "manifest.json"
    if _digest(manifest_path, 100_000) != manifest_sha256:
        raise ValueError("Unit menu manifest differs from active pointer")
    manifest = _json(manifest_path, 100_000)
    if (manifest.get("schema_version") != "developer-units-snapshot/1" or
            manifest.get("run_id") != run_id or
            manifest.get("map_run_id") != map_run_id or
            manifest.get("map_snapshot_id") != map_snapshot or
            manifest.get("map_manifest_sha256") != map_manifest_sha256):
        raise ValueError("Unit menu is not bound to the current map generation")
    files: dict[str, list[dict[str, Any]]] = {}
    for name, limit in (("buildings", 10_000_000), ("units", 120_000_000),
                        ("floors", 50_000_000), ("plans", 50_000_000),
                        ("unit_aliases", 10_000_000)):
        expected = manifest.get(f"{name}_sha256")
        total = manifest.get(f"{name}_total")
        if (not isinstance(expected, str) or SHA_RE.fullmatch(expected) is None or
                type(total) is not int or total < 0):
            raise ValueError("Invalid unit menu file descriptor")
        path = directory / f"{name}.jsonl"
        if _digest(path, limit) != expected:
            raise ValueError("Unit menu file hash mismatch")
        files[name] = _rows(path, limit)
        if len(files[name]) != total:
            raise ValueError("Unit menu file count mismatch")
    return {"manifest": manifest, "files": files, "run_directory": directory}


def load_staged(run_id: str, map_run: dict[str, Any], *,
                expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Validate a private menu run against one pinned developer map snapshot."""
    if not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None:
        raise ValueError("Invalid unit menu run ID")
    manifest_path = _safe_file(BASE / "runs" / run_id / "manifest.json")
    digest = _digest(manifest_path, 100_000)
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise ValueError("Unit menu pointer manifest SHA mismatch")
    paths = [(name, _safe_file(BASE / "runs" / run_id / name)) for name in
             ("manifest.json", "buildings.jsonl", "units.jsonl", "floors.jsonl",
              "plans.jsonl", "unit_aliases.jsonl")]
    signatures = tuple((name, path.stat().st_mtime_ns, path.stat().st_size,
                        path.stat().st_ctime_ns, path.stat().st_ino) for name, path in paths)
    map_run_id = map_run["manifest"]["run_id"]
    map_snapshot = map_run["manifest"]["snapshot_id"]
    map_manifest_sha256 = hashlib.sha256(
        (developer_map.BASE / "runs" / map_run_id / "manifest.json").read_bytes()).hexdigest()
    cache_key = (run_id, digest, signatures, map_run_id, map_snapshot,
                 map_manifest_sha256)
    global _FULL_CACHE
    if _FULL_CACHE is not None and _FULL_CACHE[0] == cache_key:
        return _FULL_CACHE[1]
    loaded = _validated(run_id, digest, signatures, map_snapshot, map_run_id,
                        map_manifest_sha256)
    # Validation depends on the pinned map's contents as well as its manifest SHA.
    # Map immutable files have already passed developer_map.load_staged().
    files = loaded["files"]
    by_registry = map_run["by_registry"]
    menu_buildings: dict[str, dict[str, Any]] = {}
    for row in files["buildings"]:
        _building(row, by_registry)
        registry_id = row["registry_id"]
        if registry_id in menu_buildings:
            raise ValueError("Duplicate menu building")
        menu_buildings[registry_id] = row
    if set(menu_buildings) != set(by_registry):
        raise ValueError("Unit menu does not cover the active map list")
    by_unit: dict[str, dict[str, Any]] = {}
    units_by_building: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in files["units"]:
        _unit(row, by_registry)
        unit_id = row["unit_id"]
        if unit_id in by_unit:
            raise ValueError("Duplicate opaque unit identity")
        by_unit[unit_id] = row
        units_by_building[row["registry_id"]].append(row)
    if any(len(units_by_building[registry_id]) != by_registry[registry_id]["unit_count"]
           for registry_id in by_registry):
        raise ValueError("Menu unit count differs from active map source count")
    by_floor: dict[str, dict[str, Any]] = {}
    floor_unit_ids: set[str] = set()
    for row in files["floors"]:
        registry_id = row.get("registry_id")
        section = row.get("section_label")
        floor = row.get("floor_number")
        if (registry_id not in by_registry or
                not _text(section, nullable=True) or
                type(floor) is not int or not -10 <= floor <= 300 or
                row.get("floor_id") != _floor_id(map_run_id, registry_id, section, floor)):
            raise ValueError("Invalid exact floor identity")
        members = row.get("unit_ids")
        if (not isinstance(members, list) or not members or
                len(members) != len(set(members)) or row["floor_id"] in by_floor):
            raise ValueError("Invalid floor members")
        for unit_id in members:
            unit = by_unit.get(unit_id)
            if (unit is None or unit["registry_id"] != registry_id or
                    unit["section_label"] != section or unit["floor_number"] != floor or
                    unit_id in floor_unit_ids):
                raise ValueError("Floor member conflicts with exact unit facts")
            floor_unit_ids.add(unit_id)
        by_floor[row["floor_id"]] = row
    if floor_unit_ids != {unit_id for unit_id, row in by_unit.items()
                          if row["floor_number"] is not None}:
        raise ValueError("Known-floor unit is missing its floor group")
    aliases = files["unit_aliases"]
    for alias in aliases:
        building = by_registry.get(alias.get("registry_id"))
        if building is None or not building.get("cross_source_identity"):
            raise ValueError("Unreviewed cross-source unit alias")
    assets: dict[str, dict[str, Any]] = {}
    plans_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    plans_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for plan in files["plans"]:
        unit_id, floor_id = plan.get("unit_id"), plan.get("floor_id")
        if ((unit_id is None) == (floor_id is None) or
                unit_id is not None and unit_id not in by_unit or
                floor_id is not None and floor_id not in by_floor):
            raise ValueError("Plan lacks exact unit or floor membership")
        role = plan.get("role")
        if role not in {"apartment_plan", "furnished_apartment_plan", "floor_plan"}:
            raise ValueError("Unknown plan role")
        if ((floor_id is not None and role != "floor_plan") or
                (unit_id is not None and role == "floor_plan")):
            raise ValueError("Plan role conflicts with exact unit or floor scope")
        _plan_regions(plan)
        if unit_id is not None:
            plans_by_unit[unit_id].append(plan)
        else:
            plans_by_floor[floor_id].append(plan)
        asset_sha = plan.get("asset_sha256")
        if asset_sha is None:
            continue
        mime = plan.get("asset_mime")
        if (not isinstance(asset_sha, str) or SHA_RE.fullmatch(asset_sha) is None or
                mime not in ASSET_MIME):
            raise ValueError("Unsafe prepared plan asset descriptor")
        prior = assets.get(asset_sha)
        if prior is not None and prior["asset_mime"] != mime:
            raise ValueError("Prepared asset MIME conflict")
        assets[asset_sha] = plan
    snapshot = {"manifest": loaded["manifest"], "run_directory": loaded["run_directory"],
                "buildings": menu_buildings, "units": by_unit,
                "units_by_building": units_by_building, "floors": by_floor,
                "plans_by_unit": plans_by_unit, "plans_by_floor": plans_by_floor,
                "assets": assets}
    _FULL_CACHE = cache_key, snapshot
    return snapshot


def resolve_active(map_run: dict[str, Any]) -> dict[str, Any] | None:
    """An absent menu is pending; a broken or mismatched pointer is never ignored."""
    if not ACTIVE_PATH.exists() and not ACTIVE_PATH.is_symlink():
        return None
    pointer = _json(ACTIVE_PATH, 4096)
    if (set(pointer) != {"schema_version", "run_id", "manifest_sha256"} or
            pointer["schema_version"] != "developer-units-pointer/1" or
            not isinstance(pointer["manifest_sha256"], str) or
            SHA_RE.fullmatch(pointer["manifest_sha256"]) is None):
        raise ValueError("Invalid unit menu pointer")
    return load_staged(pointer["run_id"], map_run,
                       expected_manifest_sha256=pointer["manifest_sha256"])


def public_unit(row: dict[str, Any], map_run_id: str,
                plans: list[dict[str, Any]]) -> dict[str, Any]:
    floor = row["floor_number"]
    section = row["section_label"]
    return {"unit_id": row["unit_id"],
            "unit_label": row["unit_number"],
            "section_id": section_id(map_run_id, row["registry_id"], section),
            "section_label": section,
            "floor_id": (_floor_id(map_run_id, row["registry_id"], section, floor)
                         if floor is not None else None),
            "floor_number": floor, "rooms_label": row["rooms"],
            "area_m2": row["area_m2"], "price_rub": row["price_rub"],
            "price_context": _price_context(row["price_basis"]),
            "observed_at": row["observed_at"],
            "record_updated_at": row["source_updated_at"],
            "availability": "unknown",
            "has_prepared_plan": any(plan.get("asset_sha256") is not None for plan in plans)}


def public_plan(plan: dict[str, Any]) -> dict[str, Any] | None:
    asset_sha = plan.get("asset_sha256")
    if asset_sha is None:
        return None
    return {"role": plan["role"], "asset_url": f"/v1/developer-unit-assets/{asset_sha}",
            "asset_sha256": asset_sha, "mime_type": plan["asset_mime"],
            "geometry_status": "source_plan_image"}


def public_plan_regions(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _plan_regions(plan) if plan is not None else []


def asset_bytes(snapshot: dict[str, Any], sha256: str) -> tuple[bytes, str]:
    if SHA_RE.fullmatch(sha256) is None or sha256 not in snapshot["assets"]:
        raise FileNotFoundError("Prepared plan asset is not in the active menu")
    descriptor = snapshot["assets"][sha256]
    mime = descriptor["asset_mime"]
    path = snapshot["run_directory"] / "assets" / (sha256 + ASSET_MIME[mime])
    path = _safe_file(path)
    if path.stat().st_size > 10_000_000:
        raise ValueError("Prepared plan asset exceeds byte limit")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError("Prepared plan asset hash mismatch")
    if not ((mime == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n")) or
            (mime == "image/jpeg" and content.startswith(b"\xff\xd8\xff")) or
            (mime == "image/webp" and content.startswith(b"RIFF") and
             content[8:12] == b"WEBP")):
        raise ValueError("Prepared plan asset content type mismatch")
    return content, mime
