"""Bounded local POI lookup and pedestrian routes for selected map points.

The API process delegates to one lazy geodata worker because PyValhalla lives
in the isolated geodata environment. All inputs are local, snapshot-pinned
files; no network router, geocoder, or paid API is consulted.
"""

from __future__ import annotations

import json
import hashlib
import atexit
import copy
from datetime import datetime, timezone
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import math
import os
import re
import select
import signal
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.origin_access import (
    Barrier,
    Origin,
    WalkSegment,
    connector_cache_key,
    route_with_origin_access,
)


ROOT = Path(__file__).resolve().parents[1]
POI_PATH = ROOT / "data/social/osm_moscow_social_pois_261008.parquet"
POI_MANIFEST = ROOT / "data/social/osm_moscow_social_pois_261008.manifest.json"
GRAPH_CONFIG = ROOT / "data/social/valhalla_moscow/valhalla_pedestrian.json"
GRAPH_MANIFEST = ROOT / "data/social/valhalla_moscow/build_manifest.json"
GEO_PYTHON = Path(os.environ.get("NOVOSTROY_GEODATA_PYTHON", "")) if os.environ.get(
    "NOVOSTROY_GEODATA_PYTHON") else ROOT / ".runtime/geodata-env/bin/python"
OMS_SIDECAR = ROOT / "data/social/oms_verified_clinics_2026.json"
OMS_MANIFEST = ROOT / "data/social/oms_verified_clinics_2026.manifest.json"
METRO_SIDECAR = ROOT / "data/social/metro_station_access_261008.json"
METRO_MANIFEST = ROOT / "data/social/metro_station_access_261008.manifest.json"
ROADS_PATH = ROOT / "data/social/osm_moscow_roads_261008.parquet"
ROADS_MANIFEST = ROOT / "data/social/osm_moscow_roads_261008.manifest.json"
WATER_PATH = ROOT / "data/social/osm_moscow_water_261008_v2.parquet"
WATERWAYS_PATH = ROOT / "data/social/osm_moscow_waterways_261008_v2.parquet"
SURFACES_MANIFEST = ROOT / "data/social/osm_moscow_surfaces_261008_v2.manifest.json"
ROUTE_POOL = 8
ITEMS_PER_KIND = 2
VERIFIED_ROUTE_POOL = 6
METRO_ENTRANCE_ROUTE_POOL = 4
ROUTING_POLICY_REVISION = "walk-no-ferry-2x2-v4-route-first-origin-access-metro-groups"
METRO_LOCAL_IDENTITY_RADIUS_M = 25.0
ORIGIN_ACCESS_LOCATE_RADIUS_M = 100.0
ORIGIN_ACCESS_CONNECTOR_MAX_M = 100.0
ORIGIN_ACCESS_CACHE_SIZE = 128
MAX_ROUTE_POINTS = 512
MAX_VIEWPORT_POINTS = 150
MAX_VIEWPORT_SPAN = 0.15
GRID_DEGREES = 0.01
SNAP_LIMIT_M = 50.0
ATTRIBUTION = "© OpenStreetMap contributors"
WORKER_REQUEST_TIMEOUT_SECONDS = 35.0
WORKER_ADMISSION_WAIT_SECONDS = 0.75
WORKER_MAX_ADMITTED_REQUESTS = 8
WORKER_RESPONSE_CACHE_TTL_SECONDS = 20.0
WORKER_RESPONSE_CACHE_MAXSIZE = 128
WORKER_RESPONSE_CACHE_REVISION = 1
MAX_SINGLEFLIGHT_WAITERS = 16

_LOCK = threading.RLock()
_QUEUE = threading.BoundedSemaphore(WORKER_MAX_ADMITTED_REQUESTS)
_WORKER: subprocess.Popen[str] | None = None
_RESPONSE_CACHE_LOCK = threading.Lock()
_RESPONSE_CACHE: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()


@dataclass
class _WorkerFlight:
    future: Future[dict[str, Any]]
    waiters: int = 0


_WORKER_INFLIGHT: dict[tuple[Any, ...], _WorkerFlight] = {}


def _terminate_worker() -> None:
    global _WORKER
    worker, _WORKER = _WORKER, None
    if worker is None:
        return
    # The rtk wrapper can exit while its geodata child still owns the process
    # group. Always signal the whole group, even when the wrapper has exited.
    try:
        os.killpg(worker.pid, signal.SIGTERM)
        worker.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except OSError:
        pass
    # The wrapper can exit before the Python child; finish the owned group.
    try:
        os.killpg(worker.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        worker.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    for stream in (worker.stdin, worker.stdout):
        if stream is not None:
            stream.close()


atexit.register(_terminate_worker)


def _coordinates(lon: Any, lat: Any) -> tuple[float, float]:
    if isinstance(lon, bool) or isinstance(lat, bool):
        raise ValueError("coordinates must be numeric")
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinates must be numeric") from exc
    if not all(map(math.isfinite, (lon, lat))) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("coordinates must be finite WGS84 lon/lat")
    return lon, lat


def _education_filter(value: str) -> str:
    if value not in {"all", "school", "kindergarten"}:
        raise ValueError("education must be all, school, or kindergarten")
    return value


def _observed_not_future(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() <= datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _worker_cache_key(request: dict[str, Any]) -> tuple[Any, ...]:
    encoded_request = json.dumps(request, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
    return (WORKER_RESPONSE_CACHE_REVISION, ROUTING_POLICY_REVISION,
            _input_signature(), encoded_request)


def _worker_request(request: dict[str, Any]) -> dict[str, Any]:
    """Use a short-lived snapshot-bound cache and coalesce identical requests.

    This front door runs before admission control so browser views requesting
    the same point can share one result without occupying worker queue slots.
    Returned values are copied so a caller cannot mutate the cached response.
    """
    deadline = time.monotonic() + WORKER_REQUEST_TIMEOUT_SECONDS
    key = _worker_cache_key(request)
    now = time.monotonic()
    owner = False
    with _RESPONSE_CACHE_LOCK:
        cached = _RESPONSE_CACHE.get(key)
        if cached is not None:
            expires_at, value = cached
            if expires_at > now:
                _RESPONSE_CACHE.move_to_end(key)
                return copy.deepcopy(value)
            _RESPONSE_CACHE.pop(key, None)

        flight = _WORKER_INFLIGHT.get(key)
        if flight is None:
            flight = _WorkerFlight(Future())
            _WORKER_INFLIGHT[key] = flight
            owner = True
        elif flight.waiters >= MAX_SINGLEFLIGHT_WAITERS:
            raise RuntimeError("local nearby worker queue is busy")
        else:
            flight.waiters += 1

    if not owner:
        try:
            remaining = max(0.0, deadline - time.monotonic())
            return copy.deepcopy(flight.future.result(timeout=remaining))
        except FutureTimeoutError as exc:
            raise RuntimeError("local nearby worker timed out") from exc
        finally:
            with _RESPONSE_CACHE_LOCK:
                flight.waiters = max(0, flight.waiters - 1)

    try:
        result = _worker_exchange(request, deadline)
        if not isinstance(result, dict):
            raise RuntimeError("local nearby worker returned an invalid result")
        stored = copy.deepcopy(result)
        with _RESPONSE_CACHE_LOCK:
            _RESPONSE_CACHE[key] = (
                time.monotonic() + WORKER_RESPONSE_CACHE_TTL_SECONDS, stored)
            _RESPONSE_CACHE.move_to_end(key)
            while len(_RESPONSE_CACHE) > WORKER_RESPONSE_CACHE_MAXSIZE:
                _RESPONSE_CACHE.popitem(last=False)
            flight.future.set_result(stored)
        return copy.deepcopy(stored)
    except BaseException as exc:
        with _RESPONSE_CACHE_LOCK:
            if not flight.future.done():
                flight.future.set_exception(exc)
        raise
    finally:
        with _RESPONSE_CACHE_LOCK:
            if _WORKER_INFLIGHT.get(key) is flight:
                _WORKER_INFLIGHT.pop(key, None)


def _worker_exchange(request: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Admit bounded work, then serialize access to the single geodata child."""
    remaining = max(0.0, deadline - time.monotonic())
    admission_wait = min(WORKER_ADMISSION_WAIT_SECONDS, remaining)
    if not _QUEUE.acquire(timeout=admission_wait):
        raise RuntimeError("local nearby worker queue is busy")
    locked = False
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0 or not _LOCK.acquire(timeout=remaining):
            raise RuntimeError("local nearby worker timed out")
        locked = True
        return _worker_exchange_locked(request, deadline)
    finally:
        if locked:
            _LOCK.release()
        _QUEUE.release()


def _worker_exchange_locked(request: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Send one request to the worker; caller holds ``_LOCK``."""
    global _WORKER
    if _WORKER is None or _WORKER.poll() is not None:
        _terminate_worker()
        if not GEO_PYTHON.is_file():
            raise RuntimeError("local geodata Python environment is missing")
        # The local workstation uses rtk; a production image supplies its
        # geodata Python explicitly and has no workstation command proxy.
        configured_python = os.environ.get("NOVOSTROY_GEODATA_PYTHON")
        if configured_python:
            command = [str(GEO_PYTHON), "-u", "-m", "backend.nearby", "--worker"]
        else:
            rtk = shutil.which("rtk")
            if rtk is None:
                raise RuntimeError("rtk command proxy is missing")
            command = [rtk, "proxy", str(GEO_PYTHON), "-u", "-m", "backend.nearby", "--worker"]
        _WORKER = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    assert _WORKER.stdin is not None and _WORKER.stdout is not None
    try:
        _WORKER.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
        _WORKER.stdin.flush()
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([_WORKER.stdout], [], [], remaining)
        if not readable:
            _terminate_worker()
            raise RuntimeError("local nearby worker timed out")
        line = _WORKER.stdout.readline()
        if not line:
            _terminate_worker()
            raise RuntimeError("local nearby worker exited")
        result = json.loads(line)
    except (BrokenPipeError, OSError, json.JSONDecodeError) as exc:
        _terminate_worker()
        raise RuntimeError("local nearby worker failed") from exc
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


def nearby_for_point(
    lon: float,
    lat: float,
    *,
    origin_id: str,
    education: str = "all",
) -> dict[str, Any]:
    """Return up to two per kind for an API-validated source-backed point.

    The caller must resolve ``origin_id`` against public map markers first.
    Coordinates are source mapped positions, not verified building entrances.
    """
    lon, lat = _coordinates(lon, lat)
    if not isinstance(origin_id, str) or not origin_id or len(origin_id) > 128:
        raise ValueError("origin_id must be a bounded opaque point ID")
    return _worker_request({"kind": "point", "lon": lon, "lat": lat,
                            "point_id": origin_id, "education": _education_filter(education)})


def park_walk_for_point(lon: float, lat: float, *, origin_id: str) -> dict[str, Any]:
    """Route from a current map point to a mapped park boundary approach.

    The API caller must validate the opaque origin against the active map.
    Park boundaries are route destinations, not asserted entrances.
    """
    lon, lat = _coordinates(lon, lat)
    if not isinstance(origin_id, str) or not origin_id or len(origin_id) > 128:
        raise ValueError("origin_id must be a bounded opaque point ID")
    return _worker_request({"kind": "park", "lon": lon, "lat": lat,
                            "point_id": origin_id})


def nearby_in_viewport(
    bbox: tuple[float, float, float, float],
    *,
    layers: tuple[str, ...] = ("metro", "education", "medical"),
    education: str = "all",
    limit: int = MAX_VIEWPORT_POINTS,
) -> dict[str, Any]:
    """Return a capped viewport slice without calculating walking routes."""
    if len(bbox) != 4:
        raise ValueError("bbox must contain four coordinates")
    west, south = _coordinates(bbox[0], bbox[1])
    east, north = _coordinates(bbox[2], bbox[3])
    if not (west < east and south < north):
        raise ValueError("bbox must have increasing coordinates")
    if east - west > MAX_VIEWPORT_SPAN or north - south > MAX_VIEWPORT_SPAN:
        raise ValueError("bbox exceeds supported viewport span")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_VIEWPORT_POINTS:
        raise ValueError("limit must be between 1 and 150")
    if not layers or len(layers) > 3 or len(set(layers)) != len(layers) or any(
        layer not in {"metro", "education", "medical"} for layer in layers
    ):
        raise ValueError("layers must be distinct metro, education, or medical")
    return _worker_request({"kind": "viewport", "bbox": [west, south, east, north],
                            "layers": list(layers), "education": _education_filter(education),
                            "limit": limit})


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12_742_017.6 * math.asin(min(1.0, math.sqrt(value)))


def _decode_polyline6(encoded: str) -> list[list[float]]:
    """Decode Valhalla's six-decimal encoded route shape to lon/lat pairs."""
    output: list[list[float]] = []
    numbers: list[int] = []
    index = 0
    while index < len(encoded):
        shift = value = 0
        while True:
            if index >= len(encoded):
                raise ValueError("truncated route polyline")
            chunk = ord(encoded[index]) - 63
            index += 1
            if not 0 <= chunk <= 63 or shift > 35:
                raise ValueError("invalid route polyline")
            value |= (chunk & 31) << shift
            shift += 5
            if chunk < 32:
                break
        numbers.append((value >> 1) ^ -(value & 1))
    if len(numbers) % 2:
        raise ValueError("route polyline has odd coordinates")
    lat = lon = 0
    for lat_delta, lon_delta in zip(numbers[::2], numbers[1::2], strict=True):
        lat += lat_delta
        lon += lon_delta
        coordinate = [round(lon / 1_000_000, 6), round(lat / 1_000_000, 6)]
        _coordinates(*coordinate)
        output.append(coordinate)
    return output


def _bounded_route_geometry(points: list[list[float]]) -> tuple[list[list[float]], float]:
    """Retain route turns using RDP and cap output at 512 coordinate pairs."""
    if len(points) <= MAX_ROUTE_POINTS:
        return points, 0.0
    latitude = math.radians(sum(point[1] for point in points) / len(points))
    scale_x = 111_320 * math.cos(latitude)
    scale_y = 111_320
    projected = [(point[0] * scale_x, point[1] * scale_y) for point in points]

    def simplify(tolerance: float) -> list[int]:
        keep = {0, len(points) - 1}
        stack = [(0, len(points) - 1)]
        while stack:
            first, last = stack.pop()
            ax, ay = projected[first]
            bx, by = projected[last]
            dx, dy = bx - ax, by - ay
            denominator = dx * dx + dy * dy
            best_distance = -1.0
            best_index = -1
            for index in range(first + 1, last):
                px, py = projected[index]
                fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator)) if denominator else 0.0
                distance = math.hypot(px - (ax + fraction * dx), py - (ay + fraction * dy))
                if distance > best_distance:
                    best_distance, best_index = distance, index
            if best_distance > tolerance and best_index >= 0:
                keep.add(best_index)
                stack.extend(((first, best_index), (best_index, last)))
        return sorted(keep)

    tolerance = 1.0
    indices = simplify(tolerance)
    while len(indices) > MAX_ROUTE_POINTS:
        tolerance *= 2
        indices = simplify(tolerance)
    return [points[index] for index in indices], tolerance


def _grid_key(lon: float, lat: float) -> tuple[int, int]:
    return math.floor(lon / GRID_DEGREES), math.floor(lat / GRID_DEGREES)


class NearbyIndex:
    """A once-loaded point grid and one lazy local pedestrian graph actor."""

    def __init__(self, records: list[dict[str, Any]], *, dataset_id: str,
                 osm_snapshot_at: str, oms_records: dict[str, dict[str, Any]] | None = None,
                 metro_links: dict[str, list[tuple[float, float]]] | None = None,
                 metro_group_ids: dict[str, str] | None = None,
                 actor_factory: Any = None,
                 walk_way_lookup: Callable[[Sequence[dict[str, Any]]], Sequence[WalkSegment]] | None = None,
                 barrier_lookup: Callable[[tuple[float, float]], Sequence[Barrier]] | None = None,
                 roads_revision: str = "unavailable",
                 barriers_revision: str = "unavailable",
                 barrier_coverage: str = "unavailable") -> None:
        self.dataset_id = dataset_id
        self.osm_snapshot_at = osm_snapshot_at
        self.oms_records = oms_records or {}
        self.metro_links = metro_links or {}
        self.metro_group_ids = metro_group_ids or {}
        self.actor_factory = actor_factory
        self.walk_way_lookup = walk_way_lookup
        self.barrier_lookup = barrier_lookup
        self.roads_revision = roads_revision
        self.barriers_revision = barriers_revision
        self.barrier_coverage = barrier_coverage
        self.actor: Any = None
        self.graph_valid = _graph_version() not in {"missing", "invalid"}
        self.records: list[dict[str, Any]] = []
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self.route_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self.origin_access_cache: OrderedDict[
            tuple[float, float], tuple[list[WalkSegment], list[Barrier]]
        ] = OrderedDict()
        self.actor_calls = 0
        for source in records:
            if source.get("category") not in {"metro_station", "school", "kindergarten", "clinic"}:
                continue
            try:
                lon, lat = _coordinates(*source["coordinate"])
            except (KeyError, TypeError, ValueError):
                continue
            row = {**source, "coordinate": (lon, lat)}
            self.grid[_grid_key(lon, lat)].append(len(self.records))
            self.records.append(row)

    @classmethod
    def from_local_files(cls) -> "NearbyIndex":
        import pyarrow.dataset as arrow_dataset
        import pyarrow.parquet as parquet
        from shapely import from_wkb
        from shapely.geometry import box, mapping
        from shapely.strtree import STRtree

        if not POI_PATH.is_file() or not POI_MANIFEST.is_file():
            raise RuntimeError("local citywide POI snapshot is missing")
        manifest = json.loads(POI_MANIFEST.read_text(encoding="utf-8"))
        source = manifest["source"]
        rows_by_source: dict[str, tuple[int, dict[str, Any]]] = {}
        entrances: dict[str, tuple[float, float]] = {}
        columns = ["id", "osm_type", "osm_id", "osm_version", "osm_timestamp",
                   "category", "name", "geometry", "access_point", "access_kind"]
        for batch in parquet.ParquetFile(POI_PATH).iter_batches(columns=columns, batch_size=4096):
            for row in batch.to_pylist():
                if row["category"] == "metro_entrance" and row["access_kind"] == "entrance_known" and row["access_point"]:
                    entrance = from_wkb(row["access_point"])
                    if entrance.geom_type == "Point" and not entrance.is_empty:
                        entrances[row["id"]] = _coordinates(entrance.x, entrance.y)
                    continue
                if row["category"] not in {"metro_station", "school", "kindergarten", "clinic"}:
                    continue
                source_shape = from_wkb(row["geometry"])
                if row["access_point"]:
                    shape = from_wkb(row["access_point"])
                    basis = "source_access_point"
                else:
                    shape = source_shape if source_shape.geom_type == "Point" else source_shape.representative_point()
                    basis = "source_point" if source_shape.geom_type == "Point" else "source_geometry_representative_point"
                if shape.geom_type != "Point" or shape.is_empty:
                    continue
                internal_id = str(row["id"])
                public_id = "poi_" + hashlib.sha256(internal_id.encode("utf-8")).hexdigest()[:24]
                record = {
                    "id": public_id, "osm_source_id": internal_id,
                    "category": row["category"], "name": row["name"],
                    "coordinate": (float(shape.x), float(shape.y)), "coordinate_basis": basis,
                    "access_kind": row["access_kind"], "osm_version": row["osm_version"],
                    "osm_timestamp": row["osm_timestamp"],
                }
                # Osmium may export a boundary line and a polygon for one OSM
                # feature. Count and route that feature once, preferring the
                # area representation and then a known source access point.
                priority = {"Polygon": 3, "MultiPolygon": 3, "Point": 2,
                            "LineString": 1, "MultiLineString": 1}.get(source_shape.geom_type, 0)
                if row["access_point"]:
                    priority += 10
                previous = rows_by_source.get(internal_id)
                if previous is None or priority > previous[0]:
                    rows_by_source[internal_id] = (priority, record)
        oms_records: dict[str, dict[str, Any]] = {}
        oms_revision = "none"
        if OMS_SIDECAR.is_file() and OMS_MANIFEST.is_file():
            oms_bytes = OMS_SIDECAR.read_bytes()
            oms_hash = hashlib.sha256(oms_bytes).hexdigest()
            oms_manifest = json.loads(OMS_MANIFEST.read_text(encoding="utf-8"))
            oms_doc = json.loads(oms_bytes)
            snapshot = oms_doc.get("source_snapshot") or {}
            today = datetime.now(timezone.utc).date()
            valid_snapshot = (
                oms_doc.get("schema_version") == 1
                and oms_manifest.get("output_sha256") == oms_hash
                and snapshot.get("poi_table_sha256") == manifest["output"]["sha256"]
                and snapshot.get("osm_sha256") == source["sha256"]
                and _observed_not_future(snapshot.get("official_snapshot_at"))
            )
            if valid_snapshot:
                oms_revision = oms_hash[:16]
                for verified in oms_doc.get("records", []):
                    if (verified.get("oms_status") != "verified"
                            or verified.get("match_method") != "unique_exact_name_address"
                            or verified.get("effective_year") != today.year
                            or verified.get("service_scope") != "participates_in_territorial_oms_program_not_all_services"
                            or verified.get("poi_table_sha256") != manifest["output"]["sha256"]
                            or not _observed_not_future(verified.get("official_snapshot_at"))):
                        continue
                    key = verified.get("osm_source_id")
                    if isinstance(key, str) and key not in oms_records:
                        oms_records[key] = verified
        metro_links: dict[str, list[tuple[float, float]]] = {}
        metro_group_ids: dict[str, str] = {}
        metro_revision = "none"
        if METRO_SIDECAR.is_file() and METRO_MANIFEST.is_file():
            metro_bytes = METRO_SIDECAR.read_bytes()
            metro_hash = hashlib.sha256(metro_bytes).hexdigest()
            metro_manifest = json.loads(METRO_MANIFEST.read_text(encoding="utf-8"))
            metro_doc = json.loads(metro_bytes)
            if (metro_doc.get("schema_version") == 1
                    and metro_manifest.get("output", {}).get("sidecar_sha256") == metro_hash
                    and metro_doc.get("poi_table_sha256") == manifest["output"]["sha256"]
                    and metro_doc.get("osm_snapshot_at") == source["osm_snapshot_timestamp"]):
                metro_revision = metro_hash[:16]
                for station in metro_doc.get("stations", []):
                    if station.get("link_basis") != "explicit_osm_relation_membership":
                        continue
                    station_id = station.get("station_source_id")
                    if station_id not in rows_by_source:
                        continue
                    relation_id = station.get("relation_source_id")
                    if isinstance(relation_id, str) and relation_id.startswith("osm:relation:"):
                        metro_group_ids[station_id] = relation_id
                    linked = [entrances[feature_id] for feature_id in station.get("entrance_source_ids", [])
                              if feature_id in entrances]
                    if linked:
                        metro_links[station_id] = linked
        dataset_id = (f"osm:{manifest['output']['sha256'][:16]}:graph:{_graph_version()}"
                      f":oms:{oms_revision}:metro:{metro_revision}:policy:{ROUTING_POLICY_REVISION}")

        # Keep connector inputs local and version-pinned. Candidate roads are
        # fetched by the way IDs returned by Valhalla's pedestrian locate;
        # unrelated roads never enter the bounded candidate pool.
        walk_way_lookup = None
        barrier_lookup = None
        roads_revision = "unavailable"
        barriers_revision = "unavailable"
        barrier_coverage = "unavailable"
        if (ROADS_PATH.is_file() and ROADS_MANIFEST.is_file()
                and WATER_PATH.is_file() and WATERWAYS_PATH.is_file()
                and SURFACES_MANIFEST.is_file()):
            road_manifest = json.loads(ROADS_MANIFEST.read_text(encoding="utf-8"))
            surfaces_manifest = json.loads(SURFACES_MANIFEST.read_text(encoding="utf-8"))
            road_output = road_manifest.get("output", {})
            surface_source = surfaces_manifest.get("source", {})
            water_meta = surfaces_manifest.get("layers", {}).get("water", {})
            waterways_meta = surfaces_manifest.get("layers", {}).get("waterways", {})
            snapshots_match = (
                road_manifest.get("source", {}).get("sha256") == source.get("sha256")
                and surface_source.get("sha256") == source.get("sha256")
                and road_output.get("file") == str(ROADS_PATH.relative_to(ROOT))
                and water_meta.get("file") == str(WATER_PATH.relative_to(ROOT))
                and waterways_meta.get("file") == str(WATERWAYS_PATH.relative_to(ROOT))
                and road_output.get("sha256")
                and water_meta.get("sha256")
                and waterways_meta.get("sha256")
                and _sha256_file(ROADS_PATH) == road_output.get("sha256")
                and _sha256_file(WATER_PATH) == water_meta.get("sha256")
                and _sha256_file(WATERWAYS_PATH) == waterways_meta.get("sha256")
            )
            if snapshots_match:
                roads_revision = str(road_output["sha256"])
                barriers_revision = hashlib.sha256(
                    f"water:{water_meta['sha256']}|waterways:{waterways_meta['sha256']}".encode("ascii")
                ).hexdigest()
                barrier_coverage = (
                    "known_osm_water_and_waterways_only; railway_fence_internal_paths_and_gates_unknown"
                )
                roads = arrow_dataset.dataset(ROADS_PATH, format="parquet")

                def lookup_walk_ways(edges: Sequence[dict[str, Any]]) -> list[WalkSegment]:
                    by_way: dict[int, list[dict[str, Any]]] = defaultdict(list)
                    for edge in edges:
                        way_id = edge.get("way_id")
                        if isinstance(way_id, int) and not isinstance(way_id, bool):
                            by_way[way_id].append(edge)
                    if not by_way:
                        return []
                    table = roads.to_table(
                        columns=["osm_id", "highway", "foot", "access", "tags_json", "geometry"],
                        filter=arrow_dataset.field("osm_id").isin(list(by_way)),
                    )
                    segments: list[WalkSegment] = []
                    for item in table.to_pylist():
                        try:
                            shape = from_wkb(item["geometry"])
                            tags = json.loads(item.get("tags_json") or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        for key in ("highway", "foot", "access"):
                            if item.get(key) is not None:
                                tags[key] = item[key]
                        if shape.geom_type == "LineString":
                            lines = [shape]
                        elif shape.geom_type == "MultiLineString":
                            lines = list(shape.geoms)
                        else:
                            continue
                        way_id = int(item["osm_id"])
                        for part_number, line in enumerate(lines):
                            coordinates = tuple((float(x), float(y)) for x, y, *_ in line.coords)
                            for edge in by_way.get(way_id, []):
                                try:
                                    graph_point = _coordinates(edge["correlated_lon"], edge["correlated_lat"])
                                except (KeyError, TypeError, ValueError):
                                    continue
                                suffix = f":part:{part_number}" if len(lines) > 1 else ""
                                segments.append(WalkSegment(
                                    f"osm:way:{way_id}{suffix}", coordinates, tags, graph_point))
                    return segments

                barrier_cache: dict[str, Any] = {}

                def load_barriers() -> tuple[Any, list[Any], list[tuple[str, str]]]:
                    if barrier_cache:
                        return (barrier_cache["tree"], barrier_cache["geometries"],
                                barrier_cache["identities"])
                    geometries: list[Any] = []
                    identities: list[tuple[str, str]] = []
                    for path in (WATER_PATH, WATERWAYS_PATH):
                        for batch in parquet.ParquetFile(path).iter_batches(
                                columns=["id", "feature_class", "geometry"], batch_size=1024):
                            for item in batch.to_pylist():
                                try:
                                    shape = from_wkb(item["geometry"])
                                except (TypeError, ValueError):
                                    continue
                                if shape.is_empty:
                                    continue
                                geometries.append(shape)
                                identities.append((str(item.get("id") or "osm:unknown"),
                                                   str(item.get("feature_class") or "unknown")))
                    barrier_cache["geometries"] = geometries
                    barrier_cache["identities"] = identities
                    barrier_cache["tree"] = STRtree(geometries)
                    return barrier_cache["tree"], geometries, identities

                def lookup_barriers(origin: tuple[float, float]) -> list[Barrier]:
                    tree, geometries, identities = load_barriers()
                    lon, lat = origin
                    # Include every mapped barrier that could intersect a
                    # connector up to the configured 100 m limit, with a
                    # small projection/rounding margin.
                    search_pad_m = ORIGIN_ACCESS_CONNECTOR_MAX_M + 10.0
                    lat_pad = search_pad_m / 110_540.0
                    lon_pad = search_pad_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
                    indices = tree.query(box(lon - lon_pad, lat - lat_pad,
                                             lon + lon_pad, lat + lat_pad))
                    result: list[Barrier] = []
                    for index in indices:
                        position = int(index)
                        source_id, kind = identities[position]
                        result.append(Barrier(source_id, kind, mapping(geometries[position])))
                    return result

                walk_way_lookup = lookup_walk_ways
                barrier_lookup = lookup_barriers
                dataset_id += f":roads:{roads_revision[:16]}:barriers:{barriers_revision[:16]}"
        return cls([item[1] for item in rows_by_source.values()], dataset_id=dataset_id,
                   osm_snapshot_at=source["osm_snapshot_timestamp"], oms_records=oms_records,
                   metro_links=metro_links, metro_group_ids=metro_group_ids,
                   walk_way_lookup=walk_way_lookup, barrier_lookup=barrier_lookup,
                   roads_revision=roads_revision, barriers_revision=barriers_revision,
                   barrier_coverage=barrier_coverage)

    def _nearest(self, origin: tuple[float, float], categories: set[str], count: int) -> list[dict[str, Any]]:
        """Use grid cells and a conservative outer-ring bound for exact air ranking."""
        cell_x, cell_y = _grid_key(*origin)
        seen: set[int] = set()
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        radius = 0
        while radius <= 128:
            for x in range(cell_x - radius, cell_x + radius + 1):
                for y in range(cell_y - radius, cell_y + radius + 1):
                    for index in self.grid.get((x, y), ()):
                        if index in seen:
                            continue
                        seen.add(index)
                        row = self.records[index]
                        if row["category"] in categories:
                            ranked.append((_distance_m(origin, row["coordinate"]), row["id"], row))
            ranked.sort(key=lambda item: (item[0], item[1]))
            if len(ranked) >= count:
                west = (cell_x - radius) * GRID_DEGREES
                east = (cell_x + radius + 1) * GRID_DEGREES
                south = (cell_y - radius) * GRID_DEGREES
                north = (cell_y + radius + 1) * GRID_DEGREES
                # Conservative lower bound to the outside of the square.
                # This keeps the grid search exact while avoiding a full scan.
                latitude_bound = 110_000 * min(origin[1] - south, north - origin[1])
                longitude_scale = max(0.0, math.cos(math.radians(max(abs(south), abs(north)))))
                longitude_bound = 110_000 * longitude_scale * min(origin[0] - west, east - origin[0])
                boundary = min(latitude_bound, longitude_bound)
                if ranked[count - 1][0] <= boundary:
                    break
            radius = 1 if radius == 0 else radius * 2
        return [{**row, "straight_line_m": round(distance)} for distance, _, row in ranked[:count]]

    def _route_network(self, origin: tuple[float, float], target: dict[str, Any]) -> dict[str, Any]:
        target_coordinate = target.get("route_coordinate", target["coordinate"])
        basis = target.get("route_coordinate_basis", target["coordinate_basis"])
        access_kind = target.get("route_access_kind", target.get("access_kind"))
        uncertain = basis != "source_access_point" or access_kind != "entrance_known"
        empty = {"status": "unavailable", "distance_m": None, "estimated_minutes": None,
                 "geometry": None, "approximate": True, "origin_access_kind": "mapped_point_access_unknown",
                 "target_access_kind": access_kind or "unverified",
                 "endpoint_coordinate": {"lon": target_coordinate[0], "lat": target_coordinate[1]},
                 "endpoint_basis": basis,
                 "endpoint_note": "До точки на карте; вход не уточнён" if uncertain else "До нанесённого входа"}
        try:
            if not self.graph_valid:
                return empty
            if self.actor is None:
                if self.actor_factory is None:
                    from valhalla import Actor
                    self.actor_factory = Actor
                graph_config = json.loads(GRAPH_CONFIG.read_text(encoding="utf-8"))
                # Hard ferry exclusion is enabled only in the private actor
                # config copy. The source graph/config remains immutable.
                graph_config["service_limits"]["allow_hard_exclusions"] = True
                tile_dir = os.environ.get("NOVOSTROY_VALHALLA_TILE_DIR")
                if tile_dir:
                    tile_path = Path(tile_dir)
                    if not tile_path.is_absolute() or not tile_path.is_dir():
                        raise RuntimeError("configured Valhalla tile directory is missing")
                    graph_config["mjolnir"]["tile_dir"] = str(tile_path)
                self.actor = self.actor_factory(graph_config)
            self.actor_calls += 1
            response = self.actor.route({"locations": [
                {"lon": origin[0], "lat": origin[1], "type": "break"},
                {"lon": target_coordinate[0], "lat": target_coordinate[1], "type": "break"}],
                "costing": "pedestrian",
                # Valhalla's zero ferry preference is not an exclusion: it
                # may still choose a ferry when the graph requires one. The
                # returned has_ferry flag is therefore checked below.
                "costing_options": {"pedestrian": {"use_ferry": 0, "use_rail_ferry": 0,
                                                   "exclude_ferries": True}},
                "units": "kilometers"})
            if isinstance(response, str):
                response = json.loads(response)
            trip = response.get("trip") or {}
            legs = trip.get("legs") or []
            if response.get("status", 0) != 0 or len(legs) != 1:
                return {**empty, "status": "no_route"}
            geometry = _decode_polyline6(legs[0]["shape"])
            if len(geometry) < 2:
                return {**empty, "status": "invalid_route"}
            # Trip locations may echo request points. Check the actual geometry
            # endpoints so a distant graph snap cannot masquerade as access.
            snap_m = [round(_distance_m(origin, tuple(geometry[0]))),
                      round(_distance_m(target_coordinate, tuple(geometry[-1])))]
            if max(snap_m) > SNAP_LIMIT_M:
                return {**empty, "status": "snap_exceeds_threshold", "snap_distance_m": snap_m}
            summary = legs[0].get("summary") or {}
            if (summary.get("has_ferry") is True
                    or (trip.get("summary") or {}).get("has_ferry") is True
                    or any(maneuver.get("ferry") is True or maneuver.get("travel_type") == "ferry"
                           for maneuver in legs[0].get("maneuvers", []) if isinstance(maneuver, dict))):
                return {**empty, "status": "contains_ferry"}
            length_km = float(summary["length"])
            seconds = float(summary["time"])
            if not math.isfinite(length_km) or not math.isfinite(seconds) or length_km < 0 or seconds < 0:
                return {**empty, "status": "invalid_route"}
            geometry, tolerance_m = _bounded_route_geometry(geometry)
            return {**empty, "status": "routed", "distance_m": round(length_km * 1000),
                    "estimated_minutes": math.ceil(seconds / 60), "geometry": geometry,
                    "_duration_seconds": seconds,
                    "geometry_simplification_tolerance_m": tolerance_m,
                    "snap_distance_m": snap_m, "routing_basis": "local_pedestrian_graph",
                    "estimate_basis": "OSM snapshot walking graph; live closures unavailable"}
        except Exception:  # local graph errors must not turn air distance into walking time
            return {**empty, "status": "unavailable"}

    def _origin_access_inputs(
        self, origin: tuple[float, float]
    ) -> tuple[list[WalkSegment], list[Barrier]] | None:
        if (self.actor is None or self.walk_way_lookup is None or self.barrier_lookup is None
                or self.roads_revision == "unavailable"
                or self.barriers_revision == "unavailable"):
            return None
        key = (round(origin[0], 7), round(origin[1], 7))
        if key in self.origin_access_cache:
            self.origin_access_cache.move_to_end(key)
            return self.origin_access_cache[key]
        try:
            located = self.actor.locate({
                "locations": [{"lon": origin[0], "lat": origin[1],
                               "radius": ORIGIN_ACCESS_LOCATE_RADIUS_M}],
                "costing": "pedestrian",
            })
            if isinstance(located, str):
                located = json.loads(located)
            if isinstance(located, dict):
                located = [located]
            if not isinstance(located, list) or not located:
                return None
            raw_edges = located[0].get("edges", []) if isinstance(located[0], dict) else []
            edges: list[dict[str, Any]] = []
            seen: set[tuple[int, float, float]] = set()
            for edge in raw_edges:
                if not isinstance(edge, dict):
                    continue
                way_id = edge.get("way_id")
                try:
                    lon, lat = _coordinates(edge.get("correlated_lon"), edge.get("correlated_lat"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(way_id, int) or isinstance(way_id, bool):
                    continue
                identity = (way_id, lon, lat)
                if identity in seen:
                    continue
                seen.add(identity)
                edges.append({**edge, "correlated_lon": lon, "correlated_lat": lat})
                if len(edges) >= 64:
                    break
            segments = list(self.walk_way_lookup(edges)) if edges else []
            barriers = list(self.barrier_lookup(origin))
            value = (segments, barriers)
        except Exception:
            # Connector evidence is optional; preserve the existing route
            # failure if any local source or locate operation is unavailable.
            return None
        self.origin_access_cache[key] = value
        if len(self.origin_access_cache) > ORIGIN_ACCESS_CACHE_SIZE:
            self.origin_access_cache.popitem(last=False)
        return value

    def _route(self, origin: tuple[float, float], target: dict[str, Any]) -> dict[str, Any]:
        """Prefer the normal network route; add a disclosed connector only to recover origin access."""
        direct = self._route_network(origin, target)
        if direct.get("status") == "routed":
            direct.pop("_duration_seconds", None)
            return direct
        status = direct.get("status")
        origin_snap_failed = (status == "snap_exceeds_threshold"
                              and isinstance(direct.get("snap_distance_m"), list)
                              and len(direct["snap_distance_m"]) >= 1
                              and direct["snap_distance_m"][0] > SNAP_LIMIT_M)
        if status != "no_route" and not origin_snap_failed:
            return direct
        inputs = self._origin_access_inputs(origin)
        if inputs is None:
            return direct
        segments, barriers = inputs
        if not segments:
            return direct
        def route_network(access: tuple[float, float]) -> dict[str, Any]:
            return self._route_network(access, target)

        try:
            connected = route_with_origin_access(
                Origin(origin), segments, barriers, route_network)
        except (TypeError, ValueError):
            connected = None
        if connected is None:
            return direct
        origin_access = dict(connected["origin_access"])
        raw_segment_id = str(origin_access.get("source_segment_id") or "")
        origin_access["source_segment_id"] = (
            "segment_" + hashlib.sha256(raw_segment_id.encode("utf-8")).hexdigest()[:16]
            if raw_segment_id else None
        )
        origin_access["barrier_check"] = self.barrier_coverage
        origin_access["caveat"] = (
            "Примерный подход к уличному графу; внутри участка, входы, ворота и текущие закрытия "
            "не проверены. Покрытие OSM водных преград ограничено; железные дороги и ограждения "
            "могут быть нанесены неполно."
        )
        network_geometry = connected["network_geometry"]
        return {
            **direct,
            **connected,
            "status": "routed",
            # Existing clients still receive a routable geometry. New clients
            # use network_geometry and draw origin_access.geometry separately.
            "geometry": network_geometry,
            "network_geometry": network_geometry,
            "snap_distance_m": connected.get("network_snap_distance_m"),
            "origin_access": origin_access,
            "origin_access_kind": "estimated_connector_to_walk_network",
            "target_access_kind": direct.get("target_access_kind"),
            "endpoint_coordinate": direct.get("endpoint_coordinate"),
            "endpoint_basis": direct.get("endpoint_basis"),
            "endpoint_note": direct.get("endpoint_note"),
        }

    def _cached_route(self, origin: tuple[float, float], row: dict[str, Any]) -> dict[str, Any]:
        endpoint = row.get("route_coordinate", row["coordinate"])
        key = connector_cache_key(
            (self.dataset_id, origin, row["osm_source_id"], endpoint),
            roads_revision=self.roads_revision,
            barriers_revision=self.barriers_revision,
        )
        if key in self.route_cache:
            self.route_cache.move_to_end(key)
            return self.route_cache[key]
        route = self._route(origin, row)
        self.route_cache[key] = route
        if len(self.route_cache) > 512:
            self.route_cache.popitem(last=False)
        return route

    @staticmethod
    def _metro_name_key(row: dict[str, Any]) -> str:
        name = unicodedata.normalize("NFKC", str(row.get("name") or "")).casefold().replace("ё", "е")
        return " ".join(re.sub(r"[^\w]+", " ", name, flags=re.UNICODE).split())

    def _group_metro_candidates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse explicit station groups and only very local exact-name duplicates.

        The local fallback is marked uncertain and will not override conflicting
        explicit relation identities. Equal names alone never create a group.
        """
        groups: list[dict[str, Any]] = []
        explicit_groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            source_id = row["osm_source_id"]
            explicit_id = self.metro_group_ids.get(source_id)
            group = explicit_groups.get(explicit_id) if explicit_id else None
            basis = "explicit_osm_stop_area_relation" if explicit_id else None
            if group is None and not explicit_id:
                name_key = self._metro_name_key(row)
                local_matches = [candidate for candidate in groups
                                 if candidate["station_identity_basis"] in {
                                     "osm_station_feature",
                                     "exact_name_and_local_distance_uncertain"}
                                 and candidate["_station_name_key"] == name_key
                                 and name_key
                                 and _distance_m(row["coordinate"], candidate["coordinate"])
                                 <= METRO_LOCAL_IDENTITY_RADIUS_M]
                if local_matches:
                    group = min(local_matches, key=lambda candidate: _distance_m(
                        row["coordinate"], candidate["coordinate"]))
                    group["station_identity_basis"] = "exact_name_and_local_distance_uncertain"
                    group["station_identity_uncertain"] = True
                    basis = group["station_identity_basis"]
            if group is None:
                basis = basis or "osm_station_feature"
                group = {**row, "_station_members": [],
                         "_station_name_key": self._metro_name_key(row),
                         "station_identity_basis": basis,
                         "station_identity_uncertain": basis != "explicit_osm_stop_area_relation",
                         "station_member_count": 0}
                groups.append(group)
                if explicit_id:
                    explicit_groups[explicit_id] = group
            group["_station_members"].append(row)
            group["station_member_count"] = len(group["_station_members"])
        return groups

    def _route_station(self, origin: tuple[float, float], row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        members = row.get("_station_members") or [row]
        linked = sorted({point for member in members
                         for point in self.metro_links.get(member["osm_source_id"], [])},
                        key=lambda point: (_distance_m(origin, point), point))
        evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if linked:
            for access in linked[:METRO_ENTRANCE_ROUTE_POOL]:
                routed_row = {**row, "route_coordinate": access,
                              "route_coordinate_basis": "source_access_point",
                              "route_access_kind": "entrance_known",
                              "metro_access_link": "explicit_osm_relation_membership"}
                evaluated.append((routed_row, self._cached_route(origin, routed_row)))
        else:
            evaluated.extend((member, self._cached_route(origin, member)) for member in members)
        routed = [pair for pair in evaluated if pair[1]["status"] == "routed"]
        if routed:
            best_row, route = min(routed, key=lambda pair: (
                pair[1]["distance_m"],
                _distance_m(origin, pair[0].get("route_coordinate", pair[0]["coordinate"])),
                pair[0]["osm_source_id"]))
        else:
            best_row, route = min(evaluated, key=lambda pair: (
                pair[0].get("straight_line_m", _distance_m(origin, pair[0]["coordinate"])),
                pair[0]["osm_source_id"]))
        # Keep the pin on a real mapped station feature while preserving how
        # the candidate records were grouped and which entrance was routed.
        output_row = {**best_row,
                      "station_identity_basis": row.get("station_identity_basis", "osm_station_feature"),
                      "station_identity_uncertain": row.get("station_identity_uncertain", True),
                      "station_member_count": row.get("station_member_count", len(members))}
        if row.get("station_identity_basis") == "explicit_osm_stop_area_relation":
            output_row["_station_group_explicit_id"] = self.metro_group_ids.get(
                best_row["osm_source_id"])
        return output_row, route

    def _rank_local(self, rows: list[dict[str, Any]], origin: tuple[float, float],
                    *, metro: bool = False) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str]:
        evaluated = [self._route_station(origin, row) if metro else (row, self._cached_route(origin, row))
                     for row in rows]
        routed = sorted((pair for pair in evaluated if pair[1]["status"] == "routed"),
                        key=lambda pair: (pair[1]["distance_m"], pair[0]["straight_line_m"], pair[0]["id"]))
        unknown = sorted((pair for pair in evaluated if pair[1]["status"] != "routed"),
                         key=lambda pair: (pair[0]["straight_line_m"], pair[0]["id"]))
        selected = routed[:ITEMS_PER_KIND]
        if len(selected) < ITEMS_PER_KIND:
            selected.extend(unknown[:ITEMS_PER_KIND - len(selected)])
        selected = selected[:ITEMS_PER_KIND]
        basis = "walking_distance" if all(route["status"] == "routed" for _, route in selected) else \
            "walking_distance_with_unrouted_nearby"
        return selected, basis

    def _item(self, row: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any]:
        lon, lat = row["coordinate"]
        verified = self.oms_records.get(row["osm_source_id"]) if row["category"] == "clinic" else None
        if verified and verified.get("osm_version") != row.get("osm_version"):
            verified = None
        item = {
            "id": row["id"], "name": row["name"] or {"metro_station": "Станция метро",
                "school": "Школа", "kindergarten": "Детский сад", "clinic": "Клиника"}[row["category"]],
            "category": row["category"], "coordinate": {"lon": lon, "lat": lat},
            "coordinate_basis": row["coordinate_basis"], "straight_line_m": row.get("straight_line_m"),
            "metro_access_link": row.get("metro_access_link", "unknown") if row["category"] == "metro_station" else None,
            "route": route, "free_oms": ({"status": "verified_program_participant",
                "scope": "Участие в территориальной программе ОМС; доступность конкретной услуги уточняется",
                "official_snapshot_at": verified.get("official_snapshot_at")}
                if verified else {"status": "unknown", "scope": None}),
            "osm_updated_at": row.get("osm_timestamp"),
        }
        if row["category"] == "metro_station":
            item.update({
                "station_identity_basis": row.get("station_identity_basis", "osm_station_feature"),
                "station_identity_uncertain": row.get("station_identity_uncertain", True),
                "station_member_count": row.get("station_member_count", 1),
            })
        return item

    def point(self, lon: float, lat: float, *, point_id: str, education: str) -> dict[str, Any]:
        key = (self.dataset_id, point_id, lon, lat, education)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        origin = (lon, lat)
        layers: dict[str, Any] = {}
        metro_source_rows = self._nearest(origin, {"metro_station"}, ROUTE_POOL)
        metro_rows = self._group_metro_candidates(metro_source_rows)
        metro_selected, metro_basis = self._rank_local(metro_rows, origin, metro=True)
        layers["metro"] = {
            "items": [self._item(row, route) for row, route in metro_selected],
            "ranking_basis": metro_basis, "selection_policy": "nearest_two_walkable_stations",
            "candidate_count": len(metro_rows), "coverage": "unknown", "attribution": ATTRIBUTION,
        }

        education_kinds = ("school", "kindergarten") if education == "all" else (education,)
        education_selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
        education_pools = 0
        education_basis: list[str] = []
        for kind in education_kinds:
            rows = self._nearest(origin, {kind}, ROUTE_POOL)
            selected, basis = self._rank_local(rows, origin)
            education_selected.extend(selected)
            education_pools += len(rows)
            education_basis.append(basis)
        layers["education"] = {
            "items": [self._item(row, route) for row, route in education_selected],
            "ranking_basis": "walking_distance_per_subcategory" if all(
                basis == "walking_distance" for basis in education_basis) else
                "walking_distance_with_unrouted_nearby_per_subcategory",
            "selection_policy": "up_to_two_schools_and_two_kindergartens" if education == "all"
                else f"up_to_two_{education}",
            "candidate_count": education_pools, "coverage": "unknown", "attribution": ATTRIBUTION,
        }

        medical_rows = self._nearest(origin, {"clinic"}, ROUTE_POOL)
        medical_selected, medical_basis = self._rank_local(medical_rows, origin)
        eligible = [row for row in self.records if row["category"] == "clinic"
                    and row["osm_source_id"] in self.oms_records
                    and self.oms_records[row["osm_source_id"]].get("osm_version") == row.get("osm_version")]
        eligible.sort(key=lambda row: (_distance_m(origin, row["coordinate"]), row["id"]))
        verified_candidates = [{**row, "straight_line_m": round(_distance_m(origin, row["coordinate"]))}
                               for row in eligible[:VERIFIED_ROUTE_POOL]]
        verified_evaluated = [(row, self._cached_route(origin, row)) for row in verified_candidates]
        verified_routed = [pair for pair in verified_evaluated if pair[1]["status"] == "routed"]
        verified_unknown = [pair for pair in verified_evaluated if pair[1]["status"] != "routed"]
        nearest_verified_unknown = min(verified_unknown, key=lambda pair: (
            pair[0]["straight_line_m"], pair[0]["id"])) if verified_unknown else None
        nearest_verified_routed = min(verified_routed, key=lambda pair: (
            pair[1]["distance_m"], pair[0]["id"])) if verified_routed else None
        # A routed verified clinic is more useful than a closer source point
        # whose walking route is unknown. Fall back to the nearest unknown
        # verified site only when this bounded verified pool has no routed one.
        verified_choice = nearest_verified_routed or nearest_verified_unknown
        verified_oms_added_as_exception = False
        if verified_choice:
            selected_verified = [
                (index, pair) for index, pair in enumerate(medical_selected)
                if pair[0]["osm_source_id"] in self.oms_records
                and self.oms_records[pair[0]["osm_source_id"]].get("osm_version")
                == pair[0].get("osm_version")
            ]
            chosen_is_routed = verified_choice[1]["status"] == "routed"
            selected_verified_routed = [
                pair for _, pair in selected_verified if pair[1]["status"] == "routed"
            ]
            chosen_already_selected = any(
                pair[0]["osm_source_id"] == verified_choice[0]["osm_source_id"]
                for pair in medical_selected
            )
            replace_selected_oms = False
            replace_index: int | None = None
            if chosen_is_routed and not chosen_already_selected:
                selected_unknown_oms = [
                    (index, pair) for index, pair in selected_verified
                    if pair[1]["status"] != "routed"
                ]
                if selected_unknown_oms:
                    # An unknown route can only fill a slot for which this
                    # bounded pool has no routed OMS candidate.
                    replace_index = max(
                        (index for index, _pair in selected_unknown_oms),
                        key=lambda index: medical_selected[index][0]["straight_line_m"],
                    )
                    replace_selected_oms = True
                elif selected_verified_routed:
                    worst_selected_route = max(
                        pair[1]["distance_m"] for pair in selected_verified_routed
                    )
                    replace_selected_oms = verified_choice[1]["distance_m"] < worst_selected_route
                    if replace_selected_oms:
                        replace_index = max(
                            (index for index, pair in selected_verified
                             if pair[1]["status"] == "routed"),
                            key=lambda index: medical_selected[index][1]["distance_m"],
                        )
                elif selected_verified:
                    # Upgrade an unrouted OMS row already filling a local slot.
                    replace_index = selected_verified[0][0]
                    replace_selected_oms = True
                else:
                    replace_selected_oms = True
                    if len(medical_selected) >= ITEMS_PER_KIND:
                        replace_index = len(medical_selected) - 1
            elif not selected_verified:
                replace_selected_oms = True
                if len(medical_selected) >= ITEMS_PER_KIND:
                    replace_index = len(medical_selected) - 1
            if replace_selected_oms:
                if replace_index is None:
                    medical_selected.append(verified_choice)
                else:
                    medical_selected[replace_index] = verified_choice
                verified_oms_added_as_exception = True
        verified_oms_present = any(
            row["osm_source_id"] in self.oms_records and
            self.oms_records[row["osm_source_id"]].get("osm_version") == row.get("osm_version")
            for row, _ in medical_selected)
        layers["medical"] = {
            "items": [self._item(row, route) for row, route in medical_selected],
            "ranking_basis": "local_walking_distance_plus_verified_oms_exception"
                if verified_oms_added_as_exception else medical_basis,
            "selection_policy": "nearest_local_plus_verified_oms" if verified_oms_added_as_exception
                else "nearest_two_walkable_clinics",
            "required_clinic_included": verified_oms_present,
            "verified_oms_added_as_exception": verified_oms_added_as_exception,
            "candidate_count": len({row["osm_source_id"] for row in
                                    [*medical_rows, *verified_candidates]}),
            "verified_oms_site_count": len(eligible), "coverage": "unknown", "attribution": ATTRIBUTION,
        }
        guaranteed = any(item["free_oms"]["status"] == "verified_program_participant"
                         for item in layers["medical"]["items"])
        result = {"status": "ok", "point_id": point_id, "education": education,
                  "origin": {"coordinate": {"lon": lon, "lat": lat},
                             "access_kind": "mapped_point_access_unknown"},
                  "dataset_id": self.dataset_id, "osm_snapshot_at": self.osm_snapshot_at,
                  "layers": layers, "free_clinic_guarantee": {"status": "met" if guaranteed else "unavailable",
                      "scope": "ОМС для конкретной услуги требует проверки" if guaranteed else
                          "Подтверждённая клиника ОМС в текущем наборе данных не найдена; это не означает отсутствия бесплатной помощи"},
                  "coverage": {"poi": "unknown", "routes": "bounded_candidate_pool",
                               "candidate_limit_per_subcategory": ROUTE_POOL,
                               "verified_oms_candidate_limit": VERIFIED_ROUTE_POOL,
                               "ranking_basis": "walking_distance_when_routed"},
                  "attribution": ATTRIBUTION}
        self.cache[key] = result
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    def viewport(self, bbox: tuple[float, float, float, float], *, layers: tuple[str, ...],
                 education: str, limit: int) -> dict[str, Any]:
        west, south, east, north = bbox
        cx, cy = (west + east) / 2, (south + north) / 2
        names = {"metro": {"metro_station"},
                 "education": {"school", "kindergarten"} if education == "all" else {education},
                 "medical": {"clinic"}}
        wanted = set().union(*(names[layer] for layer in layers))
        found: list[tuple[float, str, dict[str, Any]]] = []
        x0, y0 = _grid_key(west, south)
        x1, y1 = _grid_key(east, north)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for index in self.grid.get((x, y), ()):
                    row = self.records[index]
                    lon, lat = row["coordinate"]
                    if row["category"] in wanted and west <= lon <= east and south <= lat <= north:
                        found.append((_distance_m((cx, cy), (lon, lat)), row["id"], row))
        found.sort(key=lambda item: (item[0], item[1]))
        selected = found[:limit]
        return {"status": "ok", "dataset_id": self.dataset_id,
                "osm_snapshot_at": self.osm_snapshot_at, "education": education,
                "bbox": list(bbox), "layers": {layer: {"items": [self._item(row) for _, _, row in selected
                    if row["category"] in names[layer]], "coverage": "unknown",
                    "attribution": ATTRIBUTION} for layer in layers},
                "truncated": len(found) > limit, "returned_count": len(selected),
                "coverage": {"poi": "unknown"}, "attribution": ATTRIBUTION}


def _graph_version() -> str:
    if not GRAPH_CONFIG.is_file() or not GRAPH_MANIFEST.is_file():
        return "missing"
    graph = json.loads(GRAPH_MANIFEST.read_text(encoding="utf-8"))
    poi = json.loads(POI_MANIFEST.read_text(encoding="utf-8")) if POI_MANIFEST.is_file() else {}
    config_hash = hashlib.sha256(GRAPH_CONFIG.read_bytes()).hexdigest()
    if (graph.get("status") != "complete"
            or graph.get("builder", {}).get("config_sha256") != config_hash
            or graph.get("source", {}).get("sha256") != poi.get("source", {}).get("sha256")
            or not isinstance(graph.get("tile_file_count"), int)
            or graph["tile_file_count"] <= 0):
        return "invalid"
    return hashlib.sha256(GRAPH_MANIFEST.read_bytes()).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_signature() -> tuple[str | int | None, ...]:
    paths = (POI_PATH, POI_MANIFEST, GRAPH_CONFIG, GRAPH_MANIFEST, OMS_SIDECAR,
             OMS_MANIFEST, METRO_SIDECAR, METRO_MANIFEST, ROADS_PATH, ROADS_MANIFEST,
             WATER_PATH, WATERWAYS_PATH, SURFACES_MANIFEST,
             ROOT / "data/social/osm_moscow_parks_261008_v2.parquet")
    return (datetime.now(timezone.utc).date().isoformat(),
            *(path.stat().st_mtime_ns if path.is_file() else None for path in paths))


def _worker_main() -> int:
    index: NearbyIndex | None = None
    park_index: Any = None
    loaded_signature: tuple[str | int | None, ...] | None = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            signature = _input_signature()
            if index is None or signature != loaded_signature:
                index = NearbyIndex.from_local_files()
                park_index = None
                loaded_signature = signature
            if request["kind"] == "point":
                result = index.point(float(request["lon"]), float(request["lat"]),
                                     point_id=request["point_id"], education=request["education"])
            elif request["kind"] == "viewport":
                result = index.viewport(tuple(request["bbox"]), layers=tuple(request["layers"]),
                                        education=request["education"], limit=request["limit"])
            elif request["kind"] == "park":
                from backend.park_walk import ParkIndex

                if park_index is None:
                    source = json.loads(POI_MANIFEST.read_text(encoding="utf-8"))["source"]
                    park_index = ParkIndex.from_local_files(
                        expected_source_sha256=source["sha256"])
                result = park_index.walk(
                    (float(request["lon"]), float(request["lat"])), index._route,
                    index.barrier_lookup, dataset_id=index.dataset_id)
            else:
                raise ValueError("unknown nearby request kind")
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker_main())
