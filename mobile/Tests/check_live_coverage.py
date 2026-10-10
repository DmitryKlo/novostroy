"""Check the public data-coverage route used by the native Data tab."""

import json
import sys
import urllib.request


BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


health = get("/v1/health")
registry = get("/v1/criteria")
coverage = get("/v1/data-coverage")
assert set(coverage) == {
    "snapshot_id", "data_mode", "generated_at", "criteria_total", "datasets"
}
assert coverage["snapshot_id"] == health["snapshot_id"] == registry["snapshot_id"]
assert coverage["data_mode"] == health["data_mode"]
assert coverage["criteria_total"] == len(registry["criteria"])
datasets = {item["dataset_id"]: item for item in coverage["datasets"]}
base_ids = {"demo_offers", "real_current_offers", "city_geometry"}
assert len(coverage["datasets"]) == len(datasets)
assert set(datasets) in (base_ids, base_ids | {"building_registry"},
                         base_ids | {"developer_building_registry"})
dataset_modes = [
    ("demo_offers", "demo"),
    ("real_current_offers", "real"),
    ("city_geometry", "geospatial"),
]
if "building_registry" in datasets:
    dataset_modes.append(("building_registry", "registry"))
if "developer_building_registry" in datasets:
    dataset_modes.append(("developer_building_registry", "registry"))
for dataset_id, mode in dataset_modes:
    item = datasets[dataset_id]
    assert set(item) == {
        "dataset_id", "data_mode", "status", "snapshot_at", "records_total",
        "complexes_total", "fields", "distributions",
    }
    assert item["data_mode"] == mode and item["records_total"] >= 0
    assert (item["complexes_total"] is None) == (dataset_id in {"city_geometry", "building_registry"})
    if dataset_id == "developer_building_registry":
        assert 0 <= item["complexes_total"] <= item["records_total"]
    for field in item["fields"]:
        assert set(field) == {
            "key", "unit", "populated_count", "unknown_count", "stale_count",
            "quality_counts",
        }
        assert set(field["quality_counts"]) == {
            "observed", "derived", "estimated", "illustrative", "render_fallback"
        }
        assert all(value >= 0 for value in field["quality_counts"].values())
        assert (field["populated_count"] + field["unknown_count"] +
                field["stale_count"] == item["records_total"])
        assert sum(field["quality_counts"].values()) == field["populated_count"]
    assert len(item["distributions"]) <= 2
    if dataset_id in {"city_geometry", "building_registry", "developer_building_registry"}:
        assert not item["distributions"]
    for distribution in item["distributions"]:
        assert set(distribution) == {
            "key", "unit", "basis", "population_count", "sample_count",
            "missing_count", "excluded_count", "bins",
        }
        assert (distribution["key"], distribution["unit"], distribution["basis"]) in {
            ("offer.price_total_rub", "RUB", "asking_total"),
            ("unit.area_sqm", "m²", "total_area"),
        }
        assert distribution["population_count"] == item["records_total"]
        assert (distribution["sample_count"] + distribution["missing_count"] +
                distribution["excluded_count"] == distribution["population_count"])
        assert len(distribution["bins"]) <= 5
        assert all(set(bin_item) == {"lower", "upper", "upper_inclusive", "count"}
                   and bin_item["lower"] <= bin_item["upper"] and bin_item["count"] >= 0
                   for bin_item in distribution["bins"])
        assert sum(bin_item["count"] for bin_item in distribution["bins"]) == distribution["sample_count"]

if coverage["data_mode"] == "demo":
    real = datasets["real_current_offers"]
    assert real["records_total"] == real["complexes_total"] == 0
    assert real["status"] == "no_licensed_feed"

print("Mobile data coverage passed:", coverage["data_mode"], coverage["snapshot_id"],
      [(name, item["records_total"]) for name, item in datasets.items()])
