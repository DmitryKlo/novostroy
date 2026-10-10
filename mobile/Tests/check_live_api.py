"""Read-only integration check for the mobile routes against a running API."""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def request(path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    with urllib.request.urlopen(
        urllib.request.Request(BASE + path, data=body, headers=headers), timeout=10
    ) as response:
        return json.load(response)


health = request("/v1/health")
if health.get("catalog_view") in {"building_registry", "developer_buildings"}:
    assert health.get("registry_enabled") is True
    buildings = request("/v1/registry/buildings?limit=1")
    registry_map = request("/v1/registry/map")
    developer_mode = health["catalog_view"] == "developer_buildings"
    expected_dataset = "developer_building_registry" if developer_mode else "building_registry"
    assert buildings["dataset_id"] == registry_map["dataset_id"] == expected_dataset
    assert buildings["snapshot_id"] == registry_map["snapshot_id"] == health["registry_snapshot_id"]
    assert buildings["records_total"] == registry_map["counts"]["records"]
    if not developer_mode:
        assert registry_map["counts"]["offers"] == registry_map["counts"]["facade_models"] == 0
    assert registry_map["counts"]["geocoded"] == len(registry_map["items"])
    if developer_mode:
        points = request("/v1/map/points")
        assert points["dataset_id"] == "developer_map_points"
        assert points["registry_snapshot_id"] == buildings["snapshot_id"]
        assert points["map_run_id"] and "model_run_id" not in points
        assert all(item["point_type"] == "developer_building" and item["registry_id"]
                   and item.get("candidate_id") is None for item in points["items"])
        assert all(item.get("location_quality") == "verified" and item["location"] is not None
                   for item in registry_map["items"])
        assert all(item.get("price") is None for item in buildings["items"])
    else:
        assert all(item["location"] is not None and item["offer_count"] == 0 and item["price"] is None
                   for item in registry_map["items"])
        assert all(item["offer_count"] == 0 and item["price"] is None for item in buildings["items"])
    print("Native registry compatibility passed: unsupported catalog is explicit; records=",
          buildings["records_total"], "geocoded=", registry_map["counts"]["geocoded"])
    sys.exit(0)

registry = request("/v1/criteria")
assert registry["version"] == "1.1"
assert registry["snapshot_id"] == health["snapshot_id"]
assert registry["data_mode"] == health["data_mode"]
assert len(registry["criteria"]) == 55
descriptor_keys = {
    "key", "label_ru", "group", "type", "value_type", "unit", "operators",
    "enum_values", "availability", "filterable", "coverage_requirement",
}
assert all(set(item) <= descriptor_keys for item in registry["criteria"])
assert any(item["key"] == "unit.floor" and "min" in item["operators"]
           for item in registry["criteria"])
map_data = request("/v1/map")
assert health["snapshot_id"] == map_data["snapshot_id"]
assert health["data_mode"] == map_data["data_mode"]
assert len(map_data["center"]) == 2
assert map_data["tilesets"], "No 3D Tiles source"
for item in map_data["tilesets"]:
    assert item["url"].startswith("/assets/"), item
    tileset = request(item["url"])
    assert tileset["asset"]["version"].startswith("1."), item["url"]

all_results = request("/v1/search", {"filters": {"limit": 50}})
assert all_results["snapshot_id"] == health["snapshot_id"]
assert all_results["data_mode"] == health["data_mode"]
first_page_ids = {offer["complex_id"] for offer in all_results["items"]}
outside = next(
    (item for item in reversed(map_data["complexes"])
     if item["complex_id"] not in first_page_ids and item["offer_count"] > 0),
    None,
)
if outside is None:
    raise AssertionError("Fixture has no complex outside first result page")
target = request(
    "/v1/search",
    {"filters": {"complex_ids": [outside["complex_id"]], "limit": 100}},
)
assert target["count"] > 0, outside["complex_id"]
assert all(offer["complex_id"] == outside["complex_id"] for offer in target["items"])
print("Targeted complex outside first page:", outside["complex_id"], target["count"])

district_id = map_data["complexes"][0]["district_id"]
filtered_map = request(
    "/v1/map?" + urllib.parse.urlencode({"district_ids": district_id})
)
filtered_search = request(
    "/v1/search", {"filters": {"district_ids": [district_id], "limit": 100}}
)
assert all(item["district_id"] == district_id for item in filtered_map["complexes"])
assert all(item["district_id"] == district_id for item in filtered_search["items"])
assert filtered_map["counts"]["offers"] == filtered_search["count"]

empty_map = request("/v1/map?budget_max_rub=1")
empty_search = request("/v1/search", {"filters": {"budget_max_rub": 1}})
assert empty_map["counts"]["offers"] == empty_search["count"] == 0
assert not empty_map["complexes"], "Filtered map retained complexes without offers"

query = "2 комнаты до 12 млн рублей"
query_map = request("/v1/map?" + urllib.parse.urlencode({"query": query}))
query_search = request("/v1/search", {"query": query, "filters": {"limit": 100}})
assert query_map["counts"]["offers"] == query_search["count"]
assert query_map["applied_filters"] == {
    key: value for key, value in query_search["applied_filters"].items() if key != "limit"
}

predicate = [{"key": "unit.floor", "op": "min", "value": 7}]
encoded = urllib.parse.urlencode({"predicates": json.dumps(predicate)})
predicate_map = request("/v1/map?" + encoded)
predicate_search = request("/v1/search", {"filters": {"predicates": predicate, "limit": 1}})
assert predicate_map["counts"]["offers"] == predicate_search["count"]
assert predicate_map["applied_filters"]["predicates"] == predicate
assert len(predicate_search["items"]) <= 1
assert all(item["facts"]["unit.floor"]["value"] >= 7
           for item in predicate_search["items"])
assert all(item["source"].keys() <= {"quality", "record_mode", "observed_at", "rights_status"}
           for item in predicate_search["items"])
if health["data_mode"] == "demo":
    assert predicate_search["count"] > 0
    unavailable_fact = [{"key": "building.cctv", "op": "equals", "value": True}]
    unknown_map = request("/v1/map?" + urllib.parse.urlencode(
        {"predicates": json.dumps(unavailable_fact)}))
    unknown_search = request("/v1/search", {"filters": {"predicates": unavailable_fact}})
    assert unknown_map["counts"]["offers"] == unknown_search["count"] == 0

detail = request("/v1/offers/" + urllib.parse.quote(target["items"][0]["offer_id"], safe=""))
assert "facts" in detail and detail["facts"]
public_fact_keys = {"value", "unit", "status", "scope", "observed_at",
                    "coverage_status", "approximate", "method"}
assert all(set(fact) <= public_fact_keys for fact in detail["facts"].values())
assert all(fact["value"] is None for fact in detail["facts"].values()
           if fact["status"] in {"unknown", "stale", "conflict", "not_applicable"})
assert set(detail["source"]) <= {"quality", "record_mode", "observed_at", "rights_status"}
if health["data_mode"] == "demo":
    asking = detail["facts"]["offer.price_total_rub"]
    per_sqm = detail["facts"]["offer.price_per_sqm_rub"]
    assert asking["value"] == detail["price"]["amount_rub"]
    assert per_sqm["value"] == {
        "numerator_rub": detail["price"]["amount_rub"],
        "denominator_sqm": str(detail["area_sqm"]),
    }

recommended = request("/v1/recommendations", {"filters": {"limit": 3}})
assert recommended["snapshot_id"] == health["snapshot_id"]
for item in recommended["items"]:
    assert isinstance(item["recommendation"]["explanation"], list)
    assert "coverage" in item["recommendation"]

if len(target["items"]) >= 2:
    ids = [item["offer_id"] for item in target["items"][:2]]
    compared = request("/v1/compare", {"offer_ids": ids})
    assert [item["offer_id"] for item in compared["items"]] == ids
    detail = request("/v1/offers/" + urllib.parse.quote(ids[0], safe=""))
    assert detail["offer_id"] == ids[0]

for offer in target["items"]:
    if health["data_mode"] == "real":
        assert offer["source"]["quality"] != "illustrative"
        assert offer["source"]["record_mode"] != "demo"

print("Mobile v1.1 API contract check passed:", health["data_mode"],
      health["snapshot_id"], len(registry["criteria"]), "criteria")
