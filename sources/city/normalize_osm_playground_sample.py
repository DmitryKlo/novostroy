#!/usr/bin/env python3
"""Reuse the already-fetched tiny social POI snapshot for an OSM playground sample.

This script makes no network requests and does not claim coverage beyond the
source manifest's single central-Moscow Overpass bbox.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = ROOT / "data/social/osm_overpass_poi_raw.json"
DEFAULT_SOURCE_MANIFEST = ROOT / "data/social/poi_manifest.json"
DEFAULT_OUTPUT = ROOT / "data/city/environment/playgrounds_osm_pilot.geojson"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.input.is_file() or not args.source_manifest.is_file():
        parser.error("source Overpass JSON and its manifest are required")
    manifest_path = args.output.with_suffix(".manifest.json")
    if args.output.exists() or manifest_path.exists():
        parser.error("refusing to overwrite sample or manifest")
    source_manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw.get("elements"), list):
        parser.error("expected Overpass JSON with an elements list")
    features: list[dict[str, Any]] = []
    for element in raw["elements"]:
        tags = element.get("tags") or {}
        if tags.get("leisure") != "playground":
            continue
        point = None
        location_method = None
        approximate = False
        if element.get("type") == "node" and element.get("lon") is not None and element.get("lat") is not None:
            point = [float(element["lon"]), float(element["lat"])]
            location_method = "source_node_coordinate"
        else:
            center = element.get("center") or {}
            if center.get("lon") is not None and center.get("lat") is not None:
                point = [float(center["lon"]), float(center["lat"])]
                location_method = "overpass_derived_center"
                approximate = True
        if point is None:
            continue
        source_id = f"{element.get('type')}/{element.get('id')}"
        features.append({
            "type": "Feature",
            "id": source_id,
            "geometry": {"type": "Point", "coordinates": point},
            "properties": {
                "source": "OpenStreetMap",
                "source_id": source_id,
                "osm_type": element.get("type"),
                "osm_id": element.get("id"),
                "osm_version": element.get("version"),
                "osm_timestamp": element.get("timestamp"),
                "tags_raw": tags,
                "category": "playground",
                "location_method": location_method,
                "approximate": approximate,
                "source_coverage": source_manifest.get("coverage"),
            },
        })
    features.sort(key=lambda f: f["id"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"type": "FeatureCollection", "features": features}, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    manifest = {
        "status": "bounded_sample_only",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_manifest": str(args.source_manifest.relative_to(ROOT)),
        "source_manifest_sha256": sha256_file(args.source_manifest),
        "source_raw": str(args.input.relative_to(ROOT)),
        "source_raw_sha256": sha256_file(args.input),
        "source_raw_bytes": args.input.stat().st_size,
        "source_endpoint": source_manifest.get("endpoint"),
        "source_bbox_latlon": source_manifest.get("bbox"),
        "source_query": source_manifest.get("query"),
        "osm_base_timestamp": source_manifest.get("osm_base_timestamp"),
        "retrieved_at": source_manifest.get("retrieved_at"),
        "retrieval_method": source_manifest.get("retrieval_method"),
        "license": source_manifest.get("license"),
        "license_url": source_manifest.get("license_url"),
        "attribution": source_manifest.get("required_attribution"),
        "feature_count": len(features),
        "node_point_count": sum(f["properties"]["location_method"] == "source_node_coordinate" for f in features),
        "approximate_center_count": sum(f["properties"]["approximate"] for f in features),
        "output_file": str(args.output.relative_to(ROOT)),
        "output_bytes": args.output.stat().st_size,
        "output_sha256": sha256_file(args.output),
        "coverage_completeness": "unknown",
        "absence_semantics": "no matching feature is not confirmed absence; this is only one bounded sample query",
    }
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status": manifest["status"], "features": len(features), "output": str(args.output), "sha256": manifest["output_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
