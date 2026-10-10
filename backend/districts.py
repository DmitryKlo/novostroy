"""Mapped district labels for uniquely covered, verified registry points.

The OSM/Overture candidate boundaries are a cartographic classification,
not a certified legal district layer. No address or nearest-polygon guess is
made for records without a confirmed point or with overlapping boundaries.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.store import ROOT


DISTRICT_DIR = ROOT / "data/city/environment"
GEOJSON_PATH = DISTRICT_DIR / "moscow_district_candidates_2026-09-23.1.geojson"
MANIFEST_PATH = DISTRICT_DIR / "moscow_district_candidates_2026-09-23.1.manifest.json"


def _on_segment(lon: float, lat: float, a: list[float], b: list[float]) -> bool:
    ax, ay = a
    bx, by = b
    cross = (lon - ax) * (by - ay) - (lat - ay) * (bx - ax)
    return (abs(cross) <= 1e-10 and min(ax, bx) - 1e-12 <= lon <= max(ax, bx) + 1e-12
            and min(ay, by) - 1e-12 <= lat <= max(ay, by) + 1e-12)


def _ring_position(lon: float, lat: float, ring: list[list[float]]) -> str:
    inside = False
    for a, b in zip(ring, ring[1:]):
        if _on_segment(lon, lat, a, b):
            return "boundary"
        if (a[1] > lat) != (b[1] > lat):
            crossing = a[0] + (lat - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if lon < crossing:
                inside = not inside
    return "inside" if inside else "outside"


def _polygon_covers(lon: float, lat: float, rings: list[list[list[float]]]) -> bool:
    exterior = _ring_position(lon, lat, rings[0])
    if exterior == "outside":
        return False
    if exterior == "boundary":
        return True
    for hole in rings[1:]:
        position = _ring_position(lon, lat, hole)
        if position == "boundary":
            return True
        if position == "inside":
            return False
    return True


def _geometry_covers(lon: float, lat: float, geometry: dict[str, Any]) -> bool:
    coordinates = geometry["coordinates"]
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    return any(_polygon_covers(lon, lat, rings) for rings in polygons)


def _valid_geometry(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("type") not in {"Polygon", "MultiPolygon"}:
        return False
    polygons = [value.get("coordinates")] if value["type"] == "Polygon" else value.get("coordinates")
    if not isinstance(polygons, list) or not polygons:
        return False
    for rings in polygons:
        if not isinstance(rings, list) or not rings:
            return False
        for ring in rings:
            if (not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]
                    or any(not isinstance(pair, list) or len(pair) != 2
                           or any(type(value) not in {int, float} for value in pair) for pair in ring)):
                return False
    return True


@lru_cache(maxsize=2)
def _load_boundaries(geo_mtime_ns: int, geo_size: int,
                     manifest_mtime_ns: int, manifest_size: int) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("output_file") != GEOJSON_PATH.name:
        raise ValueError("Mapped district manifest is invalid")
    expected_hash = manifest.get("output_sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("Mapped district hash is missing")
    payload = GEOJSON_PATH.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("Mapped district boundaries do not match manifest")
    document = json.loads(payload)
    features = document.get("features") if isinstance(document, dict) else None
    if (not isinstance(features, list) or len(features) != manifest.get("feature_count")
            or document.get("source_release") != manifest.get("source_release")):
        raise ValueError("Mapped district feature count or version differs")
    boundaries: list[tuple[str, dict[str, Any]]] = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("Mapped district feature is invalid")
        name = feature["properties"].get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Mapped district name is missing")
        geometry = feature.get("geometry")
        if not _valid_geometry(geometry):
            raise ValueError("Mapped district polygon is invalid")
        boundaries.append((name.strip(), geometry))
    if len({name for name, _ in boundaries}) != len(boundaries):
        raise ValueError("Mapped district names are duplicated")
    return manifest["source_release"], boundaries


def classify_verified_points(items: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    """Return only unique polygon-cover labels, keyed by opaque registry ID."""
    geo_stat, manifest_stat = GEOJSON_PATH.stat(), MANIFEST_PATH.stat()
    release, boundaries = _load_boundaries(geo_stat.st_mtime_ns, geo_stat.st_size,
                                          manifest_stat.st_mtime_ns, manifest_stat.st_size)
    classified: dict[str, str] = {}
    for item in items:
        if item["location"] is None:
            continue
        lon, lat = item["location"]
        matches = [name for name, polygon in boundaries if _geometry_covers(lon, lat, polygon)]
        if len(matches) == 1:
            classified[item["registry_id"]] = matches[0]
    return release, classified
