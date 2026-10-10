"""Small, dependency-free geometry helpers for explainable environmental proxies.

Distances are local equirectangular approximations in metres. They are suitable
for nearby feature discovery at Moscow scale, not cadastral surveying.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

EARTH_RADIUS_M = 6_371_008.8
METHOD = "local_equirectangular_point_to_geometry_v1"


def _point(value: Sequence[float]) -> tuple[float, float]:
    if len(value) < 2:
        raise ValueError("point must contain longitude and latitude")
    lon, lat = float(value[0]), float(value[1])
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("point must be finite WGS84 longitude/latitude")
    return lon, lat


def _position(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2]
    )


def _segments(coords: Any) -> Iterable[tuple[Sequence[float], Sequence[float]]]:
    """Yield consecutive line segments from nested GeoJSON coordinates."""
    if not isinstance(coords, (list, tuple)):
        return
    if coords and _position(coords[0]):
        for start, end in zip(coords, coords[1:]):
            yield start, end
        return
    for child in coords:
        yield from _segments(child)


def _local_xy(lonlat: Sequence[float], origin: tuple[float, float]) -> tuple[float, float]:
    lon, lat = _point(lonlat)
    lon0, lat0 = origin
    x = EARTH_RADIUS_M * math.radians(lon - lon0) * math.cos(math.radians(lat0))
    y = EARTH_RADIUS_M * math.radians(lat - lat0)
    return x, y


def _segment_distance_m(
    origin: tuple[float, float], a: Sequence[float], b: Sequence[float]
) -> float:
    ax, ay = _local_xy(a, origin)
    bx, by = _local_xy(b, origin)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def _ring_contains(point: tuple[float, float], ring: Any) -> bool:
    """Ray casting in lon/lat, used only to detect distance zero inside areas."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        return False
    x, y = point
    inside = False
    previous = ring[-1]
    for current in ring:
        if not (_position(current) and _position(previous)):
            previous = current
            continue
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _polygon_contains(point: tuple[float, float], polygon: Any) -> bool:
    if not isinstance(polygon, (list, tuple)) or not polygon:
        return False
    if not _ring_contains(point, polygon[0]):
        return False
    return not any(_ring_contains(point, hole) for hole in polygon[1:])


def _geometry_contains(point: tuple[float, float], geometry: Mapping[str, Any]) -> bool:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Polygon":
        return _polygon_contains(point, coordinates)
    if kind == "MultiPolygon":
        return any(_polygon_contains(point, polygon) for polygon in coordinates or [])
    if kind == "GeometryCollection":
        return any(
            _geometry_contains(point, child)
            for child in geometry.get("geometries", [])
            if isinstance(child, Mapping)
        )
    return False


def distance_to_geometry_m(
    point_lonlat: Sequence[float], geometry: Mapping[str, Any]
) -> float:
    """Return approximate metric distance to a GeoJSON geometry.

    Points on or inside a Polygon/MultiPolygon return zero. Lines and polygon
    boundaries are measured by their consecutive coordinate segments.
    """
    origin = _point(point_lonlat)
    kind = geometry.get("type")
    if kind == "GeometryCollection":
        distances = [
            distance_to_geometry_m(origin, child)
            for child in geometry.get("geometries", [])
            if isinstance(child, Mapping)
        ]
        if not distances:
            raise ValueError("empty GeometryCollection")
        return min(distances)
    if kind not in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
        raise ValueError(f"unsupported GeoJSON geometry type: {kind!r}")
    if _geometry_contains(origin, geometry):
        return 0.0
    if kind == "Point":
        positions = [geometry.get("coordinates")]
    elif kind == "MultiPoint":
        positions = geometry.get("coordinates", [])
    else:
        positions = []
    distances = []
    for candidate in positions:
        if _position(candidate):
            x, y = _local_xy(candidate, origin)
            distances.append(math.hypot(x, y))
    for start, end in _segments(geometry.get("coordinates")):
        distances.append(_segment_distance_m(origin, start, end))
    if not distances:
        raise ValueError("geometry has no usable coordinates")
    return min(distances)


def nearest_feature_distance(
    point_lonlat: Sequence[float],
    features: Iterable[Mapping[str, Any]],
    *,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """Find the nearest feature, preserving its ID and relevant source fields."""
    origin = _point(point_lonlat)
    best: tuple[float, Mapping[str, Any]] | None = None
    for feature in features:
        if predicate is not None and not predicate(feature):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        distance = distance_to_geometry_m(origin, geometry)
        if best is None or distance < best[0]:
            best = distance, feature
    if best is None:
        return None
    distance, feature = best
    properties = feature.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    return {
        "value_m": round(distance, 1),
        "status": "derived",
        "method": METHOD,
        "approximate": True,
        "reference_feature_id": feature.get("id", properties.get("source_id")),
        "reference_properties": {
            key: properties[key]
            for key in ("class", "subtype", "source", "source_release", "source_version", "osm_type", "osm_id", "osm_version", "tags_raw")
            if key in properties
        },
        "geometry_type": feature["geometry"].get("type"),
    }


def count_features_within_m(
    point_lonlat: Sequence[float],
    features: Iterable[Mapping[str, Any]],
    radius_m: float,
    *,
    predicate: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Count source geometries inside a radius; this measures mapped presence only."""
    if not math.isfinite(radius_m) or radius_m < 0:
        raise ValueError("radius_m must be finite and non-negative")
    origin = _point(point_lonlat)
    count = 0
    ids: list[Any] = []
    for feature in features:
        if predicate is not None and not predicate(feature):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, Mapping):
            continue
        if distance_to_geometry_m(origin, geometry) <= radius_m:
            count += 1
            props = feature.get("properties")
            props = props if isinstance(props, Mapping) else {}
            ids.append(feature.get("id", props.get("source_id")))
    return {
        "value": count,
        "status": "observed_source_count",
        "radius_m": radius_m,
        "method": METHOD,
        "approximate": True,
        "mapped_feature_ids": ids,
        "absence_semantics": "none found in examined source snapshot and coverage; not confirmed real-world absence",
    }


def geometry_centroid_lonlat(geometry: Mapping[str, Any]) -> tuple[float, float]:
    """Return a planar coordinate centroid for local footprint anchoring.

    Polygon area moments include holes. The calculation is in longitude/latitude
    coordinates and is intended for city-scale lookup anchors, not surveying.
    """
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "Point":
        return _point(coordinates)
    if kind == "MultiPoint":
        points = [_point(item) for item in coordinates or [] if _position(item)]
        if not points:
            raise ValueError("MultiPoint has no usable coordinates")
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    if kind == "LineString":
        points = [_point(item) for item in coordinates or [] if _position(item)]
        if not points:
            raise ValueError("LineString has no usable coordinates")
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)

    def ring_moment(ring: Any) -> tuple[float, float, float]:
        if not isinstance(ring, (list, tuple)) or len(ring) < 4:
            return 0.0, 0.0, 0.0
        twice_area = cx = cy = 0.0
        points = [_point(item) for item in ring]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            cross = x1 * y2 - x2 * y1
            twice_area += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if abs(twice_area) < 1e-18:
            return 0.0, 0.0, 0.0
        area = abs(twice_area) / 2
        return area, cx / (3 * twice_area), cy / (3 * twice_area)

    def polygon_moment(polygon: Any) -> tuple[float, float, float]:
        if not isinstance(polygon, (list, tuple)) or not polygon:
            return 0.0, 0.0, 0.0
        outer_area, outer_x, outer_y = ring_moment(polygon[0])
        total_area = outer_area
        total_x = outer_area * outer_x
        total_y = outer_area * outer_y
        for hole in polygon[1:]:
            hole_area, hole_x, hole_y = ring_moment(hole)
            total_area -= hole_area
            total_x -= hole_area * hole_x
            total_y -= hole_area * hole_y
        if total_area <= 0:
            return 0.0, 0.0, 0.0
        return total_area, total_x / total_area, total_y / total_area

    if kind == "Polygon":
        area, x, y = polygon_moment(coordinates)
    elif kind == "MultiPolygon":
        moments = [polygon_moment(polygon) for polygon in coordinates or []]
        area = sum(moment[0] for moment in moments)
        if area <= 0:
            raise ValueError("MultiPolygon has no usable area")
        x = sum(a * cx for a, cx, _ in moments) / area
        y = sum(a * cy for a, _, cy in moments) / area
    else:
        raise ValueError(f"unsupported centroid geometry type: {kind!r}")
    if area <= 0:
        raise ValueError("geometry has no usable area")
    return _point((x, y))
