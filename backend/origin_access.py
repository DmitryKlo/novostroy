"""Bounded approximate access connectors from buildings to the walk graph.

This module deliberately does not replace or relax the pedestrian router. It
selects a short, disclosed connector to a graph-correlated OSM street segment,
then delegates the rest of the journey to the existing local network router.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from itertools import islice
from typing import Any, Callable, Mapping, Sequence


Coordinate = tuple[float, float]  # WGS84 [longitude, latitude]
CONNECTOR_POLICY_VERSION = "origin-access-connector-v2"
_EARTH_RADIUS_M = 6_371_008.8
_MAX_CONNECTOR_CANDIDATES = 4
_MAX_INPUT_CANDIDATES = 64
_MAX_LOCAL_BARRIERS = 512


@dataclass(frozen=True)
class Origin:
    """Selected map point and optional building outline in WGS84."""

    coordinate: Coordinate
    # Optional closed exterior ring. This is used as the connector's start
    # boundary; it is never treated as evidence of a door or internal path.
    footprint_ring: tuple[Coordinate, ...] | None = None


@dataclass(frozen=True)
class WalkSegment:
    """OSM way geometry/tags joined to a Valhalla-correlated point on that way."""

    source_id: str
    coordinates: tuple[Coordinate, ...]
    tags: Mapping[str, Any]
    graph_coordinate: Coordinate


@dataclass(frozen=True)
class Barrier:
    """Known blocking feature represented as a GeoJSON geometry mapping."""

    source_id: str
    kind: str
    geometry: Mapping[str, Any]


@dataclass(frozen=True)
class ConnectorPolicy:
    # 100 m covers observed source-mapped building points set back from the
    # nearest mapped footway, while remaining a visibly estimated connector.
    max_access_distance_m: float = 100.0
    max_candidates: int = 4
    max_graph_to_way_distance_m: float = 8.0
    walking_speed_mps: float = 1.2
    network_snap_limit_m: float = 50.0


DEFAULT_POLICY = ConnectorPolicy()


def _coordinate(value: Sequence[float]) -> Coordinate:
    if isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError("coordinate must be a WGS84 [longitude, latitude] pair")
    lon, lat = value
    if isinstance(lon, bool) or isinstance(lat, bool):
        raise ValueError("coordinate values must be numeric")
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinate values must be numeric") from exc
    if not (math.isfinite(lon) and math.isfinite(lat)
            and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("coordinate must be finite WGS84 longitude/latitude")
    return lon, lat


def _project(point: Coordinate, center: Coordinate) -> tuple[float, float]:
    lon, lat = point
    lon0, lat0 = center
    cos_lat = math.cos(math.radians(lat0))
    return (_EARTH_RADIUS_M * math.radians(lon - lon0) * cos_lat,
            _EARTH_RADIUS_M * math.radians(lat - lat0))


def _unproject(point: tuple[float, float], center: Coordinate) -> Coordinate:
    x, y = point
    lon0, lat0 = center
    cos_lat = math.cos(math.radians(lat0))
    if abs(cos_lat) < 1e-8:
        raise ValueError("local projection is undefined near the poles")
    return (lon0 + math.degrees(x / (_EARTH_RADIUS_M * cos_lat)),
            lat0 + math.degrees(y / _EARTH_RADIUS_M))


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _project_to_segment(point: tuple[float, float], a: tuple[float, float],
                        b: tuple[float, float]) -> tuple[float, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return a
    t = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
    return a[0] + t * dx, a[1] + t * dy


def _cross(a: tuple[float, float], b: tuple[float, float],
           c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a: tuple[float, float], b: tuple[float, float],
                        c: tuple[float, float], d: tuple[float, float]) -> bool:
    eps = 1e-7
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    if ((ab_c > eps and ab_d < -eps or ab_c < -eps and ab_d > eps)
            and (cd_a > eps and cd_b < -eps or cd_a < -eps and cd_b > eps)):
        return True

    def on_segment(p: tuple[float, float], q: tuple[float, float],
                   r: tuple[float, float]) -> bool:
        return (min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps
                and min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps)

    return ((abs(ab_c) <= eps and on_segment(a, c, b))
            or (abs(ab_d) <= eps and on_segment(a, d, b))
            or (abs(cd_a) <= eps and on_segment(c, a, d))
            or (abs(cd_b) <= eps and on_segment(c, b, d)))


def _closest_segment_pair(a: tuple[float, float], b: tuple[float, float],
                          c: tuple[float, float], d: tuple[float, float]
                          ) -> tuple[tuple[float, float], tuple[float, float]]:
    if _segments_intersect(a, b, c, d):
        r = (b[0] - a[0], b[1] - a[1])
        s = (d[0] - c[0], d[1] - c[1])
        denominator = r[0] * s[1] - r[1] * s[0]
        delta = (c[0] - a[0], c[1] - a[1])
        if abs(denominator) > 1e-10:
            t = (delta[0] * s[1] - delta[1] * s[0]) / denominator
            intersection = (a[0] + t * r[0], a[1] + t * r[1])
            return intersection, intersection
        # Collinear overlap; endpoint projections find a point in the overlap.
        choices = [(a, _project_to_segment(a, c, d)),
                   (b, _project_to_segment(b, c, d)),
                   (_project_to_segment(c, a, b), c),
                   (_project_to_segment(d, a, b), d)]
        return min(choices, key=lambda pair: _distance(*pair))
    choices = [(a, _project_to_segment(a, c, d)),
               (b, _project_to_segment(b, c, d)),
               (_project_to_segment(c, a, b), c),
               (_project_to_segment(d, a, b), d)]
    return min(choices, key=lambda pair: _distance(*pair))


def _line_segments(points: Sequence[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(points, points[1:]))


def _nearest_connector(origin: Origin, segment: WalkSegment, policy: ConnectorPolicy
                       ) -> tuple[Coordinate, Coordinate, float] | None:
    center = _coordinate(origin.coordinate)
    graph_point = _coordinate(segment.graph_coordinate)
    road = [_project(_coordinate(point), center) for point in segment.coordinates]
    if len(road) < 2:
        return None
    graph_xy = _project(graph_point, center)
    # A graph correlation must actually belong to the same locally tagged way.
    if min(_distance(graph_xy, _project_to_segment(graph_xy, *edge))
           for edge in _line_segments(road)) > policy.max_graph_to_way_distance_m:
        return None

    if origin.footprint_ring:
        ring = [_project(_coordinate(point), center) for point in origin.footprint_ring]
        if len(ring) < 4 or ring[0] != ring[-1]:
            raise ValueError("footprint_ring must be a closed polygon boundary")
        origin_edges = _line_segments(ring)
    else:
        point_xy = _project(center, center)
        origin_edges = [(point_xy, point_xy)]

    choices: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for origin_edge in origin_edges:
        for road_edge in _line_segments(road):
            choices.append(_closest_segment_pair(*origin_edge, *road_edge))
    if not choices:
        return None
    start_xy, _ = min(choices, key=lambda pair: _distance(*pair))
    start = _unproject(start_xy, center)
    # The street geometry is checked above; route from Valhalla's correlated
    # point so the network leg begins on the actual graph rather than an OSM
    # line approximation.
    end = graph_point
    return start, end, _distance(start_xy, _project(end, center))


def _suitable_segment(tags: Mapping[str, Any]) -> bool:
    highway = str(tags.get("highway", "")).strip().lower()
    allowed = {"footway", "pedestrian", "path", "residential", "living_street", "unclassified"}
    foot = str(tags.get("foot", "")).strip().lower()
    access = str(tags.get("access", "")).strip().lower()
    explicit_foot = foot in {"yes", "designated", "permissive", "official"}
    if highway == "service" or highway == "cycleway":
        if not explicit_foot:
            return False
    elif highway not in allowed:
        return False
    if access in {"no", "private", "customers", "delivery"}:
        return False
    if foot and not explicit_foot:
        return False
    if str(tags.get("construction", "")).strip().lower() not in {"", "no", "false"}:
        return False
    if str(tags.get("access:conditional", "")).strip() or str(tags.get("foot:conditional", "")).strip():
        return False
    if str(tags.get("barrier", "")).strip().lower() in {
        "fence", "wall", "city_wall", "guard_rail", "retaining_wall", "ditch",
    }:
        return False
    if str(tags.get("railway", "")).strip().lower() in {
        "rail", "subway", "tram", "light_rail", "monorail", "narrow_gauge",
    }:
        return False
    return True


def _geometry_segments(geometry: Mapping[str, Any], center: Coordinate
                       ) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]],
                                  list[list[list[tuple[float, float]]]]]:
    """Return projected line edges and polygon rings grouped by polygon."""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    line_parts: list[list[tuple[float, float]]] = []
    polygons: list[list[list[tuple[float, float]]]] = []

    def line(raw: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
        return [_project(_coordinate(point), center) for point in raw]

    if kind == "LineString":
        line_parts.append(line(coordinates))
    elif kind == "MultiLineString":
        line_parts.extend(line(part) for part in coordinates)
    elif kind == "Polygon":
        polygons.append([line(ring) for ring in coordinates])
    elif kind == "MultiPolygon":
        polygons.extend([[line(ring) for ring in polygon] for polygon in coordinates])
    elif kind == "GeometryCollection":
        for child in geometry.get("geometries", []):
            child_lines, child_rings = _geometry_segments(child, center)
            line_parts.extend([[edge[0], edge[1]] for edge in child_lines])
            polygons.extend(child_rings)
    else:
        raise ValueError(f"unsupported barrier geometry type: {kind!r}")
    if (any(len(part) < 2 for part in line_parts)
            or any(len(ring) < 4 for polygon in polygons for ring in polygon)):
        raise ValueError("barrier geometry has too few coordinates")
    return [edge for part in line_parts for edge in _line_segments(part)], polygons


def _point_in_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> bool:
    inside = False
    x, y = point
    for (x1, y1), (x2, y2) in _line_segments(ring):
        if ((y1 > y) != (y2 > y)):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _connector_crosses_barrier(start: Coordinate, end: Coordinate,
                               barriers: Sequence[Barrier]) -> bool:
    center = start
    a, b = _project(start, center), _project(end, center)
    for barrier in barriers:
        line_edges, polygons = _geometry_segments(barrier.geometry, center)
        if any(_segments_intersect(a, b, c, d) for c, d in line_edges):
            return True
        for polygon in polygons:
            if not polygon:
                continue
            if any(_segments_intersect(a, b, c, d)
                   for ring in polygon for c, d in _line_segments(ring)):
                return True
            for point in (a, b):
                if (_point_in_ring(point, polygon[0])
                        and not any(_point_in_ring(point, hole) for hole in polygon[1:])):
                    return True
    return False


def _valid_network_route(route: Mapping[str, Any], policy: ConnectorPolicy) -> tuple[
        float, float, int, list[list[float]], list[int] | None] | None:
    if route.get("status") != "routed":
        return None
    try:
        distance_m = float(route["distance_m"])
        minutes = float(route["estimated_minutes"])
        duration_seconds = float(route.get("_duration_seconds", minutes * 60.0))
        raw_geometry = route["geometry"]
        geometry = [[*_coordinate(point)] for point in raw_geometry]
    except (KeyError, TypeError, ValueError):
        return None
    if (not math.isfinite(distance_m) or distance_m < 0 or not math.isfinite(minutes)
            or minutes < 0 or not math.isfinite(duration_seconds) or duration_seconds < 0
            or len(geometry) < 2):
        return None
    snaps = route.get("snap_distance_m")
    normalized_snaps: list[int] | None = None
    if isinstance(snaps, Sequence) and not isinstance(snaps, (str, bytes)) and snaps:
        try:
            normalized_snaps = [round(float(value)) for value in snaps[:2]]
            if not all(math.isfinite(float(value)) and float(value) >= 0 for value in snaps[:2]):
                return None
            if float(snaps[0]) > policy.network_snap_limit_m:
                return None
        except (TypeError, ValueError):
            return None
    return distance_m, duration_seconds, int(math.ceil(minutes)), geometry, normalized_snaps


def route_with_origin_access(
    origin: Origin,
    candidates: Sequence[WalkSegment],
    barriers: Sequence[Barrier],
    route_network: Callable[[Coordinate], Mapping[str, Any]],
    *,
    policy: ConnectorPolicy = DEFAULT_POLICY,
) -> dict[str, Any] | None:
    """Add a bounded estimated connector, then route on the normal walk graph.

    Candidates must already be correlated to the local pedestrian graph and
    joined to OSM way tags. `None` means the caller should preserve its existing
    unavailable/no-route result; no air-distance fallback is produced.
    """
    origin = Origin(_coordinate(origin.coordinate),
                    tuple(_coordinate(point) for point in origin.footprint_ring)
                    if origin.footprint_ring else None)
    if (policy.max_access_distance_m <= 0 or policy.max_candidates < 1
            or policy.max_graph_to_way_distance_m < 0 or policy.walking_speed_mps <= 0
            or policy.network_snap_limit_m < 0
            or not isinstance(policy.max_candidates, int)
            or isinstance(policy.max_candidates, bool)
            or policy.max_candidates > _MAX_CONNECTOR_CANDIDATES
            or policy.max_access_distance_m > 100.0
            or not all(math.isfinite(value) for value in (
                policy.max_access_distance_m, policy.max_graph_to_way_distance_m,
                policy.walking_speed_mps, policy.network_snap_limit_m))):
        raise ValueError("connector policy limits must be positive")
    if len(barriers) > _MAX_LOCAL_BARRIERS:
        raise ValueError("barriers must be spatially prefiltered around the origin")
    possible: list[tuple[float, WalkSegment, Coordinate, Coordinate]] = []
    for segment in islice(candidates, _MAX_INPUT_CANDIDATES):
        if not _suitable_segment(segment.tags):
            continue
        connector = _nearest_connector(origin, segment, policy)
        if connector is None:
            continue
        start, end, distance_m = connector
        if distance_m > policy.max_access_distance_m:
            continue
        if _connector_crosses_barrier(start, end, barriers):
            continue
        possible.append((distance_m, segment, start, end))
    possible.sort(key=lambda row: (row[0], row[1].source_id))

    successful: list[tuple[float, int, dict[str, Any]]] = []
    for distance_m, segment, start, end in possible[:policy.max_candidates]:
        try:
            route = route_network(end)
        except Exception:
            continue
        valid = _valid_network_route(route, policy)
        if valid is None:
            continue
        network_distance_m, network_seconds, network_minutes, network_geometry, network_snaps = valid
        access_seconds = distance_m / policy.walking_speed_mps
        access_minutes = math.ceil(access_seconds / 60)
        total_distance = round(distance_m + network_distance_m)
        # Round only once after summing exact connector and Valhalla seconds;
        # adding already-ceiled component minutes systematically overstates.
        total_minutes = math.ceil((access_seconds + network_seconds) / 60)
        result = {
            # Keep the long-standing route status so NearbyIndex ranking and
            # API clients continue to treat this as a usable network route.
            # The separate routing_basis/origin_access fields disclose the
            # estimated connector.
            "status": "routed",
            "origin_access": {
                "status": "estimated",
                "approximate": True,
                "method": "straight_line_connector_to_graph_correlated_walkable_osm_segment",
                "geometry": [[round(start[0], 7), round(start[1], 7)],
                             [round(end[0], 7), round(end[1], 7)]],
                "distance_m": round(distance_m),
                "estimated_minutes": access_minutes,
                "source_segment_id": segment.source_id,
                "access_coordinate": {"lon": round(end[0], 7), "lat": round(end[1], 7)},
                "barrier_check": "known_features_only",
                "caveat": "Approximate approach; internal paths, entrances, gates, and live closures are not verified.",
                "policy_version": CONNECTOR_POLICY_VERSION,
            },
            "network_geometry": network_geometry,
            "network_distance_m": round(network_distance_m),
            "network_estimated_minutes": network_minutes,
            "network_snap_distance_m": network_snaps,
            "distance_m": total_distance,
            "estimated_minutes": total_minutes,
            "approximate": True,
            "routing_basis": "local_pedestrian_graph_with_estimated_origin_connector",
        }
        successful.append((total_distance, total_minutes, result))
    if not successful:
        return None
    return min(successful, key=lambda row: (row[0], row[1]))[2]


def connector_cache_key(base_key: tuple[Any, ...], *, roads_revision: str,
                        barriers_revision: str) -> tuple[Any, ...]:
    """Namespace cached results by connector policy and both local snapshots."""
    if not roads_revision or not barriers_revision:
        raise ValueError("connector source revisions are required for cache keys")
    return (*base_key, "origin_access", CONNECTOR_POLICY_VERSION,
            roads_revision, barriers_revision)
