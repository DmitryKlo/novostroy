"""Build a private OSM proximity index for mapped developer buildings.

The index is an internal distance heuristic, not a sound measurement, dB(A)
estimate, health assessment, or proof that a mapped land use is active. It
reads a pinned local OSM PBF and the active developer-map snapshot, writes a
versioned research run, and never changes a product pointer.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

from pyproj import Transformer
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint
from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "environment-noise-run/1"
ROW_SCHEMA_VERSION = "environment-noise-score/1"
FORMULA_VERSION = "internal_osm_proximity_v1"
CRS_SOURCE = "EPSG:4326"
CRS_DISTANCE = "EPSG:32637"
SEARCH_HALO_M = 15_000.0
OSM_SNAPSHOT_DATE = "2026-10-08"
SOURCE_URL = "https://download.geofabrik.de/russia/central-fed-district-261008.osm.pbf"
SOURCE_ATTRIBUTION = "© OpenStreetMap contributors"
ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
NOISE_BASIS = "internal_osm_proximity_v1"
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")

# The weights sum to 100. Each component decays linearly to zero at its
# disclosed distance. These are internal display-policy coefficients, not
# empirical acoustic or health-response parameters.
COMPONENT_POLICY: dict[str, dict[str, Any]] = {
    "major_road": {
        "weight": 40.0,
        "zero_distance_m": 1_000.0,
        "description": "nearest mapped motorway, trunk, or primary road",
        "tags": {"highway": ["motorway", "trunk", "primary"]},
    },
    "railway": {
        "weight": 25.0,
        "zero_distance_m": 1_500.0,
        "description": "nearest mapped surface railway=rail line",
        "tags": {"railway": ["rail"]},
    },
    "industrial": {
        "weight": 20.0,
        "zero_distance_m": 2_000.0,
        "description": "nearest mapped landuse=industrial area or man_made=works candidate",
        "tags": {"landuse": ["industrial"], "man_made": ["works"]},
    },
    "airport": {
        "weight": 15.0,
        "zero_distance_m": 5_000.0,
        "description": "nearest mapped aerodrome polygon or runway geometry",
        "tags": {"aeroway": ["aerodrome", "runway"]},
    },
}

OSM_FILTER_EXPRESSIONS = (
    "w/highway=motorway,trunk,primary",
    "w/railway=rail",
    "nwr/landuse=industrial",
    "nwr/man_made=works",
    "nwr/aeroway=aerodrome,runway",
)


class NoiseBuildError(RuntimeError):
    """Pinned local input or extraction is invalid; no run is published."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"Refusing to overwrite partial output: {temporary}")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"Refusing to overwrite partial output: {temporary}")
    count = 0
    try:
        with temporary.open("xb") as stream:
            for row in rows:
                stream.write(_canonical_json(row) + b"\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return count


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise NoiseBuildError(f"Required input is missing or a symlink: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoiseBuildError(f"Invalid JSON input: {path}") from exc


def _valid_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _boundary_polygon(boundary_path: Path, boundary_manifest_path: Path) -> tuple[BaseGeometry, dict[str, Any]]:
    manifest = _read_json(boundary_manifest_path)
    expected = manifest.get("output_sha256")
    if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
        raise NoiseBuildError("Boundary manifest has no valid output SHA-256")
    actual = sha256_file(boundary_path)
    if actual != expected:
        raise NoiseBuildError("Boundary GeoJSON SHA does not match its manifest")
    document = _read_json(boundary_path)
    if document.get("type") == "FeatureCollection":
        geometries = [
            shape(feature["geometry"])
            for feature in document.get("features", [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
    elif document.get("type") in {"Polygon", "MultiPolygon"}:
        geometries = [shape(document)]
    else:
        geometries = []
    if not geometries:
        raise NoiseBuildError("Boundary GeoJSON contains no polygon features")
    boundary = unary_union(geometries)
    if (
        boundary.geom_type not in {"Polygon", "MultiPolygon"}
        or boundary.is_empty
        or not boundary.is_valid
    ):
        raise NoiseBuildError("Boundary must be a valid WGS84 Polygon or MultiPolygon")
    return boundary, manifest


def _active_map_points(active_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if active_path.is_symlink() or not active_path.is_file():
        raise NoiseBuildError("Active developer-map pointer is missing or a symlink")
    pointer_bytes = active_path.read_bytes()
    pointer = json.loads(pointer_bytes)
    if pointer.get("schema_version") != "developer-map-pointer/1":
        raise NoiseBuildError("Unsupported developer-map pointer")
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"developer-map-v1-[a-f0-9]+", run_id):
        raise NoiseBuildError("Unsafe developer-map run ID")
    run_dir = active_path.parent / "runs" / run_id
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise NoiseBuildError("Active developer-map manifest is missing or a symlink")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = sha256_bytes(manifest_bytes)
    if pointer.get("manifest_sha256") != manifest_sha:
        raise NoiseBuildError("Active developer-map pointer does not pin its manifest")
    manifest = json.loads(manifest_bytes)
    if (
        manifest.get("schema_version") != "developer-map-snapshot/1"
        or manifest.get("run_id") != run_id
    ):
        raise NoiseBuildError("Active developer-map manifest identity mismatch")
    points_path = run_dir / "points.jsonl"
    expected_points_sha = manifest.get("points_sha256")
    if not isinstance(expected_points_sha, str) or sha256_file(points_path) != expected_points_sha:
        raise NoiseBuildError("Active developer-map points SHA mismatch")
    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    with points_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NoiseBuildError(f"Invalid map point JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise NoiseBuildError(f"Map point line {line_number} is not an object")
            registry_id = row.get("registry_id")
            if not isinstance(registry_id, str) or not registry_id.strip() or registry_id in seen:
                raise NoiseBuildError(f"Missing or duplicate registry_id at line {line_number}")
            seen.add(registry_id)
            points.append(row)
    if len(points) != manifest.get("points_total"):
        raise NoiseBuildError("Active developer-map point count mismatch")
    if sum(1 for row in points if row.get("location") is not None) != manifest.get("map_points_total"):
        raise NoiseBuildError("Active developer-map located count mismatch")
    return points, {
        "pointer_path": str(active_path),
        "pointer_sha256": sha256_bytes(pointer_bytes),
        "run_id": run_id,
        "snapshot_id": manifest.get("snapshot_id"),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "points_path": str(points_path),
        "points_sha256": expected_points_sha,
        "points_total": len(points),
        "map_points_total": manifest.get("map_points_total"),
        "observed_at": manifest.get("observed_at"),
        "rules_version": manifest.get("rules_version"),
    }


def _load_pbf_manifest(pbf_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    expected = manifest.get("sha256")
    if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
        raise NoiseBuildError("PBF manifest has no valid SHA-256")
    actual = sha256_file(pbf_path)
    if actual != expected:
        raise NoiseBuildError("PBF SHA does not match its manifest")
    if manifest.get("license") != "ODbL-1.0":
        raise NoiseBuildError("Expected pinned ODbL-1.0 OSM input")
    return {
        "file": str(pbf_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "sha256": actual,
        "bytes": pbf_path.stat().st_size,
        "dataset": manifest.get("dataset"),
        "source_url": manifest.get("source_url"),
        "source_last_modified": manifest.get("source_last_modified"),
        "retrieved_at": manifest.get("retrieved_at"),
        "osm_snapshot_timestamp": manifest.get("source_last_modified"),
        "license": manifest.get("license"),
        "attribution": manifest.get("attribution"),
        "license_url": manifest.get("license_url"),
    }


def _pbf_extent(osmium: str, pbf_path: Path) -> tuple[list[list[float]], dict[str, Any]]:
    completed = subprocess.run(
        [osmium, "fileinfo", "--extended", "--json", "--no-progress", str(pbf_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        info = json.loads(completed.stdout)
        boxes = info["header"]["boxes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise NoiseBuildError("Could not read PBF header coverage boxes") from exc
    parsed: list[list[float]] = []
    for box in boxes:
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(_valid_number(value) for value in box)
        ):
            raise NoiseBuildError("PBF header contains an invalid coverage box")
        parsed.append([float(value) for value in box])
    if not parsed:
        raise NoiseBuildError("PBF has no declared coverage box")
    options = (info.get("header") or {}).get("option") or {}
    snapshot = options.get("osmosis_replication_timestamp") or options.get("timestamp")
    if not isinstance(snapshot, str):
        raise NoiseBuildError("PBF header has no source snapshot timestamp")
    return parsed, {"replication_timestamp": snapshot, "fileinfo_sha256": sha256_bytes(completed.stdout.encode())}


def _bbox_covers(bounds: tuple[float, float, float, float], boxes: list[list[float]]) -> bool:
    minx, miny, maxx, maxy = bounds
    return any(minx >= left and miny >= bottom and maxx <= right and maxy <= top
               for left, bottom, right, top in boxes)


def build_search_halo(boundary: BaseGeometry) -> tuple[BaseGeometry, BaseGeometry, Transformer, Transformer]:
    """Return metric and WGS84 buffers plus explicit always-XY transformers."""
    forward = Transformer.from_crs(CRS_SOURCE, CRS_DISTANCE, always_xy=True)
    inverse = Transformer.from_crs(CRS_DISTANCE, CRS_SOURCE, always_xy=True)
    boundary_metric = transform(forward.transform, boundary)
    if boundary_metric.is_empty or not boundary_metric.is_valid:
        raise NoiseBuildError("Projected city boundary is empty or invalid")
    halo_metric = boundary_metric.buffer(SEARCH_HALO_M, quad_segs=16)
    halo_wgs84 = transform(inverse.transform, halo_metric)
    if halo_wgs84.is_empty or not halo_wgs84.is_valid:
        raise NoiseBuildError("Buffered WGS84 search halo is invalid")
    return halo_metric, halo_wgs84, forward, inverse


def classify_tags(tags: dict[str, str], geometry_type: str) -> list[str]:
    """Map only explicit active-source tags; omit underground/disused features."""
    false_values = {"", "no", "false", "0", "none"}
    for key in ("disused", "abandoned", "demolished", "construction"):
        if key in tags and str(tags[key]).strip().casefold() not in false_values:
            return []
    if any(key.startswith(("disused:", "abandoned:", "demolished:")) for key in tags):
        return []
    if str(tags.get("status", "")).casefold() in {"closed", "disused", "abandoned"}:
        return []
    if str(tags.get("operational_status", "")).casefold() in {"closed", "disused", "abandoned"}:
        return []
    if str(tags.get("aerodrome:type", "")).casefold() == "closed":
        return []
    if str(tags.get("industrial", "")).casefold() in {"disused", "abandoned"}:
        return []
    try:
        if float(tags.get("layer", "0")) < 0 or float(tags.get("level", "0")) < 0:
            return []
    except (TypeError, ValueError):
        pass
    if str(tags.get("tunnel", "")).casefold() not in false_values:
        return []
    if str(tags.get("location", "")).casefold() == "underground":
        return []
    if str(tags.get("covered", "")).casefold() not in false_values:
        return []

    result: list[str] = []
    highway = str(tags.get("highway", "")).casefold()
    if geometry_type in {"LineString", "MultiLineString"} and highway in {"motorway", "trunk", "primary"}:
        result.append("major_road")
    if geometry_type in {"LineString", "MultiLineString"} and str(tags.get("railway", "")).casefold() == "rail":
        result.append("railway")
    if str(tags.get("landuse", "")).casefold() == "industrial" and geometry_type in {"Polygon", "MultiPolygon"}:
        result.append("industrial")
    if str(tags.get("man_made", "")).casefold() == "works" and geometry_type in {
        "Point", "MultiPoint", "Polygon", "MultiPolygon"
    }:
        result.append("industrial")
    if str(tags.get("aeroway", "")).casefold() in {"aerodrome", "runway"} and geometry_type in {
        "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"
    }:
        result.append("airport")
    return list(dict.fromkeys(result))


def _valid_source_geometry(geometry: BaseGeometry) -> bool:
    return (
        not geometry.is_empty
        and geometry.geom_type in {
            "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"
        }
        and geometry.is_valid
    )


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(value, str):
            text = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return None
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def extract_feature_layers(
    exported_geojsonseq: Path,
    halo_metric: BaseGeometry,
    forward: Transformer,
    inverse: Transformer,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Parse exported OSM features, filter to source classes and exact halo."""
    layers: dict[str, list[dict[str, Any]]] = {name: [] for name in COMPONENT_POLICY}
    counters: Counter[str] = Counter()
    with exported_geojsonseq.open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            line = raw_line.lstrip("\x1e\r\n ")
            if not line:
                continue
            try:
                feature = json.loads(line)
                props = feature.get("properties") or {}
                geometry_doc = feature.get("geometry")
                if not isinstance(geometry_doc, dict):
                    counters["missing_geometry"] += 1
                    continue
                geometry_wgs84 = shape(geometry_doc)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise NoiseBuildError(f"Invalid osmium GeoJSONSeq line {line_number}") from exc
            raw_tags = {str(key): str(value) for key, value in props.items()
                        if not str(key).startswith("@") and value is not None}
            components = classify_tags(raw_tags, geometry_wgs84.geom_type)
            if not components:
                counters["tag_or_geometry_excluded"] += 1
                continue
            if not _valid_source_geometry(geometry_wgs84):
                counters["invalid_geometry"] += 1
                continue
            geometry_metric = transform(forward.transform, geometry_wgs84)
            if not geometry_metric.is_valid:
                counters["invalid_projected_geometry"] += 1
                continue
            clipped_metric = geometry_metric.intersection(halo_metric)
            if clipped_metric.is_empty or not clipped_metric.is_valid:
                counters["outside_or_invalid_halo_intersection"] += 1
                continue
            clipped_wgs84 = transform(inverse.transform, clipped_metric)
            osm_type = str(props.get("@type") or "")
            osm_id = props.get("@id")
            if osm_type not in {"node", "way", "relation"} or not str(osm_id or "").isdigit():
                counters["missing_osm_identity"] += 1
                continue
            osm_version = props.get("@version")
            try:
                osm_version = int(osm_version) if osm_version is not None else None
            except (TypeError, ValueError):
                osm_version = None
            record = {
                "source_id": f"osm:{osm_type}:{int(osm_id)}",
                "osm_type": osm_type,
                "osm_id": int(osm_id),
                "osm_version": osm_version,
                "osm_timestamp": _timestamp(props.get("@timestamp")),
                "geometry_type": clipped_wgs84.geom_type,
                "tags": raw_tags,
                "geometry_wgs84_clipped_to_15km_halo": mapping(clipped_wgs84),
            }
            for component in components:
                layers[component].append({
                    "geometry_metric": clipped_metric,
                    "record": {**record, "component": component},
                })
                counters[f"{component}_features"] += 1
    return layers, {
        "exported_features_excluded_or_dropped": dict(sorted(counters.items())),
        "feature_count_by_component": {key: len(value) for key, value in layers.items()},
    }


class NearestFeatureIndex:
    """Deterministic nearest source lookup within a completely covered halo."""

    def __init__(self, features: list[dict[str, Any]]) -> None:
        self.features = features
        self.geometries = [item["geometry_metric"] for item in features]
        self.tree = STRtree(self.geometries) if self.geometries else None

    def nearest(self, point: Point) -> tuple[dict[str, Any] | None, float | None]:
        if self.tree is None:
            return None, None
        result = self.tree.query_nearest(
            point,
            max_distance=SEARCH_HALO_M,
            all_matches=True,
            return_distance=True,
        )
        if not isinstance(result, tuple) or len(result) != 2:
            return None, None
        indices, distances = result
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for index, distance in zip(indices.tolist(), distances.tolist(), strict=True):
            feature = self.features[int(index)]
            candidates.append((float(distance), feature["record"]["source_id"], feature))
        if not candidates:
            return None, None
        distance, _source_id, feature = min(candidates, key=lambda item: (item[0], item[1]))
        return feature, distance


def score_component(distance_m: float | None, *, weight: float, zero_distance_m: float) -> float:
    if distance_m is None or distance_m >= zero_distance_m:
        return 0.0
    return weight * max(0.0, 1.0 - (distance_m / zero_distance_m))


def _nearest_evidence(
    feature: dict[str, Any] | None,
    distance_m: float | None,
    component: str,
    layer_sha256: str,
) -> dict[str, Any]:
    policy = COMPONENT_POLICY[component]
    if feature is None or distance_m is None:
        return {
            "distance_m": None,
            "distance_status": "no_mapped_candidate_within_15km_search_window",
            "component_points": 0.0,
            "influence_zero_distance_m": policy["zero_distance_m"],
            "source_layer_sha256": layer_sha256,
            "nearest_source": None,
        }
    row = feature["record"]
    distance = float(distance_m)
    return {
        "distance_m": round(distance, 2),
        "distance_status": "nearest_mapped_candidate_within_15km_search_window",
        "component_points": round(score_component(
            distance, weight=policy["weight"], zero_distance_m=policy["zero_distance_m"]
        ), 4),
        "influence_zero_distance_m": policy["zero_distance_m"],
        "source_layer_sha256": layer_sha256,
        "nearest_source": {
            "source_id": row["source_id"],
            "osm_type": row["osm_type"],
            "osm_id": row["osm_id"],
            "osm_version": row["osm_version"],
            "osm_timestamp": row["osm_timestamp"],
            "geometry_type": row["geometry_type"],
            "source_tags": row["tags"],
        },
    }


def score_registry_point(
    map_row: dict[str, Any],
    *,
    boundary: BaseGeometry,
    forward: Transformer,
    indexes: dict[str, NearestFeatureIndex],
    layer_hashes: dict[str, str],
    pbf_boxes: list[list[float]],
    pbf_sha256: str,
    as_of_date: str,
) -> dict[str, Any]:
    registry_id = map_row["registry_id"]
    location = map_row.get("location")
    common = {
        "schema_version": ROW_SCHEMA_VERSION,
        "registry_id": registry_id,
        "source_snapshot_sha256": pbf_sha256,
        "formula_version": FORMULA_VERSION,
        "as_of_date": as_of_date,
    }
    if location is None:
        return {
            **common,
            "noise_index": None,
            "noise_basis": None,
            "noise_coverage": "unlocated",
            "noise_inputs": {
                "location": None,
                "search_halo_m": SEARCH_HALO_M,
                "coverage_note": "Active registry point has no source-backed map coordinate.",
                "measured_db_a": None,
            },
        }
    if (
        not isinstance(location, list)
        or len(location) != 2
        or not all(_valid_number(value) for value in location)
        or not (-180 <= float(location[0]) <= 180 and -90 <= float(location[1]) <= 90)
    ):
        return {
            **common,
            "noise_index": None,
            "noise_coverage": "boundary_incomplete",
            "noise_inputs": {
                "location": location,
                "location_basis": map_row.get("location_basis"),
                "search_halo_m": SEARCH_HALO_M,
                "coverage_note": "Map coordinate is malformed or outside WGS84 bounds.",
                "measured_db_a": None,
            },
        }
    lon, lat = float(location[0]), float(location[1])
    source_point = Point(lon, lat)
    if not boundary.covers(source_point):
        return {
            **common,
            "noise_index": None,
            "noise_coverage": "boundary_incomplete",
            "noise_inputs": {
                "location_lon_lat": [lon, lat],
                "location_basis": map_row.get("location_basis"),
                "search_halo_m": SEARCH_HALO_M,
                "coverage_note": "Mapped point falls outside the pinned OSM city boundary.",
                "measured_db_a": None,
            },
        }
    point_metric = transform(forward.transform, source_point)
    halo_wgs84_bounds = transform(
        Transformer.from_crs(CRS_DISTANCE, CRS_SOURCE, always_xy=True).transform,
        point_metric.buffer(SEARCH_HALO_M, quad_segs=16),
    ).bounds
    if not _bbox_covers(halo_wgs84_bounds, pbf_boxes):
        return {
            **common,
            "noise_index": None,
            "noise_coverage": "boundary_incomplete",
            "noise_inputs": {
                "location_lon_lat": [lon, lat],
                "location_basis": map_row.get("location_basis"),
                "search_halo_m": SEARCH_HALO_M,
                "coverage_note": "The pinned PBF header does not cover the full 15km search halo around this point.",
                "measured_db_a": None,
            },
        }

    components: dict[str, Any] = {}
    total = 0.0
    for key, policy in COMPONENT_POLICY.items():
        nearest, distance = indexes[key].nearest(point_metric)
        evidence = _nearest_evidence(nearest, distance, key, layer_hashes[key])
        components[key] = evidence
        total += evidence["component_points"]
    index = max(0, min(100, int(math.floor(total + 0.5))))
    return {
        **common,
        "noise_basis": NOISE_BASIS,
        "noise_index": index,
        "noise_coverage": "covered",
        "noise_inputs": {
            "location_lon_lat": [lon, lat],
            "location_basis": map_row.get("location_basis"),
            "location_quality": map_row.get("location_quality"),
            "point_type": map_row.get("point_type"),
            "search_halo_m": SEARCH_HALO_M,
            "distance_crs": CRS_DISTANCE,
            "components": components,
            "formula": {
                "method": "weighted_linear_distance_decay",
                "direction": "higher means closer to selected mapped OSM source classes under this policy",
                "weights": {name: value["weight"] for name, value in COMPONENT_POLICY.items()},
                "zero_distance_m": {name: value["zero_distance_m"] for name, value in COMPONENT_POLICY.items()},
                "missing_candidate_within_complete_halo": "component contributes zero; this means no mapped candidate in the source snapshot, not real-world absence or quiet",
            },
            "coverage_note": "15km point halo is inside the declared PBF extent; OSM feature completeness and operating status are unknown.",
            "activity_status": "unknown_for_industrial_zones_and_works",
            "measured_db_a": None,
            "not_an_acoustic_measurement": True,
        },
    }


def _extract_local_features(
    source_pbf: Path,
    halo_wgs84: BaseGeometry,
    *,
    osmium: str,
    work_dir: Path,
) -> Path:
    minx, miny, maxx, maxy = halo_wgs84.bounds
    bbox = ",".join(f"{value:.9f}" for value in (minx, miny, maxx, maxy))
    region_pbf = work_dir / "moscow-15km-halo.osm.pbf"
    filtered_pbf = work_dir / "noise-filtered.osm.pbf"
    expressions = work_dir / "noise-tags.txt"
    exported = work_dir / "noise-features.geojsonseq"
    expressions.write_text("\n".join(OSM_FILTER_EXPRESSIONS) + "\n", encoding="utf-8")
    subprocess.run(
        [osmium, "extract", "--no-progress", "--bbox", bbox,
         "--strategy", "complete_ways", "--output", str(region_pbf), str(source_pbf)],
        check=True,
        timeout=600,
    )
    if not region_pbf.is_file() or region_pbf.stat().st_size == 0:
        raise NoiseBuildError("osmium extract did not produce a regional PBF")
    subprocess.run(
        [osmium, "tags-filter", "--no-progress", "--expressions", str(expressions),
         "--output", str(filtered_pbf), str(region_pbf)],
        check=True,
        timeout=600,
    )
    if not filtered_pbf.is_file() or filtered_pbf.stat().st_size == 0:
        raise NoiseBuildError("osmium tags-filter did not produce a feature PBF")
    subprocess.run(
        [osmium, "export", "--no-progress", "--output-format=geojsonseq",
         "--add-unique-id=type_id", "--geometry-types=point,linestring,polygon",
         "--attributes=type,id,version,timestamp", "--output", str(exported), str(filtered_pbf)],
        check=True,
        timeout=900,
    )
    if not exported.is_file():
        raise NoiseBuildError("osmium export did not produce GeoJSONSeq")
    return exported


def build_run(
    *,
    pbf_path: Path,
    pbf_manifest_path: Path,
    boundary_path: Path,
    boundary_manifest_path: Path,
    active_map_path: Path,
    output_dir: Path,
    osmium: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create a new immutable research run from locally pinned inputs."""
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing noise run: {output_dir}")
    if not shutil.which(osmium) and not Path(osmium).is_file():
        raise FileNotFoundError(f"osmium executable not found: {osmium}")
    for path in (pbf_path, pbf_manifest_path, boundary_path, boundary_manifest_path, active_map_path):
        if path.is_symlink() or not path.is_file():
            raise NoiseBuildError(f"Required source file is missing or a symlink: {path}")
    pbf = _load_pbf_manifest(pbf_path, pbf_manifest_path)
    boundary, boundary_manifest = _boundary_polygon(boundary_path, boundary_manifest_path)
    map_rows, map_info = _active_map_points(active_map_path)
    active_boundary_sha = None
    run_manifest_path = Path(map_info["manifest_path"])
    map_manifest = _read_json(run_manifest_path)
    for item in map_manifest.get("inputs", []):
        if item.get("role") == "moscow_boundary":
            active_boundary_sha = item.get("data_sha256")
            break
    if active_boundary_sha != sha256_file(boundary_path):
        raise NoiseBuildError("Active map and supplied city boundary do not share the pinned SHA")

    halo_metric, halo_wgs84, forward, inverse = build_search_halo(boundary)
    pbf_boxes, pbf_header = _pbf_extent(osmium, pbf_path)
    halo_bbox = [float(value) for value in halo_wgs84.bounds]
    if not _bbox_covers(tuple(halo_bbox), pbf_boxes):
        raise NoiseBuildError("PBF header extent does not cover the full 15km city halo")
    # Reject any active point whose own query window escapes the declared PBF extent.
    for row in map_rows:
        location = row.get("location")
        if isinstance(location, list) and len(location) == 2 and all(_valid_number(v) for v in location):
            pt = transform(forward.transform, Point(float(location[0]), float(location[1])))
            pt_buffer = transform(inverse.transform, pt.buffer(SEARCH_HALO_M, quad_segs=16))
            if boundary.covers(Point(float(location[0]), float(location[1]))) and not _bbox_covers(pt_buffer.bounds, pbf_boxes):
                raise NoiseBuildError(f"PBF header does not cover 15km halo for {row['registry_id']}")

    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise ValueError("created_at must include a timezone")
    created_text = created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    run_identity = hashlib.sha256(_canonical_json({
        "pbf_sha256": pbf["sha256"],
        "boundary_sha256": sha256_file(boundary_path),
        "map_snapshot_sha256": map_info["manifest_sha256"],
        "formula_version": FORMULA_VERSION,
        "halo_m": SEARCH_HALO_M,
    })).hexdigest()[:16]
    run_id = f"environment-noise-v1-{run_identity}"
    work_root = output_dir.parent
    work_root.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing noise run: {output_dir}")
    stage = output_dir.with_name(output_dir.name + ".partial")
    if stage.exists() or stage.is_symlink():
        raise FileExistsError(f"Refusing to overwrite partial noise run: {stage}")
    stage.mkdir(parents=True)
    try:
        _write_json(stage / "search-halo.geojson", {
            "type": "Feature",
            "properties": {"crs": CRS_SOURCE, "halo_m": SEARCH_HALO_M},
            "geometry": mapping(halo_wgs84),
        })
        with tempfile.TemporaryDirectory(prefix="novostroy-noise-", dir=work_root) as temp:
            exported = _extract_local_features(pbf_path, halo_wgs84,
                                               osmium=osmium, work_dir=Path(temp))
            layers, extraction_stats = extract_feature_layers(exported, halo_metric, forward, inverse)

        layer_hashes: dict[str, str] = {}
        layer_counts: dict[str, int] = {}
        indexes: dict[str, NearestFeatureIndex] = {}
        for component, features in layers.items():
            relative = Path("features") / f"{component}.jsonl"
            path = stage / relative
            layer_counts[component] = _write_jsonl(path, (item["record"] for item in features))
            layer_hashes[component] = sha256_file(path)
            indexes[component] = NearestFeatureIndex(features)
        if sum(layer_counts.values()) == 0:
            raise NoiseBuildError("No eligible OSM noise-proximity features intersect the halo")

        as_of_date = pbf_header["replication_timestamp"][:10]
        rows = [score_registry_point(
            row,
            boundary=boundary,
            forward=forward,
            indexes=indexes,
            layer_hashes=layer_hashes,
            pbf_boxes=pbf_boxes,
            pbf_sha256=pbf["sha256"],
            as_of_date=as_of_date,
        ) for row in map_rows]
        row_path = stage / "noise-scores.jsonl"
        written_rows = _write_jsonl(row_path, rows)
        if written_rows != len(map_rows):
            raise NoiseBuildError("Noise row count differs from active registry count")
        coverage_counts = Counter(row["noise_coverage"] for row in rows)
        scored = [row["noise_index"] for row in rows if isinstance(row["noise_index"], int)]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at_utc": created_text,
            "score_policy": {
                "formula_version": FORMULA_VERSION,
                "method": "weighted linear decay of nearest OSM candidate distance; each component contributes up to its weight and reaches zero at its configured distance",
                "components": COMPONENT_POLICY,
                "weights_total": sum(value["weight"] for value in COMPONENT_POLICY.values()),
                "search_halo_m": SEARCH_HALO_M,
                "distance_crs": CRS_DISTANCE,
                "source_semantics": "mapped OSM proximity only; not dB(A), measured exposure, health assessment, or actual source activity",
                "zero_semantics": "zero is emitted only after full PBF spatial-extent coverage and successful layer extraction; means no selected mapped candidate within its influence radius in this snapshot, not no real-world noise",
            },
            "source": {
                **pbf,
                "osm_replication_timestamp": pbf_header["replication_timestamp"],
                "header_coverage_boxes_wgs84": pbf_boxes,
                "feature_timestamp_fields_preserved": ["osm_type", "osm_id", "osm_version", "osm_timestamp", "tags"],
                "source_filter_expressions": list(OSM_FILTER_EXPRESSIONS),
                "feature_layer_counts": layer_counts,
                "feature_layer_files": {
                    component: {
                        "path": f"features/{component}.jsonl",
                        "records": layer_counts[component],
                        "bytes": (stage / "features" / f"{component}.jsonl").stat().st_size,
                        "sha256": layer_hashes[component],
                        "geometry_field": "geometry_wgs84_clipped_to_15km_halo",
                    }
                    for component in COMPONENT_POLICY
                },
                "extraction_stats": extraction_stats,
            },
            "boundary": {
                "file": str(boundary_path),
                "sha256": sha256_file(boundary_path),
                "manifest_path": str(boundary_manifest_path),
                "manifest_sha256": sha256_file(boundary_manifest_path),
                "relation_id": boundary_manifest.get("relation_id"),
                "relation_version": boundary_manifest.get("relation_version"),
                "relation_timestamp_utc": boundary_manifest.get("relation_timestamp_utc"),
                "legal_authority": boundary_manifest.get("boundary_is_authoritative_legal_source", False),
                "buffer_halo_m": SEARCH_HALO_M,
                "buffer_bounds_wgs84": halo_bbox,
                "buffer_geometry_file": "search-halo.geojson",
                "buffer_geometry_sha256": sha256_file(stage / "search-halo.geojson"),
                "extent_coverage_status": "declared_pbf_header_boxes_cover_full_buffered_boundary",
            },
            "map_snapshot": map_info,
            "coverage": {
                "registry_rows": len(rows),
                "located_points": sum(1 for row in map_rows if row.get("location") is not None),
                "mapped_scores": len(scored),
                "noise_coverage_counts": dict(sorted(coverage_counts.items())),
                "noise_index_min": min(scored) if scored else None,
                "noise_index_max": max(scored) if scored else None,
                "noise_index_mean": round(sum(scored) / len(scored), 3) if scored else None,
                "osm_feature_completeness": "unknown",
                "operational_status_of_industrial_sources": "unknown",
            },
            "outputs": {
                "noise_scores": {
                    "path": "noise-scores.jsonl",
                    "records": written_rows,
                    "bytes": row_path.stat().st_size,
                    "sha256": sha256_file(row_path),
                    "schema_version": ROW_SCHEMA_VERSION,
                    "one_record_per_registry_id": True,
                },
                "rights": {
                    "license": "ODbL-1.0",
                    "attribution": SOURCE_ATTRIBUTION,
                    "attribution_url": ATTRIBUTION_URL,
                    "notice": "Preserve source object IDs/versions/timestamps and ODbL attribution. This private derived index is a proximity heuristic.",
                },
            },
            # This direct field is the integration handoff consumed by the
            # enrichment pipeline's --noise-evidence-manifest contract.
            "rows_path": "noise-scores.jsonl",
            "rows_total": written_rows,
            "rows_sha256": sha256_file(row_path),
            "exclusions": {
                "rail": ["railway values other than rail", "tunnel/underground", "negative layer/level", "disused", "abandoned", "construction"],
                "all_sources": ["disused", "abandoned", "demolished", "construction", "tunnel/covered/underground geometry", "negative layer/level"],
                "not_modeled": ["traffic volume", "sound barriers", "building insulation", "topography", "wind", "flight paths", "aircraft movements", "actual factory operation", "façade orientation"],
            },
            "limitations": [
                "PBF spatial extent covers the 15 km search buffer, but OSM feature and tag completeness are unknown.",
                "landuse=industrial and man_made=works indicate mapped candidates, not verified active or noisy operations.",
                "Airport proximity omits flight paths, schedules, aircraft type, and operation frequency.",
                "Developer-map coordinates are source-corpus representative points for most buildings, not façade or sensor locations.",
                "The score is not dB(A), measured noise, a legal limit, or a validated health/noise rating.",
                "A missing mapped source is not proof of real-world absence or quiet; zeros are only internal proximity outputs under the pinned source coverage.",
            ],
            "publication": {
                "status": "staged_private_research_only",
                "active_pointer_changed": False,
                "product_activation_performed": False,
            },
        }
        manifest_path = stage / "manifest.json"
        _write_json(manifest_path, manifest)
        # Exact inventory/checksums are in the manifest; fsync files before the
        # single directory rename that makes this immutable run visible.
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        os.replace(stage, output_dir)
        manifest["manifest_sha256"] = sha256_file(output_dir / "manifest.json")
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _default_paths() -> dict[str, Path]:
    return {
        "pbf": ROOT / "data/social/osm_central_fed_district_261008.osm.pbf",
        "pbf_manifest": ROOT / "data/social/osm_central_fed_district_261008.manifest.json",
        "boundary": ROOT / "data/city/osm_moscow_city_boundary.geojson",
        "boundary_manifest": ROOT / "data/city/osm_moscow_city_boundary.manifest.json",
        "active_map": ROOT / "data/property/developer_map/active.json",
        "output_root": ROOT / ".runtime/research/environment-20261011/noise/runs",
        "osmium": ROOT / ".runtime/geodata-env/bin/osmium",
    }


def main() -> None:
    defaults = _default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=defaults["pbf"])
    parser.add_argument("--pbf-manifest", type=Path, default=defaults["pbf_manifest"])
    parser.add_argument("--boundary", type=Path, default=defaults["boundary"])
    parser.add_argument("--boundary-manifest", type=Path, default=defaults["boundary_manifest"])
    parser.add_argument("--active-map", type=Path, default=defaults["active_map"])
    parser.add_argument("--output-root", type=Path, default=defaults["output_root"])
    parser.add_argument("--osmium", default=str(defaults["osmium"]))
    args = parser.parse_args()
    # Match build_run's immutable run identity. The map manifest (not the
    # mutable pointer bytes) is the pinned snapshot identity.
    pointer = _read_json(args.active_map)
    map_run_id = pointer.get("run_id")
    if (not isinstance(map_run_id, str)
            or not re.fullmatch(r"developer-map-v1-[a-f0-9]+", map_run_id)):
        raise NoiseBuildError("Unsafe developer-map run ID")
    map_manifest_path = args.active_map.parent / "runs" / map_run_id / "manifest.json"
    map_manifest_sha = sha256_file(map_manifest_path)
    expected_hash = hashlib.sha256(_canonical_json({
        "pbf_sha256": sha256_file(args.pbf),
        "boundary_sha256": sha256_file(args.boundary),
        "map_snapshot_sha256": map_manifest_sha,
        "formula_version": FORMULA_VERSION,
        "halo_m": SEARCH_HALO_M,
    })).hexdigest()[:16]
    run_dir = args.output_root / f"environment-noise-v1-{expected_hash}"
    manifest = build_run(
        pbf_path=args.pbf,
        pbf_manifest_path=args.pbf_manifest,
        boundary_path=args.boundary,
        boundary_manifest_path=args.boundary_manifest,
        active_map_path=args.active_map,
        output_dir=run_dir,
        osmium=args.osmium,
    )
    print(json.dumps({
        "run_id": manifest["run_id"],
        "manifest": str(run_dir / "manifest.json"),
        "manifest_sha256": manifest["manifest_sha256"],
        "scores": str(run_dir / "noise-scores.jsonl"),
        "score_sha256": manifest["outputs"]["noise_scores"]["sha256"],
        "coverage": manifest["coverage"],
        "active_pointer_changed": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
