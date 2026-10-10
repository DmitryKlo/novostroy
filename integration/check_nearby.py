"""Offline contract and policy checks for the local nearby-amenity routes.

Run from the repository root:
``rtk proxy .runtime/dev-env/bin/python integration/check_nearby.py``.
This uses direct route functions and the local geodata worker, not a socket or
browser. Timings are process-local and do not describe network or device FPS.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api  # noqa: E402
from backend.nearby import NearbyIndex, _distance_m  # noqa: E402
from integration.check_live import assert_public, validate  # noqa: E402


def request(path: str, *, etag: str | None = None) -> Request:
    headers = [] if etag is None else [(b"if-none-match", etag.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": b"", "headers": headers})


def body(response: Response, path: str) -> dict[str, Any]:
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "private, max-age=0, must-revalidate"
    assert response.headers.get("etag", "").startswith('W/"')
    payload = json.loads(response.body)
    validate(path, "get", payload)
    assert_public(payload)
    return payload


def check_air_ranker() -> None:
    """A skipped grid ring or early-stop bug must differ from brute force."""
    origin = (37.6, 55.7)
    rows = []
    for index, (dx, dy) in enumerate((
        (0.00, 0.00), (0.009, 0.007), (0.017, 0.001), (0.024, 0.018),
        (0.031, 0.002), (0.045, 0.006), (-0.041, 0.027), (0.065, -0.006),
        (0.079, 0.016), (-0.091, 0.016), (0.125, 0.004), (-0.132, 0.042),
    )):
        rows.append({"id": f"poi_{index:024x}", "category": "school",
                     "coordinate": (origin[0] + dx, origin[1] + dy)})
    index = NearbyIndex(rows, dataset_id="fixture", osm_snapshot_at="2026-10-08T00:00:00Z")
    for count in (1, 3, 5, 10):
        actual = [row["id"] for row in index._nearest(origin, {"school"}, count)]
        expected = [row["id"] for row in sorted(
            rows, key=lambda row: (_distance_m(origin, row["coordinate"]), row["id"]))[:count]]
        assert actual == expected, (count, actual, expected)


def _encode_polyline6(points: list[tuple[float, float]]) -> str:
    encoded = []
    previous_lat = previous_lon = 0
    for lon, lat in points:
        current_lat, current_lon = round(lat * 1_000_000), round(lon * 1_000_000)
        for difference in (current_lat - previous_lat, current_lon - previous_lon):
            value = difference << 1 if difference >= 0 else ~(difference << 1)
            while value >= 32:
                encoded.append(chr((32 | (value & 31)) + 63))
                value >>= 5
            encoded.append(chr(value + 63))
        previous_lat, previous_lon = current_lat, current_lon
    return "".join(encoded)


def check_snap_gate() -> None:
    """Valhalla may echo input locations while actual shape snaps far away."""
    origin, target = (37.6, 55.7), (37.61, 55.71)
    far_shape = _encode_polyline6([(37.605, 55.705), target])

    class FakeActor:
        def route(self, _request: dict[str, Any]) -> dict[str, Any]:
            return {"status": 0, "trip": {
                "locations": [{"lon": origin[0], "lat": origin[1]},
                              {"lon": target[0], "lat": target[1]}],
                "legs": [{"shape": far_shape, "summary": {"length": 1.0, "time": 600}}]}}

    index = NearbyIndex([], dataset_id="fixture", osm_snapshot_at="2026-10-08T00:00:00Z",
                        actor_factory=lambda _config: FakeActor())
    index.graph_valid = True
    result = index._route(origin, {"coordinate": target,
                                   "coordinate_basis": "source_point",
                                   "access_kind": "unverified"})
    assert result["status"] == "snap_exceeds_threshold"
    assert result["distance_m"] is None and result["estimated_minutes"] is None
    assert result["geometry"] is None and max(result["snap_distance_m"]) > 50


def check_point_policy(payload: dict[str, Any], origin: dict[str, Any]) -> None:
    assert payload["point_id"] == origin["point_id"]
    assert payload["origin"]["coordinate"] == dict(zip(("lon", "lat"), origin["location"], strict=True))
    assert payload["registry_snapshot_id"] and payload["model_run_id"]
    assert payload["coverage"]["ranking_basis"] == "walking_distance_when_routed"
    assert payload["coverage"]["candidate_limit_per_subcategory"] == 8
    assert payload["coverage"]["verified_oms_candidate_limit"] == 6
    assert set(payload["layers"]) == {"metro", "education", "medical"}
    allowed = {"metro": {"metro_station"}, "education": {"school", "kindergarten"},
               "medical": {"clinic"}}
    verified_ids = set()
    sidecar = json.loads((ROOT / "data/social/oms_verified_clinics_2026.json").read_text(encoding="utf-8"))
    for record in sidecar["records"]:
        if record["effective_year"] == datetime.now(timezone.utc).year:
            verified_ids.add("poi_" + hashlib.sha256(record["osm_source_id"].encode()).hexdigest()[:24])
    if verified_ids:
        assert payload["free_clinic_guarantee"]["status"] == "met"
    for key, layer in payload["layers"].items():
        items = layer["items"]
        assert len(items) <= (4 if key == "education" and payload["education"] == "all" else 2)
        assert layer["candidate_count"] >= len(items)
        if key == "education":
            assert sum(item["category"] == "school" for item in items) <= 2
            assert sum(item["category"] == "kindergarten" for item in items) <= 2
            if payload["education"] == "all":
                assert layer["selection_policy"] == "up_to_two_schools_and_two_kindergartens"
                assert [item["category"] for item in items] == sorted(
                    (item["category"] for item in items), key=lambda category: category != "school")
            else:
                assert layer["selection_policy"] == f"up_to_two_{payload['education']}"
        elif key == "metro":
            assert layer["selection_policy"] == "nearest_two_walkable_stations"
        else:
            assert layer["selection_policy"] in {
                "nearest_two_walkable_clinics", "nearest_local_plus_verified_oms"}
        assert len({item["id"] for item in items}) == len(items)
        assert all(item["category"] in allowed[key] for item in items)
        assert all(item["route"] is not None for item in items)
        for item in items:
            route = item["route"]
            assert route["approximate"] is True
            assert route["status"] != "routed" or (
                route["distance_m"] is not None and route["estimated_minutes"] is not None
                and type(route["estimated_minutes"]) is int and route["estimated_minutes"] >= 1
                and 2 <= len(route["geometry"]) <= 512
                and max(route["snap_distance_m"]) <= 50)
            assert route["status"] == "routed" or (
                route["distance_m"] is None and route["estimated_minutes"] is None
                and route["geometry"] is None)
            assert all(math.isfinite(value) for value in item["coordinate"].values())
            if item["free_oms"]["status"] == "verified_program_participant":
                assert key == "medical" and item["id"] in verified_ids
                assert item["free_oms"]["official_snapshot_at"].startswith(
                    str(datetime.now(timezone.utc).year))
        if key == "medical":
            verified = [item for item in items if item["free_oms"]["status"] ==
                        "verified_program_participant"]
            assert bool(verified) == (payload["free_clinic_guarantee"]["status"] == "met")
            assert layer["required_clinic_included"] == bool(verified)
            if layer["selection_policy"] == "nearest_local_plus_verified_oms":
                assert layer["verified_oms_added_as_exception"] and len(items) <= 2
                assert items[-1]["free_oms"]["status"] == "verified_program_participant"
            else:
                assert not layer["verified_oms_added_as_exception"]


def check() -> None:
    check_air_ranker()
    check_snap_gate()
    point_map = api.map_points(request("/v1/map/points"), query=None, status=None, kind=None)
    points = json.loads(point_map.body)["items"]
    assert len(points) == json.loads(point_map.body)["dataset_total"]
    mapped = next(item for item in points if item["point_type"] == "mapped_construction")
    registry = next((item for item in points if item["point_type"] == "registry"), None)

    path = "/v1/map/nearby"
    started = perf_counter()
    first = api.map_nearby(request(path), mapped["point_id"], "all")
    cold_s = perf_counter() - started
    result = body(first, path)
    check_point_policy(result, mapped)
    started = perf_counter()
    warm = api.map_nearby(request(path), mapped["point_id"], "all")
    warm_s = perf_counter() - started
    assert body(warm, path) == result
    assert api.map_nearby(request(path, etag=first.headers["etag"]),
                          mapped["point_id"], "all").status_code == 304

    # Verify an independent education selection. A registry origin is exercised
    # only if the active visibility policy still leaves one confirmed point.
    another_origin = registry or mapped
    another = body(api.map_nearby(request(path), another_origin["point_id"], "school"), path)
    assert another["education"] == "school"
    assert all(item["category"] == "school" for item in another["layers"]["education"]["items"])
    check_point_policy(another, another_origin)
    try:
        api.map_nearby(request(path), "mappt_" + "f" * 24, "all")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unknown current point must return 404")

    lon, lat = mapped["location"]
    bbox = f"{lon - .01},{lat - .01},{lon + .01},{lat + .01}"
    viewport_path = "/v1/map/nearby/viewport"
    viewport_response = api.map_nearby_viewport(request(viewport_path), bbox,
                                                 "metro,education,medical", "all", 150)
    viewport = body(viewport_response, viewport_path)
    assert 0 <= viewport["returned_count"] <= 150
    assert viewport["returned_count"] == sum(len(layer["items"])
                                             for layer in viewport["layers"].values())
    assert all(item["route"] is None for layer in viewport["layers"].values()
               for item in layer["items"])
    assert api.map_nearby_viewport(request(viewport_path, etag=viewport_response.headers["etag"]),
                                   bbox, "metro,education,medical", "all", 150).status_code == 304
    for invalid_bbox in ("37.5,55.6,37.66,55.7", "37.5,55.6,37.7", "NaN,55.6,37.6,55.7"):
        try:
            api.map_nearby_viewport(request(viewport_path), invalid_bbox,
                                    "metro", "all", 100)
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("invalid viewport bounds must return 422")

    routed = sum(item["route"]["status"] == "routed"
                 for layer in result["layers"].values() for item in layer["items"])
    print(f"PASS offline nearby: active map points={len(points)}, mapped 2/4/2-cap selection, {routed} routed, "
          f"cold={cold_s:.3f}s warm={warm_s:.3f}s (in-process), "
          f"viewport={viewport['returned_count']} POI; no TCP/browser")


if __name__ == "__main__":
    check()
