#!/usr/bin/env python3
"""Bounded Overture Buildings fetch/normalization adapter.

The fetch path uses Overture's official CLI. The normalized GeoJSON preserves
all original Overture properties under `source_properties`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "data" / "city"
DEFAULT_RELEASE = "2026-09-23.1"
OVERTURE_CLI_VERSION = "1.0.2"
MAX_BBOX_KM2 = 2.0
SCHEMA_URL = "https://docs.overturemaps.org/schema/reference/buildings/building/"
LICENSE_URL = "https://docs.overturemaps.org/attribution/"


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(x.strip()) for x in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be xmin,ymin,xmax,ymax") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be xmin,ymin,xmax,ymax")
    xmin, ymin, xmax, ymax = parts
    if not all(math.isfinite(v) for v in parts):
        raise argparse.ArgumentTypeError("bbox coordinates must be finite")
    if not (-180 <= xmin < xmax <= 180 and -90 <= ymin < ymax <= 90):
        raise argparse.ArgumentTypeError("bbox must be ordered WGS84 lon/lat bounds")
    return parts  # type: ignore[return-value]


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    xmin, ymin, xmax, ymax = bbox
    mean_lat = math.radians((ymin + ymax) / 2)
    width = (xmax - xmin) * 111.32 * math.cos(mean_lat)
    height = (ymax - ymin) * 111.32
    return width * height


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_positions(coords: Any) -> Iterator[tuple[float, float]]:
    if isinstance(coords, (list, tuple)) and len(coords) >= 2 and all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in coords[:2]
    ):
        yield float(coords[0]), float(coords[1])
        return
    if isinstance(coords, (list, tuple)):
        for item in coords:
            yield from iter_positions(item)


def geometry_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    points = list(iter_positions(geometry.get("coordinates")))
    if not points:
        raise ValueError("feature has no coordinate positions")
    if any(not math.isfinite(x) or not math.isfinite(y) for x, y in points):
        raise ValueError("feature contains a non-finite coordinate")
    if any(not (-180 <= x <= 180 and -90 <= y <= 90) for x, y in points):
        raise ValueError("feature contains coordinates outside WGS84 lon/lat range")
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def validate_geometry(geometry: Any) -> tuple[float, float, float, float]:
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("expected Polygon or MultiPolygon building geometry")
    bounds = geometry_bbox(geometry)

    def check_rings(obj: Any) -> None:
        if not isinstance(obj, list):
            raise ValueError("polygon coordinates must be arrays")
        if obj and isinstance(obj[0], (list, tuple)) and len(obj[0]) >= 2 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in obj[0][:2]
        ):
            if len(obj) < 4 or obj[0] != obj[-1]:
                raise ValueError("polygon ring must contain at least four positions and close")
            return
        for child in obj:
            check_rings(child)

    check_rings(geometry["coordinates"])
    return bounds


def normalize(
    raw: dict[str, Any], bbox: tuple[float, float, float, float], release: str
) -> tuple[dict[str, Any], dict[str, int]]:
    if raw.get("type") != "FeatureCollection" or not isinstance(raw.get("features"), list):
        raise ValueError("expected a GeoJSON FeatureCollection")
    features: list[dict[str, Any]] = []
    ids: set[str] = set()
    heights = floors = extensions = 0
    for index, feature in enumerate(raw["features"]):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError(f"feature {index} is not a GeoJSON Feature")
        source_properties = feature.get("properties") or {}
        if not isinstance(source_properties, dict):
            raise ValueError(f"feature {index} properties must be an object")
        source_id = feature.get("id", source_properties.get("id"))
        if source_id is None:
            raise ValueError(f"feature {index} has no stable Overture id")
        source_id = str(source_id)
        if source_id in ids:
            raise ValueError(f"duplicate feature id {source_id}")
        ids.add(source_id)
        bounds = validate_geometry(feature.get("geometry"))
        if bounds[0] < bbox[0] or bounds[1] < bbox[1] or bounds[2] > bbox[2] or bounds[3] > bbox[3]:
            extensions += 1
        if source_properties.get("height") is not None:
            heights += 1
        if source_properties.get("num_floors") is not None:
            floors += 1
        properties = {
            "source": "Overture",
            "source_id": source_id,
            "source_release": release,
            "source_version": source_properties.get("version"),
            "name": source_properties.get("names"),
            "height_m": source_properties.get("height"),
            "floors_above_ground": source_properties.get("num_floors"),
            "floors_underground": source_properties.get("num_floors_underground"),
            "min_height_m": source_properties.get("min_height"),
            "min_floor": source_properties.get("min_floor"),
            "building_class": source_properties.get("class"),
            "subtype": source_properties.get("subtype"),
            "has_parts": source_properties.get("has_parts"),
            "is_underground": source_properties.get("is_underground"),
            "facade_color": source_properties.get("facade_color"),
            "facade_material": source_properties.get("facade_material"),
            "roof_material": source_properties.get("roof_material"),
            "roof_shape": source_properties.get("roof_shape"),
            "roof_direction_deg": source_properties.get("roof_direction"),
            "roof_orientation": source_properties.get("roof_orientation"),
            "roof_color": source_properties.get("roof_color"),
            "roof_height_m": source_properties.get("roof_height"),
            "upstream_sources": source_properties.get("sources", []),
            "source_properties": source_properties,
        }
        features.append({
            "type": "Feature",
            "id": source_id,
            "geometry": feature["geometry"],
            "properties": properties,
        })
    return {"type": "FeatureCollection", "features": features}, {
        "feature_count": len(features),
        "height_present_count": heights,
        "floors_present_count": floors,
        "geometry_bounds_extend_beyond_query_bbox_count": extensions,
    }


def write_json_new(path: Path, data: Any) -> None:
    """Create a JSON output exclusively; never overwrite an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")


def make_plan(bbox: tuple[float, float, float, float], step: float) -> dict[str, Any]:
    xmin, ymin, xmax, ymax = bbox
    cells = []
    lat = ymin
    row = 0
    while lat < ymax - 1e-12:
        top = min(lat + step, ymax)
        lon = xmin
        col = 0
        while lon < xmax - 1e-12:
            right = min(lon + step, xmax)
            cell = (lon, lat, right, top)
            cells.append({
                "tile_id": f"r{row:03d}_c{col:03d}",
                "bbox": [round(lon, 8), round(lat, 8), round(right, 8), round(top, 8)],
                "approx_area_km2": round(bbox_area_km2(cell), 4),
                "download_status": "not_downloaded",
                "feature_count": None,
                "bytes": None,
            })
            lon = right
            col += 1
        lat = top
        row += 1
    return {
        "status": "plan_only",
        "scope": "input bounding rectangle; no Moscow boundary clip has been applied",
        "bbox_wgs84": list(bbox),
        "approx_bbox_area_km2": round(bbox_area_km2(bbox), 4),
        "tile_step_degrees": step,
        "tile_count": len(cells),
        "estimated_feature_count": None,
        "estimated_bytes": None,
        "volume_status": "unknown until each tile is fetched and measured",
        "tiles": cells,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbox", type=parse_bbox, required=True, help="WGS84 xmin,ymin,xmax,ymax")
    parser.add_argument("--release", default=DEFAULT_RELEASE, help="Pinned Overture data release")
    parser.add_argument("--label", default="moscow_sample", help="Output filename prefix")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--input-geojson", type=Path, help="Normalize an existing raw Overture GeoJSON")
    parser.add_argument("--plan-grid", action="store_true", help="Write a tile plan only; performs no downloads")
    parser.add_argument("--tile-step-degrees", type=float, default=0.05)
    parser.add_argument("--max-area-km2", type=float, default=MAX_BBOX_KM2)
    parser.add_argument("--allow-large", action="store_true", help="Allow one fetch above --max-area-km2")
    args = parser.parse_args()

    bbox = args.bbox
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.plan_grid:
        if not 0 < args.tile_step_degrees <= 1:
            parser.error("--tile-step-degrees must be in (0, 1]")
        plan = make_plan(bbox, args.tile_step_degrees)
        plan_path = args.output_dir / f"{args.label}_plan.json"
        try:
            write_json_new(plan_path, plan)
        except FileExistsError:
            parser.error(f"refusing to overwrite {plan_path}")
        print(json.dumps({k: plan[k] for k in (
            "status", "bbox_wgs84", "approx_bbox_area_km2", "tile_count",
            "estimated_feature_count", "estimated_bytes", "volume_status",
        )}, ensure_ascii=False))
        return 0

    area = bbox_area_km2(bbox)
    if args.input_geojson is None and area > args.max_area_km2:
        if not args.allow_large:
            parser.error(
                f"bbox is about {area:.3f} km2, above the {args.max_area_km2:.3f} km2 per-request guard; "
                "use --plan-grid or explicitly pass --allow-large"
            )
    raw_path = args.input_geojson or args.output_dir / f"{args.label}_raw.geojson"
    normalized_path = args.output_dir / f"{args.label}_normalized.geojson"
    manifest_path = args.output_dir / f"{args.label}_manifest.json"
    if normalized_path.exists() or manifest_path.exists() or (args.input_geojson is None and raw_path.exists()):
        parser.error("refusing to overwrite an existing output; choose a new --label")

    download_started = dt.datetime.now(dt.timezone.utc)
    if args.input_geojson is None:
        uvx = shutil.which("uvx")
        if not uvx:
            parser.error("uvx is required for the official Overture CLI fetch")
        command = [
            uvx, "--system-certs", "--from", f"overturemaps=={OVERTURE_CLI_VERSION}",
            "overturemaps", "download", "--bbox=" + ",".join(map(str, bbox)),
            "--release=" + args.release, "-f", "geojson", "--type=building", "-o", str(raw_path),
        ]
        env = os.environ.copy()
        system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
        configured_ca = Path(env.get("SSL_CERT_FILE", "/no/such/certificate/file"))
        if system_ca.is_file() and not configured_ca.is_file():
            env["SSL_CERT_FILE"] = str(system_ca)
        subprocess.run(command, check=True, env=env)
    download_finished = dt.datetime.now(dt.timezone.utc)

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    normalized, summary = normalize(raw, bbox, args.release)
    write_json_new(normalized_path, normalized)
    raw_mtime = dt.datetime.fromtimestamp(raw_path.stat().st_mtime, dt.timezone.utc).isoformat()
    manifest = {
        "source": "Overture Maps Buildings",
        "source_url": "https://docs.overturemaps.org/getting-data/",
        "source_release": args.release,
        "source_release_date": args.release.split(".", 1)[0],
        "schema_version": "v2.0.0" if args.release == DEFAULT_RELEASE else None,
        "cli": f"overturemaps=={OVERTURE_CLI_VERSION}",
        "query_type": "building",
        "bbox_wgs84": list(bbox),
        "bbox_area_approx_km2": round(area, 4),
        "selection_note": "bbox query; full returned feature geometries are retained without clipping",
        "license": "ODbL-1.0 for the Buildings theme; upstream records retain their source metadata",
        "license_url": LICENSE_URL,
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
        "schema_url": SCHEMA_URL,
        "retrieved_at_utc": download_started.isoformat() if args.input_geojson is None else None,
        "normalization_run_at_utc": download_finished.isoformat(),
        "raw_artifact_mtime_utc": raw_mtime,
        "raw_file": {"path": raw_path.name, "bytes": raw_path.stat().st_size, "sha256": file_sha256(raw_path)},
        "normalized_file": {"path": normalized_path.name, "bytes": normalized_path.stat().st_size, "sha256": file_sha256(normalized_path)},
        "volume_status": "measured for this bbox only; not a city-wide Moscow count",
        **summary,
    }
    write_json_new(manifest_path, manifest)
    print(json.dumps({
        "raw": str(raw_path),
        "normalized": str(normalized_path),
        "manifest": str(manifest_path),
        **summary,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"city adapter error: {exc}", file=sys.stderr)
        raise SystemExit(2)
