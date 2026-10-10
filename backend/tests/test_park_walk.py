"""Park walking routes require a graph path to a checked boundary approach."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from shapely.geometry import Polygon, mapping

from backend import app as api
from backend.origin_access import Barrier
from backend.park_walk import Park, ParkIndex


REGISTRY_ID = "registry-" + "a" * 24
POINT_ID = "mappt_" + "b" * 24


def _index() -> ParkIndex:
    park = Park("osm:way:123", "Парк", Polygon([
        (37.0, 55.0), (37.001, 55.0), (37.001, 55.001), (37.0, 55.001)
    ]))
    return ParkIndex([park], snapshot_at="2026-10-08T20:21:06Z", source_sha256="a" * 64)


def _route(origin: tuple[float, float], target: dict) -> dict:
    boundary = target["coordinate"]
    return {"status": "routed", "geometry": [list(origin), list(boundary)],
            "distance_m": 420, "estimated_minutes": 6,
            "approximate": True}


def test_park_walk_uses_graph_to_boundary_without_claiming_entrance() -> None:
    result = _index().walk((36.9995, 55.0005), _route, lambda _: [], dataset_id="graph-v1")
    assert result["status"] == "routed"
    assert result["distance_m"] == 420 and result["estimated_minutes"] == 6
    assert result["endpoint_basis"] == "pedestrian_graph_near_park_boundary"
    assert result["access_verified"] is False
    assert result["park"]["id"].startswith("park_")
    assert "straight_line_m" not in result and "osm:way:123" not in json.dumps(result)


def test_park_walk_does_not_convert_snap_or_blocked_gap_to_walk() -> None:
    index = _index()
    origin = (36.9995, 55.0005)

    def distant(_: tuple[float, float], target: dict) -> dict:
        boundary = target["coordinate"]
        return {"status": "routed", "geometry": [list(origin),
                [boundary[0] - 0.0004, boundary[1]]],
                "distance_m": 10, "estimated_minutes": 1}

    assert index.walk(origin, distant, lambda _: [], dataset_id="graph-v1")["status"] == "unavailable"

    def short_gap(_: tuple[float, float], target: dict) -> dict:
        boundary = target["coordinate"]
        return {"status": "routed", "geometry": [list(origin),
                [boundary[0] - 0.0001, boundary[1]]],
                "distance_m": 10, "estimated_minutes": 1}

    # Missing pinned barrier coverage cannot certify even a short connector.
    assert index.walk(origin, short_gap, None, dataset_id="graph-v1")["status"] == "unavailable"
    def water_crossing(boundary: tuple[float, float]) -> list[Barrier]:
        midpoint = boundary[0] - 0.00005
        return [Barrier("osm:water:1", "water", mapping(Polygon([
            (midpoint - 0.00001, boundary[1] - 0.00001),
            (midpoint + 0.00001, boundary[1] - 0.00001),
            (midpoint + 0.00001, boundary[1] + 0.00001),
            (midpoint - 0.00001, boundary[1] + 0.00001)
        ])))]

    blocked = index.walk(origin, short_gap, water_crossing, dataset_id="graph-v1")
    assert blocked["status"] == "unavailable"
    assert blocked["distance_m"] is None and blocked["estimated_minutes"] is None


def test_park_api_is_bound_to_selected_building_and_public_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = {"snapshot_id": "developer-map-snapshot-0123456789abcdef",
                "run_id": "developer-map-v1-0123456789abcdef"}
    map_run = {"manifest": manifest, "by_registry": {REGISTRY_ID: {
        "registry_id": REGISTRY_ID, "point_id": POINT_ID, "map_eligible": True,
        "location": [36.9995, 55.0005]}}}
    menu = {"manifest": {"run_id": "developer-units-v1-0123456789abcdef"},
            "buildings": {REGISTRY_ID: {}}}
    monkeypatch.setattr(api, "active_unit_menu", lambda: (map_run, menu))
    monkeypatch.setattr(api, "park_walk_for_point", lambda *args, **kwargs:
                        _index().walk((36.9995, 55.0005), _route,
                                      lambda _: [], dataset_id="graph-v1"))
    result = api.developer_building_park_walk(REGISTRY_ID)
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/schemas.json")
                        .read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperParkWalk"}).validate(result)
    assert result["registry_id"] == REGISTRY_ID
    assert result["unit_run_id"] == menu["manifest"]["run_id"]
    assert "source_id" not in json.dumps(result)
