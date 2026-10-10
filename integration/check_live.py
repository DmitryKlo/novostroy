"""Check live FastAPI routes against the additive v1.2 contract and map parity.

Invoke `rtk python integration/check_live.py http://127.0.0.1:8000` while the
API is running. The check is read-only and does not write analytics events.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
OPENAPI = json.loads((CONTRACTS / "openapi.json").read_text(encoding="utf-8"))
SCHEMAS = json.loads((CONTRACTS / "schemas.json").read_text(encoding="utf-8"))
REGISTRY = Registry().with_resource(
    (CONTRACTS / "schemas.json").resolve().as_uri(), Resource.from_contents(SCHEMAS)
)
PUBLIC_SOURCE_FIELDS = {"quality", "record_mode", "observed_at", "rights_status"}
PRIVATE_KEYS = {"provenance", "source_id", "source_ids", "source_record_id", "source_fields",
                "source_updated_at", "source_version", "source_url", "evidence", "provider"}
PUBLIC_CRITERION_FIELDS = {"key", "label_ru", "group", "type", "value_type", "unit",
                           "operators", "enum_values", "availability", "filterable",
                           "coverage_requirement"}


def assert_public(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            assert_public(item)
    elif isinstance(value, dict):
        assert not PRIVATE_KEYS.intersection(value), PRIVATE_KEYS.intersection(value)
        if "source" in value and isinstance(value["source"], dict):
            assert set(value["source"]) <= PUBLIC_SOURCE_FIELDS
        for item in value.values():
            assert_public(item)


def get_json(base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    suffix = f"?{urlencode(params, doseq=True)}" if params else ""
    with urlopen(f"{base}{path}{suffix}", timeout=20) as response:
        assert response.status == 200, (path, response.status)
        return json.load(response)


def post_json(base: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(f"{base}{path}", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=20) as response:
        assert response.status == 200, (path, response.status)
        return json.load(response)


def validate(path: str, method: str, payload: dict[str, Any]) -> None:
    schema = OPENAPI["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    schema = {"$id": (CONTRACTS / "openapi.json").resolve().as_uri(), **schema}
    Draft202012Validator(schema, registry=REGISTRY,
                         format_checker=FormatChecker()).validate(payload)


def check_ground_layers(tilesets: list[dict[str, Any]]) -> None:
    """Keep the flat footprint floor separate from city LOD replacement."""
    kinds = [item["kind"] for item in tilesets]
    assert kinds.count("city_volumes") == 1
    assert kinds.count("ground_buildings") <= 1
    city = next(item for item in tilesets if item["kind"] == "city_volumes")
    ground = next((item for item in tilesets if item["kind"] == "ground_buildings"), None)
    if "/public-citywide-v8/" in city["url"]:
        assert ground is not None, "city v8 requires the permanent ground layer"
    if ground is not None:
        assert kinds.index("ground_buildings") < kinds.index("city_volumes")
        assert ground["url"] in {
            "/assets/public-ground-v2/tileset.json",
            "/assets/public-ground-v3/tileset.json",
        }
        assert ground["model_quality"] == "cartographic_surface"
        assert "OpenStreetMap" in ground["attribution"]


def check_tileset(base: str, route: str) -> None:
    tileset = get_json(base, route)
    assert tileset["asset"]["version"] == "1.1"
    branch = tileset["root"]
    json_route = route
    visited_json = {route}
    checked = 0
    glbs = 0
    for _ in range(12):
        contents = branch.get("contents") or ([branch["content"]] if branch.get("content") else [])
        if contents:
            uri = contents[0]["uri"]
            assert isinstance(uri, str) and uri and not uri.startswith("/")
            assert ".." not in uri.split("/") and "?" not in uri and "#" not in uri
            tile_url = urljoin(json_route, uri)
            assert tile_url.startswith(route.rsplit("/", 1)[0] + "/"), tile_url
            request = Request(f"{base}{tile_url}", method="HEAD")
            with urlopen(request, timeout=20) as response:
                assert response.status == 200
                assert int(response.headers.get("Content-Length", "0")) > 0
            checked += 1
            if uri.endswith(".glb"):
                glbs += 1
            elif uri.endswith(".json"):
                assert tile_url not in visited_json, tile_url
                visited_json.add(tile_url)
                nested = get_json(base, tile_url)
                assert nested["asset"]["version"] == "1.1"
                json_route = tile_url
                branch = nested["root"]
                continue
            else:
                raise AssertionError(f"unexpected tile content: {uri}")
        if not branch.get("children"):
            break
        branch = branch["children"][0]
    else:
        raise AssertionError(f"tileset branch too deep: {route}")
    assert checked > 0, f"tileset has no served content: {route}"
    assert glbs > 0, f"tileset has no served GLB: {route}"


def check_internal_assets_private(base: str, tilesets: list[dict[str, Any]]) -> None:
    routes = {"/assets/manifest.json"}
    for tileset in tilesets:
        folder = tileset["url"].rsplit("/", 1)[0]
        routes.add(f"{folder}/manifest.json")
        routes.add(f"{folder}/source_tile_index.jsonl")
    for route in sorted(routes):
        try:
            with urlopen(f"{base}{route}", timeout=20) as response:
                raise AssertionError(f"internal asset is public: {route} ({response.status})")
        except HTTPError as error:
            assert error.code == 404, (route, error.code)


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    health = get_json(base, "/v1/health")
    validate("/v1/health", "get", health)
    districts = get_json(base, "/v1/districts")
    validate("/v1/districts", "get", districts)
    criteria = get_json(base, "/v1/criteria")
    validate("/v1/criteria", "get", criteria)
    assert criteria["version"] == "1.1" and len(criteria["criteria"]) == 55
    assert all(set(item) == PUBLIC_CRITERION_FIELDS for item in criteria["criteria"])
    data_coverage = get_json(base, "/v1/data-coverage")
    validate("/v1/data-coverage", "get", data_coverage)
    assert data_coverage["criteria_total"] == len(criteria["criteria"])
    by_dataset = {item["dataset_id"]: item for item in data_coverage["datasets"]}
    base_datasets = {"demo_offers", "real_current_offers", "city_geometry"}
    assert set(by_dataset) in (base_datasets, base_datasets | {"building_registry"},
                               base_datasets | {"developer_building_registry"})
    for dataset in by_dataset.values():
        total = dataset["records_total"]
        if dataset["dataset_id"] in {"demo_offers", "real_current_offers"}:
            assert len(dataset["fields"]) == data_coverage["criteria_total"]
        for field in dataset["fields"]:
            assert field["populated_count"] + field["unknown_count"] + field["stale_count"] == total
            assert sum(field["quality_counts"].values()) == field["populated_count"]
        for distribution in dataset["distributions"]:
            assert distribution["population_count"] == total
            assert (distribution["sample_count"] + distribution["missing_count"]
                    + distribution["excluded_count"] == total)
            assert sum(item["count"] for item in distribution["bins"]) == distribution["sample_count"]
            assert len(distribution["bins"]) <= 5
            assert all(item["lower"] <= item["upper"] for item in distribution["bins"])
    assert by_dataset["city_geometry"]["complexes_total"] is None
    assert by_dataset["city_geometry"]["distributions"] == []
    if "building_registry" in by_dataset:
        building_coverage = by_dataset["building_registry"]
        assert building_coverage["data_mode"] == "registry"
        assert building_coverage["complexes_total"] is None
        assert building_coverage["distributions"] == []
        if health.get("registry_enabled"):
            assert building_coverage["records_total"] > 0
    if "developer_building_registry" in by_dataset:
        building_coverage = by_dataset["developer_building_registry"]
        assert building_coverage["data_mode"] == "registry"
        assert building_coverage["records_total"] > 0
        assert building_coverage["distributions"] == []
    city_fields = {field["key"]: field for field in by_dataset["city_geometry"]["fields"]}
    city_render_quality = city_fields["city.render_height_m"]["quality_counts"]
    assert city_render_quality["observed"] == city_fields["city.source_height_m"]["populated_count"]
    assert city_render_quality["estimated"] == (
        city_fields["city.render_height_from_floors_m"]["populated_count"]
        + city_fields["city.render_height_imputed_m"]["populated_count"]
    )
    assert city_fields["city.render_height_imputed_m"]["quality_counts"]["estimated"] == (
        city_fields["city.render_height_imputed_m"]["populated_count"]
    )
    assert sum(city_render_quality.values()) == by_dataset["city_geometry"]["records_total"]
    full_map = get_json(base, "/v1/map")
    validate("/v1/map", "get", full_map)
    search_all = post_json(base, "/v1/search", {"filters": {"limit": 100}})
    validate("/v1/search", "post", search_all)
    recommendations = post_json(base, "/v1/recommendations", {
        "filters": {"budget_max_rub": 20_000_000, "limit": 10},
        "preferences": {"infrastructure_categories": ["school"]},
    })
    validate("/v1/recommendations", "post", recommendations)
    metrics = get_json(base, "/v1/metrics")
    validate("/v1/metrics", "get", metrics)
    assert "conversion" in metrics["interpretation"] or "sale" in metrics["interpretation"]

    snapshot = health["snapshot_id"]
    mode = health["data_mode"]
    for payload in (districts, criteria, data_coverage, full_map, search_all, recommendations):
        assert payload["snapshot_id"] == snapshot
        assert payload["data_mode"] == mode
        assert_public(payload)
    assert full_map["counts"]["offers"] == search_all["count"]
    assert full_map["counts"]["complexes"] == len(full_map["complexes"])
    assert len(search_all["items"]) <= 100
    assert all(tileset["url"].startswith("/assets/") for tileset in full_map["tilesets"])
    check_ground_layers(full_map["tilesets"])
    check_internal_assets_private(base, full_map["tilesets"])

    if health.get("catalog_view") == "building_registry":
        assert health.get("registry_enabled") is True
        building_list = get_json(base, "/v1/registry/buildings", {"limit": 1})
        registry_map = get_json(base, "/v1/registry/map")
        validate("/v1/registry/buildings", "get", building_list)
        validate("/v1/registry/map", "get", registry_map)
        assert building_list["dataset_id"] == registry_map["dataset_id"] == "building_registry"
        assert building_list["snapshot_id"] == registry_map["snapshot_id"] == health["registry_snapshot_id"]
        assert building_list["records_total"] == registry_map["counts"]["records"]
        assert registry_map["counts"]["geocoded"] == len(registry_map["items"])
        assert registry_map["counts"]["offers"] == registry_map["counts"]["facade_models"] == 0
        assert all(item["location"] is not None for item in registry_map["items"])
        assert all(item["offer_count"] == 0 and item["price"] is None
                   for item in building_list["items"] + registry_map["items"])
        check_ground_layers(registry_map["tilesets"])
        assert_public(building_list)
        assert_public(registry_map)
        if building_list["items"]:
            detail = get_json(base, "/v1/registry/buildings/" +
                              quote(building_list["items"][0]["registry_id"], safe=""))
            validate("/v1/registry/buildings/{registry_id}", "get", detail)
            assert detail == building_list["items"][0]
            assert_public(detail)
        check_internal_assets_private(base, registry_map["tilesets"])
    elif health.get("catalog_view") == "developer_buildings":
        assert mode == "real" and health.get("registry_enabled") is True
        building_list = get_json(base, "/v1/registry/buildings", {"limit": 1})
        registry_map = get_json(base, "/v1/registry/map")
        points = get_json(base, "/v1/map/points")
        validate("/v1/registry/buildings", "get", building_list)
        validate("/v1/registry/map", "get", registry_map)
        validate("/v1/map/points", "get", points)
        assert building_list["dataset_id"] == registry_map["dataset_id"] == "developer_building_registry"
        assert points["dataset_id"] == "developer_map_points"
        assert (building_list["snapshot_id"] == registry_map["snapshot_id"] ==
                points["registry_snapshot_id"] == health["registry_snapshot_id"])
        assert (building_list["map_run_id"] == registry_map["map_run_id"] ==
                points["map_run_id"] == health["map_run_id"])
        assert building_list["dataset_total"] == registry_map["counts"]["records"]
        assert points["counts"]["points"] == registry_map["counts"]["geocoded"]
        assert points["counts"]["mapped_construction_points"] == 0
        assert all(item["point_type"] == "developer_building" for item in points["items"])
        assert health["counts"]["offers"] == 0
        assert_public(building_list)
        assert_public(registry_map)
        assert_public(points)

    if mode == "demo":
        tilesets_by_kind = {item["kind"]: item for item in full_map["tilesets"]
                            if item["kind"] != "facade"}
        expected_layers = {
            "city_volumes": "synthetic_massing",
            "roads": "cartographic_ribbon",
            "parks": "cartographic_surface",
            "water": "cartographic_surface",
        }
        if "/public-citywide-v8/" in tilesets_by_kind["city_volumes"]["url"]:
            expected_layers["ground_buildings"] = "cartographic_surface"
        assert expected_layers.keys() <= tilesets_by_kind.keys()
        for kind, quality in expected_layers.items():
            assert tilesets_by_kind[kind]["model_quality"] == quality
            assert tilesets_by_kind[kind]["attribution"].strip()
        assert health["counts"]["complexes"] == 100
        assert health["counts"]["offers"] == 1000
        assert by_dataset["demo_offers"]["records_total"] == 1000
        assert by_dataset["demo_offers"]["complexes_total"] == 100
        assert by_dataset["real_current_offers"]["records_total"] == 0
        assert by_dataset["real_current_offers"]["complexes_total"] == 0
        assert by_dataset["city_geometry"]["records_total"] == health["counts"]["city_buildings"]
        assert all(item["source"]["quality"] == "illustrative" for item in search_all["items"])
        assert full_map["complexes"], "demo map has no markers"
        last_complex = full_map["complexes"][-1]["complex_id"]
        selected = post_json(base, "/v1/search", {"filters": {
            "complex_ids": [last_complex], "limit": 100
        }})
        validate("/v1/search", "post", selected)
        assert selected["count"] > 0
        assert all(item["complex_id"] == last_complex for item in selected["items"])
        selected_map = get_json(base, "/v1/map", {"complex_ids": [last_complex]})
        validate("/v1/map", "get", selected_map)
        assert selected_map["counts"]["offers"] == selected["count"]
        assert len(selected_map["complexes"]) == 1

        predicates = [{"key": "unit.area_sqm", "op": "min", "value": 50},
                      {"key": "unit.area_sqm", "op": "max", "value": 80}]
        typed_search = post_json(base, "/v1/search", {"filters": {
            "predicates": predicates, "limit": 100,
        }})
        typed_map = get_json(base, "/v1/map", {"predicates": json.dumps(predicates)})
        validate("/v1/search", "post", typed_search)
        validate("/v1/map", "get", typed_map)
        assert typed_map["counts"]["offers"] == typed_search["count"]
        assert typed_map["applied_filters"]["predicates"] == predicates
        assert all(50 <= item["area_sqm"] <= 80 for item in typed_search["items"])
        assert_public(typed_search)
        assert_public(typed_map)

        query = "до 80 м²"
        area_search = post_json(base, "/v1/search", {"query": query, "filters": {"limit": 100}})
        area_map = get_json(base, "/v1/map", {"query": query})
        validate("/v1/map", "get", area_map)
        assert area_search["applied_filters"]["area_max_sqm"] == 80.0
        assert area_map["counts"]["offers"] == area_search["count"]

        first_complex = get_json(base, f"/v1/complexes/{selected['items'][0]['complex_id']}")
        validate("/v1/complexes/{complex_id}", "get", first_complex)
        assert_public(first_complex)
        first_offer = get_json(base, f"/v1/offers/{selected['items'][0]['offer_id']}")
        validate("/v1/offers/{offer_id}", "get", first_offer)
        assert_public(first_offer)
        assert "offer.price_per_sqm_rub" in first_offer["facts"]
        ids = [item["offer_id"] for item in selected["items"][:2]]
        if len(ids) == 2:
            comparison = post_json(base, "/v1/compare", {"offer_ids": ids[::-1]})
            validate("/v1/compare", "post", comparison)
            assert [item["offer_id"] for item in comparison["items"]] == ids[::-1]
            assert_public(comparison)
        for tileset in full_map["tilesets"]:
            check_tileset(base, tileset["url"])
    else:
        assert search_all["count"] == 0 or all(
            item["source"]["record_mode"] != "demo" for item in search_all["items"]
        )

    print(f"PASS: live v1.1 contract, 55-field dataset coverage, typed map/search parity, source redaction, assets; mode={mode}, "
          f"snapshot={snapshot}, offers={health['counts']['offers']}, "
          f"city_buildings={health['counts']['city_buildings']}, "
          f"facade_models={health['counts']['facade_models']}")


if __name__ == "__main__":
    main()
