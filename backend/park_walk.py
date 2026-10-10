"""Bounded walking distance to an OSM-mapped park boundary.

A polygon boundary is not a verified gate. A result is returned only when the
local pedestrian graph actually routes to a point within a short, checked
gap of that boundary. No polygon centroid or air distance becomes a route.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Sequence

from shapely import from_wkb
from shapely.geometry import LineString, Point, box, shape as geo_shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree
import pyarrow.parquet as parquet

from backend.origin_access import Barrier


ROOT = Path(__file__).resolve().parents[1]
PARK_PATH = ROOT / "data/social/osm_moscow_parks_261008_v2.parquet"
SURFACES_MANIFEST = ROOT / "data/social/osm_moscow_surfaces_261008_v2.manifest.json"
MAX_PARKS = 5
MAX_ROUTE_ATTEMPTS = 15
MAX_PARK_SEARCH_M = 4_000
MAX_GRAPH_BOUNDARY_GAP_M = 15.0
ATTRIBUTION = "© OpenStreetMap contributors"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    value = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 12_742_017.6 * math.asin(min(1.0, math.sqrt(value)))


def _coordinate(point: Point) -> tuple[float, float]:
    return float(point.x), float(point.y)


@dataclass(frozen=True)
class Park:
    source_id: str
    name: str | None
    geometry: Any

    @property
    def public_id(self) -> str:
        return "park_" + hashlib.sha256(self.source_id.encode("utf-8")).hexdigest()[:24]


class ParkIndex:
    def __init__(self, parks: Sequence[Park], *, snapshot_at: str,
                 source_sha256: str) -> None:
        self.parks = tuple(parks)
        self.tree = STRtree([park.geometry for park in self.parks])
        self.snapshot_at = snapshot_at
        self.source_sha256 = source_sha256

    @classmethod
    def from_local_files(cls, *, expected_source_sha256: str) -> "ParkIndex":
        manifest = json.loads(SURFACES_MANIFEST.read_text(encoding="utf-8"))
        meta = manifest["layers"]["parks"]
        if (manifest["source"]["sha256"] != expected_source_sha256
                or meta["file"] != str(PARK_PATH.relative_to(ROOT))
                or _sha256(PARK_PATH) != meta["sha256"]):
            raise ValueError("Park surface snapshot differs from pedestrian graph source")
        parks: list[Park] = []
        for batch in parquet.ParquetFile(PARK_PATH).iter_batches(
                columns=["id", "name", "feature_class", "tags_json", "geometry"],
                batch_size=512):
            for row in batch.to_pylist():
                try:
                    tags = json.loads(row.get("tags_json") or "{}")
                    geometry = from_wkb(row["geometry"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (row.get("feature_class") not in {"park", "garden"}
                        or tags.get("access") in {"private", "no"}
                        or geometry.geom_type not in {"Polygon", "MultiPolygon"}
                        or geometry.is_empty or not geometry.is_valid):
                    continue
                source_id = row.get("id")
                if not isinstance(source_id, str) or not source_id.startswith("osm:"):
                    continue
                name = row.get("name")
                if not isinstance(name, str) or not 0 < len(name.strip()) <= 160:
                    name = None
                parks.append(Park(source_id, name, geometry))
        if not parks or len(parks) > meta["row_count"]:
            raise ValueError("Park polygons are missing or exceed the pinned source count")
        return cls(parks, snapshot_at=manifest["source"]["osm_snapshot_timestamp"],
                   source_sha256=meta["sha256"])

    def _nearby_parks(self, origin: tuple[float, float]) -> list[Park]:
        lon, lat = origin
        point = Point(lon, lat)
        candidates: set[int] = set()
        for radius_m in (400, 800, 1_600, MAX_PARK_SEARCH_M):
            lat_pad = radius_m / 110_540.0
            lon_pad = radius_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
            for index in self.tree.query(box(lon - lon_pad, lat - lat_pad,
                                             lon + lon_pad, lat + lat_pad)):
                candidates.add(int(index))
            if len(candidates) >= MAX_PARKS:
                break
        ranked = []
        for index in candidates:
            park = self.parks[index]
            boundary = park.geometry.boundary
            if boundary.is_empty:
                continue
            nearby = nearest_points(point, boundary)[1]
            distance = _distance_m(origin, _coordinate(nearby))
            if distance <= MAX_PARK_SEARCH_M:
                ranked.append((distance, park.source_id, park))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [park for _, _, park in ranked[:MAX_PARKS]]

    @staticmethod
    def _boundary_candidates(park: Park,
                             origin: tuple[float, float]) -> list[tuple[float, float]]:
        point = Point(*origin)
        polygons = ([park.geometry] if park.geometry.geom_type == "Polygon"
                    else list(park.geometry.geoms))
        rings = [polygon.exterior for polygon in polygons if not polygon.exterior.is_empty]
        rings.sort(key=lambda ring: ring.distance(point))
        if not rings:
            return []
        # Three local points on the nearest outer boundary, about 60 m apart.
        # Route results, not these geometric samples, decide eligibility.
        ring = rings[0]
        position = ring.project(point)
        offset = 60.0 / 90_000.0
        result: list[tuple[float, float]] = []
        for delta in (0.0, -offset, offset):
            coordinate = _coordinate(ring.interpolate((position + delta) % ring.length))
            if all(_distance_m(coordinate, previous) > 1.0 for previous in result):
                result.append(coordinate)
        return result

    @staticmethod
    def _gap_blocked(endpoint: tuple[float, float], boundary: tuple[float, float],
                     barriers: Sequence[Barrier]) -> bool:
        if _distance_m(endpoint, boundary) < 0.5:
            return False
        connector = LineString([endpoint, boundary])
        for barrier in barriers:
            try:
                if connector.intersects(geo_shape(barrier.geometry)):
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def walk(self, origin: tuple[float, float],
             route: Callable[[tuple[float, float], dict[str, Any]], dict[str, Any]],
             barrier_lookup: Callable[[tuple[float, float]], Sequence[Barrier]] | None,
             *, dataset_id: str) -> dict[str, Any]:
        best: tuple[int, str, dict[str, Any]] | None = None
        attempted = 0
        for park in self._nearby_parks(origin):
            for boundary in self._boundary_candidates(park, origin):
                if attempted >= MAX_ROUTE_ATTEMPTS:
                    break
                attempted += 1
                target = {"coordinate": boundary,
                          "coordinate_basis": "park_outer_boundary_sample",
                          "access_kind": "boundary_approach_unverified"}
                result = route(origin, target)
                if result.get("status") != "routed" or not isinstance(result.get("geometry"), list):
                    continue
                geometry = result["geometry"]
                if not geometry:
                    continue
                endpoint = tuple(geometry[-1])
                gap_m = _distance_m(endpoint, boundary)
                if gap_m > MAX_GRAPH_BOUNDARY_GAP_M:
                    continue
                # A graph route is useful only when its short final approach
                # does not cross a mapped water barrier. Unknown gates/fences
                # remain explicitly unverified.
                # Without pinned barrier coverage, only an effectively exact
                # graph endpoint is acceptable. A short unverified crossing
                # could otherwise jump a waterway or closed park edge.
                if barrier_lookup is None and gap_m >= 0.5:
                    continue
                if barrier_lookup is not None and self._gap_blocked(
                        endpoint, boundary, barrier_lookup(boundary)):
                    continue
                metres = result.get("distance_m")
                minutes = result.get("estimated_minutes")
                if (type(metres) is not int or metres < 0 or
                        type(minutes) is not int or minutes < 0):
                    continue
                value = {"status": "routed", "park": {"id": park.public_id,
                         "name": park.name}, "distance_m": metres,
                         "estimated_minutes": minutes,
                         "approximate": True, "endpoint_basis": "pedestrian_graph_near_park_boundary",
                         "boundary_gap_m": round(gap_m), "access_verified": False,
                         "origin_access": result.get("origin_access")}
                ranked = (metres, park.public_id, value)
                if best is None or ranked[:2] < best[:2]:
                    best = ranked
            if attempted >= MAX_ROUTE_ATTEMPTS:
                break
        base = {"dataset_id": dataset_id, "osm_snapshot_at": self.snapshot_at,
                "park_candidate_limit": MAX_PARKS, "route_attempts": attempted,
                "attribution": ATTRIBUTION}
        if best is None:
            return {**base, "status": "unavailable", "park": None,
                    "distance_m": None, "estimated_minutes": None,
                    "approximate": True, "endpoint_basis": None,
                    "boundary_gap_m": None, "access_verified": False,
                    "origin_access": None}
        return {**base, **best[2]}
