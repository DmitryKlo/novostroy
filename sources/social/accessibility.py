"""Pure social-access calculations and an opt-in Valhalla pedestrian adapter.

Straight-line distance is never treated as walking distance or time. A route
request is made only when a caller explicitly invokes ``ValhallaClient.route``;
the endpoint must come from server configuration, never from a user query.
"""

from __future__ import annotations

import json
import math
import os
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


AccessKind = Literal[
    "entrance_known",
    "center_approx",
    "mapped_point_access_unknown",
]
EARTH_MEAN_RADIUS_M = 6_371_008.8
_ROUTED_METHODS = {"routed", "valhalla_pedestrian"}
_COVERAGE_STATES = {"complete", "partial", "unknown"}


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """WGS84 point in lon/lat order, with its access meaning made explicit."""

    coordinates: tuple[float, float]
    kind: AccessKind
    source_id: str | None = None

    def __post_init__(self) -> None:
        lon, lat = _validated_coordinates(self.coordinates)
        if self.kind not in {
            "entrance_known",
            "center_approx",
            "mapped_point_access_unknown",
        }:
            raise ValueError(f"unsupported access kind: {self.kind}")
        object.__setattr__(self, "coordinates", (lon, lat))


def _validated_coordinates(value: Any) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError("coordinates must be [longitude, latitude]")
    lon, lat = value
    if isinstance(lon, bool) or isinstance(lat, bool):
        raise ValueError("coordinates must be numeric WGS84 values")
    try:
        lon_value = float(lon)
        lat_value = float(lat)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinates must be numeric WGS84 values") from exc
    if not math.isfinite(lon_value) or not math.isfinite(lat_value):
        raise ValueError("coordinates must be finite")
    if not -180 <= lon_value <= 180 or not -90 <= lat_value <= 90:
        raise ValueError("coordinates are outside WGS84 bounds")
    return lon_value, lat_value


def _osm_key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    osm_type = record.get("osm_type", record.get("type"))
    osm_id = record.get("osm_id", record.get("id"))
    if osm_type is None or osm_id is None:
        return None
    return str(osm_type), str(osm_id)


def _revision_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    try:
        version = int(record.get("osm_version", record.get("version", -1)))
    except (TypeError, ValueError):
        version = -1
    return (
        version,
        str(record.get("osm_timestamp") or record.get("timestamp") or ""),
        str(record.get("observed_at") or ""),
    )


def deduplicate_osm_features(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate by OSM ``(type, id)``; retain the newest known revision.

    Rows without a stable OSM type and ID are retained independently rather
    than merged using a guessed spatial identity. Ties preserve the first row.
    """
    selected: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
    unkeyed: list[dict[str, Any]] = []
    for source in records:
        record = dict(source)
        key = _osm_key(record)
        if key is None:
            unkeyed.append(record)
            continue
        previous = selected.get(key)
        if previous is None:
            selected[key] = record
        elif _revision_key(record) > _revision_key(previous):
            selected[key] = record
    return [*selected.values(), *unkeyed]


def poi_access_point(poi: Mapping[str, Any]) -> AccessPoint | None:
    """Get an explicitly marked entrance or a source point with an honest label.

    ``entrance`` is accepted only when it carries ``access_kind='entrance_known'``.
    An Overpass center is an approximation; an OSM node is a mapped point but
    does not by itself prove that the node is a public entrance.
    """
    source_id = poi.get("source_id")
    entrance = poi.get("entrance")
    if isinstance(entrance, Mapping):
        kind = entrance.get("access_kind")
        coordinates = entrance.get("coordinates")
        if kind == "entrance_known" and coordinates is not None:
            return AccessPoint(tuple(coordinates), "entrance_known", source_id)

    location = poi.get("location")
    if not isinstance(location, Mapping):
        return None
    coordinates = location.get("coordinates")
    if coordinates is None:
        return None
    if location.get("role") == "overpass_center" or location.get("approximate") is True:
        kind: AccessKind = "center_approx"
    else:
        kind = "mapped_point_access_unknown"
    return AccessPoint(tuple(coordinates), kind, source_id)


def straight_line_distance_m(
    first: AccessPoint,
    second: AccessPoint,
) -> float:
    """Great-circle distance in meters; this is not a route distance or time."""
    lon1, lat1 = (math.radians(value) for value in first.coordinates)
    lon2, lat2 = (math.radians(value) for value in second.coordinates)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    haversine = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    central_angle = 2 * math.asin(min(1.0, math.sqrt(haversine)))
    return round(EARTH_MEAN_RADIUS_M * central_angle, 3)


def nearest_poi(
    origin: AccessPoint,
    pois: Iterable[Mapping[str, Any]],
    *,
    category: str | None = None,
) -> dict[str, Any] | None:
    """Return the nearest POI by labeled straight-line meters, never walk time."""
    candidates: list[tuple[float, tuple[str, str], Mapping[str, Any], AccessPoint]] = []
    for poi in pois:
        if category is not None and poi.get("category") != category:
            continue
        target = poi_access_point(poi)
        if target is None:
            continue
        key = _osm_key(poi) or ("~unkeyed", str(poi.get("source_id") or ""))
        candidates.append((straight_line_distance_m(origin, target), key, poi, target))
    if not candidates:
        return None
    distance, _, poi, target = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "source": poi.get("source"),
        "source_id": poi.get("source_id"),
        "osm_type": poi.get("osm_type", poi.get("type")),
        "osm_id": poi.get("osm_id", poi.get("id")),
        "category": poi.get("category"),
        "straight_line_m": distance,
        "metric": "straight_line",
        "unit": "m",
        "from_access_kind": origin.kind,
        "to_access_kind": target.kind,
        "entrance_known": target.kind == "entrance_known",
        "approximate": (
            origin.kind != "entrance_known" or target.kind != "entrance_known"
        ),
    }


def social_payload_for_complex(
    origin: AccessPoint,
    pois: Iterable[Mapping[str, Any]],
    required_categories: Sequence[str],
    *,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Build an honest POI overlay payload for a complex detail response.

    This bounded OSM sample cannot establish category or route completeness,
    so the index remains null. A caller must provide the stable source snapshot
    identifier (ideally the normalized file hash from the manifest). The
    overlay reports only the nearest mapped POI in each requested category,
    with a straight-line metric and explicit point-access quality.
    """
    categories = list(dict.fromkeys(required_categories))
    if any(not isinstance(category, str) or not category for category in categories):
        raise ValueError("required_categories must contain non-empty strings")
    records = deduplicate_osm_features(pois)
    observed = sorted(
        {
            record["category"]
            for record in records
            if isinstance(record.get("category"), str) and record.get("category")
        }
    )
    nearest = [
        result
        for category in categories
        if (result := nearest_poi(origin, records, category=category)) is not None
    ]
    return {
        "index": None,
        "coverage": {
            "status": "unknown",
            "observed_categories": observed,
            "required_categories": categories,
            "method": "straight_line",
            "source_version": source_version,
            "coverage_completeness": "unknown",
        },
        "nearest_pois": nearest,
        "poi_source": {
            "name": "OpenStreetMap",
            "source_kind": "osm",
            "coverage": "sample",
            "attribution": "© OpenStreetMap contributors",
            "license": "ODbL 1.0",
            "license_url": "https://www.openstreetmap.org/copyright",
        },
    }


def _validate_status(value: str | None) -> str:
    if isinstance(value, str) and value in _COVERAGE_STATES:
        return value
    # Values like "sample" or a missing status are informative but do not
    # establish completeness.
    return "unknown"


def _validate_weights(
    categories: Sequence[str],
    weights: Mapping[str, float] | None,
) -> dict[str, float]:
    if weights is None:
        return {category: 1.0 for category in categories}
    extra = set(weights) - set(categories)
    if extra:
        raise ValueError(f"weights contain unrequested categories: {sorted(extra)}")
    result: dict[str, float] = {}
    for category in categories:
        value = weights.get(category, 1.0)
        if isinstance(value, bool):
            raise ValueError("category weights must be positive finite numbers")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("category weights must be positive finite numbers") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError("category weights must be positive finite numbers")
        result[category] = numeric
    return result


def accessibility_index(
    requested_categories: Sequence[str],
    *,
    poi_coverage: Mapping[str, str],
    route_coverage: Mapping[str, str],
    routes_by_category: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    threshold_minutes: float = 15.0,
    weights: Mapping[str, float] | None = None,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Compute the fixed-denominator 0-1 index only on complete routed data.

    For category ``c``, its score is ``max(0, 1 - minutes/T)`` using
    the fastest actual pedestrian route. A complete, verified category with no
    POIs inside the covered area scores zero. Unknown/partial POI or route
    coverage makes the aggregate ``value`` null; missing categories are never
    treated as zero or removed from the denominator.
    """
    if any(not isinstance(category, str) or not category for category in requested_categories):
        raise ValueError("requested categories must be non-empty strings")
    categories = list(dict.fromkeys(requested_categories))
    if isinstance(threshold_minutes, bool):
        raise ValueError("threshold_minutes must be a positive finite number")
    try:
        threshold = float(threshold_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold_minutes must be a positive finite number") from exc
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("threshold_minutes must be a positive finite number")
    category_weights = _validate_weights(categories, weights)
    routes_by_category = routes_by_category or {}

    observed = [
        category
        for category in categories
        if _validate_status(poi_coverage.get(category)) == "complete"
        and _validate_status(route_coverage.get(category)) == "complete"
    ]
    all_complete = bool(categories) and len(observed) == len(categories)
    has_partial_evidence = any(
        _validate_status(poi_coverage.get(category)) != "unknown"
        or _validate_status(route_coverage.get(category)) != "unknown"
        for category in categories
    )
    status = (
        "complete"
        if all_complete
        else "partial"
        if has_partial_evidence
        else "unknown"
    )
    total_weight = sum(category_weights.values())
    observed_weight = sum(category_weights[category] for category in observed)
    coverage_ratio = observed_weight / total_weight if total_weight else 0.0
    category_scores: dict[str, float] = {}
    value: float | None = None

    if all_complete and source_version:
        for category in categories:
            route_records = routes_by_category.get(category, ())
            durations: list[float] = []
            for route in route_records:
                if route.get("method") not in _ROUTED_METHODS:
                    raise ValueError(
                        "index requires pedestrian route results; straight-line distance is not travel time"
                    )
                duration = route.get("duration_s")
                if isinstance(duration, bool):
                    raise ValueError("route duration_s must be a non-negative finite number")
                try:
                    duration_value = float(duration)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "route duration_s must be a non-negative finite number"
                    ) from exc
                if not math.isfinite(duration_value) or duration_value < 0:
                    raise ValueError("route duration_s must be a non-negative finite number")
                durations.append(duration_value)
            minutes = min(durations) / 60 if durations else None
            score = (
                max(0.0, 1.0 - minutes / threshold)
                if minutes is not None
                else 0.0
            )
            category_scores[category] = round(score, 6)
        value = round(
            sum(category_weights[category] * category_scores[category] for category in categories)
            / total_weight,
            6,
        )

    return {
        "value": value,
        "category_scores": category_scores,
        "coverage": {
            "status": status,
            "observed_categories": observed,
            "required_categories": categories,
            "method": "routed",
            "source_version": source_version,
            "coverage_ratio": round(coverage_ratio, 6),
        },
        "threshold_minutes": threshold,
    }


class ValhallaError(RuntimeError):
    """A configured Valhalla route could not be obtained or parsed."""


class ValhallaClient:
    """Explicit Valhalla pedestrian caller, configured by the server operator.

    Constructing a client performs no network call. ``route`` is the only
    method that contacts the configured endpoint. Do not pass a URL from a
    user's request. Approximate points are rejected unless the caller opts in.
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 10.0) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Valhalla endpoint must be a configured HTTP(S) URL without credentials/query")
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_s must be a positive finite number") from exc
        if isinstance(timeout_s, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be a positive finite number")
        path = parsed.path.rstrip("/")
        self.endpoint = endpoint.rstrip("/") if path.endswith("/route") else f"{endpoint.rstrip('/')}/route"
        self.timeout_s = timeout

    @classmethod
    def from_environment(
        cls,
        *,
        variable: str = "NOVOSTROY_VALHALLA_ENDPOINT",
        timeout_s: float = 10.0,
    ) -> ValhallaClient | None:
        """Load an operator-configured endpoint; return ``None`` when disabled."""
        endpoint = os.environ.get(variable)
        return cls(endpoint, timeout_s=timeout_s) if endpoint else None

    def route(
        self,
        origin: AccessPoint,
        destination: AccessPoint,
        *,
        allow_approximate: bool = False,
    ) -> dict[str, Any]:
        """Explicitly request a pedestrian route and return provider metrics."""
        approximate = (
            origin.kind != "entrance_known" or destination.kind != "entrance_known"
        )
        if approximate and not allow_approximate:
            raise ValueError("route endpoints must be known entrances unless approximation is explicitly allowed")
        body = {
            "locations": [
                {"lon": origin.coordinates[0], "lat": origin.coordinates[1], "type": "break"},
                {
                    "lon": destination.coordinates[0],
                    "lat": destination.coordinates[1],
                    "type": "break",
                },
            ],
            "costing": "pedestrian",
            "units": "kilometers",
        }
        request = Request(
            self.endpoint,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = response.read(1_000_001)
        except (OSError, URLError) as exc:
            raise ValhallaError(f"Valhalla pedestrian route failed: {exc}") from exc
        if len(payload) > 1_000_000:
            raise ValhallaError("Valhalla response exceeded 1 MB")
        try:
            result = json.loads(payload)
            trip = result["trip"]
            summary = trip["summary"]
            status = trip.get("status", 0)
            distance_km = float(summary["length"])
            duration_s = float(summary["time"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValhallaError("Valhalla returned an invalid route response") from exc
        if status != 0:
            message = trip.get("status_message", "route unavailable")
            raise ValhallaError(f"Valhalla route status {status}: {message}")
        if not math.isfinite(distance_km) or distance_km < 0:
            raise ValhallaError("Valhalla route length is invalid")
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValhallaError("Valhalla route duration is invalid")
        return {
            "distance_m": round(distance_km * 1000, 3),
            "duration_s": round(duration_s, 3),
            "method": "routed",
            "provider": "valhalla",
            "costing": "pedestrian",
            "approximate": approximate,
        }
