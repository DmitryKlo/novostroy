"""One pinned environment overlay across the developer map and apartment menu."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from starlette.requests import Request

from backend import app as api, developer_enrichment, developer_experience, developer_units


MAP_ID = "developer-map-v1-" + "a" * 16
SNAPSHOT_ID = "developer-map-snapshot-" + "b" * 16
UNIT_RUN = "developer-units-v1-" + "c" * 16
NORTH = "registry-" + "1" * 24
SOUTH = "registry-" + "2" * 24


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/v1/map/points",
                    "headers": []})


def _schema(name: str, value: dict) -> None:
    document = json.loads((Path(__file__).resolve().parents[2] /
                           "contracts/schemas.json").read_text())
    Draft202012Validator({**document, "$ref": f"#/$defs/{name}"},
                         format_checker=FormatChecker()).validate(value)


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict, str]:
    def building(registry_id: str, name: str, lon: float) -> dict:
        return {"registry_id": registry_id,
                "point_id": "mappt_" + registry_id[-24:],
                "point_type": "developer_building", "name": name,
                "complex_label": "ЖК Проверка", "developer_name": None,
                "address": "Москва, улица Проверки, 1", "address_complete": True,
                "address_basis": "publisher_building_address",
                "location": [lon, 55.7], "location_quality": "verified",
                "location_basis": "source_building_polygon", "map_eligible": True,
                "unit_count": 2 if registry_id == NORTH else 1,
                "observed_at": "2026-10-11T12:00:00Z"}

    north, south = building(NORTH, "Северный корпус", 37.6), building(SOUTH, "Южный корпус", 37.7)
    snapshot = {"manifest": {"run_id": MAP_ID, "snapshot_id": SNAPSHOT_ID,
                             "buildings_total": 2, "map_points_total": 2},
                "rows": [north, south],
                "by_registry": {NORTH: north, SOUTH: south}, "groups": []}

    def unit(registry_id: str, number: str, floor: int, price: int) -> dict:
        return {"registry_id": registry_id, "unit_id": "unit_" + number * 24,
                "unit_number": number, "section_label": "1", "floor_number": floor,
                "rooms": "2", "area_m2": 45.0, "price_rub": price,
                "price_basis": "asking_total", "observed_at": "2026-10-11T12:00:00Z",
                "source_updated_at": None}

    cheap = unit(NORTH, "1", 14, 12_000_000)
    expensive = unit(NORTH, "2", 13, 25_000_000)
    other = unit(SOUTH, "3", 14, 12_000_000)
    floor_13 = developer_units._floor_id(MAP_ID, NORTH, "1", 13)
    floor_14 = developer_units._floor_id(MAP_ID, NORTH, "1", 14)
    floor_other = developer_units._floor_id(MAP_ID, SOUTH, "1", 14)
    media_id = "media_" + "d" * 24
    prepared = b"\x89PNG\r\n\x1a\nsource-bound"
    media = {"media_id": media_id, "registry_id": NORTH,
             "unit_id": cheap["unit_id"], "floor_id": None,
             "scope": "unit_plan", "role": "apartment_plan",
             "observed_at": "2026-10-11T12:00:00Z",
             "source_url": "https://cdn.fsk.ru/private-plan.png",
             "prepared_source": "menu_asset",
             "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
             "prepared_bytes": len(prepared)}
    overlay = {"manifest": {"run_id": "developer-enrichment-v1-" + "e" * 16},
               "buildings": {
                  NORTH: {"registry_id": NORTH, "visibility_eligible": True,
                           "developer_display_name": "Level Group",
                           "developer_display_basis": "aggregator_asserted_unverified",
                           "developer_legal_name": None,
                           "district_id": "district:северный", "district_name": "Северный",
                           "okrug_id": "okrug:северный", "okrug_name": "Северный округ",
                           "noise_index": 37,
                           "air_model": {
                               "model_basis": "open_meteo_cams_europe_grid_cell_forecast",
                               "sample_scope": "requested_district_candidate_point_to_returned_grid_cell",
                               "measured_district_average": False,
                               "forecast_at": datetime.now(timezone.utc).isoformat(),
                               "retrieved_at": datetime.now(timezone.utc).isoformat(),
                               "data_stale_after_utc": (
                                   datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                               "pm2_5_ug_m3": 10.0, "pm10_ug_m3": 20.0,
                               "no2_ug_m3": 12.0,
                               "returned_grid_center_lon_lat": [37.6, 55.7]}},
                   SOUTH: {"registry_id": SOUTH, "visibility_eligible": True,
                           "district_id": "district:южный", "district_name": "Южный",
                           "okrug_id": "okrug:южный", "okrug_name": "Южный округ",
                           "noise_index": None}},
               "units": {row["unit_id"]: {"observed_at": row["observed_at"],
                                          "source_updated_at": None,
                                          "finishing_label": "Без отделки",
                                          "source_sale_status": None,
                                          "delivery_deadline_raw": None,
                                          "source_previous_price_rub": None,
                                          "source_discount_percent": None}
                         for row in (cheap, expensive, other)},
               "media": {media_id: media},
               "media_by_registry": {NORTH: [], SOUTH: []},
               "media_by_unit": {cheap["unit_id"]: [media]},
               "media_by_floor": {}}
    menu = {"manifest": {"run_id": UNIT_RUN},
            "buildings": {registry_id: {"registry_id": registry_id,
                                        "total_floors": 16 if registry_id == NORTH else None,
                                        "total_floors_basis": (
                                            "source_apartment_record_building_floors"
                                            if registry_id == NORTH else None),
                                        "max_observed_unit_floor": 14,
                                        "source_section_total_floors": {}}
                          for registry_id in (NORTH, SOUTH)},
            "units": {row["unit_id"]: row for row in (cheap, expensive, other)},
            "units_by_building": {NORTH: [cheap, expensive], SOUTH: [other]},
            "floors": {floor_13: {"floor_id": floor_13, "registry_id": NORTH,
                                  "section_label": "1", "floor_number": 13,
                                  "unit_ids": [expensive["unit_id"]]},
                       floor_14: {"floor_id": floor_14, "registry_id": NORTH,
                                  "section_label": "1", "floor_number": 14,
                                  "unit_ids": [cheap["unit_id"]]},
                       floor_other: {"floor_id": floor_other, "registry_id": SOUTH,
                                     "section_label": "1", "floor_number": 14,
                                     "unit_ids": [other["unit_id"]]}},
            "plans_by_floor": {}, "plans_by_unit": {}, "enrichment": overlay}
    monkeypatch.setattr(api, "active_developer_map", lambda: snapshot)
    monkeypatch.setattr(developer_experience, "current", lambda _: (menu, {}))
    monkeypatch.setattr(api, "background_tilesets", lambda _: [])
    monkeypatch.setattr(api, "load_catalogue", lambda: {})
    monkeypatch.setattr(api, "asset_manifest", lambda: {"city_buildings": 0})
    monkeypatch.setattr(developer_units, "asset_bytes", lambda *_: (prepared, "image/png"))
    return snapshot, menu, media_id


def test_district_noise_and_apartment_filter_share_one_unit(example: tuple[dict, dict, str]) -> None:
    _, _, _ = example
    kwargs = {"query": None, "status": None, "kind": None,
              "district": "Северный", "okrug_id": "okrug:северный",
              "max_noise_index": 40, "price_max_rub": 15_000_000, "rooms": 2}
    listing = api.registry_buildings(**kwargs, limit=48, offset=0)
    mapped = api.registry_map(**kwargs)
    points = json.loads(api.map_points(_request(), **kwargs).body)
    assert listing["records_total"] == len(mapped["items"]) == len(points["items"]) == 1
    assert all(row["registry_id"] == NORTH for row in
               (listing["items"][0], mapped["items"][0], points["items"][0]))
    assert listing["items"][0]["matching_units_count"] == 1
    assert listing["facets"]["noise_known_count"] == 1
    assert len(listing["facets"]["okrugs"]) == 2
    for schema, result in (("DeveloperRegistryListResult", listing),
                           ("DeveloperRegistryMapResult", mapped),
                           ("DeveloperMapPointsResult", points)):
        _schema(schema, result)
    for result in (listing, mapped, points):
        assert "private-plan.png" not in json.dumps(result)


def test_air_index_filter_has_same_house_and_menu_meaning(
    example: tuple[dict, dict, str]
) -> None:
    _, menu, _ = example
    kwargs = {"max_air_index": 30, "price_max_rub": 15_000_000, "rooms": 2}
    listing = api.registry_buildings(query=None, status=None, kind=None,
                                     limit=48, offset=0, **kwargs)
    mapped = api.registry_map(query=None, status=None, kind=None, **kwargs)
    points = json.loads(api.map_points(_request(), query=None, status=None,
                                       kind=None, **kwargs).body)
    assert [row["registry_id"] for row in listing["items"]] == [NORTH]
    assert [row["registry_id"] for row in mapped["items"]] == [NORTH]
    assert [row["registry_id"] for row in points["items"]] == [NORTH]
    assert listing["facets"]["air_index_known_count"] == 1
    assert listing["items"][0]["air_quality"]["internal_index"]["value"] == 30
    assert listing["items"][0]["air_quality"]["internal_index"]["official_index"] is False
    assert listing["items"][0]["developer_display_name"] == "Level Group"
    assert listing["items"][0]["developer_display_basis"] == "aggregator_asserted_unverified"
    assert listing["items"][0]["developer_legal_name"] is None
    assert listing["items"][0]["total_floors"] == 16
    assert listing["items"][0]["total_floors_basis"] == "source_apartment_record_building_floors"
    assert listing["items"][0]["max_observed_unit_floor"] == 14
    assert "source_identity" not in json.dumps(listing)
    tree = api.developer_building_floors(NORTH, **kwargs)
    assert tree["matching_units_total"] == 1
    floor_id = tree["sections"][0]["floors"][0]["floor_id"]
    floor = api.developer_building_floor(NORTH, floor_id, **kwargs)
    units = api.developer_building_units(
        NORTH, section_id=None, floor_id=floor_id, floor_known=None,
        limit=48, offset=0, **kwargs)
    detail = api.developer_building_unit(NORTH, units["items"][0]["unit_id"], **kwargs)
    assert floor["matching_unit_count"] == units["total"] == 1
    assert detail["unit_id"] == units["items"][0]["unit_id"]
    for value in (listing, mapped, points, tree, floor, units, detail):
        _schema({id(listing): "DeveloperRegistryListResult",
                 id(mapped): "DeveloperRegistryMapResult",
                 id(points): "DeveloperMapPointsResult",
                 id(tree): "DeveloperFloorsResult",
                 id(floor): "DeveloperFloorDetail",
                 id(units): "DeveloperUnitsResult",
                 id(detail): "DeveloperUnitDetail"}[id(value)], value)
    assert api.registry_buildings(query=None, status=None, kind=None,
                                  limit=48, offset=0,
                                  max_air_index=29)["records_total"] == 0
    with pytest.raises(HTTPException) as exc:
        api.developer_building_floors(NORTH, max_air_index=29)
    assert exc.value.status_code == 404
    menu["enrichment"]["buildings"][NORTH]["air_model"]["data_stale_after_utc"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert api.registry_buildings(query=None, status=None, kind=None,
                                  limit=48, offset=0,
                                  max_air_index=100)["records_total"] == 0
    assert api.registry_buildings(query=None, status=None, kind=None,
                                  limit=48, offset=0)["records_total"] == 2
    assert api.registry_buildings(query="Level Group", status=None, kind=None,
                                  limit=48, offset=0)["records_total"] == 1


def test_section_counts_do_not_invent_a_whole_building_total(
    example: tuple[dict, dict, str]
) -> None:
    snapshot, menu, _ = example
    menu["buildings"][SOUTH]["total_floors"] = None
    menu["buildings"][SOUTH]["total_floors_basis"] = None
    menu["buildings"][SOUTH]["source_section_total_floors"] = {
        "1": [16], "2": [10]}
    projected = api._developer_public_structure(snapshot["by_registry"][SOUTH], menu)
    assert projected["total_floors"] is None
    assert projected["section_floor_counts"] == [
        {"section_label": "1", "total_floors": 16,
         "total_floors_basis": "source_section_floor_count"},
        {"section_label": "2", "total_floors": 10,
         "total_floors_basis": "source_section_floor_count"}]


def test_floor_and_detail_filters_reject_unrelated_unit(example: tuple[dict, dict, str]) -> None:
    _, menu, media_id = example
    kwargs = {"district": "district:северный", "okrug_id": "okrug:северный",
              "max_noise_index": 40, "price_max_rub": 15_000_000, "rooms": 2}
    tree = api.developer_building_floors(NORTH, **kwargs)
    assert tree["matching_units_total"] == 1
    assert [floor["floor_number"] for section in tree["sections"]
            for floor in section["floors"]] == [14]
    _schema("DeveloperFloorsResult", tree)
    floor_id = tree["sections"][0]["floors"][0]["floor_id"]
    floor = api.developer_building_floor(NORTH, floor_id, **kwargs)
    units = api.developer_building_units(NORTH, floor_id=floor_id, section_id=None,
                                        floor_known=None, limit=48, offset=0, **kwargs)
    detail = api.developer_building_unit(NORTH, units["items"][0]["unit_id"], **kwargs)
    assert floor["unit_count"] == units["total"] == 1
    assert units["items"][0]["unit_id"] == detail["unit_id"]
    assert detail["media"][0]["media_id"] == media_id
    assert detail["media"][0]["asset_url"] == f"/v1/developer-media/{media_id}"
    assert detail["observed_attributes"]["availability_confirmed_current"] is False
    assert "private-plan.png" not in json.dumps((tree, floor, units, detail))
    for schema, result in (("DeveloperFloorDetail", floor),
                           ("DeveloperUnitsResult", units),
                           ("DeveloperUnitDetail", detail)):
        _schema(schema, result)
    floor_13 = developer_units._floor_id(MAP_ID, NORTH, "1", 13)
    with pytest.raises(HTTPException) as exc:
        api.developer_building_floor(NORTH, floor_13, **kwargs)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        api.developer_building_unit(NORTH, menu["units_by_building"][NORTH][1]["unit_id"], **kwargs)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        api.developer_building_units(SOUTH, section_id=None, floor_id=None,
                                    floor_known=None, limit=48, offset=0,
                                    max_noise_index=40)
    assert exc.value.status_code == 404


def test_media_serves_only_snapshot_bound_prepared_bytes(example: tuple[dict, dict, str]) -> None:
    _, _, media_id = example
    asset = api.developer_media_asset(media_id)
    assert asset.media_type == "image/png"
    assert asset.body.startswith(b"\x89PNG")
    assert asset.headers["x-content-type-options"] == "nosniff"
    with pytest.raises(HTTPException) as exc:
        api.developer_media_asset("media_" + "f" * 24)
    assert exc.value.status_code == 404


def test_media_serves_validated_cached_jpeg_with_its_real_mime(
    example: tuple[dict, dict, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend import developer_media_cache

    _, menu, media_id = example
    media_row = menu["enrichment"]["media"][media_id]
    media_row["prepared_source"] = None
    content = b"\xff\xd8\xff\xe0stripped-jpeg"
    path = tmp_path / "prepared.jpg"
    path.write_bytes(content)
    monkeypatch.setattr(developer_media_cache, "get_or_prepare", lambda *_args, **_kwargs:
                        SimpleNamespace(path=path, mime_type="image/jpeg",
                                        sha256=hashlib.sha256(content).hexdigest(),
                                        byte_length=len(content)))
    asset = api.developer_media_asset(media_id)
    assert asset.media_type == "image/jpeg" and asset.body == content


def test_noise_unknown_is_not_zero(example: tuple[dict, dict, str]) -> None:
    unfiltered = api.registry_buildings(query=None, status=None, kind=None,
                                        limit=48, offset=0)
    assert unfiltered["records_total"] == 2
    filtered = api.registry_buildings(query=None, status=None, kind=None,
                                      max_noise_index=100, limit=48, offset=0)
    assert filtered["records_total"] == 1
    assert filtered["items"][0]["registry_id"] == NORTH
    with pytest.raises(HTTPException) as exc:
        api.registry_buildings(query=None, status=None, kind=None,
                               max_noise_index=101, limit=48, offset=0)
    assert exc.value.status_code == 422


def test_dated_air_grid_sample_is_never_a_district_measurement() -> None:
    now = datetime.now(timezone.utc)
    model = {"model_basis": "open_meteo_cams_europe_grid_cell_forecast",
             "sample_scope": "requested_district_candidate_point_to_returned_grid_cell",
             "requested_point_lon_lat": [37.52, 55.87],
             "returned_grid_center_lon_lat": [37.5, 55.9],
             "forecast_at": (now - timedelta(hours=1)).isoformat(),
             "retrieved_at": now.isoformat(),
             "pm2_5_ug_m3": 7.7, "pm10_ug_m3": 11.0, "no2_ug_m3": 18.6,
             "measured_district_average": False,
             "attribution": "Open-Meteo / CAMS, CC BY 4.0",
             "source_response_sha256": "a" * 64}
    assert developer_enrichment._valid_air_model(model)
    public = api._public_air_quality({"air_model": model})
    _schema("DeveloperAirQuality", public)
    assert public["status"] == "modeled_forecast" and public["stale"] is False
    assert public["pm2_5_ug_m3"] == 7.7
    assert public["measurement"] is False and public["measured_district_average"] is False
    model["forecast_at"] = (now - timedelta(hours=30)).isoformat()
    assert api._public_air_quality({"air_model": model})["stale"] is True
    model["measured_district_average"] = True
    assert not developer_enrichment._valid_air_model(model)
