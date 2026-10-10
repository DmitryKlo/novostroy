"""Offline routing from explicit OSM POI access points using local Valhalla tiles.

This module never contacts a routing service. It requires a local PyValhalla
installation, a built graph, and a POI table with source-provided access points.
It deliberately does not substitute polygon centroids or arbitrary POI geometry
for a verified entrance or mapped transit waiting point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "data/social/valhalla_moscow/valhalla_pedestrian.json"
DEFAULT_GRAPH_MANIFEST = ROOT / "data/social/valhalla_moscow/build_manifest.json"
DEFAULT_POI_TABLE = ROOT / "data/social/osm_moscow_social_pois_261008.parquet"
ALLOWED_ACCESS_KINDS = {"entrance_known", "mapped_waiting_point"}
EARTH_MEAN_RADIUS_M = 6_371_008.8


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Great-circle distance between lon/lat points; never a route metric."""
    lon1, lat1 = (math.radians(value) for value in first)
    lon2, lat2 = (math.radians(value) for value in second)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    haversine = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    angle = 2 * math.asin(min(1.0, math.sqrt(haversine)))
    return round(EARTH_MEAN_RADIUS_M * angle, 3)


def _read_poi_access(table_path: Path, feature_id: str) -> dict[str, Any]:
    try:
        import pyarrow.parquet as parquet
        from shapely import from_wkb
    except ImportError as exc:  # pragma: no cover - depends on local tool env
        raise RuntimeError(
            "POI loading requires pyarrow and shapely in the isolated geodata environment"
        ) from exc

    columns = [
        "id",
        "osm_type",
        "osm_id",
        "osm_version",
        "osm_timestamp",
        "category",
        "name",
        "access_kind",
        "access_point",
    ]
    matches: list[dict[str, Any]] = []
    for batch in parquet.ParquetFile(table_path).iter_batches(columns=columns):
        for record in batch.to_pylist():
            if record["id"] == feature_id:
                matches.append(record)
    if len(matches) != 1:
        raise ValueError(
            f"expected one POI row for {feature_id!r}; found {len(matches)}"
        )

    record = matches[0]
    access_kind = record.get("access_kind")
    if access_kind not in ALLOWED_ACCESS_KINDS:
        raise ValueError(
            f"{feature_id!r} has unverified access_kind={access_kind!r}; no route was requested"
        )
    access_wkb = record.get("access_point")
    if not access_wkb:
        raise ValueError(
            f"{feature_id!r} has no source-provided access_point; no geometry fallback is allowed"
        )
    point = from_wkb(access_wkb)
    if point.geom_type != "Point":
        raise ValueError(
            f"{feature_id!r} access_point is {point.geom_type}, not a point"
        )
    lon, lat = float(point.x), float(point.y)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"{feature_id!r} access point is outside WGS84 bounds")

    return {
        "source_id": str(record["id"]),
        "osm_type": str(record["osm_type"]),
        "osm_id": int(record["osm_id"]),
        "osm_version": int(record["osm_version"]),
        "source_updated_at": record.get("osm_timestamp"),
        "category": record.get("category"),
        "name": record.get("name"),
        "access_kind": access_kind,
        "coordinates": {"lon": lon, "lat": lat, "crs": "EPSG:4326"},
        "coordinate_provenance": "OSM source-provided access_point WKB",
    }


def _json_response(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    raise TypeError(f"unexpected Valhalla response type: {type(value).__name__}")


def route_poi_pair(
    *,
    config_path: Path,
    poi_table: Path,
    from_id: str,
    to_id: str,
    max_snap_m: float = 50.0,
    graph_manifest_path: Path | None = DEFAULT_GRAPH_MANIFEST,
) -> dict[str, Any]:
    """Route between two verified source access points on the local graph.

    `router_estimated_seconds` is Valhalla's snapshot-based cost estimate. It
    is not a promised journey duration and the graph has no live closure feed.
    """
    if max_snap_m < 0 or not math.isfinite(max_snap_m):
        raise ValueError("max_snap_m must be finite and non-negative")
    config_path = config_path.resolve()
    poi_table = poi_table.resolve()
    if not config_path.is_file() or not poi_table.is_file():
        raise FileNotFoundError("local Valhalla config and POI table must exist")

    origin = _read_poi_access(poi_table, from_id)
    destination = _read_poi_access(poi_table, to_id)
    origin_lon = origin["coordinates"]["lon"]
    origin_lat = origin["coordinates"]["lat"]
    destination_lon = destination["coordinates"]["lon"]
    destination_lat = destination["coordinates"]["lat"]
    straight_line_m = _distance_m(
        (origin_lon, origin_lat), (destination_lon, destination_lat)
    )

    try:
        from valhalla import Actor, ValhallaError
    except ImportError as exc:  # pragma: no cover - depends on local tool env
        raise RuntimeError(
            "routing requires pyvalhalla==3.9.0 in the isolated geodata environment"
        ) from exc

    config = json.loads(config_path.read_text(encoding="utf-8"))
    actor = Actor(config)
    request = {
        "locations": [
            {"lat": origin_lat, "lon": origin_lon, "type": "break"},
            {"lat": destination_lat, "lon": destination_lon, "type": "break"},
        ],
        "costing": "pedestrian",
        "units": "kilometers",
    }
    try:
        response = _json_response(actor.route(request))
    except ValhallaError as exc:
        return {
            "schema_version": 1,
            "status": "no_route",
            "from": origin,
            "to": destination,
            "straight_line_m": straight_line_m,
            "route": None,
            "error": {
                "provider": "local_valhalla",
                "code": getattr(exc, "code", None),
                "message": getattr(exc, "message", str(exc)),
            },
            "routing_scope": "OSM snapshot graph; live closures are not modeled",
        }

    trip = response.get("trip") or {}
    legs = trip.get("legs") or []
    if response.get("status", 0) != 0 or not legs:
        return {
            "schema_version": 1,
            "status": "no_route",
            "from": origin,
            "to": destination,
            "straight_line_m": straight_line_m,
            "route": None,
            "error": {
                "provider": "local_valhalla",
                "code": response.get("status"),
                "message": response.get("status_message", "Valhalla returned no route"),
            },
            "routing_scope": "OSM snapshot graph; live closures are not modeled",
        }

    snapped_locations = trip.get("locations") or []
    if len(snapped_locations) != 2:
        raise ValueError("Valhalla route response did not contain two snapped locations")
    snapped: list[dict[str, Any]] = []
    for source, location in zip((origin, destination), snapped_locations, strict=True):
        snapped_lon = float(location["lon"])
        snapped_lat = float(location["lat"])
        source_lon = source["coordinates"]["lon"]
        source_lat = source["coordinates"]["lat"]
        snapped.append(
            {
                "source_id": source["source_id"],
                "lon": snapped_lon,
                "lat": snapped_lat,
                "snap_distance_m": _distance_m(
                    (source_lon, source_lat), (snapped_lon, snapped_lat)
                ),
                "side_of_street": location.get("side_of_street"),
            }
        )

    summary = legs[0].get("summary") or {}
    edges: list[dict[str, Any]] = []
    trace_error: dict[str, Any] | None = None
    encoded_shape = legs[0].get("shape")
    if encoded_shape:
        try:
            attributes = _json_response(
                actor.trace_attributes(
                    {
                        "encoded_polyline": encoded_shape,
                        "shape_match": "edge_walk",
                        "costing": "pedestrian",
                        "units": "kilometers",
                        "filters": {
                            "action": "include",
                            "attributes": [
                                "edge.id",
                                "edge.way_id",
                                "edge.names",
                                "edge.length",
                                "edge.use",
                                "edge.begin_shape_index",
                                "edge.end_shape_index",
                            ],
                        },
                    }
                )
            )
            for edge in attributes.get("edges", []):
                edges.append(
                    {
                        "graph_edge_id": edge.get("id"),
                        "osm_way_id": edge.get("way_id"),
                        "names": edge.get("names", []),
                        "length_km": edge.get("length"),
                        "use": edge.get("use"),
                        "begin_shape_index": edge.get("begin_shape_index"),
                        "end_shape_index": edge.get("end_shape_index"),
                    }
                )
        except (ValhallaError, TypeError, ValueError, KeyError) as exc:
            trace_error = {
                "type": type(exc).__name__,
                "message": getattr(exc, "message", str(exc)),
            }

    graph_manifest: dict[str, Any] | None = None
    graph_manifest_path = graph_manifest_path.resolve() if graph_manifest_path else None
    if graph_manifest_path and graph_manifest_path.is_file():
        graph_manifest = json.loads(graph_manifest_path.read_text(encoding="utf-8"))

    max_observed_snap = max(item["snap_distance_m"] for item in snapped)
    status = "routed" if max_observed_snap <= max_snap_m else "snap_exceeds_threshold"
    return {
        "schema_version": 1,
        "status": status,
        "from": origin,
        "to": destination,
        "straight_line_m": straight_line_m,
        "snap_threshold_m": max_snap_m,
        "snapped_points": snapped,
        "route": {
            "provider": "local_valhalla",
            "costing": "pedestrian",
            "distance_km": summary.get("length"),
            "router_estimated_seconds": summary.get("time"),
            "route_shape_polyline": encoded_shape,
            "edge_count": len(edges),
            "unique_osm_way_ids": sorted(
                {edge["osm_way_id"] for edge in edges if edge["osm_way_id"] is not None}
            ),
            "edges": edges,
            "edge_trace_error": trace_error,
            "response_status": response.get("status"),
            "response_message": response.get("status_message"),
        },
        "provenance": {
            "poi_table": str(poi_table),
            "poi_table_sha256": _sha256(poi_table),
            "router_config": str(config_path),
            "router_config_sha256": _sha256(config_path),
            "graph_manifest_path": str(graph_manifest_path) if graph_manifest_path else None,
            "graph_manifest": graph_manifest,
        },
        "routing_scope": (
            "Valhalla pedestrian-costing estimate on pinned OSM graph; "
            "not a journey-time guarantee; live closures and current opening/access state are not modeled"
        ),
    }


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--pois", type=Path, default=DEFAULT_POI_TABLE)
    parser.add_argument("--graph-manifest", type=Path, default=DEFAULT_GRAPH_MANIFEST)
    parser.add_argument("--max-snap-m", type=float, default=50.0)
    parser.add_argument("--output", type=Path, help="write one JSON result; refuses to overwrite")
    parser.add_argument("from_id", help="stable POI table id with a source access point")
    parser.add_argument("to_id", help="stable POI table id with a source access point")
    args = parser.parse_args(argv)

    try:
        result = route_poi_pair(
            config_path=args.config,
            poi_table=args.pois,
            from_id=args.from_id,
            to_id=args.to_id,
            max_snap_m=args.max_snap_m,
            graph_manifest_path=args.graph_manifest,
        )
        if args.output:
            _write_json(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0 if result["status"] == "routed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
