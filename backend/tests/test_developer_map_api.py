"""Publisher-building map publication and shared API projection gates."""

from __future__ import annotations

import hashlib
import json
import asyncio
import shutil
from pathlib import Path

import anyio
import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from starlette.requests import Request

from backend import app as api
from backend import developer_experience, developer_map


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha(data)


def _jsonl(rows: list[dict]) -> bytes:
    return b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
                    for row in rows)


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": [(key.lower().encode(), value.encode())
                                for key, value in (headers or {}).items()]})


@pytest.fixture
def snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    base = tmp_path / "data/property/developer_map"
    monkeypatch.setattr(developer_map, "ROOT", tmp_path)
    monkeypatch.setattr(developer_map, "BASE", base)
    monkeypatch.setattr(developer_map, "ACTIVE_PATH", base / "active.json")
    # This fixture exercises map-only generation; isolate it from the live menu bundle.
    monkeypatch.setattr(developer_experience, "ACTIVE_PATH", tmp_path / "no-experience.json")
    monkeypatch.setattr(api.developer_map, "current", developer_map.resolve_active)
    monkeypatch.setattr(api, "data_mode", lambda: "real")
    developer_map._validated.cache_clear()
    run_id = "developer-map-v1-" + "a" * 16
    rows = [
        {"schema_version": "developer-map-building/1", "registry_id": "registry-" + f"{index:024x}",
         "point_id": "mappt_" + f"{index:024x}", "point_type": "developer_building",
         "name": f"Корпус {index}", "complex_label": "ЖК Проверка",
         "project_name": "ЖК Проверка", "developer_name": "Девелопер",
         "address": f"Москва, улица Пример, {index}", "address_complete": True,
         "address_basis": "publisher_building_address",
         "location": [37.6 + index / 1000, 55.7], "location_quality": "verified",
         "location_basis": "publisher_building_polygon", "map_eligible": True,
         "unit_count": index + 1, "observed_at": "2026-10-10T12:00:00Z",
         "source_group": "test_publisher", "publisher_project_id": "project-1",
         "publisher_building_id": f"building-{index}", "identity_basis": "publisher_building_id",
         "source_corpus_label": f"Корпус {index}", "source_evidence_sha256": "1" * 64}
        for index in (1, 2)
    ]
    rows.append({**rows[0], "registry_id": "registry-" + "3" * 24,
                 "point_id": "mappt_" + "3" * 24, "name": "Корпус без точки",
                 "location": None, "location_quality": None, "location_basis": None,
                 "map_eligible": False, "publisher_building_id": None,
                 "identity_basis": "source_project_corpus_label",
                 "source_corpus_label": "Корпус 3"})
    groups = [{"schema_version": "developer-map-group/1", "group_id": "group_" + "4" * 24,
               "group_label": "ЖК Проверка", "proof_basis": "same_publisher_project_id",
               "member_registry_ids": [row["registry_id"] for row in rows],
               "member_point_ids": [row["point_id"] for row in rows[:2]],
               "source_group": "test_publisher", "publisher_project_id": "project-1",
               "evidence_sha256": "2" * 64}]
    directory = base / "runs" / run_id
    manifest = {"schema_version": "developer-map-snapshot/1", "run_id": run_id,
                "snapshot_id": "developer-map-snapshot-" + "b" * 16,
                "buildings_total": 3, "map_points_total": 2}
    for name, values in (("points", rows), ("groups", groups), ("rejections", [])):
        manifest[f"{name}_sha256"] = _write(directory / f"{name}.jsonl", _jsonl(values))
        manifest[f"{name}_total"] = len(values)
    manifest_sha = _write(directory / "manifest.json", json.dumps(manifest).encode())
    _write(base / "active.json", json.dumps({"schema_version": "developer-map-pointer/1",
                                            "run_id": run_id,
                                            "manifest_sha256": manifest_sha}).encode())
    try:
        yield {"rows": rows, "manifest": manifest, "base": base}
    finally:
        developer_map._validated.cache_clear()


def test_active_snapshot_validates_and_projects_without_private_ids(snapshot: dict) -> None:
    loaded = developer_map.resolve_active()
    assert loaded is not None
    assert len(loaded["rows"]) == 3
    assert len(loaded["by_point"]) == 2
    public = developer_map.public_building(snapshot["rows"][0])
    assert public["developer_name"] == "Девелопер"
    assert public["price"] is None and public["offer_count"] == 0
    assert not ({"publisher_project_id", "publisher_building_id", "source_group",
                 "source_evidence_sha256"} & public.keys())
    assert len(developer_map.public_groups(loaded, set(loaded["by_point"]))) == 1
    assert developer_map.public_groups(loaded, {snapshot["rows"][0]["point_id"]}) == []


def test_bad_pointer_and_mutated_files_fail_closed(snapshot: dict) -> None:
    active = snapshot["base"] / "active.json"
    original = active.read_bytes()
    active.write_text('{"schema_version":"developer-map-pointer/1","run_id":"bad"}')
    with pytest.raises(ValueError):
        developer_map.resolve_active()
    active.write_bytes(original)
    run = snapshot["manifest"]["run_id"]
    points = snapshot["base"] / "runs" / run / "points.jsonl"
    points.write_bytes(points.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        developer_map.resolve_active()


def test_symlink_and_project_marker_are_not_building_points(snapshot: dict,
                                                             tmp_path: Path) -> None:
    active = snapshot["base"] / "active.json"
    target = tmp_path / "pointer.json"
    target.write_bytes(active.read_bytes())
    active.unlink()
    active.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        developer_map.resolve_active()
    row = {**snapshot["rows"][0], "location_basis": "project_center"}
    with pytest.raises(ValueError, match="Project marker"):
        developer_map._point(row)


def test_list_map_points_share_snapshot_and_never_mix_legacy(snapshot: dict,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "background_tilesets", lambda _: [])
    monkeypatch.setattr(api, "load_catalogue", lambda: {"data_mode": "real", "snapshot_id": "real-empty-v1",
                                                        "snapshot_at": "2026-10-10T12:00:00Z",
                                                        "districts": [], "complexes": [], "offers": []})
    monkeypatch.setattr(api, "asset_manifest", lambda: {"city_buildings": 0})
    listing = api.registry_buildings(query=None, status=None, kind=None, limit=48, offset=0)
    mapped = api.registry_map(query=None, status=None, kind=None)
    response = api.map_points(_request("/v1/map/points"), query=None, status=None, kind=None)
    points = json.loads(response.body)
    assert listing["dataset_total"] == listing["records_total"] == 3
    assert len(mapped["items"]) == len(points["items"]) == 2
    assert listing["snapshot_id"] == mapped["snapshot_id"] == points["registry_snapshot_id"]
    assert points["counts"]["mapped_construction_points"] == 0
    assert points["counts"]["unlocated_buildings"] == 1
    assert all(item["point_type"] == "developer_building" for item in points["items"])
    assert len(points["group_evidence"]["groups"]) == 1
    assert points["group_evidence"]["map_run_id"] == listing["map_run_id"]
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/schemas.json").read_text())
    for name, payload in (("DeveloperRegistryListResult", listing),
                          ("DeveloperRegistryMapResult", mapped),
                          ("DeveloperMapPointsResult", points)):
        Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"},
                             format_checker=FormatChecker()).validate(payload)
    again = api.map_points(_request("/v1/map/points", {"If-None-Match": response.headers["etag"]}),
                           query=None, status=None, kind=None)
    assert again.status_code == 304
    filtered = json.loads(api.map_points(_request("/v1/map/points"), query="Корпус 1",
                                         status=None, kind=None).body)
    assert filtered["counts"]["points"] == 1 and filtered["group_evidence"]["groups"] == []
    with pytest.raises(HTTPException) as exc:
        api.registry_buildings(query=None, status="Строится", kind=None, limit=48, offset=0)
    assert exc.value.status_code == 422


def test_health_and_coverage_report_real_buildings_without_demo(snapshot: dict,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    catalogue = {"data_mode": "real", "snapshot_id": "real-empty-v1",
                 "snapshot_at": "2026-10-10T12:00:00Z", "districts": [],
                 "complexes": [], "offers": []}
    monkeypatch.setattr(api, "load_catalogue", lambda: catalogue)
    monkeypatch.setattr(api, "counts", lambda _: {"districts": 0, "complexes": 0,
                                                   "offers": 0, "city_buildings": 0,
                                                   "facade_models": 0})
    health = api.health()
    assert health["data_mode"] == "real"
    assert health["catalog_view"] == "developer_buildings"
    assert health["status"] == "developer_buildings_ready"
    assert health["counts"]["offers"] == health["counts"]["complexes"] == 0
    assert health["registry_snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    from backend.coverage import _developer_dataset
    coverage = _developer_dataset(developer_map.resolve_active())
    assert coverage["dataset_id"] == "developer_building_registry"
    assert coverage["records_total"] == 3
    assert next(field for field in coverage["fields"] if field["key"] == "building.location")[
        "populated_count"] == 2


def test_asgi_pointer_failure_is_503_without_legacy_fallback(snapshot: dict,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "resolve_generation", lambda: None)
    monkeypatch.setattr(api, "data_mode", lambda: "real")
    monkeypatch.setattr(api, "load_catalogue", lambda: {
        "data_mode": "real", "snapshot_id": "real-empty-v1",
        "snapshot_at": "2026-10-10T12:00:00Z", "districts": [],
        "complexes": [], "offers": []})

    async def inline_run_sync(func: object, *args: object, **kwargs: object) -> object:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_run_sync)

    async def invoke() -> int:
        path = "/v1/map/points"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "server": ("localhost", 8000), "client": ("127.0.0.1", 12345),
                 "headers": []}
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await asyncio.wait_for(api.app(scope, receive, send), timeout=5)
        return next(message for message in messages
                    if message["type"] == "http.response.start")["status"]

    assert asyncio.run(invoke()) == 200
    pointer = snapshot["base"] / "active.json"
    value = json.loads(pointer.read_text())
    value["manifest_sha256"] = "0" * 64
    pointer.write_text(json.dumps(value))
    assert asyncio.run(invoke()) == 503


def test_new_pointer_rotates_snapshot_and_point_etag(snapshot: dict) -> None:
    request = _request("/v1/map/points")
    first = api.map_points(request, query=None, status=None, kind=None)
    old_run = snapshot["manifest"]["run_id"]
    new_run = "developer-map-v1-" + "c" * 16
    source_dir = snapshot["base"] / "runs" / old_run
    new_dir = snapshot["base"] / "runs" / new_run
    shutil.copytree(source_dir, new_dir)
    new_manifest = {**snapshot["manifest"], "run_id": new_run,
                    "snapshot_id": "developer-map-snapshot-" + "d" * 16}
    manifest_sha = _write(new_dir / "manifest.json", json.dumps(new_manifest).encode())
    _write(snapshot["base"] / "active.json", json.dumps({
        "schema_version": "developer-map-pointer/1", "run_id": new_run,
        "manifest_sha256": manifest_sha}).encode())
    second = api.map_points(_request("/v1/map/points", {"If-None-Match": first.headers["etag"]}),
                            query=None, status=None, kind=None)
    assert second.status_code == 200
    assert second.headers["etag"] != first.headers["etag"]
    assert json.loads(second.body)["registry_snapshot_id"] == new_manifest["snapshot_id"]
