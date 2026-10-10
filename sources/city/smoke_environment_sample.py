#!/usr/bin/env python3
"""Run environmental proximity derivations against existing real pilot files.

Offline smoke only: it does not fetch data and does not imply citywide OSM
playground completeness, acoustic exposure, or a real-estate listing score.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sources.city.environment_derived import (  # noqa: E402
    METHOD,
    count_features_within_m,
    geometry_centroid_lonlat,
    nearest_feature_distance,
)
BUILDING_FILE = ROOT / "data/city/overture_moscow_sample_normalized.geojson"
BUILDING_MANIFEST = ROOT / "data/city/overture_moscow_sample_manifest.json"
PLAYGROUND_FILE = ROOT / "data/city/environment/playgrounds_osm_pilot.geojson"
PLAYGROUND_MANIFEST = ROOT / "data/city/environment/playgrounds_osm_pilot.manifest.json"
OUTPUT = ROOT / "data/city/environment/environment_proxy_smoke.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_file(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        "file": str(path.relative_to(ROOT)),
        "sha256": sha256_file(path),
        "manifest": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")
    for path in (BUILDING_FILE, BUILDING_MANIFEST, PLAYGROUND_FILE, PLAYGROUND_MANIFEST):
        if not path.is_file():
            parser.error(f"required sample is missing: {path}")
    buildings = json.loads(BUILDING_FILE.read_text(encoding="utf-8"))["features"]
    playgrounds = json.loads(PLAYGROUND_FILE.read_text(encoding="utf-8"))["features"]
    points = [geometry_centroid_lonlat(feature["geometry"]) for feature in buildings]
    results = []
    for building, point in zip(buildings, points):
        nearest = nearest_feature_distance(point, playgrounds)
        count = count_features_within_m(point, playgrounds, 500)
        results.append({
            "building_source_id": building.get("id", (building.get("properties") or {}).get("source_id")),
            "anchor_lonlat": [round(point[0], 7), round(point[1], 7)],
            "anchor_method": "planar_polygon_centroid_lonlat",
            "nearest_mapped_playground": nearest,
            "mapped_playground_count_500m": count,
            "distance_scope": "pilot_query_bbox_only",
        })
    nearest_distances = [item["nearest_mapped_playground"]["value_m"] for item in results if item["nearest_mapped_playground"]]
    counts = [item["mapped_playground_count_500m"]["value"] for item in results]
    artifact = {
        "status": "offline_real_sample_smoke_not_citywide",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": METHOD,
        "distance_semantics": "approximate map geometry proximity only; not measured noise, an acoustic score, or accessibility",
        "count_semantics": "mapped OSM features in one bounded query result; no feature absence is not confirmed real-world absence",
        "inputs": {
            "buildings": _source_file(BUILDING_MANIFEST),
            "playgrounds": _source_file(PLAYGROUND_MANIFEST),
        },
        "building_feature_count": len(buildings),
        "playground_feature_count": len(playgrounds),
        "nearest_playground_distance_m_summary": {
            "min": min(nearest_distances) if nearest_distances else None,
            "median": statistics.median(nearest_distances) if nearest_distances else None,
            "max": max(nearest_distances) if nearest_distances else None,
        },
        "mapped_playground_count_500m_summary": {
            "min": min(counts) if counts else None,
            "median": statistics.median(counts) if counts else None,
            "max": max(counts) if counts else None,
        },
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(artifact, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status": artifact["status"], "buildings": len(buildings), "playgrounds": len(playgrounds), "nearest_median_m": artifact["nearest_playground_distance_m_summary"]["median"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
