#!/usr/bin/env python3
"""Convert one downloaded OSM administrative multipolygon relation to GeoJSON."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "data/city/osm_moscow_relation_102269_full.osm"
DEFAULT_OUTPUT = ROOT / "data/city/osm_moscow_city_boundary.geojson"
RELATION_ID = "102269"
EARTH_RADIUS_M = 6_371_008.8


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def join_rings(segments: list[list[str]]) -> list[list[str]]:
    pending = [segment[:] for segment in segments]
    rings: list[list[str]] = []
    while pending:
        ring = pending.pop(0)
        while ring[0] != ring[-1]:
            endpoint = ring[-1]
            found = None
            reverse = False
            for i, candidate in enumerate(pending):
                if candidate[0] == endpoint:
                    found = i
                    break
                if candidate[-1] == endpoint:
                    found = i
                    reverse = True
                    break
            if found is None:
                raise ValueError(f"unclosed OSM boundary ring at node {endpoint}")
            segment = pending.pop(found)
            if reverse:
                segment.reverse()
            ring.extend(segment[1:])
        if len(ring) < 4:
            raise ValueError("boundary ring has fewer than four nodes")
        rings.append(ring)
    return rings


def signed_area(coords: list[list[float]]) -> float:
    return sum(
        coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1]
        for i in range(len(coords) - 1)
    ) / 2


def spherical_area_m2(coords: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = map(math.radians, coords[i])
        lon2, lat2 = map(math.radians, coords[i + 1])
        total += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    return abs(total) * EARTH_RADIUS_M**2 / 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--relation-id", default=RELATION_ID)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")

    root = ET.parse(args.input).getroot()
    relation = root.find(f"relation[@id='{args.relation_id}']")
    if relation is None:
        parser.error(f"relation {args.relation_id} not found in {args.input}")
    tags = {tag.get("k"): tag.get("v") for tag in relation.findall("tag")}
    if tags.get("boundary") != "administrative" or tags.get("name") not in {"Москва", "Moscow"}:
        parser.error("input relation is not the expected Moscow administrative boundary")

    nodes = {node.get("id"): [float(node.get("lon")), float(node.get("lat"))] for node in root.findall("node")}
    ways = {
        way.get("id"): [nd.get("ref") for nd in way.findall("nd")]
        for way in root.findall("way")
    }
    members = relation.findall("member")
    segments_by_role: dict[str, list[list[str]]] = {"outer": [], "inner": []}
    for member in members:
        if member.get("type") == "way" and member.get("role") in segments_by_role:
            way_id = member.get("ref")
            if way_id not in ways:
                raise ValueError(f"relation member way {way_id} is missing")
            segments_by_role[member.get("role")].append(ways[way_id])

    outer_rings = join_rings(segments_by_role["outer"])
    inner_rings = join_rings(segments_by_role["inner"]) if segments_by_role["inner"] else []
    if inner_rings:
        raise ValueError("inner rings need explicit hole-to-outer assignment before export")
    polygons = []
    all_points: list[list[float]] = []
    total_area = 0.0
    for ring in outer_rings:
        coords = [nodes[node_id] for node_id in ring]
        if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in coords):
            raise ValueError("boundary has coordinates outside WGS84 lon/lat")
        if signed_area(coords) < 0:
            coords.reverse()
        polygons.append([coords])
        all_points.extend(coords)
        total_area += spherical_area_m2(coords)

    geometry: dict[str, Any] = {"type": "MultiPolygon", "coordinates": polygons}
    properties = {
        "source": "OpenStreetMap",
        "source_type": "administrative boundary; relation geometry assembled from member ways",
        "relation_id": int(args.relation_id),
        "relation_version": int(relation.get("version", "0")),
        "relation_timestamp_utc": relation.get("timestamp"),
        "name": tags.get("name"),
        "name_en": tags.get("name:en"),
        "admin_level": tags.get("admin_level"),
        "ref": tags.get("ref"),
        "osm_license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
    }
    feature = {"type": "Feature", "id": f"relation/{args.relation_id}", "geometry": geometry, "properties": properties}
    feature_collection = {"type": "FeatureCollection", "features": [feature]}
    bounds = [
        min(point[0] for point in all_points),
        min(point[1] for point in all_points),
        max(point[0] for point in all_points),
        max(point[1] for point in all_points),
    ]
    raw_mtime = args.input.stat().st_mtime
    import datetime as dt
    manifest = {
        "source_url": f"https://api.openstreetmap.org/api/0.6/relation/{args.relation_id}/full",
        "source_file": args.input.name,
        "source_bytes": args.input.stat().st_size,
        "source_sha256": sha256(args.input),
        "relation_id": int(args.relation_id),
        "relation_version": int(relation.get("version", "0")),
        "relation_timestamp_utc": relation.get("timestamp"),
        "retrieved_artifact_mtime_utc": dt.datetime.fromtimestamp(raw_mtime, dt.timezone.utc).isoformat(),
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "output_file": args.output.name,
        "output_sha256": None,
        "feature_count": 1,
        "outer_ring_count": len(outer_rings),
        "inner_ring_count": len(inner_rings),
        "outer_way_count": len(segments_by_role["outer"]),
        "bbox_wgs84": bounds,
        "approx_area_km2_spherical": round(total_area / 1_000_000, 3),
        "boundary_is_authoritative_legal_source": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(feature_collection, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    manifest["output_sha256"] = sha256(args.output)
    manifest_path = args.output.with_suffix(".manifest.json")
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: manifest[k] for k in (
        "relation_id", "relation_version", "relation_timestamp_utc", "outer_ring_count",
        "outer_way_count", "bbox_wgs84", "approx_area_km2_spherical", "output_sha256",
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ET.ParseError, ValueError) as exc:
        raise SystemExit(f"OSM boundary adapter error: {exc}")
