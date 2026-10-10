"""Stage a private apartment/floor menu pinned to one developer-map generation.

The input is an immutable local harvest delivery. No network or active pointer
mutation occurs during ``build``. Floor numbers from units are lower bounds;
only explicit publisher floor-count fields may produce an estimated height.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from sources.property.developer_map import (
    ROOT, UNIT_PARTITIONS, _canonical, _checked_delivery, _checked_file,
    _file_sha, _fsync_dir, _rows, _sha, _write_private, validate_run,
)


RULES = "developer-unit-menu-v1"
OUTPUT = ROOT / "data/property/developer_units"
MAP_ROOT = ROOT / "data/property/developer_map"
PLAN_ROLES = {"apartment_plan", "furnished_apartment_plan", "floor_plan"}
REVIEWED_SECTION_RANGE = {
    "registry_id": "registry-874a6b9e5d79e29805accb61",
    "source_group": "proplanirovki",
    "publisher_project_id": "6229c373a0f098122780f32c",
    "publisher_building_id": "6229c60da0f0987a7c80f455",
    "project_page": "https://nametkin-tower.ru/",
    "project_sections": 4,
    "project_section_floor_range": [24, 29],
    "evidence_date": "2026-10-11",
    "basis": "official_project_section_range_no_corpus_part_assignment",
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _floor(value: Any) -> int | None:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d{1,3}", value.strip()):
        number = int(value.strip())
    else:
        return None
    return number if -10 <= number <= 300 else None


def _positive_floors(value: Any) -> int | None:
    number = _floor(value)
    return number if number is not None and 1 <= number <= 300 else None


def _unit_id(source: str, publisher_unit_id: str) -> str:
    return "unit_" + _sha(f"{source}\0{publisher_unit_id}".encode())[:24]


def _floor_id(map_run_id: str, registry_id: str, section: str | None,
              floor: int) -> str:
    return "floor_" + _sha(
        f"{map_run_id}\0{registry_id}\0{section or ''}\0{floor}".encode())[:24]


def _point_key(source: str, project: str, building: dict[str, Any]) -> tuple[str, str, str] | None:
    building_id = _text(building.get("publisher_building_id"))
    if building_id:
        return source, project, "id:" + building_id
    corpus = _text(building.get("corpus"))
    return (source, project, "corpus:" + corpus.casefold()) if corpus else None


def _map_index(map_run_id: str) -> tuple[dict[tuple[str, str, str], dict[str, Any]],
                                         dict[tuple[str, str, str], dict[str, Any]],
                                         list[dict[str, Any]], str, dict[str, Any]]:
    manifest = validate_run(MAP_ROOT, map_run_id)
    manifest_sha = _file_sha(MAP_ROOT / "runs" / map_run_id / "manifest.json")
    pointer_path = MAP_ROOT / "active.json"
    pointer = json.loads(pointer_path.read_bytes())
    if pointer.get("run_id") != map_run_id or pointer.get("manifest_sha256") != manifest_sha:
        raise ValueError("Menu must bind to the active developer-map generation")
    points = list(_rows(MAP_ROOT / "runs" / map_run_id / "points.jsonl"))
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    aliases: dict[tuple[str, str, str], dict[str, Any]] = {}
    for point in points:
        source = point["source_group"]
        project = point["publisher_project_id"]
        building = point.get("publisher_building_id")
        key = (source, project, "id:" + building if building else
               "corpus:" + point["source_corpus_label"].casefold())
        if key in index:
            raise ValueError("Ambiguous map building identity")
        index[key] = point
        # A101 has one reviewed numeric building ID alongside the same exact
        # project/corpus label used by its other units. This is a source-owned
        # alias, not a building-number or proximity match.
        if source == "a101" and building and point.get("source_corpus_label"):
            label_key = (source, project,
                         "corpus:" + point["source_corpus_label"].casefold())
            if label_key in index and index[label_key] is not point:
                raise ValueError("Ambiguous A101 project/corpus alias")
            index[label_key] = point
        # For the sole reviewed physical merge, retain the second provider's
        # observations as private review aliases rather than duplicate units.
        for second_source, second in (point.get("cross_source_identity") or {}).items():
            if not isinstance(second, dict):
                continue
            second_key = (second_source, second["publisher_project_id"],
                          "id:" + second["publisher_building_id"])
            if second_key in aliases:
                raise ValueError("Ambiguous cross-source unit alias")
            aliases[second_key] = point
    return index, aliases, points, manifest_sha, manifest


def _select_units(paths: dict[str, Path], index: dict[tuple[str, str, str], dict[str, Any]],
                  aliases: dict[tuple[str, str, str], dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                              list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    seen_source_ids: set[tuple[str, str]] = set()
    for partition in UNIT_PARTITIONS:
        for row in _rows(paths[partition]):
            source = row.get("source_group")
            unit = row.get("unit") or {}
            project = row.get("project") or {}
            publisher_unit_id = _text(unit.get("publisher_unit_id"))
            project_id = _text(project.get("publisher_project_id"))
            if not source or not publisher_unit_id or not project_id:
                continue
            key = _point_key(source, project_id, row.get("building") or {})
            if key is None:
                continue
            point = index.get(key)
            alias_point = aliases.get(key)
            if point is None and alias_point is None:
                continue
            source_identity = source, publisher_unit_id
            if source_identity in seen_source_ids:
                raise ValueError("Repeated publisher unit ID in delivery")
            seen_source_ids.add(source_identity)
            if alias_point is not None:
                alias_rows.append({"registry_id": alias_point["registry_id"],
                                   "source_group": source,
                                   "publisher_project_id": project_id,
                                   "publisher_building_id": _text((row.get("building") or {}).get("publisher_building_id")),
                                   "publisher_unit_id": publisher_unit_id,
                                   "observed_at": row["observed_at"],
                                   "unit_number": _text(unit.get("number")),
                                   "floor_number": _floor(unit.get("floor")),
                                   "area_m2": unit.get("area_m2"),
                                   "price_rub": row.get("price_rub"),
                                   "alias_status": "review_possible_same_unit",
                                   "source_evidence_sha256": _sha(_canonical(row))})
                continue
            selected.append({"source_row": row, "point": point,
                             "unit_id": _unit_id(source, publisher_unit_id),
                             "publisher_unit_id": publisher_unit_id})
    return selected, alias_rows


def _raw_evidence(path: Path, wanted: set[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in _rows(path):
        observation_id = row.get("observation_id")
        if observation_id in wanted:
            if observation_id in found:
                raise ValueError("Duplicate raw observation identity")
            found[observation_id] = row
    if set(found) != wanted:
        raise ValueError("Admitted unit observation missing from raw delivery")
    return found


def _menu_outputs(selected: list[dict[str, Any]], aliases: list[dict[str, Any]],
                  raw: dict[str, dict[str, Any]], points: list[dict[str, Any]],
                  map_run_id: str) -> dict[str, list[dict[str, Any]]]:
    units: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    floors: dict[tuple[str, str | None, int], list[str]] = defaultdict(list)
    per_building: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "max_unit_floor": None, "explicit_building_floors": set(),
        "section_floors": defaultdict(set)})
    for item in selected:
        projected = item["source_row"]
        point = item["point"]
        source = projected["source_group"]
        unit = projected["unit"]
        raw_row = raw[projected["observation_id"]]
        if (raw_row.get("source_group") != source or
                _text(raw_row.get("publisher_unit_id")) != item["publisher_unit_id"]):
            raise ValueError("Raw unit does not match selected publisher identity")
        section = _text(unit.get("section"))
        floor = _floor(unit.get("floor"))
        unit_id = item["unit_id"]
        registry_id = point["registry_id"]
        price = projected.get("price_rub")
        if price is not None and (type(price) is not int or price < 0):
            price = None
        area = unit.get("area_m2")
        if area is not None and (type(area) not in (int, float) or not math.isfinite(area)):
            area = None
        units.append({"unit_id": unit_id, "registry_id": registry_id,
                      "source_group": source,
                      "publisher_project_id": point["publisher_project_id"],
                      "publisher_building_id": point.get("publisher_building_id"),
                      "source_corpus_label": point.get("source_corpus_label"),
                      "publisher_unit_id": item["publisher_unit_id"],
                      "section_label": section, "floor_number": floor,
                      "unit_number": _text(unit.get("number")), "rooms": _text(unit.get("rooms")),
                      "area_m2": area, "price_rub": price,
                      "price_basis": _text(projected.get("price_basis")),
                      "observed_at": projected["observed_at"],
                      "source_updated_at": projected.get("source_updated_at"),
                      "rights_status": projected.get("source_rights_status", "unknown"),
                      "source_evidence_sha256": _sha(_canonical({"projection": projected,
                                                                   "raw": raw_row}))})
        if floor is not None:
            floors[(registry_id, section, floor)].append(unit_id)
            current = per_building[registry_id]["max_unit_floor"]
            per_building[registry_id]["max_unit_floor"] = floor if current is None else max(current, floor)
        source_fields = raw_row.get("source_fields") or {}
        if source == "proplanirovki":
            explicit = _positive_floors(source_fields.get("building_floors"))
            if explicit is not None:
                per_building[registry_id]["explicit_building_floors"].add(explicit)
        elif source == "fsk":
            explicit = _positive_floors(source_fields.get("api_section_floors_count"))
            if explicit is not None and section is not None:
                per_building[registry_id]["section_floors"][section].add(explicit)
        for media in raw_row.get("media") or []:
            role = media.get("role")
            if role not in PLAN_ROLES or not _text(media.get("url")):
                continue
            if role == "floor_plan":
                # A floor image from a unit page is usable as an exact floor
                # only when that unit has a section and floor, and the source
                # itself marks the image as floor-scoped.
                if section is None or floor is None or media.get("scope") != "floor":
                    continue
                bound_unit_id = None
                bound_floor_id = _floor_id(map_run_id, registry_id, section, floor)
            else:
                bound_unit_id = unit_id
                bound_floor_id = None
            plans.append({"plan_id": "plan_" + _sha(_canonical({
                "unit_id": bound_unit_id, "floor_id": bound_floor_id,
                "role": role, "url": media["url"]}))[:24],
                "registry_id": registry_id, "unit_id": bound_unit_id,
                "floor_id": bound_floor_id,
                "role": role, "source_role": role, "source_scope": media.get("scope"),
                "source_field": media.get("source_field"),
                "source_url": media["url"], "rights_status": media.get("rights_status", "unknown"),
                "source_evidence_sha256": _sha(_canonical(media)),
                "asset_sha256": None, "asset_mime": None})
    floor_rows = [{"floor_id": _floor_id(map_run_id, registry_id, section, floor),
                   "registry_id": registry_id, "section_label": section,
                   "floor_number": floor, "unit_ids": sorted(unit_ids)}
                  for (registry_id, section, floor), unit_ids in sorted(
                      floors.items(), key=lambda x: (x[0][0], x[0][1] or "", x[0][2]))]
    building_rows = []
    for point in points:
        registry_id = point["registry_id"]
        evidence = per_building[registry_id]
        explicit = evidence["explicit_building_floors"]
        sections = evidence["section_floors"]
        section_values = {v for values in sections.values() for v in values}
        if len(explicit) == 1 and not section_values:
            total = next(iter(explicit))
            basis = "source_apartment_record_building_floors"
        else:
            total, basis = None, None
        max_floor = evidence["max_unit_floor"]
        conflict = total is not None and max_floor is not None and max_floor > total
        if conflict:
            total, basis = None, None
        reviewed_range = None
        if registry_id == REVIEWED_SECTION_RANGE["registry_id"]:
            for key in ("source_group", "publisher_project_id", "publisher_building_id"):
                if point.get(key) != REVIEWED_SECTION_RANGE[key]:
                    raise ValueError("Reviewed section range no longer matches source identity")
            reviewed_range = REVIEWED_SECTION_RANGE
            total, basis, conflict = None, None, True
        building_rows.append({"registry_id": registry_id, "total_floors": total,
                              "total_floors_basis": basis,
                              "max_observed_unit_floor": max_floor,
                              "estimated_height_m": total * 3.0 if total else None,
                              "height_basis": "source_total_floors_times_3m_estimate" if total else None,
                              "height_conflict": conflict or len(explicit) > 1 or
                                                 any(len(values) > 1 for values in sections.values()) or
                                                 (len(section_values) > 1),
                              "source_section_total_floors": {
                                  section: sorted(values) for section, values in sorted(sections.items())},
                              "source_section_estimated_height_m": {
                                  section: sorted(value * 3.0 for value in values)
                                  for section, values in sorted(sections.items())},
                              "source_building_total_floors": sorted(explicit),
                              "reviewed_section_range": reviewed_range,
                              "height_quality": "estimated_from_source_reported_floors" if total else
                                                "section_only_or_unknown_or_review"})
    unique_plans: dict[str, dict[str, Any]] = {}
    for plan in plans:
        old = unique_plans.setdefault(plan["plan_id"], plan)
        if old != plan:
            raise ValueError("Conflicting source plan evidence for one bound plan")
    return {"buildings": sorted(building_rows, key=lambda x: x["registry_id"]),
            "units": sorted(units, key=lambda x: x["unit_id"]),
            "floors": sorted(floor_rows, key=lambda x: x["floor_id"]),
            "plans": sorted(unique_plans.values(), key=lambda x: x["plan_id"]),
            "unit_aliases": sorted(aliases, key=lambda x: (x["source_group"], x["publisher_unit_id"]))}


def build(delivery_manifest: Path, map_run_id: str, output_root: Path = OUTPUT) -> dict[str, Any]:
    delivery, paths, delivery_sha = _checked_delivery(delivery_manifest)
    raw_path = _checked_file(delivery_manifest.parent, delivery["files"], "observations")
    index, aliases, points, map_manifest_sha, map_manifest = _map_index(map_run_id)
    selected, alias_rows = _select_units(paths, index, aliases)
    if not selected:
        raise ValueError("No units linked to active buildings")
    wanted = {item["source_row"]["observation_id"] for item in selected}
    raw = _raw_evidence(raw_path, wanted)
    outputs = _menu_outputs(selected, alias_rows, raw, points, map_run_id)
    if len(outputs["units"]) + len(outputs["unit_aliases"]) != sum(
            item["admitted_unit_ids"] for item in map_manifest["coverage"].values()):
        raise ValueError("Menu did not retain every admitted source unit observation")
    if len(outputs["buildings"]) != map_manifest["buildings_total"]:
        raise ValueError("Menu does not cover every active building")
    payloads = {name: b"".join(_canonical(row) + b"\n" for row in rows)
                for name, rows in outputs.items()}
    hashes = {name: _sha(payload) for name, payload in payloads.items()}
    inputs = {"delivery_manifest_sha256": delivery_sha,
              "raw_observations_sha256": _file_sha(raw_path),
              "map_run_id": map_run_id, "map_snapshot_id": map_manifest["snapshot_id"],
              "map_manifest_sha256": map_manifest_sha}
    run_hash = _sha(_canonical({"rules": RULES, "inputs": inputs, "outputs": hashes}))
    run_id = "developer-units-v1-" + run_hash[:16]
    manifest = {"schema_version": "developer-units-snapshot/1", "rules_version": RULES,
                "run_id": run_id, **inputs,
                **{name + "_sha256": digest for name, digest in hashes.items()},
                **{name + "_total": len(rows) for name, rows in outputs.items()},
                "source_observations_total": len(outputs["units"]) + len(outputs["unit_aliases"]),
                "source_rights_status": "unknown",
                "plan_assets_prepared": 0,
                "max_listed_floor_is_total_floors": False,
                "render_height_policy": "only_explicit_source_floors_times_3m_estimate"}
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs = output_root / "runs"
    runs.mkdir(exist_ok=True, mode=0o700)
    target = runs / run_id
    manifest_bytes = _canonical(manifest) + b"\n"
    if target.exists():
        if target.is_symlink() or (target / "manifest.json").read_bytes() != manifest_bytes or any(
                (target / f"{name}.jsonl").read_bytes() != payload for name, payload in payloads.items()):
            raise ValueError("Immutable menu run ID collision")
        return manifest
    staging = Path(tempfile.mkdtemp(prefix=".stage-developer-units-", dir=runs))
    os.chmod(staging, 0o700)
    try:
        for name, payload in payloads.items():
            _write_private(staging / f"{name}.jsonl", payload)
        _write_private(staging / "manifest.json", manifest_bytes)
        _fsync_dir(staging)
        os.replace(staging, target)
        _fsync_dir(runs)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    if (MAP_ROOT / "active.json").read_bytes() != _canonical({
            "schema_version": "developer-map-pointer/1", "run_id": map_run_id,
            "manifest_sha256": map_manifest_sha}) + b"\n":
        raise ValueError("Map pointer changed during unit menu build")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--delivery-manifest", required=True, type=Path)
    build_cmd.add_argument("--map-run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.delivery_manifest, args.map_run_id, args.output_root),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
