"""Contract and binary tile acceptance checks for the local vertical slice."""

from __future__ import annotations

import importlib
import hashlib
import json
import math
import struct
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from analytics.store import create_session_token
from backend.app import app
from backend.store import ASSET_ROOT, ROOT
from backend.store import load_catalogue, project_catalogue_facts
from pipeline.build_tiles import ecef


client = TestClient(app)


def test_fixture_matches_wire_schema() -> None:
    contracts = ROOT / "contracts"
    schema = json.loads((contracts / "fixture.schema.json").read_text(encoding="utf-8"))
    schema["$id"] = (contracts / "fixture.schema.json").resolve().as_uri()
    common = json.loads((contracts / "schemas.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource((contracts / "schemas.json").resolve().as_uri(), Resource.from_contents(common))
    catalogue = json.loads((ROOT / "data/demo/catalog.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, registry=registry).validate(catalogue)
    assert len(catalogue["complexes"]) == 100
    assert len(catalogue["offers"]) == 1000
    assert all(offer["source"]["quality"] == "illustrative" for offer in catalogue["offers"])
    assert all(offer["availability"] is None and offer["unit_id"] is None for offer in catalogue["offers"])


def test_map_search_share_strict_filter_and_pre_limit_count() -> None:
    district = client.get("/v1/districts").json()["items"][0]["district_id"]
    filters = {"district_ids": [district], "rooms": [2], "budget_max_rub": 20_000_000, "limit": 1}
    search = client.post("/v1/search", json={"filters": filters})
    map_response = client.get("/v1/map", params=[("district_ids", district), ("rooms", 2), ("budget_max_rub", 20_000_000)])
    assert search.status_code == map_response.status_code == 200
    assert len(search.json()["items"]) <= 1
    assert search.json()["count"] == map_response.json()["counts"]["offers"]
    assert sum(item["offer_count"] for item in map_response.json()["complexes"]) == search.json()["count"]
    assert all(item["district_id"] == district for item in map_response.json()["complexes"])


def test_hard_filters_and_invalid_requests() -> None:
    response = client.post("/v1/search", json={"filters": {"budget_max_rub": 1}})
    assert response.status_code == 200 and response.json()["count"] == 0
    response = client.get("/v1/map", params={"budget_min_rub": 100, "budget_max_rub": 1})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_request"
    response = client.get("/v1/map", params={"district_ids": "invented"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "unknown_district"
    response = client.get("/v1/map", params={"surprise": "1"})
    assert response.status_code == 422
    response = client.post("/v1/search", json={"filters": {"budget_max_rub": "10000000"}})
    assert response.status_code == 422
    response = client.post("/v1/search", json={"query": "рядом с Соколом"})
    assert response.status_code == 200 and response.json()["count"] == 0
    assert response.json()["clarification"]["field"] == "district_ids"


def test_complex_id_filter_reaches_offers_outside_default_limit() -> None:
    selected = "demo-complex-099"
    search = client.post("/v1/search", json={"filters": {"complex_ids": [selected], "limit": 100}})
    assert search.status_code == 200 and search.json()["count"] == 10
    assert all(offer["complex_id"] == selected for offer in search.json()["items"])
    map_response = client.get("/v1/map", params={"complex_ids": selected})
    assert map_response.status_code == 200 and map_response.json()["counts"]["offers"] == 10
    assert [item["complex_id"] for item in map_response.json()["complexes"]] == [selected]


def test_registry_predicates_match_map_search_and_known_facts() -> None:
    registry = client.get("/v1/criteria")
    assert registry.status_code == 200
    assert registry.json()["version"] == "1.1"
    assert registry.json()["snapshot_id"] == client.get("/v1/health").json()["snapshot_id"]
    assert len(registry.json()["criteria"]) == 55
    predicate = [{"key": "unit.floor", "op": "min", "value": 7}]
    found = client.post("/v1/search", json={"filters": {"predicates": predicate, "limit": 1}})
    mapped = client.get("/v1/map", params={"predicates": json.dumps(predicate)})
    assert found.status_code == mapped.status_code == 200
    assert found.json()["count"] > 0
    assert len(found.json()["items"]) == 1
    assert found.json()["count"] == mapped.json()["counts"]["offers"]
    assert mapped.json()["applied_filters"]["predicates"] == predicate
    detail = client.get("/v1/offers/demo-offer-0001").json()
    assert detail["facts"]["offer.price_total_rub"]["value"] == detail["price"]["amount_rub"]
    assert detail["facts"]["offer.price_per_sqm_rub"]["value"] == {
        "numerator_rub": detail["price"]["amount_rub"], "denominator_sqm": str(detail["area_sqm"]),
    }
    assert client.post("/v1/search", json={"filters": {"predicates": [
        {"key": "building.cctv", "op": "equals", "value": True},
    ]}}).json()["count"] == 0


def test_invalid_predicates_are_422_on_map_and_search() -> None:
    for predicate in (
        {"key": "invented.key", "op": "min", "value": 7},
        {"key": "unit.floor", "op": "contains", "value": "7"},
        {"key": "building.cctv", "op": "equals", "value": "true"},
    ):
        assert client.post("/v1/search", json={"filters": {"predicates": [predicate]}}).status_code == 422
        assert client.get("/v1/map", params={"predicates": json.dumps([predicate])}).status_code == 422
    assert client.get("/v1/map", params={"predicates": "[invalid"}).status_code == 422


def test_complex_fact_projection_keeps_scope_and_explicit_unknown() -> None:
    catalogue = load_catalogue()
    original = deepcopy(catalogue["offers"][0])
    group = catalogue["complexes"][0]
    group["facts"] = {"building.cctv": {"value": True, "unit": "boolean", "status": "illustrative",
                  "scope": "complex", "observed_at": catalogue["snapshot_at"],
                  "provenance": {"rights_status": "demo", "source_id": "hidden"}}}
    catalogue["offers"][0]["facts"] = {"building.cctv": {"value": None, "unit": "boolean",
        "status": "unknown", "scope": "unit", "observed_at": None,
        "provenance": {"rights_status": "demo"}}}
    projected = project_catalogue_facts(catalogue)
    assert projected["offers"][0]["facts"]["building.cctv"]["status"] == "unknown"
    assert projected["offers"][1]["facts"]["building.cctv"]["scope"] == "complex"
    assert projected["offers"][0]["facts"]["unit.floor"]["value"] == original["floor"]
    assert original["facts"] != projected["offers"][0]["facts"]


def test_public_responses_do_not_expose_provider_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("backend.app")
    catalogue = load_catalogue()
    marker = "private-provider-identity"
    catalogue["districts"][0]["source"] = marker
    catalogue["complexes"][0]["source"].update({"name": marker, "url": marker,
        "source_id": marker, "source_fields": {"raw": marker}})
    catalogue["complexes"][0]["facts"] = {"building.cctv": {"value": True,
        "unit": "boolean", "status": "illustrative", "scope": "complex",
        "observed_at": catalogue["snapshot_at"], "provenance": {"rights_status": "demo",
        "source_id": marker, "method": marker, "coverage_status": "partial"}}}
    catalogue["offers"][0]["source"].update({"name": marker, "url": marker,
        "source_id": marker, "source_record_id": marker, "source_fields": {"raw": marker}})
    catalogue = project_catalogue_facts(catalogue)
    monkeypatch.setattr(module, "load_catalogue", lambda: catalogue)
    paths = [
        client.get("/v1/health"), client.get("/v1/districts"), client.get("/v1/criteria"),
        client.get("/v1/complexes/demo-complex-001"), client.get("/v1/offers/demo-offer-0001"),
        client.get("/v1/map", params={"complex_ids": "demo-complex-001"}),
        client.post("/v1/search", json={"filters": {"complex_ids": ["demo-complex-001"]}}),
        client.post("/v1/recommendations", json={"filters": {"complex_ids": ["demo-complex-001"]}}),
        client.post("/v1/compare", json={"offer_ids": ["demo-offer-0001", "demo-offer-0002"]}),
    ]
    assert all(response.status_code == 200 for response in paths)
    assert all(marker not in response.text for response in paths)
    assert all("source_ids" not in item and "evidence" not in item
               for item in paths[2].json()["criteria"])
    public_offer = paths[4].json()
    assert set(public_offer["source"]) <= {"quality", "record_mode", "observed_at", "rights_status"}
    assert "provenance" not in public_offer["facts"]["building.cctv"]
    assert public_offer["facts"]["building.cctv"]["coverage_status"] == "partial"
    assert "method" not in public_offer["facts"]["building.cctv"]
    city_tiles = [tile for tile in paths[5].json()["tilesets"] if tile["kind"] == "city_volumes"]
    assert city_tiles and city_tiles[0]["attribution"]


def test_data_coverage_separates_demo_real_and_render_estimates(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    response = client.get("/v1/data-coverage")
    assert response.status_code == 200
    payload = response.json()
    Draft202012Validator({**schema, "$ref": "#/$defs/DataCoverageResult"}).validate(payload)
    assert payload["snapshot_id"] == client.get("/v1/health").json()["snapshot_id"]
    demo, real, city, registry = payload["datasets"]
    assert [item["dataset_id"] for item in payload["datasets"]] == [
        "demo_offers", "real_current_offers", "city_geometry", "building_registry"]
    assert (demo["records_total"], demo["complexes_total"]) == (1000, 100)
    assert (real["records_total"], real["complexes_total"], real["status"]) == (0, 0, "no_licensed_feed")
    assert len(demo["fields"]) == len(real["fields"]) == payload["criteria_total"]
    assert {field["key"] for field in demo["fields"]} == {
        criterion["key"] for criterion in client.get("/v1/criteria").json()["criteria"]}
    for distribution in demo["distributions"]:
        assert len(distribution["bins"]) <= 5
        assert sum(item["count"] for item in distribution["bins"]) == distribution["sample_count"]
        assert distribution["sample_count"] + distribution["missing_count"] + distribution["excluded_count"] == 1000
    assert real["distributions"][0]["bins"] == []
    by_key = {field["key"]: field for field in city["fields"]}
    height = by_key["city.render_height_m"]
    assert city["records_total"] == 564031
    assert height["quality_counts"]["observed"] == 6595
    assert height["quality_counts"]["estimated"] == 117191
    assert height["quality_counts"]["render_fallback"] == 440245
    assert by_key["city.render_height_imputed_m"]["quality_counts"]["estimated"] == 59
    assert by_key["city.source_num_floors"]["populated_count"] == 119335
    assert registry["data_mode"] == "registry" and registry["distributions"] == []
    assert "source_id" not in response.text and "provider_name" not in response.text
    monkeypatch.setenv("NOVOSTROY_DATA_MODE", "real")
    empty_real = client.get("/v1/data-coverage").json()
    assert empty_real["data_mode"] == "real"
    assert empty_real["datasets"][0]["records_total"] == empty_real["datasets"][1]["records_total"] == 0
    assert empty_real["datasets"][0]["status"] == "not_loaded"


def test_data_coverage_conditional_price_is_excluded() -> None:
    from backend.coverage import _distributions

    offers = [
        {"price": {"basis": "asking_total", "amount_rub": 12_000_000}, "area_sqm": 40},
        {"price": {"basis": "conditional_total", "amount_rub": 10_000_000}, "area_sqm": 45},
        {"price": None, "area_sqm": None},
    ]
    price, area = _distributions(offers)
    assert (price["sample_count"], price["missing_count"], price["excluded_count"]) == (1, 1, 1)
    assert (area["sample_count"], area["missing_count"], area["excluded_count"]) == (2, 1, 0)


def test_compare_order_and_not_found() -> None:
    ids = ["demo-offer-0010", "demo-offer-0001"]
    response = client.post("/v1/compare", json={"offer_ids": ids})
    assert response.status_code == 200
    assert [item["offer_id"] for item in response.json()["items"]] == ids
    assert client.post("/v1/compare", json={"offer_ids": [ids[0], ids[0]]}).status_code == 422
    missing = client.post("/v1/compare", json={"offer_ids": [ids[0], "unknown"]})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "not_found"


def test_real_mode_never_displays_demo_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVOSTROY_DATA_MODE", "real")
    health = client.get("/v1/health").json()
    assert health["data_mode"] == "real" and health["counts"]["offers"] == 0
    assert health["counts"]["complexes"] == 0
    assert client.post("/v1/search", json={}).json()["count"] == 0
    map_payload = client.get("/v1/map").json()
    assert map_payload["data_mode"] == "real" and map_payload["counts"]["offers"] == 0
    assert all(tile["kind"] != "facade" for tile in map_payload["tilesets"])
    assert client.get("/v1/offers/demo-offer-0001").status_code == 404


def test_stale_real_offer_hidden_from_detail_compare_and_map(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("backend.app")
    demo = client.get("/v1/offers/demo-offer-0001").json()
    stale = {**demo, "offer_id": "stale-real-offer",
             "source": {**demo["source"], "quality": "dated_asking", "record_mode": "developer_offer", "rights_status": "cleared"},
             "price": {**demo["price"], "observed_at": "2020-01-01T00:00:00Z"}}
    payload = {"snapshot_id": "test-real", "snapshot_at": "2020-01-01T00:00:00Z", "data_mode": "real",
               "districts": [], "complexes": [], "offers": [stale, demo]}
    monkeypatch.setattr(module, "load_catalogue", lambda: payload)
    assert client.get("/v1/offers/stale-real-offer").status_code == 404
    assert client.post("/v1/compare", json={"offer_ids": ["stale-real-offer", "demo-offer-0001"]}).status_code == 404
    assert client.post("/v1/search", json={}).json()["count"] == 0
    assert client.get("/v1/map").json()["counts"]["offers"] == 0


def test_events_only_persist_bounded_aggregate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = importlib.import_module("backend.app")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    before = client.get("/v1/metrics").json()
    assert before["event_counts"] == {}
    token = create_session_token()
    accepted = client.post("/v1/events", json={"event_type": "search", "session_id": token})
    assert accepted.status_code == 200 and accepted.json() == {"accepted": True}
    assert client.get("/v1/metrics").json()["event_counts"] == {"search": 1}
    rejected = client.post("/v1/events", json={"event_type": "search", "properties": {"email": "person@example.org"}})
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "invalid_request"
    database = tmp_path / "data/runtime/analytics.sqlite3"
    assert token.encode() not in database.read_bytes()
    assert b"person@example.org" not in database.read_bytes()


def parse_glb(path: Path) -> dict:
    blob = path.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", blob)
    assert magic == b"glTF" and version == 2 and total == len(blob)
    json_length, json_type = struct.unpack_from("<I4s", blob, 12)
    assert json_type == b"JSON"
    document = json.loads(blob[20:20 + json_length])
    binary_length, binary_type = struct.unpack_from("<I4s", blob, 20 + json_length)
    assert binary_type == b"BIN\x00"
    assert binary_length >= document["buffers"][0]["byteLength"]
    assert document["meshes"][0]["primitives"]
    for primitive in document["meshes"][0]["primitives"]:
        position = document["accessors"][primitive["attributes"]["POSITION"]]
        feature = document["accessors"][primitive["attributes"]["_FEATURE_ID_0"]]
        assert position["count"] > 0 and feature["count"] == position["count"]
        assert "EXT_mesh_features" in primitive["extensions"]
    return document


def test_renderable_tiles_and_georeferencing() -> None:
    city = json.loads((ASSET_ROOT / "city/tileset.json").read_text(encoding="utf-8"))
    assert city["asset"]["version"] == "1.1"
    index = json.loads((ASSET_ROOT / "city/source_tile_index.json").read_text(encoding="utf-8"))
    assert len(index["features"]) == 31
    assert {entry["height_policy"] for entry in index["features"]} == {"source_height_m", "display_fallback_15m"}
    for child in city["root"]["children"]:
        path = ASSET_ROOT / "city" / child["content"]["uri"]
        document = parse_glb(path)
        assert document["asset"]["extras"]["source_count"] > 0
        assert len(child["transform"]) == 16
    for number in range(1, 4):
        folder = ASSET_ROOT / f"facades/demo-complex-{number:03d}"
        tileset = json.loads((folder / "tileset.json").read_text(encoding="utf-8"))
        assert tileset["asset"]["version"] == "1.1"
        lon, lat = client.get(f"/v1/complexes/demo-complex-{number:03d}").json()["location"]
        transform = tileset["root"]["transform"]
        expected = ecef(lon, lat)
        assert math.dist(transform[12:15], expected) < 0.01
        tile = tileset["root"]
        for lod in range(3):
            document = parse_glb(folder / tile["content"]["uri"])
            assert document["asset"]["extras"]["model_quality"] == "synthetic_facade"
            if lod < 2:
                assert len(tile["children"]) == 1
                tile = tile["children"][0]


def test_assets_served_locally() -> None:
    active = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))["city_tileset_url"]
    for path in (active, "/assets/facades/demo-complex-001/tileset.json", "/assets/facades/demo-complex-001/lod2.glb"):
        response = client.get(path)
        assert response.status_code == 200 and response.content


def test_internal_asset_manifests_and_source_ids_are_not_public_routes() -> None:
    for path in (
        "/assets/manifest.json",
        "/assets/citywide-v3/manifest.json",
        "/assets/citywide-v3/source_tile_index.jsonl",
        "/assets/roads-v1/source_tile_index.jsonl",
    ):
        assert client.get(path).status_code == 404
    active = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))["city_tileset_url"]
    assert client.get(active).status_code == 200
    assert client.get(active.removesuffix("tileset.json") + "cell_3761_5575.glb").status_code == 200
    assert client.get("/assets/citywide-v2/tileset.json").status_code == 404


def test_gzip_assets_preserve_decoded_bytes_and_range_head() -> None:
    active = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))["city_tileset_url"]
    for path, local in (
        (active, ASSET_ROOT / active.removeprefix("/assets/")),
        ("/assets/facades/demo-complex-001/lod2.glb", ASSET_ROOT / "facades/demo-complex-001/lod2.glb"),
    ):
        if not local.is_file():
            continue
        original = local.read_bytes()
        identity = client.get(path, headers={"Accept-Encoding": "identity"})
        compressed = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert identity.status_code == compressed.status_code == 200
        assert compressed.headers["content-encoding"] == "gzip"
        assert compressed.headers["content-type"] == identity.headers["content-type"]
        assert hashlib.sha256(compressed.content).digest() == hashlib.sha256(original).digest()
        head = client.head(path, headers={"Accept-Encoding": "gzip"})
        assert head.status_code == 200 and "content-encoding" not in head.headers
        assert head.headers["content-length"] == str(len(original))
        partial = client.get(path, headers={"Accept-Encoding": "gzip", "Range": "bytes=0-15"})
        assert partial.status_code == 206
        assert partial.headers["content-range"].startswith("bytes 0-15/")
        assert "content-encoding" not in partial.headers
        assert partial.content == original[:16]


def test_completed_citywide_tile_graph_and_source_index() -> None:
    folder = ASSET_ROOT / "citywide"
    if not (folder / "manifest.json").is_file():
        pytest.skip("full citywide build not present in this checkout")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    tileset = json.loads((folder / "tileset.json").read_text(encoding="utf-8"))
    assert manifest["partial"] is False
    regions = tileset["root"]["children"]
    details = [child for parent in regions for child in parent["children"]]
    assert len(regions) == manifest["coarse_region_tiles"]
    assert len(details) == manifest["detailed_tiles"]
    assert all((folder / tile["content"]["uri"]).is_file() for tile in regions + details)
    assert sum(1 for _ in (folder / "source_tile_index.jsonl").open(encoding="utf-8")) == manifest["source_rows"]
    assert manifest["source_rows"] == manifest["unknown_height_count"] + manifest["assumed_floor_height_count"] + manifest["measured_height_count"]
    coarse = parse_glb(folder / regions[0]["content"]["uri"])
    fine = parse_glb(folder / details[0]["content"]["uri"])
    assert coarse["asset"]["extras"]["geometry_type"] == "coarse_synthetic_cell_aggregation"
    assert fine["asset"]["extras"]["geometry_type"] == "synthetic_massing_from_public_footprints"
    asset_manifest = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert any(tile["url"] == asset_manifest["city_tileset_url"]
               for tile in client.get("/v1/map").json()["tilesets"])
