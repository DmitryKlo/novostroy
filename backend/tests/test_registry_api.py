"""Direct route and ASGI response checks without an HTTP transport or socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import anyio.to_thread
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from starlette.requests import Request

import backend.app as api
import backend.registry as registry
import backend.visibility as visibility
import backend.generation as generation
from backend.store import ASSET_ROOT, ROOT, load_catalogue


def _row(source_id: str, name: str, *, location: list[float] | None = None,
         city: str = "Москва", kind: str = "multifamily", status: str = "Строится") -> dict:
    return {"schema_version": "estateline-projects-v1",
            "registry_id": source_id, "source_row_id": "same-unrelated-row-id",
            "name": name, "city": city, "record_kind": kind,
            "address": f"Москва, {name}", "latest_known_date": "2026-09-06",
            "status": status, "handover": "IV кв. 2026", "handover_year": 2026,
            "handover_quarter": 4, "handover_precision": "quarter", "floors": 22,
            "building_type": "монолитный", "location": location,
            "location_quality": "verified" if location else None,
            "provenance": {"source_url": "https://private.example/id", "source_path": "/private/file.xlsx",
                           "location_source": {"osm_type": "node", "osm_id": 123, "osm_version": 1,
                                               "osm_timestamp": "2026-01-01T00:00:00Z",
                                               "members": [123],
                                               "representative_point_method": "node_coordinate",
                                               "source_attribution": "© OpenStreetMap contributors"}},
            "location_method": "strict_osm_address_tuple"}


def _write_import(path: Path, rows: list[dict]) -> None:
    projects = path / "projects.jsonl"
    projects.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (path / "source_manifest.json").write_text(json.dumps({
        "schema_version": "estateline-projects-v1", "records_total": len(rows),
        "projects_sha256": hashlib.sha256(projects.read_bytes()).hexdigest(),
        "rights_status": "user_supplied_internal_use"}), encoding="utf-8")
    registry._load_registry.cache_clear()


@pytest.fixture
def imported_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    rows = [
        _row("estateline:source-url-id-1", "Жилой корпус А", location=[37.62, 55.75]),
        _row("estateline:source-url-id-2", "Жилой корпус Б", status="Завершён"),
        _row("estateline:source-url-id-3", "Малоэтажный дом", kind="low_rise"),
    ]
    _write_import(tmp_path, rows)
    monkeypatch.setattr(registry, "PROJECTS_PATH", tmp_path / "projects.jsonl")
    monkeypatch.setattr(registry, "MANIFEST_PATH", tmp_path / "source_manifest.json")
    monkeypatch.setattr(visibility, "ACTIVE_PATH", tmp_path / "visibility-not-published.json")
    monkeypatch.setattr(generation, "ACTIVE_PATH", tmp_path / "generation-not-published.json")
    monkeypatch.delenv("NOVOSTROY_REGISTRY_MODE", raising=False)
    yield rows
    registry._load_registry.cache_clear()


def _validate(definition: str, value: dict) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"}).validate(value)


def test_auto_bootstrap_list_filters_map_and_private_identity(imported_registry: list[dict]) -> None:
    health = api.health()
    assert health["catalog_view"] == "building_registry" and health["registry_enabled"] is True
    payload = api.registry_buildings(query=None, status=None, kind=None, limit=2, offset=0)
    _validate("RegistryListResult", payload)
    assert payload["dataset_id"] == "building_registry"
    assert payload["dataset_total"] == payload["records_total"] == 3
    assert len(payload["items"]) == 2
    assert sum(f["count"] for f in payload["facets"]["kinds"]) == 3
    all_items = api.registry_buildings(query=None, status=None, kind=None, limit=100, offset=0)["items"]
    assert len({item["registry_id"] for item in all_items}) == 3
    assert all(item["has_apartment_data"] is False and item["offer_count"] == 0
               and item["price"] is None for item in all_items)
    assert all(item["latest_known_date"] == "2026-09-06" for item in all_items)
    public_json = json.dumps(payload)
    assert "source-url-id" not in public_json and "private.example" not in public_json
    assert "source_row_id" not in public_json and "estateline" not in public_json
    selected = api.registry_buildings(query="КОРПУС", status="Завершён", kind=None, limit=48, offset=0)
    assert selected["records_total"] == 1 and selected["items"][0]["name"] == "Жилой корпус Б"
    low_rise = api.registry_buildings(query=None, status=None, kind="low_rise", limit=48, offset=0)
    assert low_rise["records_total"] == 1
    detail = api.registry_building_detail(low_rise["items"][0]["registry_id"])
    _validate("RegistryBuilding", detail)
    with pytest.raises(HTTPException) as missing:
        api.registry_building_detail("estateline:source-url-id-3")
    assert missing.value.status_code == 404
    mapped = api.registry_map(query=None, status=None, kind=None)
    _validate("RegistryMapResult", mapped)
    assert mapped["counts"]["records"] == 3
    assert mapped["counts"]["geocoded"] == len(mapped["items"]) == 1
    assert mapped["counts"]["offers"] == mapped["counts"]["facade_models"] == 0
    expected_tiles = {"city_volumes", "roads", "parks", "water"}
    if api.asset_manifest().get("ground_tileset_url"):
        expected_tiles.add("ground_buildings")
    assert {tile["kind"] for tile in mapped["tilesets"]} == expected_tiles
    assert all(tile["attribution"] for tile in mapped["tilesets"])
    status_map = api.registry_map(query=None, status="Завершён", kind=None)
    assert status_map["counts"]["records"] == 1 and status_map["counts"]["geocoded"] == 0
    with pytest.raises(HTTPException) as invalid:
        api.registry_map(query=None, status=None, kind="school")
    assert invalid.value.status_code == 422
    coverage = api.data_coverage_view()["datasets"][-1]
    assert coverage["dataset_id"] == "building_registry" and coverage["records_total"] == 3
    assert coverage["complexes_total"] is None and coverage["distributions"] == []
    assert {item["key"]: item["populated_count"] for item in coverage["fields"]}["building.location"] == 1


def test_registry_rejects_duplicate_identity_and_city_conflict(imported_registry: list[dict],
                                                                 tmp_path: Path) -> None:
    duplicate = _row(imported_registry[0]["registry_id"], "Different building")
    _write_import(tmp_path, [*imported_registry, duplicate])
    with pytest.raises(ValueError, match="duplicated"):
        registry.load_registry()
    wrong_city = _row("estateline:another-id", "Область", city="Московская область")
    _write_import(tmp_path, [wrong_city])
    with pytest.raises(ValueError, match="non-Moscow"):
        registry.load_registry()


def test_registry_never_maps_unverified_location(imported_registry: list[dict], tmp_path: Path) -> None:
    imported_registry[1]["location"] = [37.7, 55.8]
    imported_registry[1]["location_quality"] = "ambiguous"
    _write_import(tmp_path, imported_registry)
    with pytest.raises(ValueError, match="verified Moscow point"):
        registry.load_registry()


def test_registry_manifest_integrity_is_required(imported_registry: list[dict]) -> None:
    registry.PROJECTS_PATH.write_text(registry.PROJECTS_PATH.read_text(encoding="utf-8") + "\n",
                                      encoding="utf-8")
    registry._load_registry.cache_clear()
    with pytest.raises(ValueError, match="hash does not match"):
        registry.load_registry()


def test_visibility_policy_excludes_archived_from_list_map_and_detail(
    monkeypatch: pytest.MonkeyPatch, imported_registry: list[dict], tmp_path: Path,
) -> None:
    source = tmp_path / "visibility"
    run_id = "visibility-v1-" + "a" * 16
    run_dir = source / "runs" / run_id
    run_dir.mkdir(parents=True)
    projects_hash = hashlib.sha256(registry.PROJECTS_PATH.read_bytes()).hexdigest()
    manifest_hash = hashlib.sha256(registry.MANIFEST_PATH.read_bytes()).hexdigest()
    rows = [{"registry_id": row["registry_id"], "visibility": "archived" if index == 0 else "visible",
             "source_observed_at": row["latest_known_date"]}
            for index, row in enumerate(imported_registry)]
    records_path = run_dir / "records.jsonl"
    records_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    manifest = {"schema_version": "registry-visibility/1", "run_id": run_id,
                "rules_version": "registry-visibility-v4-age-purpose", "max_age_days": 365,
                "as_of_date": (today - timedelta(days=1)).isoformat(),
                "next_refresh_due": (today + timedelta(days=1)).isoformat(),
                "source_sha256": projects_hash, "source_manifest_sha256": manifest_hash,
                "records_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
                "records_total": 3, "decisions": {"visible": 2, "archived": 1}}
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer_path = source / "active.json"
    pointer_path.write_text(json.dumps({"run_id": run_id,
                                       "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}),
                            encoding="utf-8")
    monkeypatch.setattr(visibility, "VISIBILITY_ROOT", source)
    monkeypatch.setattr(visibility, "ACTIVE_PATH", pointer_path)
    visibility._load_run.cache_clear()
    loaded = registry.load_registry()
    assert loaded["visibility"]["archived_total"] == 1
    payload = api.registry_buildings(query=None, status=None, kind=None, limit=48, offset=0)
    mapped = api.registry_map(query=None, status=None, kind=None)
    assert payload["dataset_total"] == payload["records_total"] == mapped["counts"]["records"] == 2
    assert mapped["counts"]["geocoded"] == 0
    hidden = registry._public_record(imported_registry[0])["registry_id"]
    assert hidden not in {item["registry_id"] for item in payload["items"]}
    with pytest.raises(HTTPException) as missing:
        api.registry_building_detail(hidden)
    assert missing.value.status_code == 404
    visibility._load_run.cache_clear()


def test_registry_fact_filters_and_unknown_walking_evidence(imported_registry: list[dict]) -> None:
    loaded = registry.load_registry()
    one = next(item for item in loaded["items"] if item["location"] is not None)
    all_ids = {item["registry_id"] for item in loaded["items"]}
    facts = {one["registry_id"]: {"metro": 12, "school": 8,
                                   "kindergarten": None, "medical": None}}
    assert len(registry.select_records(loaded, handover_year=2026, handover_quarter=4,
                                       floors_min=20, floors_max=25)) == 3
    assert registry.select_records(loaded, handover_year=2025) == []
    assert registry.select_records(loaded, floors_min=23) == []
    assert {row["registry_id"] for row in registry.select_records(
        loaded, walk_metro_max_min=15, walking_minutes=facts)} == {one["registry_id"]}
    assert registry.select_records(loaded, walk_metro_max_min=10, walking_minutes=facts) == []
    assert registry.select_records(loaded, walk_school_max_min=7, walking_minutes=facts) == []
    assert {row["registry_id"] for row in registry.select_records(
        loaded, walk_metro_max_min=15, walk_school_max_min=10,
        walking_minutes=facts)} == {one["registry_id"]}
    assert registry.select_records(loaded, walk_metro_max_min=15,
                                   walk_kindergarten_max_min=10, walking_minutes=facts) == []
    assert all_ids == set(loaded["visible_ids"])
    for invalid in ("", "x" * 161):
        with pytest.raises(ValueError, match="district"):
            registry.select_records(loaded, district=invalid)
    with pytest.raises(HTTPException) as invalid:
        api.registry_map(query=None, status=None, kind=None, district="x" * 161)
    assert invalid.value.status_code == 422


def test_verified_district_and_walking_filter_parity_across_routes(
    monkeypatch: pytest.MonkeyPatch, imported_registry: list[dict],
) -> None:
    loaded = registry.load_registry()
    geocoded = next(item for item in loaded["items"] if item["location"] is not None)
    district = loaded["district_names"][geocoded["registry_id"]]
    facts = {geocoded["registry_id"]: {"metro": 12, "school": 8,
                                       "kindergarten": None, "medical": None}}
    run = {"registry_snapshot_id": loaded["source_snapshot_id"],
           "model_run_id": "fixture-run", "checked_at": "2026-10-10T00:00:00Z",
           "items": [], "geometries": {}}
    monkeypatch.setattr(api, "active_registry", lambda: loaded)
    monkeypatch.setattr(api, "active_model_run", lambda _registry=None: run)
    monkeypatch.setattr(api, "load_point_labels", lambda _: {})
    monkeypatch.setattr(api, "load_group_evidence", lambda snapshot, run_id, ids: {
        "schema_version": 1, "visibility": "public", "registry_snapshot_id": snapshot,
        "model_run_id": run_id, "groups": []})
    monkeypatch.setattr(api, "walk_minutes_for_registry", lambda _: facts)
    filters = dict(query=None, status=None, kind=None, district=district,
                   handover_year=None, handover_quarter=None, floors_min=None, floors_max=None,
                   walk_metro_max_min=15, walk_school_max_min=10,
                   walk_kindergarten_max_min=None, walk_medical_max_min=None)
    listing = api.registry_buildings(**filters, limit=48, offset=0)
    mapping = api.registry_map(**filters)
    request = Request({"type": "http", "method": "GET", "path": "/v1/map/points",
                       "query_string": b"", "headers": []})
    points = json.loads(api.map_points(request, **filters).body)
    assert listing["records_total"] == mapping["counts"]["records"] == 1
    assert mapping["counts"]["geocoded"] == points["counts"]["registry_points"] == 1
    assert listing["items"][0]["registry_id"] == points["items"][0]["registry_id"]
    assert points["counts"]["mapped_construction_points"] == 0
    assert points["counts"]["excluded_mapped_by_registry_filters"] == 0


def test_group_evidence_uses_source_snapshot_but_projects_public_snapshot(
    monkeypatch: pytest.MonkeyPatch, imported_registry: list[dict], caplog: pytest.LogCaptureFixture,
) -> None:
    loaded = registry.load_registry()
    run = {"registry_snapshot_id": loaded["source_snapshot_id"],
           "model_run_id": "fixture-run", "checked_at": "2026-10-10T00:00:00Z",
           "items": [], "geometries": {}}
    monkeypatch.setattr(api, "active_registry", lambda: loaded)
    monkeypatch.setattr(api, "active_model_run", lambda _registry=None: run)
    monkeypatch.setattr(api, "load_point_labels", lambda _: {})
    passed: list[tuple[str, str, set[str]]] = []

    def empty_evidence(snapshot: str, run_id: str, point_ids: set[str]) -> dict:
        passed.append((snapshot, run_id, point_ids))
        return {"schema_version": 1, "visibility": "public",
                "registry_snapshot_id": snapshot, "model_run_id": run_id, "groups": []}

    monkeypatch.setattr(api, "load_group_evidence", empty_evidence)
    request = Request({"type": "http", "method": "GET", "path": "/v1/map/points",
                       "query_string": b"", "headers": []})
    filters = dict(query=None, status=None, kind=None, district=None,
                   handover_year=None, handover_quarter=None, floors_min=None, floors_max=None,
                   walk_metro_max_min=None, walk_school_max_min=None,
                   walk_kindergarten_max_min=None, walk_medical_max_min=None)
    body = json.loads(api.map_points(request, **filters).body)
    assert passed[0][0] == loaded["source_snapshot_id"]
    assert passed[0][1] == run["model_run_id"]
    assert len(passed[0][2]) == 1
    assert body["group_evidence"]["registry_snapshot_id"] == loaded["snapshot_id"]
    assert body["group_evidence"]["groups"] == []

    def corrupt_evidence(*_args: object) -> dict:
        raise api.GroupEvidenceError("private source ID must stay private")

    monkeypatch.setattr(api, "load_group_evidence", corrupt_evidence)
    degraded = json.loads(api.map_points(request, **filters).body)
    assert degraded["counts"]["points"] == 1
    assert degraded["group_evidence"]["groups"] == []
    assert degraded["group_evidence"]["registry_snapshot_id"] == loaded["snapshot_id"]
    assert "private source ID" not in json.dumps(degraded)
    assert "private source ID" not in caplog.text


def test_group_projection_requires_all_members_in_filtered_points(
    monkeypatch: pytest.MonkeyPatch, imported_registry: list[dict],
) -> None:
    loaded = registry.load_registry()
    loaded = {**loaded, "items": [dict(item) for item in loaded["items"]]}
    second = next(item for item in loaded["items"] if item["name"] == "Жилой корпус Б")
    second["location"] = [37.63, 55.76]
    run = {"registry_snapshot_id": loaded["source_snapshot_id"],
           "model_run_id": "fixture-run", "checked_at": "2026-10-10T00:00:00Z",
           "items": [], "geometries": {}}
    monkeypatch.setattr(api, "active_registry", lambda: loaded)
    monkeypatch.setattr(api, "active_model_run", lambda _registry=None: run)
    monkeypatch.setattr(api, "load_point_labels", lambda _: {})

    def one_verified_group(snapshot: str, run_id: str, point_ids: set[str]) -> dict:
        assert snapshot == loaded["source_snapshot_id"] and len(point_ids) == 2
        return {"schema_version": 1, "visibility": "public",
                "registry_snapshot_id": snapshot, "model_run_id": run_id,
                "groups": [{"group_id": "group_" + "a" * 24,
                            "group_scope": "residential_complex",
                            "identity_basis": "explicit_relation_membership",
                            "group_label": "ЖК «Проверенный пример»",
                            "point_ids": sorted(point_ids)}]}

    monkeypatch.setattr(api, "load_group_evidence", one_verified_group)
    request = Request({"type": "http", "method": "GET", "path": "/v1/map/points",
                       "query_string": b"", "headers": []})
    filters = dict(query=None, status=None, kind=None, district=None,
                   handover_year=None, handover_quarter=None, floors_min=None, floors_max=None,
                   walk_metro_max_min=None, walk_school_max_min=None,
                   walk_kindergarten_max_min=None, walk_medical_max_min=None)
    full = json.loads(api.map_points(request, **filters).body)
    assert len(full["group_evidence"]["groups"]) == 1
    assert full["group_evidence"]["registry_snapshot_id"] == loaded["snapshot_id"]
    filtered = json.loads(api.map_points(request, **{**filters, "query": "корпус А"}).body)
    assert filtered["counts"]["points"] == 1
    assert filtered["group_evidence"]["groups"] == []


def test_supplied_import_all_public_rows_match_schema() -> None:
    path = ROOT / "data/property/estateline/projects.jsonl"
    if not path.is_file():
        pytest.skip("supplied registry import is not present")
    snapshot = registry.load_registry()
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/RegistryBuilding"},
                                     format_checker=FormatChecker())
    assert snapshot["items"]
    for item in snapshot["items"]:
        validator.validate(item)
        assert item["city"] == "Москва"
        assert item["price"] is None and item["offer_count"] == 0
        assert item["location"] is None or item["location_quality"] == "verified"
    serialized = json.dumps(snapshot, ensure_ascii=False)
    for private_text in ("estateline:", "EstateLine", "source_row_id", "source_url_page_id",
                         "source_url", "source_report", "provenance", ".xlsx",
                         "1_xls_estateline", "https://", "http://"):
        assert private_text.casefold() not in serialized.casefold()


def test_registry_mode_off_and_incomplete_import(monkeypatch: pytest.MonkeyPatch,
                                                  imported_registry: list[dict]) -> None:
    monkeypatch.setenv("NOVOSTROY_REGISTRY_MODE", "off")
    assert api.health()["registry_enabled"] is False
    with pytest.raises(HTTPException) as missing:
        api.registry_buildings(query=None, status=None, kind=None, limit=48, offset=0)
    assert missing.value.status_code == 404
    monkeypatch.setenv("NOVOSTROY_REGISTRY_MODE", "auto")
    registry.MANIFEST_PATH.unlink()
    with pytest.raises(RuntimeError, match="incomplete"):
        registry.registry_enabled()


def test_ground_descriptor_is_shared_without_double_count(monkeypatch: pytest.MonkeyPatch,
                                                           imported_registry: list[dict]) -> None:
    manifest = dict(api.asset_manifest())
    manifest["ground_tileset_url"] = "/assets/public-ground-v2/tileset.json"
    monkeypatch.setattr(api, "asset_manifest", lambda: manifest)
    registry_map = api.registry_map(query=None, status=None, kind=None)
    request = Request({"type": "http", "method": "GET", "path": "/v1/map",
                       "query_string": b"", "headers": []})
    offer_map = api.map_view(request, query=None, budget_min_rub=None, budget_max_rub=None,
                             district_ids=None, complex_ids=None, rooms=None,
                             area_min_sqm=None, area_max_sqm=None, price_basis=None,
                             predicates=None)
    clarification_map = api.map_view(request, query="рядом с Соколом", budget_min_rub=None,
                                     budget_max_rub=None, district_ids=None, complex_ids=None,
                                     rooms=None, area_min_sqm=None, area_max_sqm=None,
                                     price_basis=None, predicates=None)
    for mapped in (registry_map, offer_map):
        assert mapped["tilesets"][0]["kind"] == "ground_buildings"
        ground = [item for item in mapped["tilesets"] if item["kind"] == "ground_buildings"]
        assert len(ground) == 1
        _validate("Tileset", ground[0])
        assert ground[0] == {
            "id": "overture-moscow-building-ground",
            "url": "/assets/public-ground-v2/tileset.json",
            "kind": "ground_buildings", "model_quality": "cartographic_surface",
            "label": "Контуры зданий",
            "attribution": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps",
        }
        assert mapped["counts"]["city_buildings"] == manifest["city_buildings"]
        assert "ground_buildings" not in mapped["counts"]
    assert clarification_map["clarification"] is not None
    assert any(item["kind"] == "ground_buildings" for item in clarification_map["tilesets"])
    assert clarification_map["counts"]["city_buildings"] == manifest["city_buildings"]


def _scope(*, method: str = "GET", headers: dict[str, str] | None = None) -> dict:
    return {"type": "http", "method": method, "path": "/assets/public-roads-v1/tileset.json",
            "root_path": "", "query_string": b"", "headers": [
                (name.lower().encode("ascii"), value.encode("ascii"))
                for name, value in (headers or {}).items()]}


async def _static_response(scope: dict) -> list[dict]:
    files = api.RenderAssetFiles(directory=ASSET_ROOT)
    response = await files.get_response("public-roads-v1/tileset.json", scope)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(scope, receive, send)
    return messages


def _headers(message: dict) -> dict[str, str]:
    return {key.decode("ascii"): value.decode("ascii") for key, value in message["headers"]}


def test_public_asset_cache_validators_ranges_and_api_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # The managed sandbox denies worker threads, so exercise Starlette's file
    # response logic with its synchronous file operations in this process.
    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    async def run() -> None:
        first = await _static_response(_scope())
        first_headers = _headers(first[0])
        assert first[0]["status"] == 200
        assert first_headers["cache-control"] == "public, max-age=31536000, immutable"
        assert first_headers.get("etag") and first_headers.get("last-modified")
        etag = await _static_response(_scope(headers={"If-None-Match": first_headers["etag"]}))
        assert etag[0]["status"] == 304 and _headers(etag[0])["cache-control"].endswith("immutable")
        dated = await _static_response(_scope(headers={"If-Modified-Since": first_headers["last-modified"]}))
        assert dated[0]["status"] == 304
        partial = await _static_response(_scope(headers={"Range": "bytes=0-15"}))
        assert partial[0]["status"] == 206
        assert _headers(partial[0])["content-range"].startswith("bytes 0-15/")
        assert _headers(partial[0])["cache-control"].endswith("immutable")
        assert b"".join(message.get("body", b"") for message in partial[1:]) == (
            ASSET_ROOT / "public-roads-v1/tileset.json").read_bytes()[:16]
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        with pytest.raises(HTTPException) as internal:
            await files.get_response("public-roads-v1/manifest.json", _scope())
        assert internal.value.status_code == 404
        for name in ("public-citywide-v6/tileset.json",
                     str(next((ASSET_ROOT / "public-citywide-v6").glob("*.glb")).relative_to(ASSET_ROOT))):
            scope = _scope(method="HEAD")
            scope["path"] = f"/assets/{name}"
            response = await files.get_response(name, scope)
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")

        async def wrapped(scope: dict, receive: object, send: object) -> None:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"etag", b"private"), (b"last-modified", b"private")]})
            await send({"type": "http.response.body", "body": b"{}"})

        output: list[dict] = []

        async def sink(message: dict) -> None:
            output.append(message)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        await api.PrivateApiCacheMiddleware(wrapped)(
            {"type": "http", "path": "/v1/health"}, receive, sink)
        assert _headers(output[0]) == {"cache-control": "no-store"}

    asyncio.run(asyncio.wait_for(run(), timeout=10))


@pytest.mark.parametrize("public_name,pointer_key", [
    ("public-citywide-v7", "city_tileset_url"),
    ("public-citywide-v8", "city_tileset_url"),
    ("public-citywide-v11", "city_tileset_url"),
    ("public-citywide-v12", "city_tileset_url"),
    ("public-ground-v2", "ground_tileset_url"),
    ("public-ground-v3", "ground_tileset_url"),
])
def test_region_tileset_active_and_compatible_roots(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path, public_name: str,
                                                    pointer_key: str) -> None:
    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "ASSET_ROOT", tmp_path)
    public_root = tmp_path / public_name
    public_root.mkdir()
    (public_root / "manifest.json").write_text(
        json.dumps({"public_redacted_source_ids": True}), encoding="utf-8")
    name = "region_750_1109.tileset.json"
    (public_root / name).write_text('{"asset":{"version":"1.1"}}', encoding="utf-8")
    (public_root / "sub").mkdir()
    (public_root / "sub/tileset.json").write_text("{}", encoding="utf-8")
    pointer = {pointer_key: f"/assets/{public_name}/tileset.json",
               "previous_public_roots": []}
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=tmp_path)
        response = await files.get_response(f"{public_name}/{name}", _scope(method="HEAD"))
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["cache-control"].endswith("immutable")
        for invalid in ("region_750_1109.extra.tileset.json", "region_x_1109.tileset.json",
                        "manifest.json", "sub/tileset.json"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{public_name}/{invalid}", _scope(method="HEAD"))
            assert denied.value.status_code == 404
        pointer[pointer_key] = "/assets/public-citywide-v9/tileset.json"
        with pytest.raises(HTTPException) as inactive:
            await files.get_response(f"{public_name}/{name}", _scope(method="HEAD"))
        assert inactive.value.status_code == 404
        pointer["previous_public_roots"] = [public_name]
        response = await files.get_response(f"{public_name}/{name}", _scope(method="HEAD"))
        assert response.status_code == 200
        assert response.headers["cache-control"].endswith("immutable")

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_unpublished_ground_v1_is_never_a_public_root(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    public_root = tmp_path / "public-ground-v1"
    public_root.mkdir()
    (public_root / "manifest.json").write_text(
        json.dumps({"public_redacted_source_ids": True}), encoding="utf-8")
    (public_root / "tileset.json").write_text("{}", encoding="utf-8")
    (public_root / "region_1_2.tileset.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(api, "ASSET_ROOT", tmp_path)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "ground_tileset_url": "/assets/public-ground-v1/tileset.json",
        "previous_public_roots": ["public-ground-v1"],
    })
    with pytest.raises(RuntimeError, match="ground tileset pointer"):
        api.background_tilesets(load_catalogue())

    async def check() -> None:
        files = api.RenderAssetFiles(directory=tmp_path)
        for name in ("tileset.json", "region_1_2.tileset.json"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"public-ground-v1/{name}", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_staged_v7_public_assets_keep_private_files_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    public_root = ASSET_ROOT / "public-citywide-v7"
    if not public_root.is_dir():
        pytest.skip("v7 candidate is not staged")
    region = next(public_root.glob("region_*_*.tileset.json"))
    glb = next(public_root.glob("*.glb"))

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    pointer = {"city_tileset_url": "/assets/public-citywide-v7/tileset.json",
               "previous_public_roots": []}
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for name in ("tileset.json", region.name, glb.name):
            response = await files.get_response(f"public-citywide-v7/{name}", _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers.get("etag") and response.headers.get("last-modified")
            if name.endswith(".json"):
                assert response.headers["content-type"].startswith("application/json")
            if name == region.name:
                cached = await files.get_response(f"public-citywide-v7/{name}",
                                                  _scope(headers={"If-None-Match": response.headers["etag"]}))
                assert cached.status_code == 304
                assert cached.headers["cache-control"].endswith("immutable")
        for name in ("manifest.json", "source_tile_index.jsonl", "part_tile_index.jsonl",
                     "region_750_1109.extra.tileset.json"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"public-citywide-v7/{name}", _scope(method="HEAD"))
            assert denied.value.status_code == 404
        pointer["city_tileset_url"] = "/assets/public-citywide-v6/tileset.json"
        pointer["previous_public_roots"] = ["public-citywide-v7"]
        assert (await files.get_response(f"public-citywide-v7/{region.name}",
                                         _scope(method="HEAD"))).status_code == 200

    asyncio.run(asyncio.wait_for(check(), timeout=10))


@pytest.mark.parametrize("public_name,pointer_key", [
    ("public-ground-v2", "ground_tileset_url"),
    ("public-ground-v3", "ground_tileset_url"),
    ("public-citywide-v8", "city_tileset_url"),
    ("public-citywide-v9", "city_tileset_url"),
    ("public-citywide-v10", "city_tileset_url"),
    ("public-citywide-v11", "city_tileset_url"),
])
def test_staged_ground_and_city_assets_cache_and_privacy(monkeypatch: pytest.MonkeyPatch,
                                                          public_name: str, pointer_key: str) -> None:
    public_root = ASSET_ROOT / public_name
    if not public_root.is_dir():
        pytest.skip(f"{public_name} candidate is not staged")
    region = next(public_root.glob("region_*_*.tileset.json"))
    glb = next(public_root.glob("*.glb"))

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    pointer = {pointer_key: f"/assets/{public_name}/tileset.json", "previous_public_roots": []}
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for name in ("tileset.json", region.name, glb.name):
            response = await files.get_response(f"{public_name}/{name}", _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers.get("etag") and response.headers.get("last-modified")
            if name.endswith(".json"):
                assert response.headers["content-type"].startswith("application/json")
            if name == region.name:
                cached = await files.get_response(f"{public_name}/{name}",
                                                  _scope(headers={"If-None-Match": response.headers["etag"]}))
                assert cached.status_code == 304
                assert cached.headers["cache-control"].endswith("immutable")
        scope = _scope(headers={"Range": "bytes=0-15"})
        scope["path"] = f"/assets/{public_name}/{glb.name}"
        partial = await files.get_response(f"{public_name}/{glb.name}", scope)
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        await partial(scope, receive, send)
        assert messages[0]["status"] == 206
        assert _headers(messages[0])["content-range"].startswith("bytes 0-15/")
        assert _headers(messages[0])["cache-control"].endswith("immutable")
        with glb.open("rb") as stream:
            expected = stream.read(16)
        assert b"".join(message.get("body", b"") for message in messages[1:]) == expected
        for name in ("manifest.json", "source_tile_index.jsonl", "part_tile_index.jsonl",
                     "region_x_0.tileset.json"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{public_name}/{name}", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_active_v11_pointer_preserves_public_compatibility_and_private_gate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = api.asset_manifest()
    if pointer.get("city_tileset_url") != "/assets/public-citywide-v11/tileset.json":
        pytest.skip("v11 public pointer is not active")
    assert "public-citywide-v10" in pointer["previous_public_roots"]
    assert pointer["ground_tileset_url"] == "/assets/public-ground-v3/tileset.json"
    assert pointer["roads_tileset_url"] == "/assets/public-roads-v2/tileset.json"
    assert pointer["project_models_root"].startswith("public-project-models-v1-")
    region = next((ASSET_ROOT / "public-citywide-v11").glob("region_*_*.tileset.json"))
    glb = next((ASSET_ROOT / "public-citywide-v11").glob("*.glb"))
    prior_region = next((ASSET_ROOT / "public-citywide-v10").glob("region_*_*.tileset.json"))

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for root, name in (("public-citywide-v11", "tileset.json"),
                           ("public-citywide-v11", region.name),
                           ("public-citywide-v11", glb.name),
                           ("public-citywide-v10", prior_region.name)):
            response = await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers["etag"] and response.headers["last-modified"]
            cached = await files.get_response(f"{root}/{name}",
                                              _scope(headers={"If-None-Match": response.headers["etag"]}))
            assert cached.status_code == 304
        for root, name in (("public-citywide-v11", "manifest.json"),
                           ("public-citywide-v11", "region_x_1.tileset.json"),
                           ("citywide-v11", "tileset.json")):
            with pytest.raises(HTTPException) as blocked:
                await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
            assert blocked.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_active_v12_pointer_preserves_v11_and_private_gate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = api.asset_manifest()
    if pointer.get("city_tileset_url") != "/assets/public-citywide-v12/tileset.json":
        pytest.skip("v12 public pointer is not active")
    assert "public-citywide-v11" in pointer["previous_public_roots"]
    assert pointer["ground_tileset_url"] == "/assets/public-ground-v3/tileset.json"
    assert pointer["roads_tileset_url"] == "/assets/public-roads-v2/tileset.json"
    assert pointer["project_models_root"].startswith("public-project-models-v1-")
    region = next((ASSET_ROOT / "public-citywide-v12").glob("region_*_*.tileset.json"))
    glb = next((ASSET_ROOT / "public-citywide-v12").glob("*.glb"))
    prior_region = next((ASSET_ROOT / "public-citywide-v11").glob("region_*_*.tileset.json"))

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for root, name in (("public-citywide-v12", "tileset.json"),
                           ("public-citywide-v12", region.name),
                           ("public-citywide-v12", glb.name),
                           ("public-citywide-v11", prior_region.name)):
            response = await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers["etag"] and response.headers["last-modified"]
            cached = await files.get_response(f"{root}/{name}",
                                              _scope(headers={"If-None-Match": response.headers["etag"]}))
            assert cached.status_code == 304
        for root, name in (("public-citywide-v12", "manifest.json"),
                           ("public-citywide-v12", "region_x_1.tileset.json"),
                           ("citywide-v12", "tileset.json")):
            with pytest.raises(HTTPException) as blocked:
                await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
            assert blocked.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_staged_v12_actual_static_cache_range_and_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-citywide-v12/tileset.json").is_file():
        pytest.skip("v12 public candidate is not staged")
    pointer = {**api.asset_manifest(),
               "city_tileset_url": "/assets/public-citywide-v12/tileset.json"}
    pointer["previous_public_roots"] = sorted(set(pointer["previous_public_roots"])
                                              | {"public-citywide-v11"})
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    region = next((ASSET_ROOT / "public-citywide-v12").glob("region_*_*.tileset.json"))
    glb = next((ASSET_ROOT / "public-citywide-v12").glob("*.glb"))

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for name in ("tileset.json", region.name, glb.name):
            path = f"public-citywide-v12/{name}"
            response = await files.get_response(path, _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
            cached = await files.get_response(path, _scope(headers={"If-None-Match": response.headers["etag"]}))
            assert cached.status_code == 304
        path = f"public-citywide-v12/{glb.name}"
        scope = _scope(headers={"Range": "bytes=0-15"})
        response = await files.get_response(path, scope)
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        await response(scope, receive, send)
        assert messages[0]["status"] == 206
        assert _headers(messages[0])["content-range"].startswith("bytes 0-15/")
        assert b"".join(message.get("body", b"") for message in messages[1:]) == glb.read_bytes()[:16]
        for path in ("public-citywide-v12/manifest.json", "citywide-v12/tileset.json",
                     "public-citywide-v12/region_x_1.tileset.json"):
            with pytest.raises(HTTPException) as blocked:
                await files.get_response(path, _scope(method="HEAD"))
            assert blocked.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_v8_pointer_keeps_sanitized_city_v4_to_v7_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-citywide-v8").is_dir():
        pytest.skip("v8 candidate is not staged")

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "city_tileset_url": "/assets/public-citywide-v8/tileset.json",
        "previous_public_roots": [f"public-citywide-v{version}" for version in range(4, 8)],
    })

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for version in range(4, 8):
            response = await files.get_response(f"public-citywide-v{version}/tileset.json",
                                                _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
        region = next((ASSET_ROOT / "public-citywide-v7").glob("region_*_*.tileset.json"))
        response = await files.get_response(f"public-citywide-v7/{region.name}", _scope(method="HEAD"))
        assert response.status_code == 200
        assert response.headers["cache-control"].endswith("immutable")

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_v9_pointer_keeps_sanitized_v8_region_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-citywide-v9" / "tileset.json").is_file():
        pytest.skip("v9 candidate is not staged")

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "city_tileset_url": "/assets/public-citywide-v9/tileset.json",
        "previous_public_roots": [f"public-citywide-v{version}" for version in range(4, 9)],
    })

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for version in (8, 9):
            root = f"public-citywide-v{version}"
            region = next((ASSET_ROOT / root).glob("region_*_*.tileset.json"))
            for name in ("tileset.json", region.name):
                response = await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
                assert response.status_code == 200
                assert response.headers["cache-control"].endswith("immutable")
                assert response.headers.get("etag") and response.headers.get("last-modified")
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{root}/manifest.json", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_v10_pointer_keeps_sanitized_v9_region_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-citywide-v10" / "tileset.json").is_file():
        pytest.skip("v10 candidate is not staged")

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "city_tileset_url": "/assets/public-citywide-v10/tileset.json",
        "previous_public_roots": [f"public-citywide-v{version}" for version in range(4, 10)],
    })

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for version in (9, 10):
            root = f"public-citywide-v{version}"
            region = next((ASSET_ROOT / root).glob("region_*_*.tileset.json"))
            for name in ("tileset.json", region.name):
                response = await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
                assert response.status_code == 200
                assert response.headers["cache-control"].endswith("immutable")
                assert response.headers.get("etag") and response.headers.get("last-modified")
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{root}/manifest.json", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_roads_v2_pointer_keeps_sanitized_v1_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-roads-v2" / "tileset.json").is_file():
        pytest.skip("roads v2 candidate is not staged")

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "roads_tileset_url": "/assets/public-roads-v2/tileset.json",
        "previous_public_roots": ["public-roads-v1"],
    })

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for version in (1, 2):
            root = f"public-roads-v{version}"
            glb = next((ASSET_ROOT / root).glob("*.glb"))
            for name in ("tileset.json", glb.name):
                response = await files.get_response(f"{root}/{name}", _scope(method="HEAD"))
                assert response.status_code == 200
                assert response.headers["cache-control"].endswith("immutable")
                assert response.headers.get("etag") and response.headers.get("last-modified")
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{root}/manifest.json", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_v3_pointer_keeps_ground_v2_and_blocks_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (ASSET_ROOT / "public-ground-v3").is_dir():
        pytest.skip("ground v3 candidate is not staged")

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    monkeypatch.setattr(api, "asset_manifest", lambda: {
        "ground_tileset_url": "/assets/public-ground-v3/tileset.json",
        "previous_public_roots": ["public-ground-v2", "public-ground-v1"],
    })

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        region = next((ASSET_ROOT / "public-ground-v2").glob("region_*_*.tileset.json"))
        glb = next((ASSET_ROOT / "public-ground-v2").glob("*.glb"))
        for name in ("tileset.json", region.name, glb.name):
            response = await files.get_response(f"public-ground-v2/{name}", _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
        for name in ("tileset.json", "region_1_2.tileset.json"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"public-ground-v1/{name}", _scope(method="HEAD"))
            assert denied.value.status_code == 404

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_active_ground_and_city_pointer_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = api.asset_manifest()
    if not manifest.get("ground_tileset_url"):
        pytest.skip("ground pointer has not been published")
    assert manifest["ground_tileset_url"] in {
        "/assets/public-ground-v2/tileset.json", "/assets/public-ground-v3/tileset.json"}
    if manifest["ground_tileset_url"].startswith("/assets/public-ground-v3/"):
        assert "public-ground-v2" in manifest["previous_public_roots"]
    assert "public-ground-v1" not in manifest["previous_public_roots"]
    assert manifest["city_tileset_url"] in {
        "/assets/public-citywide-v8/tileset.json",
        "/assets/public-citywide-v9/tileset.json",
        "/assets/public-citywide-v10/tileset.json",
        "/assets/public-citywide-v11/tileset.json",
        "/assets/public-citywide-v12/tileset.json",
    }
    if manifest["city_tileset_url"].startswith("/assets/public-citywide-v9/"):
        assert "public-citywide-v8" in manifest["previous_public_roots"]
    if manifest["city_tileset_url"].startswith("/assets/public-citywide-v10/"):
        assert {"public-citywide-v8", "public-citywide-v9"} <= set(manifest["previous_public_roots"])
    if manifest["city_tileset_url"].startswith("/assets/public-citywide-v11/"):
        assert {"public-citywide-v8", "public-citywide-v9", "public-citywide-v10"} <= set(
            manifest["previous_public_roots"])
    if manifest["city_tileset_url"].startswith("/assets/public-citywide-v12/"):
        assert {"public-citywide-v9", "public-citywide-v10", "public-citywide-v11"} <= set(
            manifest["previous_public_roots"])
    if manifest.get("roads_tileset_url") == "/assets/public-roads-v2/tileset.json":
        assert "public-roads-v1" in manifest["previous_public_roots"]
    assert {f"public-citywide-v{version}" for version in range(4, 8)} <= set(
        manifest["previous_public_roots"])
    mapped = api.registry_map(query=None, status=None, kind=None)
    assert mapped["tilesets"][0]["kind"] == "ground_buildings"
    assert mapped["tilesets"][0]["url"] == manifest["ground_tileset_url"]
    assert mapped["counts"]["city_buildings"] == manifest["city_buildings"]
    assert "ground_buildings" not in mapped["counts"]
    request = Request({"type": "http", "method": "GET", "path": "/v1/map",
                       "query_string": b"", "headers": []})
    offer_map = api.map_view(request, query=None, budget_min_rub=None, budget_max_rub=None,
                             district_ids=None, complex_ids=None, rooms=None,
                             area_min_sqm=None, area_max_sqm=None, price_basis=None,
                             predicates=None)
    assert offer_map["tilesets"][0]["kind"] == "ground_buildings"
    assert offer_map["counts"]["city_buildings"] == manifest["city_buildings"]
    assert "ground_buildings" not in offer_map["counts"]

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)

    async def check() -> None:
        files = api.RenderAssetFiles(directory=ASSET_ROOT)
        for pointer in ("ground_tileset_url", "city_tileset_url"):
            relative = manifest[pointer].removeprefix("/assets/")
            response = await files.get_response(relative, _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            region = next((ASSET_ROOT / Path(relative).parent).glob("region_*_*.tileset.json"))
            response = await files.get_response(f"{Path(relative).parent}/{region.name}",
                                                _scope(method="HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")

    asyncio.run(asyncio.wait_for(check(), timeout=10))
