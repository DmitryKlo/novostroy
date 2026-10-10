"""Bounded model-review gates without a server or ASGI worker thread."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import anyio.to_thread
import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from starlette.requests import Request

import backend.app as api
import backend.developer_experience as developer_experience
import backend.developer_map as developer_map
import backend.generation as generation
import backend.map_point_groups as map_point_groups
import backend.models as models
import backend.nearby as nearby
import backend.point_labels as point_labels
import backend.registry as registry
from backend.store import ROOT


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    body = b"".join(_json_bytes(row) for row in rows)
    path.write_bytes(body)
    return _sha(body)


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": b"", "headers": [
                        (key.lower().encode(), value.encode())
                        for key, value in (headers or {}).items()]})


def _body(response: object) -> dict:
    return json.loads(response.body)


def _validate(definition: str, value: dict) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"},
                         format_checker=FormatChecker()).validate(value)


@pytest.fixture
def model_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    # Legacy model fixtures must not consume the workspace's live developer pointers.
    monkeypatch.setattr(developer_map, "ACTIVE_PATH", tmp_path / "no-developer-map.json")
    monkeypatch.setattr(developer_experience, "ACTIVE_PATH", tmp_path / "no-experience.json")
    root = tmp_path / "models"
    run_id = "models-v1-0123456789abcdef"
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    assets = tmp_path / "assets"
    public_root = "public-project-models-v1-0123456789abcdef"
    public = assets / public_root
    public.mkdir(parents=True)
    glb = b"glTF" + b"\x00" * 16
    glb_name = f"model_{_sha(glb)}.glb"
    (public / glb_name).write_bytes(glb)
    tileset = _json_bytes({"asset": {"version": "1.1"},
                           "root": {"content": {"uri": glb_name}},
                           "extras": {"source_ids_redacted": True}})
    tile_name = f"model_{_sha(tileset)}.tileset.json"
    (public / tile_name).write_bytes(tileset)
    public_manifest = _json_bytes({
        "public_redacted_source_ids": True, "run_id": run_id, "model_count": 1,
        "files": [{"path": tile_name, "sha256": _sha(tileset), "bytes": len(tileset)},
                  {"path": glb_name, "sha256": _sha(glb), "bytes": len(glb)}],
    })
    (public / "public_manifest.json").write_bytes(public_manifest)
    registry_id = "registry-" + "a" * 24
    identifiers = ["cand_" + digit * 24 for digit in "1234"]

    def row(index: int, entity: str, decision: str, reasons: list[str],
            *, linked: bool = False, ready: bool = False) -> dict:
        attempts = {
            "registry_project": {
                "estate_project_geometry": "absent_in_source", "strict_address_key": "parsed",
                "exact_osm_location_object_matches": 0, "strict_osm_address_matches": 0,
                "verified_point_in_polygon_matches": 0, "validated_parts_parent_matches": 0,
                "donor_geometry": "not_admitted_without_verified_project_corpus_identity",
            },
            "registry_review_record": {
                "source_scope": "review_required_before_project_geometry_search",
                "estate_project_geometry": "absent_in_source",
                "donor_geometry": "not_admitted_without_residential_and_corpus_identity",
            },
            "mapped_construction": {
                "source_polygon": "valid", "typed_osm_identity": "verified_against_audit_jsonl",
                "display_rights": "ODbL-1.0_cleared", "height_tag": "absent_or_invalid",
                "above_ground_levels": "absent_or_invalid",
                "validated_part_source_id_matches": 0, "validated_parts_parent_match": False,
                "donor_geometry": "not_used",
            },
            "external_event": {
                "moscow_residential_scope": "sochi_event",
                "event_identity": "source_record_id_preserved",
                "individual_polygon": "absent_in_source",
                "donor_geometry": "not_admitted_for_non_moscow_or_non_building_event",
            },
        }[entity]
        return {"candidate_id": identifiers[index], "registry_id": registry_id if linked else None,
                "entity_type": entity, "name": f"Объект {index}",
                "attempt_log": attempts,
                "address": "/secret/provider/report.xlsx", "provider_url": "https://private.example/id",
                "decision": decision, "asset_readiness": "ready_to_render" if ready else "unavailable",
                "project_link_status": "review" if linked else "unlinked",
                "reason_codes": reasons, "explanation_ru": "PRIVATE SOURCE TEXT",
                "depiction": "construction_outline" if ready else "none",
                "model_quality": "mapped_footprint" if ready else None,
                "geometry_quality": "mapped_footprint" if ready else None,
                "geometry_as_of": "2026-10-08" if ready else None,
                "height_evidence": {"mapped_height_m": None, "mapped_levels": None,
                                    "estimated_render_height_m": None,
                                    "render_height_m": None, "render_height_basis": None} if ready else None,
                "location": [37.6, 55.7] if ready else None,
                "bbox": [37.599, 55.699, 37.601, 55.701] if ready else None,
                "preview_footprint": [[37.599, 55.699], [37.601, 55.699],
                                      [37.601, 55.701]] if ready else None,
                "preview_geometry": {"type": "Polygon", "coordinates": [[
                    [37.599, 55.699], [37.601, 55.699], [37.601, 55.701],
                    [37.599, 55.699]]]} if ready else None,
                "selected_model": {
                    "tileset_url": f"/assets/{public_root}/{tile_name}",
                    "kind": "mapped_construction", "content_hash": _sha(tileset),
                    "public_attribution": "© OpenStreetMap contributors; ODbL 1.0",
                    "rights_evidence": {"license": "ODbL-1.0", "display_cleared": True},
                } if ready else None}

    rows = [row(0, "registry_project", "unavailable", ["no_measured_geometry"], linked=True),
            row(1, "registry_review_record", "review", ["scope_review_required"]),
            row(2, "mapped_construction", "ready", ["height_unknown_flat_only"], ready=True),
            row(3, "external_event", "rejected", ["not_moscow_residential"])]
    assessments_sha = _write_jsonl(run_dir / "assessments.jsonl", rows)
    candidates_sha = _write_jsonl(run_dir / "candidates.jsonl", [])
    source_path = tmp_path / "osm-construction.jsonl"
    source_sha = _write_jsonl(source_path, [{
        "source_record_id": "way/123/v1", "osm_type": "way", "osm_id": 123,
        "osm_version": 1, "source_version": "2026-10-08T20:21:06Z",
        "name": "Корпус 5", "address_json": json.dumps({
            "addr:street": "улица А", "addr:housenumber": "2"}, ensure_ascii=False),
    }])
    evaluations_sha = _write_jsonl(run_dir / "source_evaluations.jsonl", [{
        "candidate_id": identifiers[2], "source_group": "osm_construction",
        "source_record_id": "way/123/v1", "source_version": "2026-10-08T20:21:06Z",
    }])
    manifest = {
        "schema_version": "model-candidates/1", "run_id": run_id,
        "registry_snapshot_id": "building-registry-synthetic",
        "checked_at": "2026-10-09T12:00:00Z", "records_total": len(rows),
        "decision_counts": {"ready": 1, "review": 1, "rejected": 1, "unavailable": 1},
        "asset_readiness_counts": {"ready_to_render": 1, "unavailable": 3},
        "source_counts": {"estateline_accepted": 1, "estateline_review": 1,
                          "osm_construction": 1, "minstroy_residential_event": 1,
                          "minstroy_text_audit": 0},
        "assessments_sha256": assessments_sha, "candidates_sha256": candidates_sha,
        "source_evaluations_sha256": evaluations_sha, "public_root": public_root,
        "public_manifest_sha256": _sha(public_manifest),
        "input_hashes": {"osm_construction_jsonl": source_sha},
    }
    manifest_bytes = _json_bytes(manifest)
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    root.joinpath("active.json").write_bytes(_json_bytes({
        "run_id": run_id, "manifest_sha256": _sha(manifest_bytes)}))
    pointer = {"project_models_root": public_root, "previous_public_roots": []}
    monkeypatch.setattr(models, "MODELS_ROOT", root)
    monkeypatch.setattr(models, "ACTIVE_PATH", root / "active.json")
    monkeypatch.setattr(models, "ASSET_ROOT", assets)
    fake_registry = {
        "snapshot_id": "building-eligibility-synthetic",
        "source_snapshot_id": "building-registry-synthetic",
        "snapshot_at": "2026-09-06",
        "visible_ids": {registry_id},
        "visibility": {"source_total": 1, "archived_total": 0},
        "items": [{"registry_id": registry_id, "name": "Жилой корпус А",
                   "address": "Москва, улица А, 1", "status": "Строится",
                   "record_kind": "multifamily", "location": [37.6, 55.7],
                   "latest_known_date": "2026-09-06"}],
    }
    monkeypatch.setattr(models, "load_registry", lambda: fake_registry)
    monkeypatch.setattr(api, "load_registry", lambda: fake_registry)
    monkeypatch.setattr(generation, "ACTIVE_PATH", tmp_path / "no-generation.json")
    monkeypatch.setattr(map_point_groups, "MODELS_ROOT", root)
    monkeypatch.setattr(api, "registry_enabled", lambda: True)
    monkeypatch.setattr(models, "asset_manifest", lambda: pointer)
    monkeypatch.setattr(api, "ASSET_ROOT", assets)
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)
    monkeypatch.setattr(point_labels, "OSM_SOURCE", source_path)
    models._load_run.cache_clear()
    models._public_model_filenames.cache_clear()
    point_labels._load_labels.cache_clear()
    yield {"run_dir": run_dir, "public": public, "public_root": public_root,
           "tile_name": tile_name, "glb_name": glb_name, "identifiers": identifiers,
           "registry_id": registry_id, "pointer": pointer, "root": root}
    models._load_run.cache_clear()
    models._public_model_filenames.cache_clear()
    point_labels._load_labels.cache_clear()


def _list(**filters: object) -> dict:
    arguments = {"query": None, "decision": None, "reason_code": None,
                 "source_group": None, "registry_id": None, "limit": 48, "offset": 0}
    arguments.update(filters)
    return _body(api.model_candidates_list(_request("/v1/models/candidates"), **arguments))


def test_summary_filter_pagination_detail_geometry_and_privacy(model_run: dict) -> None:
    summary = _body(api.model_candidates_summary(_request("/v1/models/candidates/summary")))
    _validate("ModelCandidateSummary", summary)
    assert summary["counts"] == {"scanned": 4, "ready": 1, "review": 1,
                                  "rejected": 1, "unavailable": 1}
    assert summary["ready_depictions"] == {"construction_outline": 1}
    assert sum(summary["ready_depictions"].values()) == summary["counts"]["ready"]
    assert sum(item["count"] for item in summary["facets"]["source_groups"]) == 4
    all_items = _list(limit=2)
    _validate("ModelCandidateListResult", all_items)
    assert all_items["dataset_total"] == all_items["records_total"] == 4
    assert len(all_items["items"]) == 2
    assert len(_list(limit=2, offset=2)["items"]) == 2
    assert _list(decision="rejected")["records_total"] == 1
    assert _list(reason_code="height_unknown_flat_only")["records_total"] == 1
    assert _list(source_group="mapped_construction")["records_total"] == 1
    linked = _list(registry_id=model_run["registry_id"])
    assert linked["records_total"] == 1 and linked["items"][0]["name"] == "Жилой корпус А"
    assert _list(registry_id="registry-" + "f" * 24)["records_total"] == 0
    ready_id = model_run["identifiers"][2]
    detail = _body(api.model_candidates_detail(ready_id,
                                               _request(f"/v1/models/candidates/{ready_id}")))
    _validate("ModelCandidateDetailResult", detail)
    assert detail["item"]["model_tileset"]["kind"] == "mapped_construction"
    assert detail["item"]["project_link_status"] == "unlinked"
    geometry = _body(api.model_candidates_geometry(ready_id,
                                                   _request(f"/v1/models/candidates/{ready_id}/geometry")))
    _validate("ModelCandidateGeometryResult", geometry)
    assert geometry["center"] == [37.6, 55.7]
    assert geometry["footprint"] and len(geometry["footprint"]) == 3
    assert geometry["preview_geometry"]["type"] == "Polygon"
    nonready = _body(api.model_candidates_detail(model_run["identifiers"][3],
                                                  _request("/v1/models/candidates/ignored")))
    assert nonready["item"]["model_tileset"] is None
    for payload in (summary, all_items, detail, geometry, nonready):
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "private.example" not in serialized and "report.xlsx" not in serialized
        assert "PRIVATE SOURCE TEXT" not in serialized and "selected_model" not in serialized
        assert "source_record_id" not in serialized
    assert "model_tileset" not in json.dumps(all_items)


def test_invalid_filters_ids_and_no_unchecked_geometry(model_run: dict) -> None:
    for filters in ({"decision": "unknown"}, {"reason_code": "raw_source_code"},
                    {"source_group": "osm_construction"}, {"registry_id": "../../x"}):
        with pytest.raises(HTTPException) as invalid:
            _list(**filters)
        assert invalid.value.status_code == 422
    for identifier in ("../manifest.json", "cand_wrong", "cand_" + "f" * 24):
        with pytest.raises(HTTPException) as missing:
            api.model_candidates_detail(identifier, _request("/v1/models/candidates/x"))
        assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as no_geometry:
        api.model_candidates_geometry(model_run["identifiers"][0],
                                      _request("/v1/models/candidates/x/geometry"))
    assert no_geometry.value.status_code == 404


def test_map_points_preserve_coincident_identities_and_filter_semantics(model_run: dict) -> None:
    request = _request("/v1/map/points")
    response = api.map_points(request, query=None, status=None, kind=None)
    payload = _body(response)
    _validate("MapPointsResult", payload)
    assert payload["dataset_id"] == "map_points"
    assert payload["dataset_total"] == 2
    assert payload["counts"] == {
        "points": 2, "registry_points": 1, "registry_points_total": 1,
        "mapped_construction_points": 1, "coordinate_positions": 1,
        "excluded_mapped_by_registry_filters": 0,
    }
    assert {item["point_type"] for item in payload["items"]} == {
        "registry", "mapped_construction"}
    assert len({item["point_id"] for item in payload["items"]}) == 2
    assert len({tuple(item["location"]) for item in payload["items"]}) == 1
    for item in payload["items"]:
        assert item["point_id"].startswith("mappt_")
        assert (item["registry_id"] is None) != (item["candidate_id"] is None)
    registry_point = next(item for item in payload["items"] if item["point_type"] == "registry")
    mapped_point = next(item for item in payload["items"] if item["point_type"] == "mapped_construction")
    assert registry_point["address"] == "Москва, улица А, 1"
    assert registry_point["address_basis"] == "registry_source"
    assert registry_point["known_catalog_registry_id"] == registry_point["registry_id"]
    assert registry_point["complex_label"] is None
    assert mapped_point["address"] == "улица А, 2"
    assert mapped_point["address_basis"] == "osm_street_house"
    assert mapped_point["known_catalog_registry_id"] is None
    assert mapped_point["complex_label"] is None  # A building name is not a ЖК link.
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("tileset", ".glb", "height", "bbox", "private.example",
                      "source_record_id", "report.xlsx", "attempt_log"):
        assert forbidden not in serialized
    assert payload["registry_rights_status"] == "user_supplied_internal_use"
    assert "OpenStreetMap contributors" in payload["mapped_attribution"]
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.headers["etag"].startswith("W/")
    cached = api.map_points(_request("/v1/map/points", {"If-None-Match": response.headers["etag"]}),
                            query=None, status=None, kind=None)
    assert cached.status_code == 304
    assert _body(api.map_points(request, query="Объект", status=None, kind=None))["counts"]["points"] == 1
    assert _body(api.map_points(request, query="Жилой", status=None, kind=None))["counts"]["points"] == 1
    assert _body(api.map_points(request, query="улица А, 2", status=None, kind=None))["items"][0][
        "candidate_id"] == model_run["identifiers"][2]
    filtered = _body(api.map_points(request, query=None, status="Строится", kind=None))
    assert filtered["counts"]["registry_points"] == 1
    assert filtered["counts"]["mapped_construction_points"] == 0
    assert filtered["counts"]["excluded_mapped_by_registry_filters"] == 1
    assert filtered["applied_filters"] == {"status": "Строится"}
    assert filtered["items"][0]["point_id"] == next(
        item["point_id"] for item in payload["items"] if item["point_type"] == "registry")
    kind_filtered = _body(api.map_points(request, query=None, status=None, kind="low_rise"))
    assert kind_filtered["counts"]["points"] == 0
    assert kind_filtered["counts"]["excluded_mapped_by_registry_filters"] == 1
    assert _body(api.map_points(request, query="Объект", status="Строится", kind=None))["counts"][
        "excluded_mapped_by_registry_filters"] == 1
    with pytest.raises(HTTPException) as invalid:
        api.map_points(request, query=None, status=None, kind="school")
    assert invalid.value.status_code == 422


def test_map_query_matches_displayed_generic_name(model_run: dict) -> None:
    run = models.load_model_run()
    mapped = next(item for item in run["items"] if item["entity_type"] == "mapped_construction")
    mapped["name"] = "Кандидат модели " + mapped["candidate_id"][-6:]
    request = _request("/v1/map/points")
    payload = _body(api.map_points(request, query="Строящийся объект на карте",
                                   status=None, kind=None))
    assert payload["counts"]["points"] == 1
    assert payload["items"][0]["point_type"] == "mapped_construction"
    assert payload["items"][0]["name"] == "Строящийся объект на карте"


def test_point_labels_require_pinned_source_hash_and_exact_typed_identity(model_run: dict) -> None:
    run = models.load_model_run()
    labels = point_labels.load_point_labels(run)
    mapped_id = model_run["identifiers"][2]
    assert labels[mapped_id]["address"] == "улица А, 2"
    assert labels[mapped_id]["complex_label"] is None
    assert point_labels._complex_label("way/123/v1", "Корпус 5", {}) == (None, None)
    assert point_labels._complex_label("relation/1/v1", "ЖК Ahead", {}) == (
        "ЖК Ahead", "osm_name")
    assert point_labels._complex_label("relation/1/v1", None, {
        "addr:place": "Жилой комплекс Шифт"}) == (
        "Жилой комплекс Шифт", "osm_addr_place")
    assert point_labels._complex_label("way/1450746348/v3", "Корпус 21", {}) == (
        'ЖК "Зиларт-Спарк" (дом 21)', "osm_parent_relation")
    assert point_labels._complex_label("way/1450746348/v4", "Корпус 21", {}) == (None, None)
    assert point_labels._complex_label("way/1450746348/v3", "Корпус 21", {},
                                       parent_audit_matches=False) == (None, None)

    source = point_labels.OSM_SOURCE
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(HTTPException) as corrupt:
        api.map_points(_request("/v1/map/points"), query=None, status=None, kind=None)
    assert corrupt.value.status_code == 503
    assert "way/123/v1" not in str(corrupt.value.detail)


def test_point_label_address_precision_and_untrusted_text() -> None:
    assert point_labels._address({"addr:street": "улица А",
                                  "addr:housenumber": "23/5"}) == (
        "улица А, 23/5", "osm_street_house")
    assert point_labels._address({"addr:place": "ЖК Шагал",
                                  "addr:housenumber": "к20"}) == (
        "ЖК Шагал, к20", "osm_place_house")
    assert point_labels._address({"addr:street": "улица А"}) == (
        "улица А", "osm_street")
    assert point_labels._address({"addr:housenumber": "20"}) == (None, None)
    assert point_labels._address({"addr:street": "https://private.example/x",
                                  "addr:housenumber": "20"}) == (None, None)


def test_nearby_routes_resolve_current_point_ids_and_bound_viewport(
        model_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_worker(payload: dict) -> dict:
        calls.append(payload)
        return {"status": "ok", "dataset_id": "nearby-test-v1", "layers": {},
                "point_id": payload.get("point_id")}

    monkeypatch.setattr(nearby, "_worker_request", fake_worker)
    points = _body(api.map_points(_request("/v1/map/points"), query=None, status=None,
                                  kind=None))["items"]
    registry_point = next(item for item in points if item["point_type"] == "registry")
    mapped_point = next(item for item in points if item["point_type"] == "mapped_construction")
    response = api.map_nearby(_request("/v1/map/nearby"), mapped_point["point_id"], "school")
    payload = _body(response)
    assert payload["registry_snapshot_id"] == "building-eligibility-synthetic"
    assert payload["model_run_id"] == model_run["run_dir"].name
    assert payload["point_id"] == mapped_point["point_id"]
    assert calls[-1] == {"kind": "point", "lon": 37.6, "lat": 55.7,
                          "point_id": mapped_point["point_id"], "education": "school"}
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert api.map_nearby(_request("/v1/map/nearby", {"If-None-Match": response.headers["etag"]}),
                          mapped_point["point_id"], "school").status_code == 304
    api.map_nearby(_request("/v1/map/nearby"), registry_point["point_id"], "all")
    assert calls[-1]["point_id"] == registry_point["point_id"]
    with pytest.raises(HTTPException) as missing:
        api.map_nearby(_request("/v1/map/nearby"), "mappt_" + "f" * 24, "all")
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as malformed:
        api.map_nearby(_request("/v1/map/nearby"), "not-a-point", "all")
    assert malformed.value.status_code == 422

    viewport = api.map_nearby_viewport(_request("/v1/map/nearby/viewport"),
                                       "37.5,55.6,37.65,55.75", "metro,education", "all", 150)
    assert _body(viewport)["dataset_id"] == "nearby-test-v1"
    assert calls[-1]["kind"] == "viewport" and calls[-1]["limit"] == 150
    for invalid_bbox in ("37.5,55.6,37.66,55.7", "37.5,55.6,37.7", "NaN,55.6,37.6,55.7"):
        with pytest.raises(HTTPException) as invalid:
            api.map_nearby_viewport(_request("/v1/map/nearby/viewport"), invalid_bbox,
                                    "metro", "all", 100)
        assert invalid.value.status_code == 422
    with pytest.raises(HTTPException) as invalid_layers:
        api.map_nearby_viewport(_request("/v1/map/nearby/viewport"),
                                "37.5,55.6,37.6,55.7", "metro,metro", "all", 100)
    assert invalid_layers.value.status_code == 422


def test_nearby_asgi_private_validator_200_and_304(
        model_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    point = next(item for item in _body(api.map_points(_request("/v1/map/points"),
                                                  query=None, status=None, kind=None))["items"]
                 if item["point_type"] == "mapped_construction")
    monkeypatch.setattr(nearby, "_worker_request", lambda request: {
        "status": "ok", "dataset_id": "nearby-test-v1", "layers": {},
        "point_id": request.get("point_id"),
    })

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)

    async def invoke(path: str, query: dict[str, str], etag: str | None = None) -> tuple[int, dict, bytes]:
        headers = [(b"if-none-match", etag.encode())] if etag else []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": urlencode(query).encode(),
                 "server": ("localhost", 8000), "client": ("127.0.0.1", 12345),
                 "headers": headers}
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await asyncio.wait_for(api.app(scope, receive, send), timeout=5)
        started = next(message for message in messages if message["type"] == "http.response.start")
        return (started["status"],
                {key.decode(): value.decode() for key, value in started["headers"]},
                b"".join(message.get("body", b"") for message in messages
                         if message["type"] == "http.response.body"))

    async def check() -> None:
        for path, query in (("/v1/map/nearby", {"point_id": point["point_id"]}),
                            ("/v1/map/nearby/viewport", {
                                "bbox": "37.5,55.6,37.6,55.7", "layers": "metro"})):
            status, headers, body = await invoke(path, query)
            assert status == 200 and body
            assert headers["cache-control"] == "private, max-age=0, must-revalidate"
            assert headers["etag"].startswith('W/"')
            status, cached_headers, cached_body = await invoke(path, query, headers["etag"])
            assert status == 304 and not cached_body
            assert cached_headers["cache-control"] == "private, max-age=0, must-revalidate"
            assert cached_headers["etag"] == headers["etag"]
        status, missing_headers, _ = await invoke(
            "/v1/map/nearby", {"point_id": "mappt_" + "f" * 24})
        assert status == 404 and missing_headers["cache-control"] == "no-store"
        assert "etag" not in missing_headers
        monkeypatch.setattr(api, "EXPOSE_SOURCES", True)
        status, exposed_headers, _ = await invoke(
            "/v1/map/nearby", {"point_id": point["point_id"]})
        assert status == 200 and exposed_headers["cache-control"] == "no-store"
        assert "etag" not in exposed_headers

    asyncio.run(check())


def test_bounded_preview_geometry_preserves_holes_and_multipolygons() -> None:
    bbox = [37.5, 55.5, 37.8, 55.8]
    outer = [[37.6, 55.6], [37.7, 55.6], [37.7, 55.7], [37.6, 55.7], [37.6, 55.6]]
    hole = [[37.63, 55.63], [37.67, 55.63], [37.67, 55.67],
            [37.63, 55.67], [37.63, 55.63]]
    second = [[37.71, 55.71], [37.72, 55.71], [37.72, 55.72], [37.71, 55.71]]
    preview = {"type": "MultiPolygon", "coordinates": [[outer, hole], [second]]}
    assert models._preview_geometry(preview, bbox) == preview
    with pytest.raises(ValueError, match="not closed"):
        models._preview_geometry({"type": "Polygon", "coordinates": [[*outer[:-1],
                                                                           [37.61, 55.61]]]}, bbox)
    large_ring = [[37.6 + 0.00001 * i, 55.6] for i in range(512)] + [[37.6, 55.6]]
    with pytest.raises(ValueError, match="coordinate limit"):
        models._preview_geometry({"type": "Polygon", "coordinates": [large_ring]}, bbox)


def test_etag_and_corrupted_manifest_fail_closed(model_run: dict) -> None:
    request = _request("/v1/models/candidates/summary")
    first = api.model_candidates_summary(request)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "public, max-age=0, must-revalidate"
    etag = first.headers["etag"]
    cached = api.model_candidates_summary(_request("/v1/models/candidates/summary",
                                                   {"If-None-Match": etag}))
    assert cached.status_code == 304 and cached.body == b""
    assert cached.headers["etag"] == etag
    manifest_path = model_run["run_dir"] / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    with pytest.raises(HTTPException) as unavailable:
        api.model_candidates_summary(request)
    assert unavailable.value.status_code == 503


def test_model_cache_middleware_keeps_only_valid_public_validators(
        monkeypatch: pytest.MonkeyPatch, model_run: dict) -> None:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def probe(status: int, headers: list[tuple[bytes, bytes]],
                    path: str = "/v1/models/candidates") -> dict[bytes, bytes]:
        async def wrapped(scope: dict, incoming: object, send: object) -> None:
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": b"{}"})

        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        await api.PrivateApiCacheMiddleware(wrapped)(
            {"type": "http", "path": path}, receive, send)
        return dict(messages[0]["headers"])

    valid = [(b"cache-control", b"public, max-age=0, must-revalidate"),
             (b"etag", b'"abc"')]
    assert asyncio.run(probe(200, valid))[b"etag"] == b'"abc"'
    failed = asyncio.run(probe(503, valid))
    assert failed == {b"cache-control": b"no-store"}
    missing_validator = asyncio.run(probe(200, []))
    assert missing_validator == {b"cache-control": b"no-store"}
    private = [(b"cache-control", b"private, max-age=0, must-revalidate"),
               (b"etag", b'W/"abc"')]
    assert asyncio.run(probe(200, private, "/v1/map/points")) == dict(private)
    assert asyncio.run(probe(422, private, "/v1/map/points")) == {
        b"cache-control": b"no-store"}
    monkeypatch.setattr(api, "EXPOSE_SOURCES", True)
    exposed = api.model_candidates_summary(_request("/v1/models/candidates/summary"))
    assert exposed.headers["cache-control"] == "no-store" and "etag" not in exposed.headers


def test_model_static_boundary_immutable_etag_range_and_compat(monkeypatch: pytest.MonkeyPatch,
                                                                model_run: dict) -> None:
    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    root = model_run["public_root"]
    files = api.RenderAssetFiles(directory=api.ASSET_ROOT)

    def scope(name: str, method: str = "GET", headers: dict[str, str] | None = None) -> dict:
        return {"type": "http", "method": method, "path": f"/assets/{root}/{name}",
                "root_path": "", "query_string": b"", "headers": [
                    (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]}

    async def check() -> None:
        tile = model_run["tile_name"]
        glb = model_run["glb_name"]
        for name in (tile, glb):
            response = await files.get_response(f"{root}/{name}", scope(name, "HEAD"))
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers["etag"] and response.headers["last-modified"]
            cached = await files.get_response(f"{root}/{name}",
                                              scope(name, headers={"If-None-Match": response.headers["etag"]}))
            assert cached.status_code == 304
            partial_scope = scope(name, headers={"Range": "bytes=0-3"})
            partial = await files.get_response(f"{root}/{name}", partial_scope)
            messages: list[dict] = []

            async def send(message: dict) -> None:
                messages.append(message)

            async def receive() -> dict:
                return {"type": "http.request", "body": b"", "more_body": False}

            await partial(partial_scope, receive, send)
            assert messages[0]["status"] == 206
            headers = dict(messages[0]["headers"])
            assert headers[b"content-range"].startswith(b"bytes 0-3/")
        for name in ("public_manifest.json", "manifest.json", "active.json",
                     "model_bad.tileset.json", "model_" + "f" * 64 + ".glb",
                     "../private/model_" + "f" * 64 + ".glb"):
            with pytest.raises(HTTPException) as denied:
                await files.get_response(f"{root}/{name}", scope(name, "HEAD"))
            assert denied.value.status_code == 404
        model_run["pointer"]["project_models_root"] = "public-project-models-v1-" + "f" * 16
        with pytest.raises(HTTPException) as unpublished:
            await files.get_response(f"{root}/{tile}", scope(tile, "HEAD"))
        assert unpublished.value.status_code == 404
        model_run["pointer"]["previous_public_roots"] = [root]
        assert (await files.get_response(f"{root}/{tile}", scope(tile, "HEAD"))).status_code == 200

    asyncio.run(asyncio.wait_for(check(), timeout=10))


def test_staged_supplied_run_schema_and_static_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate the pinned 2317-row offline artifact before its active pointer flips."""
    run_dir = ROOT / "data/models/models-v1-92dc659b5e75fa52"
    if not run_dir.is_dir():
        pytest.skip("The versioned model candidate run is not staged")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pointer = {"project_models_root": manifest["public_root"],
               "previous_public_roots": []}
    monkeypatch.setattr(models, "asset_manifest", lambda: pointer)
    monkeypatch.setattr(api, "asset_manifest", lambda: pointer)
    stat = manifest_path.stat()
    source_registry = registry.load_registry()
    names = tuple((row["registry_id"], row["name"]) for row in source_registry["items"])
    run = models._load_run(run_dir.name, _sha(manifest_path.read_bytes()),
                           manifest["registry_snapshot_id"], names,
                           manifest["public_root"], stat.st_mtime_ns, stat.st_size)
    assert run["counts"] == {"scanned": 2317, "ready": 756, "review": 60,
                             "rejected": 29, "unavailable": 1472}
    assert len(run["geometries"]) == 756
    assert all(geometry["preview_geometry"] is not None
               for geometry in run["geometries"].values())
    assert sum(geometry["preview_geometry"]["type"] == "MultiPolygon"
               for geometry in run["geometries"].values()) == 18
    actual_depictions = {kind: sum(item["depiction"] == kind and item["decision"] == "ready"
                                     for item in run["items"])
                          for kind in ("construction_outline", "schematic_massing")}
    assert actual_depictions == {"construction_outline": 483, "schematic_massing": 273}
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/ModelCandidateItem"},
                                     format_checker=FormatChecker())
    for item in run["items"]:
        validator.validate(item)
        assert item["registry_id"] is None or item["entity_type"] == "registry_project"
    serialized = json.dumps(run["items"], ensure_ascii=False)
    for forbidden in ("source_record_id", "provider_url", "selected_model",
                      "source_url", ".xlsx", "data/property", "https://"):
        assert forbidden not in serialized

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)
    ready_id = next(item["candidate_id"] for item in run["items"] if item["ready_to_render"])
    tile_url = run["geometries"][ready_id]["model_tileset"]["url"]
    public_root, tile_name = tile_url.removeprefix("/assets/").split("/", 1)
    tile_doc = json.loads((api.ASSET_ROOT / public_root / tile_name).read_text(encoding="utf-8"))
    glb_name = tile_doc["root"]["content"]["uri"]

    async def check() -> None:
        files = api.RenderAssetFiles(directory=api.ASSET_ROOT)
        for name in ("tileset.json", "public_manifest.json", "manifest.json"):
            with pytest.raises(HTTPException) as blocked:
                await files.get_response(f"{public_root}/{name}", {
                    "type": "http", "method": "HEAD", "headers": []})
            assert blocked.value.status_code == 404
        for name in (tile_name, glb_name):
            scope = {"type": "http", "method": "HEAD", "headers": [],
                     "path": f"/assets/{public_root}/{name}"}
            response = await files.get_response(f"{public_root}/{name}", scope)
            assert response.status_code == 200
            assert response.headers["cache-control"].endswith("immutable")
            assert response.headers.get("etag")
            conditional = {**scope, "method": "GET", "headers": [
                (b"if-none-match", response.headers["etag"].encode())]}
            cached = await files.get_response(f"{public_root}/{name}", conditional)
            assert cached.status_code == 304

    asyncio.run(asyncio.wait_for(check(), timeout=20))
