"""Validate public route functions and contracts without local TCP or ASGI threads.

Run with `rtk proxy .runtime/dev-env/bin/python integration/check_offline_routes.py`.
This checks route payloads and invariants, but cannot verify HTTP middleware,
the Vite proxy, browser loading, or network cache behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import app as api  # noqa: E402
from integration.check_live import assert_public, check_ground_layers, validate  # noqa: E402


def check() -> None:
    health = api.health()
    validate("/v1/health", "get", health)
    districts = api.districts()
    validate("/v1/districts", "get", districts)
    criteria = api.criteria()
    validate("/v1/criteria", "get", criteria)
    coverage = api.data_coverage_view()
    validate("/v1/data-coverage", "get", coverage)
    assert_public([health, districts, criteria, coverage])

    request = Request({"type": "http", "method": "GET", "path": "/v1/map",
                       "query_string": b"", "headers": []})
    offer_map = api.map_view(request, query=None, budget_min_rub=None,
                             budget_max_rub=None, district_ids=None, complex_ids=None,
                             rooms=None, area_min_sqm=None, area_max_sqm=None,
                             price_basis=None, predicates=None)
    validate("/v1/map", "get", offer_map)
    assert_public(offer_map)
    assert offer_map["snapshot_id"] == health["snapshot_id"]
    assert offer_map["data_mode"] == health["data_mode"]
    check_ground_layers(offer_map["tilesets"])
    ground_url = api.asset_manifest().get("ground_tileset_url")
    if ground_url:
        assert next(item for item in offer_map["tilesets"]
                    if item["kind"] == "ground_buildings")["url"] == ground_url

    datasets = {item["dataset_id"]: item for item in coverage["datasets"]}
    assert set(datasets) == {"demo_offers", "real_current_offers", "city_geometry",
                             "building_registry"}
    building_dataset = datasets["building_registry"]
    assert building_dataset["data_mode"] == "registry"
    assert building_dataset["complexes_total"] is None
    assert building_dataset["distributions"] == []

    if health["registry_enabled"]:
        assert health["catalog_view"] == "building_registry"
        all_records = api.active_registry()["items"]
        assert_public(all_records)
        assert all(item["offer_count"] == 0 and item["price"] is None and
                   item["has_apartment_data"] is False for item in all_records)
        listing = api.registry_buildings(query=None, status=None, kind=None,
                                         limit=2, offset=0)
        registry_map = api.registry_map(query=None, status=None, kind=None)
        validate("/v1/registry/buildings", "get", listing)
        validate("/v1/registry/map", "get", registry_map)
        assert_public([listing, registry_map])
        assert listing["dataset_id"] == registry_map["dataset_id"] == "building_registry"
        assert listing["snapshot_id"] == registry_map["snapshot_id"] == health["registry_snapshot_id"]
        assert listing["dataset_total"] == building_dataset["records_total"] == len(all_records)
        assert listing["records_total"] == registry_map["counts"]["records"]
        assert registry_map["counts"]["geocoded"] == len(registry_map["items"])
        assert registry_map["counts"]["offers"] == registry_map["counts"]["facade_models"] == 0
        check_ground_layers(registry_map["tilesets"])
        if ground_url:
            assert next(item for item in registry_map["tilesets"]
                        if item["kind"] == "ground_buildings")["url"] == ground_url
        assert all(item["location"] is not None for item in registry_map["items"])
        assert all(item["offer_count"] == 0 and item["price"] is None
                   for item in listing["items"] + registry_map["items"])
        if listing["items"]:
            detail = api.registry_building_detail(listing["items"][0]["registry_id"])
            validate("/v1/registry/buildings/{registry_id}", "get", detail)
            assert detail == listing["items"][0]
            assert_public(detail)
        print(f"PASS offline routes: registry={listing['dataset_total']} records, "
              f"geocoded={registry_map['counts']['geocoded']}, no registry offers")
    else:
        assert health["catalog_view"] == "demo"
        assert building_dataset["records_total"] == 0
        print("PASS offline routes: demo; building registry not loaded")


if __name__ == "__main__":
    check()
