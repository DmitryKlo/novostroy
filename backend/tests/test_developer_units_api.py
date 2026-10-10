"""Apartment menu provenance, projection and floor pagination checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from backend import app as api, developer_map, developer_units


MAP_RUN = "developer-map-v1-0123456789abcdef"
MAP_SNAPSHOT = "developer-map-snapshot-0123456789abcdef"
MENU_RUN = "developer-units-v1-0123456789abcdef"
REGISTRY_ID = "registry-" + "a" * 24
PROJECT_ID = "project-1"
BUILDING_ID = "building-1"


def _write(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
                    for row in rows)


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
             wrong_building: bool = False, prepared_plan: bool = False,
             floor_regions: bool = False, wrong_region_image: bool = False
             ) -> tuple[dict, dict, str, str]:
    monkeypatch.setattr(developer_units, "ROOT", tmp_path)
    monkeypatch.setattr(developer_units, "BASE", tmp_path / "data/property/developer_units")
    monkeypatch.setattr(developer_units, "ACTIVE_PATH",
                        tmp_path / "data/property/developer_units/active.json")
    monkeypatch.setattr(developer_map, "BASE", tmp_path / "data/property/developer_map")
    map_manifest = {"run_id": MAP_RUN, "snapshot_id": MAP_SNAPSHOT}
    map_path = developer_map.BASE / "runs" / MAP_RUN / "manifest.json"
    map_sha = _write(map_path, json.dumps(map_manifest).encode())
    map_row = {"registry_id": REGISTRY_ID, "source_group": "fsk",
               "publisher_project_id": PROJECT_ID, "publisher_building_id": BUILDING_ID,
               "source_corpus_label": "1", "unit_count": 2}
    map_run = {"manifest": map_manifest, "by_registry": {REGISTRY_ID: map_row}}
    unit_a = "unit_" + hashlib.sha256(b"fsk\0unit-1").hexdigest()[:24]
    unit_b = "unit_" + hashlib.sha256(b"fsk\0unit-2").hexdigest()[:24]
    floor_id = developer_units._floor_id(MAP_RUN, REGISTRY_ID, "2", 5)
    base_unit = {"registry_id": REGISTRY_ID, "source_group": "fsk",
                 "publisher_project_id": PROJECT_ID,
                 "publisher_building_id": "other-building" if wrong_building else BUILDING_ID,
                 "source_corpus_label": "1", "section_label": "2",
                 "rooms": "2", "price_basis": "asking_total",
                 "observed_at": "2026-10-10T16:00:00Z", "source_updated_at": None,
                 "source_evidence_sha256": "b" * 64}
    units = [dict(base_unit, publisher_unit_id="unit-1", unit_id=unit_a,
                  unit_number="125", floor_number=5, area_m2=43.3,
                  price_rub=24_845_540),
             dict(base_unit, publisher_unit_id="unit-2", unit_id=unit_b,
                  unit_number=None, floor_number=None, area_m2=None, price_rub=None)]
    plan = {"unit_id": unit_a, "floor_id": None, "role": "apartment_plan",
            "source_url": "https://private.invalid/plan.png"}
    content = b"\x89PNG\r\n\x1a\n" + b"safe-raster-fixture"
    asset_sha = hashlib.sha256(content).hexdigest()
    if prepared_plan:
        plan.update({"asset_sha256": asset_sha, "asset_mime": "image/png"})
        _write(developer_units.BASE / "runs" / MENU_RUN / "assets" / f"{asset_sha}.png",
               content)
    plans = [plan]
    if floor_regions:
        _write(developer_units.BASE / "runs" / MENU_RUN / "assets" / f"{asset_sha}.png",
               content)
        plans.append({"unit_id": None, "floor_id": floor_id, "role": "floor_plan",
                      "asset_sha256": asset_sha, "asset_mime": "image/png",
                      "regions_asset_sha256": "c" * 64 if wrong_region_image else asset_sha,
                      "source_url": "https://private.invalid/floor.png",
                      "regions": [{"region_id": "region_" + "a" * 24,
                                   "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.4]],
                                   "source_label": "Квартира на плане",
                                   "source_rooms": "2", "source_area_m2": 83.7,
                                   "unit_id": None, "match_status": "unconfirmed",
                                   "basis": "raster_traced_floor_plan", "reconstruction": True}]})
    records = {"buildings": [{"registry_id": REGISTRY_ID, "total_floors": 10,
                               "total_floors_basis": "source_building", "max_observed_unit_floor": 5,
                               "estimated_height_m": None, "height_basis": None,
                               "source_section_total_floors": {"2": [16]}}],
               "units": units,
               "floors": [{"registry_id": REGISTRY_ID, "section_label": "2",
                           "floor_number": 5, "floor_id": floor_id, "unit_ids": [unit_a]}],
               "plans": plans, "unit_aliases": []}
    run_dir = developer_units.BASE / "runs" / MENU_RUN
    manifest = {"schema_version": "developer-units-snapshot/1", "run_id": MENU_RUN,
                "map_run_id": MAP_RUN, "map_snapshot_id": MAP_SNAPSHOT,
                "map_manifest_sha256": map_sha}
    for name, rows in records.items():
        manifest[f"{name}_total"] = len(rows)
        manifest[f"{name}_sha256"] = _write(run_dir / f"{name}.jsonl", _jsonl(rows))
    menu_sha = _write(run_dir / "manifest.json", json.dumps(manifest).encode())
    _write(developer_units.ACTIVE_PATH, json.dumps({
        "schema_version": "developer-units-pointer/1", "run_id": MENU_RUN,
        "manifest_sha256": menu_sha}).encode())
    developer_units._FULL_CACHE = None
    developer_units._validated.cache_clear()
    return map_run, manifest, unit_a, asset_sha


def _schema(name: str, value: dict) -> None:
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/schemas.json").read_text())
    Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"},
                         format_checker=FormatChecker()).validate(value)


def test_exact_unit_menu_routes_and_private_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, unit_id, _ = _fixture(monkeypatch, tmp_path)
    menu = developer_units.resolve_active(map_run)
    assert menu is not None
    monkeypatch.setattr(api, "active_unit_menu", lambda: (map_run, menu))
    floors = api.developer_building_floors(REGISTRY_ID)
    listing = api.developer_building_units(REGISTRY_ID, section_id=None, floor_id=None,
                                           floor_known=None, limit=48, offset=0)
    unassigned = api.developer_building_units(REGISTRY_ID, section_id=None, floor_id=None,
                                              floor_known=False, limit=48, offset=0)
    detail = api.developer_building_unit(REGISTRY_ID, unit_id)
    floor = api.developer_building_floor(REGISTRY_ID,
                                        floors["sections"][0]["floors"][0]["floor_id"])
    for name, value in (("DeveloperFloorsResult", floors),
                        ("DeveloperUnitsResult", listing),
                        ("DeveloperUnitDetail", detail),
                        ("DeveloperFloorDetail", floor)):
        _schema(name, value)
    assert listing["total"] == 2 and unassigned["total"] == 1
    assert floors["unassigned_unit_count"] == 1 and floors["total_floors"] == 10
    assert floors["sections"][0]["total_floors"] == 16
    assert floor["hotspots"] == [] and floor["geometry_status"] == "none"
    assert floor["plan_regions"] == []
    assert detail["price_rub"] == 24_845_540
    assert detail["price_context"] == "source_displayed_total"
    assert detail["availability"] == "unknown" and detail["plans"] == []
    serialized = json.dumps([floors, listing, detail, floor])
    assert "publisher_unit_id" not in serialized and "private.invalid" not in serialized
    assert "unit-1" not in serialized and "fsk" not in serialized


def test_unit_identity_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, _, _ = _fixture(monkeypatch, tmp_path, wrong_building=True)
    with pytest.raises(ValueError, match="crosses publisher building identity"):
        developer_units.resolve_active(map_run)


def test_prepared_asset_is_hash_bound_and_never_proxies_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, unit_id, asset_sha = _fixture(monkeypatch, tmp_path, prepared_plan=True)
    menu = developer_units.resolve_active(map_run)
    assert menu is not None
    monkeypatch.setattr(api, "active_unit_menu", lambda: (map_run, menu))
    detail = api.developer_building_unit(REGISTRY_ID, unit_id)
    _schema("DeveloperUnitDetail", detail)
    assert detail["plans"] == [{"role": "apartment_plan",
                                "asset_url": f"/v1/developer-unit-assets/{asset_sha}",
                                "asset_sha256": asset_sha, "mime_type": "image/png",
                                "geometry_status": "source_plan_image"}]
    response = api.developer_unit_asset(asset_sha)
    assert response.media_type == "image/png" and response.body.startswith(b"\x89PNG")
    assert b"private.invalid" not in response.body
    asset = developer_units.BASE / "runs" / MENU_RUN / "assets" / f"{asset_sha}.png"
    asset.write_bytes(b"bad")
    with pytest.raises(ValueError, match="hash mismatch"):
        developer_units.asset_bytes(menu, asset_sha)


def test_traced_floor_regions_are_image_bound_and_never_claim_unit_price(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, _, asset_sha = _fixture(monkeypatch, tmp_path, floor_regions=True)
    menu = developer_units.resolve_active(map_run)
    assert menu is not None
    monkeypatch.setattr(api, "active_unit_menu", lambda: (map_run, menu))
    floor_id = next(iter(menu["floors"]))
    detail = api.developer_building_floor(REGISTRY_ID, floor_id)
    _schema("DeveloperFloorDetail", detail)
    assert detail["plan"]["asset_sha256"] == asset_sha
    assert len(detail["plan_regions"]) == 1
    region = detail["plan_regions"][0]
    assert region["source_area_m2"] == 83.7
    assert region["unit_id"] is None and region["unit_action"] is False
    assert region["coordinate_frame"] == "image_normalized_top_left"
    assert "price_rub" not in json.dumps(region)
    assert "private.invalid" not in json.dumps(detail)


def test_traced_floor_regions_reject_wrong_prepared_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, _, _ = _fixture(monkeypatch, tmp_path, floor_regions=True,
                                wrong_region_image=True)
    with pytest.raises(ValueError, match="not bound"):
        developer_units.resolve_active(map_run)


def test_apartment_filters_require_one_observed_unit_and_exclude_unknowns() -> None:
    menu = {"units_by_building": {
        "a": [{"price_rub": 5_000_000, "area_m2": 20.0, "rooms": "Студия",
               "floor_number": 2},
              {"price_rub": 20_000_000, "area_m2": 80.0,
               "rooms": "Двухкомнатная", "floor_number": 10}],
        "b": [{"price_rub": None, "area_m2": 50.0, "rooms": "2Е-комн.",
               "floor_number": None}],
    }}
    checked = developer_units.normalize_unit_filters(price_max_rub=6_000_000,
                                                      rooms=2)
    assert developer_units.matching_unit_counts(menu, {"a", "b"}, checked) == {"a": 0, "b": 0}
    checked = developer_units.normalize_unit_filters(price_per_m2_min_rub=250_000,
                                                      rooms=0, unit_floor_max=2)
    assert developer_units.matching_unit_counts(menu, {"a", "b"}, checked) == {"a": 1, "b": 0}
    checked = developer_units.normalize_unit_filters(price_min_rub=1,
                                                      area_min_m2=40, rooms=2)
    assert developer_units.matching_unit_counts(menu, {"a", "b"}, checked) == {"a": 1, "b": 0}
    with pytest.raises(ValueError, match="reversed"):
        developer_units.normalize_unit_filters(area_min_m2=80, area_max_m2=40)
