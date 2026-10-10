#!/usr/bin/env python3
"""Bounded read-only HTTP regression gate for the current user journeys.

Example:
  rtk python integration/check_user_regressions.py --base-url http://127.0.0.1:8000

The checker performs ordinary GET requests only. Data-dependent cases are
reported as BLOCKED when their exact positive fixture is not supplied or the
corresponding source-backed feature is not in the active API snapshot. The
default exit is nonzero for both failures and blocked required cases; use
``--allow-staged`` only while an overlay or API feature is intentionally being
prepared.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POINT_ID = "mappt_085c57b4f2e108ce48ae5422"
DEFAULT_YASENEVSKY_REGISTRY_ID = "registry-df0c5887d92ea0335cdba75c"
DEFAULT_PARK_REGISTRY_ID = "registry-ffc1b55b59b7001dabd36f2f"
DEFAULT_NONLAKE_REGISTRY_ID = "registry-106b93819cc0478f967fb893"
DEFAULT_NONLAKE_UNIT_ID = "unit_bd1fc63e1b2f0e6a82214d7e"
DEFAULT_NONLAKE_MEDIA_ID = "media_7c42ddd95b6d4153d7d31810"
DEFAULT_HOUSE_MEDIA_ID = "media_8aa65b1080609d4957e30f8a"
DEFAULT_HOUSE_MEDIA_REGISTRY_ID = "registry-b1feab9d36bced349dfa4b1d"
DEFAULT_MAX_REQUESTS = 64
MAX_RESPONSE_BYTES = 24_000_000
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
OPAQUE_SEGMENT_RE = re.compile(r"segment_[0-9a-f]{16}\Z")
FORBIDDEN_PUBLIC_KEYS = {
    "osm_source_id", "source_id", "source_url", "publisher_project_id",
    "publisher_building_id", "publisher_unit_id", "publisher_media_id",
    "source_record_id", "official_source_url",
}
FORBIDDEN_STRAIGHT_DISTANCE_KEYS = {
    "straight_line_m", "straight_distance_m", "air_distance_m",
    "distance_straight_m",
}
FORBIDDEN_ROUTE_TIMING_KEYS = {
    "duration_seconds", "duration_s", "network_seconds", "access_seconds",
    "network_duration_seconds", "access_duration_seconds", "routing_seconds",
}


class GateFailure(AssertionError):
    pass


class GateBlocked(RuntimeError):
    pass


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    request_count: int = 0


class JsonHttpClient:
    """A small one-shot GET client with a strict request and response budget."""

    def __init__(self, base_url: str, *, timeout: float, max_requests: int) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("--base-url must be an HTTP(S) origin/path without credentials or query")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests: list[dict[str, Any]] = []

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def _read(self, path: str) -> tuple[int, bytes, str]:
        if (not path.startswith("/") or path.startswith("//") or ".." in path.split("/")
                or "#" in path):
            raise ValueError("API paths must be bounded absolute paths on the configured origin")
        if self.request_count >= self.max_requests:
            raise GateFailure(f"HTTP request budget exceeded ({self.max_requests})")
        request = Request(self.base_url + path, headers={"Accept": "application/json, image/*, model/gltf-binary"},
                          method="GET")
        status = 0
        body = b""
        content_type = ""
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = int(response.status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as error:
            status = int(error.code)
            body = error.read(MAX_RESPONSE_BYTES + 1)
            content_type = error.headers.get("Content-Type", "") if error.headers else ""
        except (URLError, TimeoutError, OSError) as error:
            self.requests.append({"method": "GET", "path": path, "status": None,
                                  "bytes": 0, "error": type(error).__name__})
            raise GateFailure(f"GET {path} failed: {type(error).__name__}") from error
        self.requests.append({"method": "GET", "path": path, "status": status,
                              "bytes": len(body), "content_type": content_type.split(";", 1)[0]})
        if len(body) > MAX_RESPONSE_BYTES:
            raise GateFailure(f"GET {path} exceeded response byte limit")
        return status, body, content_type

    def get_json(self, path: str, *, expected_status: int = 200) -> dict[str, Any]:
        status, body, content_type = self._read(path)
        if status != expected_status:
            code = None
            try:
                error_body = json.loads(body)
                detail = error_body.get("detail") if isinstance(error_body, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            suffix = f"; API code={code}" if code else ""
            raise GateFailure(f"GET {path}: expected HTTP {expected_status}, got {status}{suffix}")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateFailure(f"GET {path}: response is not valid JSON") from error
        if not isinstance(value, dict):
            raise GateFailure(f"GET {path}: expected a JSON object")
        if expected_status == 200 and "json" not in content_type.lower():
            raise GateFailure(f"GET {path}: unexpected Content-Type {content_type!r}")
        return value

    def get_bytes(self, path: str, *, expected_status: int = 200) -> tuple[bytes, str]:
        status, body, content_type = self._read(path)
        if status != expected_status:
            raise GateFailure(f"GET {path}: expected HTTP {expected_status}, got {status}")
        return body, content_type.split(";", 1)[0].lower()


def _query(path: str, params: dict[str, Any]) -> str:
    pairs = [(key, value) for key, value in params.items() if value is not None]
    return path + ("?" + urlencode(pairs) if pairs else "")


def _assert_no_private_source_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "source_segment_id":
                if not isinstance(child, str) or not OPAQUE_SEGMENT_RE.fullmatch(child):
                    raise GateFailure(f"non-opaque source segment ID leaked at {path}.{key}")
                continue
            if key in FORBIDDEN_PUBLIC_KEYS:
                raise GateFailure(f"private provenance key leaked at {path}.{key}")
            _assert_no_private_source_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_private_source_fields(child, path=f"{path}[{index}]")
    elif isinstance(value, str) and re.search(r"https?://", value, re.IGNORECASE):
        raise GateFailure(f"absolute source URL leaked at {path}")


def _assert_no_straight_distance_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_STRAIGHT_DISTANCE_KEYS:
                raise GateFailure(f"straight-line distance is exposed at {path}.{key}")
            _assert_no_straight_distance_fields(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_straight_distance_fields(child, path=f"{path}[{index}]")


def _assert_no_private_route_timing(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_ROUTE_TIMING_KEYS:
                raise GateFailure(f"private route seconds leaked at {path}.{key}")
            _assert_no_private_route_timing(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_private_route_timing(child, path=f"{path}[{index}]")


def _finite_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _valid_lonlat_line(value: Any) -> bool:
    return (isinstance(value, list) and len(value) >= 2 and all(
        isinstance(point, list) and len(point) >= 2
        and _finite_number(point[0]) and _finite_number(point[1])
        and -180 <= point[0] <= 180 and -90 <= point[1] <= 90
        for point in value))


def validate_route_payload(payload: dict[str, Any]) -> dict[str, int]:
    """Check public route honesty, finite metrics and origin/target snap limits."""
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise GateFailure("nearby response has no layers object")
    routed = 0
    unrouted = 0
    for layer_name, layer in layers.items():
        if not isinstance(layer, dict) or not isinstance(layer.get("items", []), list):
            raise GateFailure(f"nearby layer {layer_name!r} is malformed")
        for item in layer.get("items", []):
            if not isinstance(item, dict):
                raise GateFailure(f"nearby layer {layer_name!r} has a malformed item")
            route = item.get("route")
            if not isinstance(route, dict):
                raise GateFailure("nearby item is missing route status")
            status = route.get("status")
            snaps = route.get("snap_distance_m")
            if status == "routed":
                routed += 1
                if (not _finite_number(route.get("distance_m")) or route["distance_m"] < 0
                        or type(route.get("estimated_minutes")) is not int
                        or route["estimated_minutes"] < 0
                        or not _valid_lonlat_line(route.get("geometry"))):
                    raise GateFailure(f"routed POI {item.get('name')!r} has invalid totals or geometry")
                network_geometry = route.get("network_geometry")
                connector = route.get("origin_access")
                if connector is not None:
                    if (not isinstance(connector, dict) or connector.get("status") != "estimated"
                            or connector.get("approximate") is not True
                            or not _finite_number(connector.get("distance_m"))
                            or connector.get("distance_m", 0) > 100
                            or not _valid_lonlat_line(connector.get("geometry"))
                            or not _valid_lonlat_line(network_geometry)
                            or not OPAQUE_SEGMENT_RE.fullmatch(
                                str(connector.get("source_segment_id") or ""))):
                        raise GateFailure("origin connector is missing its disclosed geometry or opaque segment ID")
                    if route.get("origin_access_kind") != "estimated_connector_to_walk_network":
                        raise GateFailure("connector route is missing the public estimated-access classification")
                    if route.get("geometry") != network_geometry:
                        raise GateFailure("connector was merged into network route geometry instead of being disclosed separately")
                    if route["distance_m"] < connector["distance_m"]:
                        raise GateFailure("combined route distance is shorter than its estimated connector")
                    network_distance = route.get("network_distance_m")
                    if (not _finite_number(network_distance)
                            or abs(route["distance_m"] -
                                   (connector["distance_m"] + network_distance)) > 1):
                        raise GateFailure("combined route distance omits or miscounts connector/network distance")
                if isinstance(snaps, list) and len(snaps) >= 2:
                    if any(not _finite_number(value) or value < 0 for value in snaps):
                        raise GateFailure("routed POI has invalid snap distances")
                    # Origin connector may replace only the origin snap. The
                    # target must still attach to the walk graph within policy.
                    if snaps[1] > 50 or (connector is None and snaps[0] > 50):
                        raise GateFailure(f"route to {item.get('name')!r} exceeds target/origin snap policy")
                else:
                    raise GateFailure("routed POI has no two-element snap distance")
            else:
                unrouted += 1
                if (route.get("distance_m") is not None
                        or route.get("estimated_minutes") is not None
                        or route.get("geometry") is not None
                        or route.get("network_distance_m") is not None
                        or route.get("network_estimated_minutes") is not None
                        or route.get("network_geometry") is not None
                        or route.get("origin_access") is not None):
                    raise GateFailure(f"unrouted POI {item.get('name')!r} exposes route totals/geometry")
            _assert_no_private_source_fields(route)
            _assert_no_straight_distance_fields(route)
            _assert_no_private_route_timing(route)
    return {"routed": routed, "unrouted": unrouted}


class RegressionGate:
    def __init__(self, client: JsonHttpClient) -> None:
        self.client = client
        self.results: list[CheckResult] = []

    def run(self, name: str, function: Callable[[], str]) -> None:
        before = self.client.request_count
        try:
            detail = function()
            status = "PASS"
        except GateBlocked as error:
            status, detail = "BLOCKED", str(error)
        except (GateFailure, KeyError, TypeError, ValueError, AssertionError, IndexError) as error:
            status, detail = "FAIL", str(error) or type(error).__name__
        self.results.append(CheckResult(name, status, detail,
                                        self.client.request_count - before))


def _ids(payload: dict[str, Any], *, point_rows: bool = False) -> set[str]:
    values = set()
    for row in payload.get("items", []):
        registry_id = row.get("registry_id")
        if point_rows and registry_id is None:
            continue
        if isinstance(registry_id, str):
            values.add(registry_id)
    return values


def _matching_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {row["registry_id"]: row["matching_units_count"]
            for row in payload.get("items", [])
            if isinstance(row.get("registry_id"), str)
            and type(row.get("matching_units_count")) is int}


def _check_stone_grain(client: JsonHttpClient, point_id: str) -> str:
    path = _query("/v1/map/nearby", {"point_id": point_id})
    payload = client.get_json(path)
    if payload.get("point_id") != point_id:
        raise GateFailure("nearby response is not bound to the requested current map point")
    dataset = payload.get("dataset_id")
    if not isinstance(dataset, str) or ":roads:" not in dataset or ":barriers:" not in dataset:
        raise GateFailure("active route dataset omits roads/barriers revisions; origin connector inputs are not pinned")
    _assert_no_private_source_fields(payload)
    counts = validate_route_payload(payload)
    if counts["routed"] < 1:
        raise GateFailure("Stone Grain produced no routable local POI")
    metro = [item for item in payload["layers"].get("metro", {}).get("items", [])
             if item.get("route", {}).get("status") == "routed"]
    if not metro:
        raise GateFailure("Stone Grain has no routed metro station")
    clinics = [item for item in payload["layers"].get("medical", {}).get("items", [])
               if item.get("free_oms", {}).get("status") == "verified_program_participant"]
    if not clinics:
        raise GateFailure("Stone Grain response lacks the verified OMS clinic candidate")
    if not any(item.get("route", {}).get("status") == "routed" for item in clinics):
        raise GateFailure("verified OMS clinic is present but its pedestrian route is not available")
    school_rows = [item for item in payload["layers"].get("education", {}).get("items", [])
                   if item.get("category") == "school"]
    if not school_rows:
        raise GateBlocked("no school selected in bounded pool; target barrier case is not visible")
    barrier_school = next((item for item in school_rows if item.get("name") == "Школа №1100"), None)
    if barrier_school is None:
        barrier_note = "Школа №1100 not in selected school results; no route exposed"
    else:
        barrier_route = barrier_school.get("route", {})
        barrier_snaps = barrier_route.get("snap_distance_m")
        if barrier_route.get("status") == "snap_exceeds_threshold":
            if (not isinstance(barrier_snaps, list) or len(barrier_snaps) < 2
                    or not _finite_number(barrier_snaps[1]) or barrier_snaps[1] <= 50
                    or barrier_route.get("distance_m") is not None
                    or barrier_route.get("estimated_minutes") is not None
                    or barrier_route.get("geometry") is not None):
                raise GateFailure("the pinned school target outside the walk graph was forced or misreported as routed")
            barrier_note = f"Школа №1100 target snap={barrier_snaps[1]}m remains unavailable"
        else:
            # A source access point may change the target snap. The shared
            # route validator still rejects any routed endpoint beyond 50 m.
            barrier_note = f"Школа №1100 status={barrier_route.get('status')}"
    for school in school_rows:
        route = school["route"]
        snaps = route.get("snap_distance_m")
        if (route.get("status") == "routed" and isinstance(snaps, list)
                and len(snaps) >= 2 and snaps[1] > 50):
            raise GateFailure("school target beyond the snap limit was forced into a route")
    # At least one successfully routed non-entrance target demonstrates that
    # the endpoint keeps network geometry and the estimated connector separate.
    connector_routes = [item for layer in payload["layers"].values()
                        for item in layer.get("items", [])
                        if item.get("route", {}).get("status") == "routed"
                        and item["route"].get("origin_access")]
    if not connector_routes:
        raise GateFailure("no route exercised the estimated origin connector")
    return (f"point={point_id}; routed={counts['routed']}; unrouted={counts['unrouted']}; "
            f"metro={metro[0].get('name')}; verified_oms={clinics[0].get('name')}; "
            f"{barrier_note}")


def _check_yasenevsky(client: JsonHttpClient, registry_id: str) -> str:
    filters = {"query": "Ясеневский", "price_max_rub": 15_000_000, "rooms": 2}
    list_view = client.get_json(_query("/v1/registry/buildings", {**filters, "limit": 20}))
    map_view = client.get_json(_query("/v1/registry/map", filters))
    points = client.get_json(_query("/v1/map/points", filters))
    list_ids, map_ids, point_ids = _ids(list_view), _ids(map_view), _ids(points, point_rows=True)
    eligible_list_ids = {row["registry_id"] for row in list_view.get("items", [])
                         if row.get("map_eligible") is True}
    if registry_id not in list_ids or registry_id not in eligible_list_ids:
        raise GateBlocked(f"Yasenevsky fixture {registry_id} not present in current filtered list")
    if map_ids != point_ids or map_ids != eligible_list_ids:
        raise GateFailure("registry list/map/map-points disagree for Ясеневский room/price filters")
    counts = [_matching_counts(payload).get(registry_id)
              for payload in (list_view, map_view, points)]
    if len(set(counts)) != 1 or type(counts[0]) is not int or counts[0] <= 0:
        raise GateFailure(f"list/map/points matching-unit counts differ: {counts}")
    run_ids = {payload.get("unit_run_id") for payload in (list_view, map_view, points)}
    if len(run_ids) != 1 or None in run_ids:
        raise GateFailure("list/map/points are not pinned to the same apartment-menu run")

    prefix = f"/v1/developer-buildings/{registry_id}"
    house_query = {"price_max_rub": 15_000_000, "rooms": 2}
    baseline_floors = client.get_json(_query(prefix + "/floors", {}))
    filtered_floors = client.get_json(_query(prefix + "/floors", house_query))
    units = client.get_json(_query(prefix + "/units", {**house_query, "limit": 100}))
    if (units.get("total") != counts[0]
            or type(filtered_floors.get("matching_units_total")) is not int):
        raise GateBlocked("active API lacks the current matching_units_total floor DTO")
    if units.get("total") != 9:
        raise GateFailure(f"pinned Yasenevsky <=15m/2-room fixture expected 9 units, got {units.get('total')}")
    if filtered_floors.get("matching_units_total") != units.get("total"):
        raise GateFailure("house/list/menu counts disagree for price<=15m and 2-room case")
    if filtered_floors.get("unit_run_id") != units.get("unit_run_id") or units.get("unit_run_id") not in run_ids:
        raise GateFailure("deep floor/unit menu uses a different unit snapshot")
    floor_rows = [floor for section in filtered_floors.get("sections", [])
                  for floor in section.get("floors", [])]
    if sum(floor.get("unit_count", 0) for floor in floor_rows) + filtered_floors.get(
            "matching_unassigned_unit_count", 0) != units.get("total"):
        raise GateFailure("filtered floor counts do not add up to matching units")
    if any(floor.get("unit_count") != floor.get("matching_unit_count") for floor in floor_rows):
        raise GateFailure("a filtered floor exposes inconsistent matching counts")
    baseline_floor_numbers = {floor.get("floor_number")
                              for section in baseline_floors.get("sections", [])
                              for floor in section.get("floors", [])}
    matching_floor_numbers = {floor.get("floor_number") for floor in floor_rows}
    if 13 not in baseline_floor_numbers:
        raise GateBlocked("Yasenevsky fixture does not expose source floor 13 in the baseline menu")
    if 13 in matching_floor_numbers:
        raise GateFailure("floor 13 is shown despite having no matching <=15m two-room units")
    floor_13 = next(floor for section in baseline_floors.get("sections", [])
                    for floor in section.get("floors", [])
                    if floor.get("floor_number") == 13)
    empty_floor_detail = client.get_json(
        _query(prefix + f"/floors/{floor_13['floor_id']}", house_query), expected_status=404)
    if empty_floor_detail.get("detail", {}).get("code") != "not_found":
        raise GateFailure("floor 13 did not return a consistent not_found response under empty filters")
    stale_floor_units = client.get_json(_query(prefix + "/units", {
        "unit_floor_min": 13, "unit_floor_max": 13, "limit": 1,
    }))
    stale_rows = stale_floor_units.get("items", [])
    if not stale_rows:
        raise GateBlocked("no baseline apartment exists on floor 13 for the deep-unit rejection check")
    stale_unit = client.get_json(
        _query(prefix + f"/units/{stale_rows[0]['unit_id']}", house_query), expected_status=404)
    if stale_unit.get("detail", {}).get("code") != "not_found":
        raise GateFailure("floor-13 unit detail did not honor the active price/room filters")

    ranged = {**house_query, "unit_floor_min": 14, "unit_floor_max": 17}
    list_range = client.get_json(_query("/v1/registry/buildings", {**filters, **ranged, "limit": 20}))
    map_range = client.get_json(_query("/v1/registry/map", {**filters, **ranged}))
    points_range = client.get_json(_query("/v1/map/points", {**filters, **ranged}))
    range_ids = (_ids(list_range), _ids(map_range), _ids(points_range, point_rows=True))
    if not (range_ids[0] == range_ids[1] == range_ids[2]) or registry_id not in range_ids[0]:
        raise GateFailure("14–17 floor-filtered list/map/points sets disagree or lose the fixture")
    range_counts = [_matching_counts(payload).get(registry_id)
                    for payload in (list_range, map_range, points_range)]
    if len(set(range_counts)) != 1 or type(range_counts[0]) is not int or range_counts[0] <= 0:
        raise GateFailure(f"14–17 floor-filtered counts disagree or have no positive fixture: {range_counts}")
    range_floors = client.get_json(_query(prefix + "/floors", ranged))
    range_units = client.get_json(_query(prefix + "/units", {**ranged, "limit": 100}))
    if type(range_floors.get("matching_units_total")) is not int:
        raise GateBlocked("active API lacks current filtered floor count DTO")
    if not (range_floors.get("matching_units_total") == range_units.get("total") == range_counts[0] == 4):
        raise GateFailure("14–17 filtered list, floors, and unit menu counts disagree")
    if range_units.get("applied_filters", {}).get("rooms") != 2 or range_units.get(
            "applied_filters", {}).get("price_max_rub") != 15_000_000:
        raise GateFailure("room/price constraints were not preserved in deep unit selection")
    range_unit_rows = range_units.get("items", [])
    if not range_unit_rows or range_units.get("total") != range_counts[0]:
        raise GateFailure("14–17 unit-menu positive fixture is empty or count-mismatched")
    if any(type(row.get("floor_number")) is not int or not 14 <= row["floor_number"] <= 17
           or row.get("rooms_label") is None or row.get("price_rub") is None
           or row["price_rub"] > 15_000_000 for row in range_unit_rows):
        raise GateFailure("a deep-filtered unit does not satisfy all room/price/floor constraints")
    ranged_floor_rows = [floor for section in range_floors.get("sections", [])
                         for floor in section.get("floors", [])]
    if any(not 14 <= floor.get("floor_number", -999) <= 17 for floor in ranged_floor_rows):
        raise GateFailure("deep floor menu contains a floor outside 14–17")
    selected = range_unit_rows[0]
    floor_id = selected.get("floor_id")
    unit_id = selected.get("unit_id")
    if not isinstance(floor_id, str) or not isinstance(unit_id, str):
        raise GateFailure("deep-filtered unit lacks opaque floor/unit IDs")
    floor_detail = client.get_json(_query(prefix + f"/floors/{floor_id}", ranged))
    unit_detail = client.get_json(_query(prefix + f"/units/{unit_id}", ranged))
    if floor_detail.get("floor_id") != floor_id or floor_detail.get("unit_count") != sum(
            row.get("floor_id") == floor_id for row in range_unit_rows):
        raise GateFailure("deep floor detail is inconsistent with its filtered unit list")
    if (unit_detail.get("unit_id") != unit_id
            or unit_detail.get("floor_number") != selected.get("floor_number")
            or unit_detail.get("rooms_label") != selected.get("rooms_label")
            or unit_detail.get("price_rub") != selected.get("price_rub")):
        raise GateFailure("deep unit detail differs from the filtered unit-list row")
    if unit_detail.get("applied_filters", {}).get("unit_floor_min") != 14 or unit_detail.get(
            "applied_filters", {}).get("unit_floor_max") != 17:
        raise GateFailure("deep unit detail did not preserve the 14–17 floor filter")
    for label, detail in (("floor", floor_detail), ("unit", unit_detail)):
        applied = detail.get("applied_filters", {})
        if applied.get("rooms") != 2 or applied.get("price_max_rub") != 15_000_000:
            raise GateFailure(f"deep {label} detail lost the two-room/15m price filters")
        if detail.get("unit_run_id") != units.get("unit_run_id"):
            raise GateFailure(f"deep {label} detail uses a different apartment snapshot")
    return (f"registry={registry_id}; filtered_units={units['total']}; floor13 omitted; "
            f"14-17_units={range_units['total']}; list/map/points/floor/detail counts agree")


def _check_filter_parity(client: JsonHttpClient, *, field: str, value: str | int | None,
                         query: str | None, registry_id: str | None,
                         unknown_registry_id: str | None,
                         unknown_query: str | None) -> str:
    if value is None or query is None or registry_id is None:
        raise GateBlocked(f"{field}: awaiting active overlay values/positive fixture")
    params = {"query": query, field: value}
    try:
        listing = client.get_json(_query("/v1/registry/buildings", {**params, "limit": 20}))
        map_view = client.get_json(_query("/v1/registry/map", params))
        points = client.get_json(_query("/v1/map/points", params))
    except GateFailure as error:
        if "HTTP 503" in str(error) and any(code in str(error) for code in
                                             ("developer_enrichment_unavailable",
                                              "developer_snapshot_unavailable")):
            raise GateBlocked(f"{field}: active API has no ready environment overlay") from error
        raise
    list_ids, map_ids, point_ids = _ids(listing), _ids(map_view), _ids(points, point_rows=True)
    if not (list_ids == map_ids == point_ids) or registry_id not in list_ids:
        raise GateFailure(f"{field} filter differs across list/map/points or loses positive case")
    if any(view.get("applied_filters", {}).get(field) != value
           for view in (listing, map_view, points)):
        raise GateFailure(f"{field} filter is not reported consistently across list/map/points")
    counts = [_matching_counts(payload).get(registry_id) for payload in (listing, map_view, points)]
    if len(set(counts)) != 1 or type(counts[0]) is not int or counts[0] <= 0:
        raise GateFailure(f"{field} list/map/points unit counts disagree: {counts}")
    menu_prefix = f"/v1/developer-buildings/{registry_id}"
    floors = client.get_json(_query(menu_prefix + "/floors", {field: value}))
    units = client.get_json(_query(menu_prefix + "/units", {field: value, "limit": 100}))
    if floors.get("matching_units_total") != units.get("total") or units.get("total", 0) <= 0:
        raise GateFailure(f"{field} floors and units menu counts disagree or lack positive rows")
    if floors.get("applied_filters", {}).get(field) != value or units.get(
            "applied_filters", {}).get(field) != value:
        raise GateFailure(f"{field} filter was not propagated to the apartment menu")
    unit_rows = units.get("items", [])
    if not unit_rows:
        raise GateFailure(f"{field} positive fixture has no unit for floor/unit detail checks")
    chosen = unit_rows[0]
    floor_id, unit_id = chosen.get("floor_id"), chosen.get("unit_id")
    if not isinstance(floor_id, str) or not isinstance(unit_id, str):
        raise GateFailure(f"{field} filtered unit lacks opaque floor/unit IDs")
    floor_detail = client.get_json(_query(menu_prefix + f"/floors/{floor_id}", {field: value}))
    unit_detail = client.get_json(_query(menu_prefix + f"/units/{unit_id}", {field: value}))
    for label, detail in (("floor", floor_detail), ("unit", unit_detail)):
        if detail.get("applied_filters", {}).get(field) != value:
            raise GateFailure(f"{field} filter did not propagate to {label} detail")
    if floor_detail.get("floor_id") != floor_id or unit_detail.get("unit_id") != unit_id:
        raise GateFailure(f"{field} filtered detail is bound to another floor or unit")
    if not unknown_registry_id or not unknown_query:
        raise GateBlocked(f"{field}: positive parity passed, but a null-value control fixture is required")
    if unknown_registry_id:
        baseline_unknown = client.get_json(_query(
            "/v1/registry/buildings", {"query": unknown_query, "limit": 20}))
        unknown_row = next((row for row in baseline_unknown.get("items", [])
                            if row.get("registry_id") == unknown_registry_id), None)
        if unknown_row is None:
            raise GateBlocked("unknown-control record is not available in the bounded baseline query")
        unknown_fields = {
            "district": ("district_id", "district_name"),
            "okrug_id": ("okrug_id",),
            "max_noise_index": ("noise_index",),
        }[field]
        if any(key not in unknown_row for key in unknown_fields):
            raise GateBlocked(f"unknown {field} fixture omits its source-backed nullable field")
        if any(unknown_row.get(key) is not None for key in unknown_fields):
            raise GateBlocked(f"control record is not unknown for {field}; choose a null-value registry ID")
        filtered_unknown_query = {"query": unknown_query, field: value}
        unknown_views = (
            client.get_json(_query("/v1/registry/buildings",
                                   {**filtered_unknown_query, "limit": 20})),
            client.get_json(_query("/v1/registry/map", filtered_unknown_query)),
            client.get_json(_query("/v1/map/points", filtered_unknown_query)),
        )
        if any(unknown_registry_id in _ids(view, point_rows=(index == 2))
               for index, view in enumerate(unknown_views)):
            raise GateFailure(f"unknown {field} record was included when that filter is active")
    return (f"{field}={value}; list/map/points={len(list_ids)} matching IDs; "
            f"menu_units={units['total']}; floor/unit detail agree" +
            ("; unknown excluded" if unknown_registry_id else ""))


def _check_park(client: JsonHttpClient, registry_id: str) -> str:
    try:
        payload = client.get_json(f"/v1/developer-buildings/{registry_id}/park-walk")
    except GateFailure as error:
        if "got 404" in str(error):
            raise GateBlocked("park-walk route is not present in this API build") from error
        raise
    if payload.get("registry_id") != registry_id:
        raise GateFailure("park walk is not bound to requested building")
    if payload.get("status") != "routed":
        raise GateBlocked(f"park route status is {payload.get('status')!r}")
    if (not _finite_number(payload.get("distance_m")) or payload["distance_m"] <= 0
            or type(payload.get("estimated_minutes")) is not int
            or payload["estimated_minutes"] <= 0
            or not _finite_number(payload.get("boundary_gap_m"))
            or payload["boundary_gap_m"] > 15
            or payload.get("endpoint_basis") != "pedestrian_graph_near_park_boundary"
            or payload.get("access_verified") is not False):
        raise GateFailure("park route has non-finite totals or does not disclose boundary approach status")
    _assert_no_private_source_fields(payload)
    return (f"registry={registry_id}; {payload['distance_m']}m/{payload['estimated_minutes']}min; "
            f"boundary gap={payload['boundary_gap_m']}m; access unverified")


def _check_model_asset(client: JsonHttpClient, registry_id: str) -> str:
    descriptor = client.get_json(f"/v1/developer-buildings/{registry_id}/model")
    if descriptor.get("registry_id") != registry_id:
        raise GateFailure("house model descriptor is bound to another building")
    if descriptor.get("status") != "ready":
        raise GateBlocked(f"house media/model is {descriptor.get('status')!r}")
    asset_path = descriptor.get("asset_url")
    if (not isinstance(asset_path, str)
            or not re.fullmatch(r"/v1/developer-model-assets/[0-9a-f]{64}", asset_path)
            or descriptor.get("asset_sha256") != asset_path.rsplit("/", 1)[-1]):
        raise GateFailure("house model URL/hash is not an opaque allowlisted asset reference")
    data, content_type = client.get_bytes(asset_path)
    if content_type not in {"model/gltf-binary", "application/octet-stream"} or not data.startswith(b"glTF"):
        raise GateFailure("house model asset did not return a GLB HTTP body")
    _assert_no_private_source_fields(descriptor)
    return f"registry={registry_id}; schematic model asset HTTP 200; sha={descriptor['asset_sha256'][:12]}…"


def _check_nonlake_plan(client: JsonHttpClient, *, registry_id: str | None,
                        unit_id: str | None, media_id: str | None) -> str:
    if not registry_id or not unit_id or not media_id:
        raise GateBlocked("awaiting an active non-Lake exact unit-plan fixture")
    if registry_id == "registry-b1feab9d36bced349dfa4b1d":
        raise GateFailure("non-Lake plan fixture must identify another project")
    payload = client.get_json(f"/v1/developer-buildings/{registry_id}/units/{unit_id}")
    if payload.get("registry_id") != registry_id or payload.get("unit_id") != unit_id:
        raise GateFailure("non-Lake plan response is bound to another building/unit")
    links = _media_rows(payload, media_id)
    if not links:
        raise GateBlocked("exact non-Lake apartment media link is not active in the unit DTO")
    if len(links) != 1:
        raise GateFailure("exact non-Lake unit DTO exposes duplicate media links")
    if links[0].get("role") != "apartment_plan" or links[0].get("scope") != "unit_plan":
        raise GateFailure("non-Lake media ID is not typed as an apartment/unit plan")
    _assert_no_private_source_fields(payload)
    return _check_image_media(client, media_id, registry_id=registry_id,
                              unit_id=unit_id, expected_scope="unit_plan",
                              expected_role="apartment_plan", link=links[0])


def _media_rows(value: Any, media_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        media = value.get("media")
        if isinstance(media, list):
            rows.extend(row for row in media if isinstance(row, dict)
                        and row.get("media_id") == media_id)
        for child in value.values():
            rows.extend(_media_rows(child, media_id))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_media_rows(child, media_id))
    return rows


def _check_image_media(client: JsonHttpClient, media_id: str, *, registry_id: str,
                       unit_id: str | None = None,
                       expected_scope: str | None = None,
                       expected_role: str | None = None,
                       expected_label: str | None = None,
                       link: dict[str, Any] | None = None) -> str:
    if not re.fullmatch(r"media_[0-9a-f]{24}", media_id):
        raise GateFailure("media ID is not an opaque stable public identifier")
    if link is None:
        raise GateBlocked("media asset is not linked from a source-bound public DTO")
    if link.get("registry_id") not in {None, registry_id}:
        raise GateFailure("media link is bound to another building")
    if unit_id is not None and link.get("unit_id") not in {None, unit_id}:
        raise GateFailure("media link is bound to another apartment")
    expected_asset_path = f"/v1/developer-media/{media_id}"
    if link.get("asset_url") != expected_asset_path:
        raise GateFailure("media DTO does not point to the opaque local asset route")
    if link.get("availability") not in {"prepared", "on_demand"}:
        raise GateBlocked(f"media availability is {link.get('availability')!r}")
    if expected_scope is not None and link.get("scope") != expected_scope:
        raise GateFailure("linked media has a different scope")
    if expected_role is not None and link.get("role") != expected_role:
        raise GateFailure("linked media has a different role")
    if expected_label is not None and link.get("label") != expected_label:
        raise GateFailure("linked media label does not describe its visual type")
    if not isinstance(link.get("observed_at"), str):
        raise GateFailure("media DTO omits its observation timestamp")
    try:
        body, content_type = client.get_bytes(expected_asset_path)
    except GateFailure as error:
        if "got 404" in str(error) or "got 503" in str(error):
            raise GateBlocked("source-bound media ID is not prepared or retrievable in this API build") from error
        raise
    if not content_type.startswith("image/") or not body:
        raise GateFailure(f"developer-media returned no image bytes: {content_type}")
    return f"media_id={media_id}; image HTTP 200; MIME={content_type.split(';', 1)[0]}"


def _check_developer_media(client: JsonHttpClient, media_id: str | None,
                           registry_id: str) -> str:
    if not media_id:
        raise GateBlocked("awaiting an active house-media media_id fixture")
    payload = client.get_json(f"/v1/registry/buildings/{registry_id}")
    if payload.get("registry_id") != registry_id:
        raise GateFailure("house media detail is bound to another building")
    _assert_no_private_source_fields(payload)
    links = _media_rows(payload, media_id)
    if not links:
        raise GateBlocked("exact project media link is not active in the building DTO")
    if len(links) != 1:
        raise GateFailure("house building DTO exposes duplicate project media links")
    scope = links[0].get("scope")
    if scope == "project_gallery_single_corpus":
        expected_role, expected_label = "facade_photo", "Фото ЖК"
    elif scope == "project_gallery":
        expected_role, expected_label = "project_rendering", "Визуализация ЖК"
    else:
        raise GateFailure("house image is not scoped to the project gallery")
    return _check_image_media(client, media_id, registry_id=registry_id,
                              expected_scope=scope,
                              expected_role=expected_role,
                              expected_label=expected_label, link=links[0])


def _default_output(base_url: str) -> Path:
    host = (urlsplit(base_url).hostname or "endpoint").replace(".", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / ".runtime/research/user-regressions" / f"user-regressions-{host}-{stamp}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="local or public API origin; GET-only, no credentials/query")
    parser.add_argument("--output", type=Path,
                        help="JSON report path (default: .runtime/research/user-regressions/)")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--allow-staged", action="store_true",
                        help="exit zero with explicit BLOCKED cases during staged overlay development")
    parser.add_argument("--stone-point-id", default=DEFAULT_POINT_ID)
    parser.add_argument("--yasen-registry-id", default=DEFAULT_YASENEVSKY_REGISTRY_ID)
    parser.add_argument("--park-registry-id", default=DEFAULT_PARK_REGISTRY_ID)
    parser.add_argument("--district", help="exact active overlay district name or ID")
    parser.add_argument("--okrug-id", help="exact active overlay administrative-district ID")
    parser.add_argument("--max-noise-index", type=int, help="active 0..100 noise filter value")
    parser.add_argument("--filter-query", default="Ясеневский",
                        help="small exact text slice for cross-view district/noise checks")
    parser.add_argument("--filter-registry-id",
                        help="positive registry ID for district/noise apartment-menu checks")
    parser.add_argument("--unknown-registry-id",
                        help="control record with null district/okrug/noise facts")
    parser.add_argument("--unknown-query",
                        help="exact building-name query locating the nullable control record")
    parser.add_argument("--nonlake-registry-id", default=DEFAULT_NONLAKE_REGISTRY_ID)
    parser.add_argument("--nonlake-unit-id", default=DEFAULT_NONLAKE_UNIT_ID)
    parser.add_argument("--nonlake-media-id", default=DEFAULT_NONLAKE_MEDIA_ID)
    parser.add_argument("--house-media-id", default=DEFAULT_HOUSE_MEDIA_ID)
    parser.add_argument("--house-media-registry-id", default=DEFAULT_HOUSE_MEDIA_REGISTRY_ID)
    parser.add_argument("--model-registry-id", default="registry-b1feab9d36bced349dfa4b1d",
                        help="known model/house-media building; model is explicitly schematic")
    args = parser.parse_args(argv)
    if args.max_requests < 1 or args.max_requests > 64:
        parser.error("--max-requests must be between 1 and 64")
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        parser.error("--timeout must be a finite positive number")
    if args.max_noise_index is not None and not 0 <= args.max_noise_index <= 100:
        parser.error("--max-noise-index must be in 0..100")
    if args.unknown_registry_id is not None and not args.filter_registry_id:
        args.filter_registry_id = args.yasen_registry_id

    try:
        client = JsonHttpClient(args.base_url, timeout=args.timeout,
                                max_requests=args.max_requests)
    except ValueError as error:
        parser.error(str(error))
    gate = RegressionGate(client)

    def health() -> str:
        payload = client.get_json("/v1/health")
        if payload.get("data_mode") != "real" or not payload.get("registry_snapshot_id"):
            raise GateFailure("API is not serving a source-backed developer registry snapshot")
        if not payload.get("unit_run_id"):
            raise GateBlocked("active apartment overlay pointer is not ready")
        return (f"map={payload.get('map_run_id')}; units={payload.get('unit_run_id')}; "
                f"registry={payload.get('registry_snapshot_id')}")

    gate.run("health_and_active_snapshots", health)
    gate.run("stone_grain_origin_connector_and_route_safety",
             lambda: _check_stone_grain(client, args.stone_point_id))
    gate.run("yasenevsky_list_map_points_floor_unit_parity",
             lambda: _check_yasenevsky(client, args.yasen_registry_id))

    for field, value in (("district", args.district), ("okrug_id", args.okrug_id),
                         ("max_noise_index", args.max_noise_index)):
        gate.run(f"{field}_cross_view_menu_and_unknown_behavior",
                     lambda field=field, value=value: _check_filter_parity(
                     client, field=field, value=value, query=args.filter_query,
                     registry_id=args.filter_registry_id,
                     unknown_registry_id=args.unknown_registry_id,
                     unknown_query=args.unknown_query))

    gate.run("park_walk_finite_route", lambda: _check_park(client, args.park_registry_id))
    gate.run("house_model_asset_http_and_redaction",
             lambda: _check_model_asset(client, args.model_registry_id))
    gate.run("nonlake_apartment_plan_http_and_redaction",
             lambda: _check_nonlake_plan(client, registry_id=args.nonlake_registry_id,
                                         unit_id=args.nonlake_unit_id,
                                         media_id=args.nonlake_media_id))
    gate.run("house_media_asset_http_and_redaction",
             lambda: _check_developer_media(client, args.house_media_id,
                                            args.house_media_registry_id))

    passed = sum(result.status == "PASS" for result in gate.results)
    failed = sum(result.status == "FAIL" for result in gate.results)
    blocked = sum(result.status == "BLOCKED" for result in gate.results)
    overall = "FAIL" if failed else "BLOCKED" if blocked else "PASS"
    report = {
        "schema": "user-regression-http-report/1",
        "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "base_origin": urlunsplit((*urlsplit(client.base_url)[:2], "", "", "")),
        "overall": overall,
        "summary": {"pass": passed, "fail": failed, "blocked": blocked,
                    "request_count": client.request_count,
                    "request_budget": client.max_requests},
        "checks": [asdict(result) for result in gate.results],
        "requests": client.requests,
        "scope": "read_only_http_get; no credentials; no retries; bounded response and request counts",
    }
    output = args.output or _default_output(args.base_url)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{overall}: PASS={passed} FAIL={failed} BLOCKED={blocked} "
          f"GETs={client.request_count}/{client.max_requests} report={output}")
    for result in gate.results:
        print(f"{result.status:7} {result.name}: {result.detail}")
    if failed or blocked and not args.allow_staged:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
