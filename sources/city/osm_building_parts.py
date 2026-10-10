#!/usr/bin/env python3
"""Extract Moscow OSM buildings, parts and building relations from a pinned PBF.

The source PBF is not modified. The adapter creates a small tag-filtered PBF in
a temporary directory, clips polygonal geometries to the pinned Moscow OSM
boundary, and writes source tags plus geometry to GeoParquet. OSM height tags
remain source claims; they are never represented as survey measurements.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import resource
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import osmium
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import polygonize
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PBF = ROOT / "data/social/osm_moscow_city_261008.osm.pbf"
DEFAULT_PBF_MANIFEST = ROOT / "data/social/osm_moscow_city_261008.manifest.json"
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"
DEFAULT_OVERTURE = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
DEFAULT_OVERTURE_MANIFEST = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json"
DEFAULT_OVERTURE_PARTS = ROOT / "data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.parquet"
DEFAULT_OVERTURE_PARTS_MANIFEST = ROOT / "data/city/overture_moscow_building_parts_2026-09-23.1_cityclipped.manifest.json"
DEFAULT_OUTPUT = ROOT / "data/city/osm_moscow_buildings_parts_261008.parquet"
DEFAULT_EXAMPLES = ROOT / "data/city/osm_moscow_multilevel_building_examples_261008.geojson"
OSMIUM = ROOT / ".runtime/geodata-env/bin/osmium"
FILTERS = ("w/building", "w/building:part", "r/building", "r/building:part", "r/type=building")
BATCH_SIZE = 2048
DIMENSION_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))"
    r"(?:\s+(m|metre|metres|meter|meters|km|kilometre|kilometres|kilometer|kilometers|"
    r"mi|mile|miles|nmi|yd|yard|yards|ft|foot|feet|in|inch|inches))?\s*$",
    re.IGNORECASE,
)
FEET_INCHES_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)'\s*(\d+(?:[.,]\d+)?)\"\s*$")
FEET_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)'\s*$")
UNIT_FACTORS = {
    "m": 1.0, "metre": 1.0, "metres": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0,
    "kilometer": 1000.0, "kilometers": 1000.0,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "nmi": 1852.0, "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def tags_dict(tags: Any) -> dict[str, str]:
    return {tag.k: tag.v for tag in tags}


def parse_dimension(raw: str | None, *, positive: bool = False) -> tuple[float | None, str]:
    if raw is None or not raw.strip():
        return None, "missing"
    feet_inches = FEET_INCHES_RE.fullmatch(raw)
    if feet_inches:
        feet, inches = (float(part.replace(",", ".")) for part in feet_inches.groups())
        if not 0 <= inches < 12:
            return None, "invalid_feet_inches"
        value = (feet * 12 + inches) * 0.0254
        if positive and value <= 0:
            return value, "nonpositive"
        return value, "parsed"
    feet_only = FEET_RE.fullmatch(raw)
    if feet_only:
        value = float(feet_only.group(1).replace(",", ".")) * 0.3048
        if positive and value <= 0:
            return value, "nonpositive"
        return value, "parsed"
    match = DIMENSION_RE.fullmatch(raw)
    if not match:
        return None, "unsupported_syntax_or_unit"
    number, unit = match.groups()
    try:
        value = float(number.replace(",", ".")) * UNIT_FACTORS.get((unit or "m").lower(), 1.0)
    except ValueError:
        return None, "invalid_number"
    if not math.isfinite(value):
        return None, "non_finite"
    if positive and value <= 0:
        return value, "nonpositive"
    return value, "parsed"


def parse_count(raw: str | None, *, positive: bool = False) -> tuple[float | None, str]:
    if raw is None or not raw.strip():
        return None, "missing"
    value, status = parse_dimension(raw)
    if status != "parsed":
        return None, status
    # Level tags are counts, not lengths: only bare scalar values are accepted.
    match = DIMENSION_RE.fullmatch(raw)
    if not match or match.group(2):
        return None, "unsupported_syntax_or_unit"
    if positive and value is not None and value <= 0:
        return value, "nonpositive"
    return value, "parsed"


def property_fields(tags: dict[str, str]) -> dict[str, Any]:
    height_m, height_status = parse_dimension(tags.get("height"), positive=True)
    min_height_m, min_height_status = parse_dimension(tags.get("min_height"))
    roof_height_m, roof_height_status = parse_dimension(tags.get("roof:height"), positive=True)
    levels, levels_status = parse_count(tags.get("building:levels"), positive=True)
    min_level, min_level_status = parse_count(tags.get("building:min_level"))
    roof_levels, roof_levels_status = parse_count(tags.get("roof:levels"))
    direction, direction_status = parse_count(tags.get("roof:direction"))
    if direction is not None and not (0 <= direction <= 360):
        direction, direction_status = None, "outside_0_360"
    return {
        "building_tag": tags.get("building"),
        "building_part_tag": tags.get("building:part"),
        "height_tag_raw": tags.get("height"), "height_m": height_m, "height_parse_status": height_status,
        "min_height_tag_raw": tags.get("min_height"), "min_height_m": min_height_m,
        "min_height_parse_status": min_height_status,
        "building_levels_tag_raw": tags.get("building:levels"), "building_levels": levels,
        "building_levels_parse_status": levels_status,
        "building_min_level_tag_raw": tags.get("building:min_level"), "building_min_level": min_level,
        "building_min_level_parse_status": min_level_status,
        "roof_height_tag_raw": tags.get("roof:height"), "roof_height_m": roof_height_m,
        "roof_height_parse_status": roof_height_status,
        "roof_levels_tag_raw": tags.get("roof:levels"), "roof_levels": roof_levels,
        "roof_levels_parse_status": roof_levels_status,
        "roof_shape": tags.get("roof:shape"), "roof_direction_tag_raw": tags.get("roof:direction"),
        "roof_direction_deg": direction, "roof_direction_parse_status": direction_status,
    }


def polygonal(geometry: Any) -> Any | None:
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    pieces = []
    for part in getattr(geometry, "geoms", []):
        result = polygonal(part)
        if result is not None:
            pieces.extend([result] if result.geom_type == "Polygon" else list(result.geoms))
    if not pieces:
        return None
    merged = shapely.union_all(pieces)
    return merged if merged.geom_type in {"Polygon", "MultiPolygon"} else None


def clipped_geometry(geometry: Any, boundary: Any) -> tuple[Any | None, str]:
    if geometry is None or geometry.is_empty:
        return None, "missing_geometry"
    if not geometry.is_valid:
        geometry = polygonal(shapely.make_valid(geometry))
        if geometry is None:
            return None, "invalid_no_polygon"
    try:
        if not geometry.intersects(boundary):
            return None, "outside_boundary"
        clipped = geometry if boundary.covers(geometry) else polygonal(geometry.intersection(boundary))
        if clipped is None or clipped.is_empty:
            return None, "touch_only_or_nonpolygonal"
        if not clipped.is_valid:
            clipped = polygonal(shapely.make_valid(clipped))
        if clipped is None or clipped.is_empty:
            return None, "invalid_after_clip"
        return clipped, "clipped" if clipped is not geometry else "inside_boundary"
    except Exception:
        return None, "clip_error"


def way_geometry(way: Any) -> Any | None:
    points = []
    for node in way.nodes:
        if not node.location.valid():
            return None
        points.append((node.location.lon, node.location.lat))
    if len(points) < 2:
        return None
    if way.is_closed() and len(points) >= 4:
        polygon = Polygon(points)
        return polygon if polygon.is_valid else polygonal(shapely.make_valid(polygon))
    try:
        return LineString(points)
    except Exception:
        return None


def relation_geometry(info: dict[str, Any], member_geometries: dict[int, Any]) -> Any | None:
    outlines = []
    inners = []
    outline_lines = []
    inner_lines = []
    parts = []
    outer_lines = []
    for member in info["members"]:
        if member["type"] != "w":
            continue
        geom = member_geometries.get(member["ref"])
        if geom is None:
            continue
        role = member["role"]
        if role in {"outline", "outer", ""}:
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                outlines.append(geom)
            elif geom.geom_type in {"LineString", "MultiLineString"}:
                outline_lines.append(geom)
        if role == "inner":
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                inners.append(geom)
            elif geom.geom_type in {"LineString", "MultiLineString"}:
                inner_lines.append(geom)
        if role in {"part", "buildingpart"}:
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                parts.append(geom)
            elif geom.geom_type in {"LineString", "MultiLineString"}:
                outer_lines.append(geom)
    if outlines:
        outer = shapely.union_all(outlines)
        if inners:
            outer = outer.difference(shapely.union_all(inners))
        return polygonal(outer)
    if outline_lines:
        rings = list(polygonize(shapely.union_all(outline_lines)))
        if rings:
            outer = shapely.union_all(rings)
            if inner_lines:
                inner_rings = list(polygonize(shapely.union_all(inner_lines)))
                if inner_rings:
                    outer = outer.difference(shapely.union_all(inner_rings))
            elif inners:
                outer = outer.difference(shapely.union_all(inners))
            return polygonal(outer)
    if parts:
        return polygonal(shapely.union_all(parts))
    if outer_lines:
        rings = list(polygonize(shapely.union_all(outer_lines)))
        if rings:
            return polygonal(shapely.union_all(rings))
    return None


def geo_schema() -> pa.Schema:
    fields = [
        pa.field("osm_type", pa.string()), pa.field("osm_id", pa.int64()),
        pa.field("osm_version", pa.int32()), pa.field("osm_timestamp", pa.string()),
        pa.field("entity_kind", pa.string()), pa.field("geometry", pa.binary()),
        pa.field("source_geometry", pa.binary()), pa.field("geometry_status", pa.string()),
        pa.field("osm_tags_json", pa.string()), pa.field("member_refs_json", pa.string()),
        pa.field("building_relation_ids", pa.list_(pa.int64())),
        pa.field("building_relation_roles_json", pa.string()),
        pa.field("building_tag", pa.string()), pa.field("building_part_tag", pa.string()),
        pa.field("height_tag_raw", pa.string()), pa.field("height_m", pa.float64()),
        pa.field("height_parse_status", pa.string()), pa.field("min_height_tag_raw", pa.string()),
        pa.field("min_height_m", pa.float64()), pa.field("min_height_parse_status", pa.string()),
        pa.field("building_levels_tag_raw", pa.string()), pa.field("building_levels", pa.float64()),
        pa.field("building_levels_parse_status", pa.string()), pa.field("building_min_level_tag_raw", pa.string()),
        pa.field("building_min_level", pa.float64()), pa.field("building_min_level_parse_status", pa.string()),
        pa.field("roof_height_tag_raw", pa.string()), pa.field("roof_height_m", pa.float64()),
        pa.field("roof_height_parse_status", pa.string()), pa.field("roof_levels_tag_raw", pa.string()),
        pa.field("roof_levels", pa.float64()), pa.field("roof_levels_parse_status", pa.string()),
        pa.field("roof_shape", pa.string()), pa.field("roof_direction_tag_raw", pa.string()),
        pa.field("roof_direction_deg", pa.float64()), pa.field("roof_direction_parse_status", pa.string()),
        pa.field("overture_match_method", pa.string()), pa.field("overture_feature_id", pa.string()),
        pa.field("overture_source_record_id", pa.string()), pa.field("overture_source_version", pa.int32()),
        pa.field("overture_height_m", pa.float64()), pa.field("overture_num_floors", pa.int32()),
        pa.field("overture_min_height_m", pa.float64()), pa.field("overture_min_floor", pa.int32()),
        pa.field("overture_roof_height_m", pa.float64()), pa.field("overture_roof_shape", pa.string()),
        pa.field("overture_roof_direction_deg", pa.float64()),
        pa.field("overture_feature_type", pa.string()), pa.field("overture_building_id", pa.string()),
        pa.field("overture_overlap_iou", pa.float64()),
        pa.field("spatial_candidate_feature_id", pa.string()), pa.field("spatial_candidate_height_m", pa.float64()),
        pa.field("spatial_candidate_num_floors", pa.int32()),
        pa.field("source_attribution", pa.string()),
    ]
    geo = {
        "version": "1.1.0", "primary_column": "geometry",
        "columns": {
            "geometry": {"encoding": "WKB", "geometry_types": ["Polygon", "MultiPolygon"]},
            "source_geometry": {"encoding": "WKB", "geometry_types": ["Polygon", "MultiPolygon"]},
        },
    }
    metadata = {b"geo": json.dumps(geo, separators=(",", ":")).encode(),
                b"source_license": b"ODbL-1.0", b"source_attribution": b"(c) OpenStreetMap contributors"}
    return pa.schema(fields, metadata=metadata)


class RelationPass(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.relations: dict[int, dict[str, Any]] = {}
        self.way_to_relations: dict[int, list[tuple[int, str]]] = defaultdict(list)

    def relation(self, relation: Any) -> None:
        tags = tags_dict(relation.tags)
        if not (tags.get("type") == "building" or "building" in tags or "building:part" in tags):
            return
        members = [{"type": m.type, "ref": int(m.ref), "role": m.role or ""} for m in relation.members]
        info = {"id": int(relation.id), "version": int(relation.version), "timestamp": timestamp(relation.timestamp),
                "tags": tags, "members": members}
        self.relations[int(relation.id)] = info
        for member in members:
            if member["type"] == "w" and member["role"] in {"", "outline", "outer", "inner", "part", "buildingpart"}:
                self.way_to_relations[member["ref"]].append((int(relation.id), member["role"]))


def way_row(way: Any, geometry: Any, clipped: Any, status: str, memberships: list[tuple[int, str]]) -> dict[str, Any]:
    tags = tags_dict(way.tags)
    roles = [{"relation_id": rel_id, "role": role} for rel_id, role in memberships]
    roles_set = {role for _, role in memberships}
    if "building:part" in tags:
        kind = "building_part"
    elif "part" in roles_set:
        kind = "relation_member_part"
    elif "building" in tags:
        kind = "building"
    else:
        kind = "relation_member_outline"
    return {
        "osm_type": "way", "osm_id": int(way.id), "osm_version": int(way.version),
        "osm_timestamp": timestamp(way.timestamp), "entity_kind": kind,
        "geometry": shapely.to_wkb(clipped) if clipped is not None else None,
        "source_geometry": shapely.to_wkb(geometry) if geometry is not None and geometry.geom_type in {"Polygon", "MultiPolygon"} else None,
        "geometry_status": status, "osm_tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True),
        "member_refs_json": "[]", "building_relation_ids": sorted({rel_id for rel_id, _ in memberships}),
        "building_relation_roles_json": json.dumps(roles, ensure_ascii=False, separators=(",", ":")),
        **property_fields(tags), "source_attribution": "© OpenStreetMap contributors",
    }


def relation_row(info: dict[str, Any], geometry: Any, clipped: Any, status: str) -> dict[str, Any]:
    tags = info["tags"]
    if tags.get("type") == "building":
        kind = "building_relation"
    elif "building:part" in tags:
        kind = "building_multipolygon_part_relation"
    else:
        kind = "building_multipolygon_relation"
    return {
        "osm_type": "relation", "osm_id": info["id"], "osm_version": info["version"],
        "osm_timestamp": info["timestamp"], "entity_kind": kind,
        "geometry": shapely.to_wkb(clipped) if clipped is not None else None,
        "source_geometry": shapely.to_wkb(geometry) if geometry is not None else None,
        "geometry_status": status, "osm_tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True),
        "member_refs_json": json.dumps(info["members"], ensure_ascii=False, separators=(",", ":")),
        "building_relation_ids": [info["id"]], "building_relation_roles_json": "[]",
        **property_fields(tags), "source_attribution": "© OpenStreetMap contributors",
    }


def write_batch(writer: pq.ParquetWriter, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    if rows:
        writer.write_table(pa.Table.from_pylist(rows, schema=schema), row_group_size=len(rows))
        rows.clear()


def source_identity(record_id: str | None) -> tuple[str, int, int | None] | None:
    if not record_id:
        return None
    match = re.fullmatch(r"([nwr])(\d+)(?:@(\d+))?", record_id)
    if not match:
        return None
    type_code, ident, version = match.groups()
    return {"n": "node", "w": "way", "r": "relation"}[type_code], int(ident), int(version) if version else None


def parse_overture_records(
    inputs: list[tuple[str, Path]],
) -> tuple[dict[tuple[str, int], list[dict[str, Any]]], Counter[str]]:
    """Index source record IDs from both Building and BuildingPart city extracts."""
    index: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    source_counts: Counter[str] = Counter()
    for feature_type, path in inputs:
        parquet = pq.ParquetFile(path)
        columns = ["id", "sources", "height", "num_floors", "min_height", "min_floor",
                   "roof_height", "roof_shape", "roof_direction"]
        if "building_id" in parquet.schema_arrow.names:
            columns.append("building_id")
        for batch in parquet.iter_batches(batch_size=8192, columns=columns):
            for row in batch.to_pylist():
                for source in row.get("sources") or []:
                    if source.get("dataset") != "OpenStreetMap":
                        continue
                    identity = source_identity(source.get("record_id"))
                    if identity is None:
                        continue
                    osm_type, osm_id, version = identity
                    index[(osm_type, osm_id)].append({
                        "feature_id": row["id"], "record_id": source.get("record_id"),
                        "source_version": version, "height_m": row.get("height"),
                        "num_floors": row.get("num_floors"), "feature_type": feature_type,
                        "building_id": row.get("building_id"), "min_height_m": row.get("min_height"),
                        "min_floor": row.get("min_floor"), "roof_height_m": row.get("roof_height"),
                        "roof_shape": row.get("roof_shape"), "roof_direction_deg": row.get("roof_direction"),
                    })
                    source_counts[feature_type] += 1
    return index, source_counts


def load_overture_geometries(path: Path) -> tuple[list[Any], list[str], list[float | None], list[int | None]]:
    parquet = pq.ParquetFile(path)
    geometries: list[Any] = []
    ids: list[str] = []
    heights: list[float | None] = []
    floors: list[int | None] = []
    for batch in parquet.iter_batches(batch_size=8192, columns=["id", "geometry", "height", "num_floors"]):
        table = batch.to_pydict()
        geoms = shapely.from_wkb(table["geometry"])
        geometries.extend(list(geoms))
        ids.extend(table["id"])
        heights.extend(table["height"])
        floors.extend(table["num_floors"])
    return geometries, ids, heights, floors


def choose_spatial_match(geometry: Any, tree: STRtree, ids: list[str], heights: list[Any], floors: list[Any]) -> dict[str, Any]:
    candidates = []
    for raw_index in tree.query(geometry):
        index = int(raw_index)
        candidate = tree.geometries.take([index])[0] if hasattr(tree, "geometries") else None
        if candidate is None:
            continue
        if not geometry.intersects(candidate):
            continue
        area_a, area_b = geometry.area, candidate.area
        if area_a <= 0 or area_b <= 0:
            continue
        ratio = area_a / area_b
        if not 0.8 <= ratio <= 1.25:
            continue
        intersection = geometry.intersection(candidate).area
        iou = intersection / (area_a + area_b - intersection)
        if iou >= 0.85:
            candidates.append((iou, index))
    candidates.sort(reverse=True)
    if not candidates:
        return {"method": "no_match", "feature_id": None, "iou": None, "height": None, "floors": None}
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.05:
        return {"method": "ambiguous_spatial_overlap", "feature_id": None,
                "iou": candidates[0][0], "height": None, "floors": None}
    score, index = candidates[0]
    return {"method": "strong_spatial_overlap_candidate", "feature_id": ids[index], "iou": score,
            "height": heights[index], "floors": floors[index]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF)
    parser.add_argument("--pbf-manifest", type=Path, default=DEFAULT_PBF_MANIFEST)
    parser.add_argument("--osmium", type=Path, default=OSMIUM)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--overture", type=Path, default=DEFAULT_OVERTURE)
    parser.add_argument("--overture-manifest", type=Path, default=DEFAULT_OVERTURE_MANIFEST)
    parser.add_argument("--overture-parts", type=Path, default=DEFAULT_OVERTURE_PARTS)
    parser.add_argument("--overture-parts-manifest", type=Path, default=DEFAULT_OVERTURE_PARTS_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    manifest_path = args.output.with_suffix(".manifest.json")
    if args.output.exists() or manifest_path.exists() or args.examples.exists():
        parser.error("refusing to overwrite existing output; choose new output paths")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    for path in (args.pbf, args.pbf_manifest, args.osmium, args.boundary, args.boundary_manifest,
                 args.overture, args.overture_manifest, args.overture_parts, args.overture_parts_manifest):
        if not path.is_file():
            parser.error(f"required file does not exist: {path}")

    source_manifest = json.loads(args.pbf_manifest.read_text(encoding="utf-8"))
    source_info = source_manifest["extraction"]
    if source_info.get("sha256") != sha256(args.pbf):
        parser.error("PBF SHA256 does not match its pinned manifest")
    boundary_manifest = json.loads(args.boundary_manifest.read_text(encoding="utf-8"))
    overture_manifest = json.loads(args.overture_manifest.read_text(encoding="utf-8"))
    overture_parts_manifest = json.loads(args.overture_parts_manifest.read_text(encoding="utf-8"))
    boundary_doc = json.loads(args.boundary.read_text(encoding="utf-8"))
    boundary = shape(boundary_doc["features"][0]["geometry"])
    if not boundary.is_valid or boundary.is_empty:
        parser.error("pinned Moscow boundary must be valid and nonempty")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.examples.parent.mkdir(parents=True, exist_ok=True)
    schema = geo_schema()
    counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    relation_counts: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    output_rows = 0
    filtered_subset_bytes = 0

    with tempfile.TemporaryDirectory(prefix="osm-moscow-building-parts-") as temp_dir:
        filtered = Path(temp_dir) / "buildings_relations.pbf"
        command = [str(args.osmium), "tags-filter", "--no-progress", "--output", str(filtered), str(args.pbf), *FILTERS]
        subprocess.run(command, check=True, capture_output=True, text=True)
        filtered_hash = sha256(filtered)
        filtered_subset_bytes = filtered.stat().st_size

        relation_pass = RelationPass()
        relation_pass.apply_file(str(filtered))
        relation_counts["selected_relations"] = len(relation_pass.relations)
        for info in relation_pass.relations.values():
            tags = info["tags"]
            if tags.get("type") == "building":
                relation_counts["type_building"] += 1
            elif "building:part" in tags:
                relation_counts["tagged_building_part_relations"] += 1
            else:
                relation_counts["tagged_building_relations"] += 1
            for member in info["members"]:
                relation_counts[f"member_role:{member['role'] or '<empty>'}"] += 1

        raw_path = Path(temp_dir) / "raw_features.parquet"
        raw_writer = pq.ParquetWriter(raw_path, schema, compression="zstd", version="2.6")
        pending: list[dict[str, Any]] = []
        relation_member_geometries: dict[int, Any] = {}
        relation_part_summaries: dict[int, list[dict[str, Any]]] = defaultdict(list)

        class WayPass(osmium.SimpleHandler):
            def way(self, way: Any) -> None:
                nonlocal output_rows
                tags = tags_dict(way.tags)
                memberships = relation_pass.way_to_relations.get(int(way.id), [])
                tagged = "building" in tags or "building:part" in tags
                if not (tagged or memberships):
                    return
                counts["selected_way_entities"] += 1
                for key in ("height", "min_height", "building:levels", "building:min_level",
                            "roof:height", "roof:levels", "roof:shape", "roof:direction"):
                    if key in tags:
                        tag_counts[key] += 1
                geom = way_geometry(way)
                if geom is None:
                    counts["ways_without_reconstructable_geometry"] += 1
                clipped = None
                status = "missing_geometry"
                if geom is not None and geom.geom_type in {"Polygon", "MultiPolygon"}:
                    clipped, status = clipped_geometry(geom, boundary)
                elif geom is not None:
                    status = "open_way_not_polygon"
                if memberships and geom is not None:
                    relation_member_geometries[int(way.id)] = geom
                for rel_id, role in memberships:
                    if role in {"part", "buildingpart"} and geom is not None and geom.geom_type in {"Polygon", "MultiPolygon"}:
                        relation_part_summaries[rel_id].append({
                            "way_id": int(way.id), "way_version": int(way.version),
                            "way_timestamp": timestamp(way.timestamp), "tags": tags, "geometry": geom,
                            "clipped": clipped, "status": status,
                        })
                include = False
                if clipped is not None:
                    volume_member = any(role in {"", "outline", "outer", "part", "buildingpart"} for _, role in memberships)
                    include = tagged or volume_member
                elif geom is not None and geom.geom_type in {"LineString", "MultiLineString"}:
                    counts["open_way_entities"] += 1
                else:
                    counts["unpolygonal_way_entities"] += 1
                if clipped is None and geom is not None and memberships:
                    # Relation-only line segments stay in relation member refs; they are not rendered as footprints.
                    if geom.intersects(boundary):
                        counts["relation_members_intersecting_boundary_without_area"] += 1
                if not include:
                    if status == "outside_boundary":
                        counts["ways_outside_boundary"] += 1
                    return
                row = way_row(way, geom, clipped, status, memberships)
                pending.append(row)
                entity_counts[row["entity_kind"]] += 1
                counts["clipped_part_ways" if row["entity_kind"] in {"building_part", "relation_member_part"} else "clipped_building_ways"] += 1
                output_rows += 1
                if len(pending) >= args.batch_size:
                    write_batch(raw_writer, pending, schema)

        way_pass = WayPass()
        way_pass.apply_file(str(filtered), locations=True, idx="flex_mem")
        counts.update({f"relation_{key}": value for key, value in Counter(way_pass.handler_stats).items()} if hasattr(way_pass, "handler_stats") else {})

        # Relations use explicit outline/part member roles. Parent geometry is context only;
        # individual part ways remain separate source features.
        relation_geometries: dict[int, Any] = {}
        for rel_id, info in relation_pass.relations.items():
            geom = relation_geometry(info, relation_member_geometries)
            relation_geometries[rel_id] = geom
            clipped, status = clipped_geometry(geom, boundary)
            if clipped is None:
                counts["relations_without_boundary_polygon"] += 1
                continue
            row = relation_row(info, geom, clipped, status)
            pending.append(row)
            entity_counts[row["entity_kind"]] += 1
            relation_counts["clipped_relation_rows"] += 1
            output_rows += 1
            if len(pending) >= args.batch_size:
                write_batch(raw_writer, pending, schema)

        example_relation_count = 0
        for rel_id, parts in relation_part_summaries.items():
            if relation_geometries.get(rel_id) is None:
                continue
            visible_parts = [part for part in parts if part["clipped"] is not None]
            if len(visible_parts) < 2:
                continue
            tagged_dims = []
            for part in visible_parts:
                tags = part["tags"]
                height, hs = parse_dimension(tags.get("height"), positive=True)
                levels, ls = parse_count(tags.get("building:levels"), positive=True)
                val = ("height_m", height) if hs == "parsed" else (("building_levels", levels) if ls == "parsed" else None)
                if val:
                    tagged_dims.append({"way_id": part["way_id"], "metric": val[0], "value": val[1], "tags": tags})
            unique_metrics = {x["metric"] for x in tagged_dims}
            differing = False
            if len(unique_metrics) == 1:
                vals = [x["value"] for x in tagged_dims]
                differing = max(vals) - min(vals) >= (2.0 if tagged_dims[0]["metric"] == "height_m" else 1.0)
            if differing:
                relation_counts["multi_part_relation_with_distinct_tagged_height_or_levels"] += 1
                if example_relation_count < 6:
                    example_relation_count += 1
                    for part in visible_parts:
                        if part["clipped"] is None:
                            continue
                        examples.append({
                            "type": "Feature", "id": f"way/{part['way_id']}",
                            "geometry": mapping(part["clipped"]),
                            "properties": {
                                "relation_id": rel_id, "osm_type": "way", "osm_id": part["way_id"],
                                "osm_version": part["way_version"],
                                "osm_timestamp": part["way_timestamp"],
                                "entity_kind": "building_part_relation_member",
                                "height_tag": part["tags"].get("height"),
                                "min_height_tag": part["tags"].get("min_height"),
                                "building_levels_tag": part["tags"].get("building:levels"),
                                "building_min_level_tag": part["tags"].get("building:min_level"),
                                "roof_height_tag": part["tags"].get("roof:height"),
                                "roof_levels_tag": part["tags"].get("roof:levels"),
                                "roof_shape_tag": part["tags"].get("roof:shape"),
                                "roof_direction_tag": part["tags"].get("roof:direction"),
                                "building_part_tag": part["tags"].get("building:part"),
                                "relation_version": relation_pass.relations[rel_id]["version"],
                                "relation_timestamp": relation_pass.relations[rel_id]["timestamp"],
                                "way_tag_source": part["tags"].get("source") or part["tags"].get("source:height") or part["tags"].get("source_ref:building:levels"),
                                "provenance": "OSM tags and relation membership; not survey-verified",
                                "attribution": "© OpenStreetMap contributors",
                            },
                        })

        write_batch(raw_writer, pending, schema)
        raw_writer.close()

        # Match by OSM record identity first. Spatial overlap is a strict, labeled candidate only.
        overture_inputs = [("building", args.overture), ("building_part", args.overture_parts)]
        overture_index, overture_osm_source_records = parse_overture_records(overture_inputs)
        raw_file = pq.ParquetFile(raw_path)
        unmatched_polygon_count = 0
        exact_match_count = 0
        for batch in raw_file.iter_batches(batch_size=8192, columns=["osm_type", "osm_id", "entity_kind", "geometry"]):
            for row in batch.to_pylist():
                exact = overture_index.get((row["osm_type"], row["osm_id"]), [])
                if exact:
                    exact_match_count += 1
                elif row["entity_kind"] in {"building", "relation_member_outline"} and row["geometry"]:
                    unmatched_polygon_count += 1

        spatial_tree = None
        spatial_geometries: list[Any] = []
        spatial_ids: list[str] = []
        spatial_heights: list[Any] = []
        spatial_floors: list[Any] = []
        if unmatched_polygon_count:
            spatial_geometries, spatial_ids, spatial_heights, spatial_floors = load_overture_geometries(args.overture)
            spatial_tree = STRtree(spatial_geometries)

        output_writer = pq.ParquetWriter(args.output, schema, compression="zstd", version="2.6")
        final_batch: list[dict[str, Any]] = []
        matching = Counter()
        new_sources = Counter()
        conflicts = Counter()
        for batch in raw_file.iter_batches(batch_size=4096):
            for row in batch.to_pylist():
                identity = (row["osm_type"], row["osm_id"])
                exact = overture_index.get(identity, [])
                expected_type = "building_part" if row["entity_kind"] in {
                    "building_part", "relation_member_part", "building_multipolygon_part_relation"
                } else "building"
                preferred = [item for item in exact if item["feature_type"] == expected_type]
                exact_candidates = preferred or exact
                if len(exact_candidates) == 1:
                    selected = exact_candidates[0]
                    method = "exact_osm_source_id"
                    matching[method] += 1
                    if selected["feature_type"] != expected_type:
                        matching["exact_id_source_type_differs"] += 1
                    if selected["source_version"] == row["osm_version"]:
                        matching["exact_id_and_version"] += 1
                    else:
                        matching["exact_id_version_differs"] += 1
                    row.update({
                        "overture_match_method": method, "overture_feature_id": selected["feature_id"],
                        "overture_source_record_id": selected["record_id"],
                        "overture_source_version": selected["source_version"],
                        "overture_height_m": selected["height_m"], "overture_num_floors": selected["num_floors"],
                        "overture_min_height_m": selected["min_height_m"],
                        "overture_min_floor": selected["min_floor"],
                        "overture_roof_height_m": selected["roof_height_m"],
                        "overture_roof_shape": selected["roof_shape"],
                        "overture_roof_direction_deg": selected["roof_direction_deg"],
                        "overture_feature_type": selected["feature_type"],
                        "overture_building_id": selected["building_id"],
                        "overture_overlap_iou": None, "spatial_candidate_feature_id": None,
                        "spatial_candidate_height_m": None, "spatial_candidate_num_floors": None,
                    })
                    if row.get("height_parse_status") == "parsed":
                        if selected["height_m"] is None:
                            new_sources["height_tags_on_exact_match_missing_overture_height"] += 1
                        elif abs(float(row["height_m"]) - float(selected["height_m"])) > 0.5:
                            # Current Moscow BuildingPart rows with an exact OpenStreetMap
                            # source ID preserve the OSM ground-to-top tag value even though
                            # the Overture schema text describes height as a vertical span.
                            key = ("osm_height_tag_differs_over_0_5m_from_exact_overture_part_source"
                                   if selected["feature_type"] == "building_part"
                                   else "height_tag_differs_over_0_5m_from_overture_building")
                            conflicts[key] += 1
                    if row.get("building_levels_parse_status") == "parsed":
                        if selected["num_floors"] is None:
                            new_sources["levels_tags_on_exact_match_missing_overture_floors"] += 1
                        elif abs(float(row["building_levels"]) - float(selected["num_floors"])) >= 1:
                            conflicts["levels_tag_differs_over_1_from_overture"] += 1
                    compare_fields = (
                        ("min_height_parse_status", "min_height_m", "min_height_m", 0.5,
                         "min_height_tags_on_exact_match_missing_overture_min_height",
                         "min_height_tag_differs_over_0_5m_from_overture"),
                        ("building_min_level_parse_status", "building_min_level", "min_floor", 1.0,
                         "building_min_level_tags_on_exact_match_missing_overture_min_floor",
                         "building_min_level_tag_differs_over_1_from_overture"),
                        ("roof_height_parse_status", "roof_height_m", "roof_height_m", 0.5,
                         "roof_height_tags_on_exact_match_missing_overture_roof_height",
                         "roof_height_tag_differs_over_0_5m_from_overture"),
                    )
                    for status_field, osm_field, overture_field, tolerance, missing_key, conflict_key in compare_fields:
                        if row.get(status_field) == "parsed":
                            if selected[overture_field] is None:
                                new_sources[missing_key] += 1
                            elif abs(float(row[osm_field]) - float(selected[overture_field])) > tolerance:
                                conflicts[conflict_key] += 1
                    if row.get("roof_shape"):
                        if selected["roof_shape"] is None:
                            new_sources["roof_shape_tags_on_exact_match_missing_overture_roof_shape"] += 1
                        else:
                            matching["roof_shape_raw_equal_when_both_present"] += int(
                                row["roof_shape"].strip().lower() == str(selected["roof_shape"]).strip().lower()
                            )
                            matching["roof_shape_raw_differs_when_both_present_not_assumed_conflict"] += int(
                                row["roof_shape"].strip().lower() != str(selected["roof_shape"]).strip().lower()
                            )
                    if row.get("roof_direction_parse_status") == "parsed":
                        if selected["roof_direction_deg"] is None:
                            new_sources["roof_direction_tags_on_exact_match_missing_overture_roof_direction"] += 1
                        else:
                            matching["roof_direction_both_present_conventions_not_comparable"] += 1
                    if row.get("roof_levels_parse_status") == "parsed":
                        new_sources["roof_levels_tag_has_no_direct_overture_equivalent"] += 1
                elif len(exact_candidates) > 1:
                    matching["ambiguous_exact_osm_source_id"] += 1
                    row.update({
                        "overture_match_method": "ambiguous_exact_osm_source_id",
                        "overture_feature_id": None, "overture_source_record_id": None,
                        "overture_source_version": None, "overture_height_m": None,
                        "overture_num_floors": None, "overture_feature_type": None,
                        "overture_min_height_m": None, "overture_min_floor": None,
                        "overture_roof_height_m": None, "overture_roof_shape": None,
                        "overture_roof_direction_deg": None,
                        "overture_building_id": None, "overture_overlap_iou": None,
                        "spatial_candidate_feature_id": None, "spatial_candidate_height_m": None,
                        "spatial_candidate_num_floors": None,
                    })
                else:
                    match_info = {"method": "no_match", "feature_id": None, "iou": None, "height": None, "floors": None}
                    if (spatial_tree is not None and row.get("geometry") and
                            row.get("entity_kind") in {"building", "relation_member_outline"}):
                        geom = shapely.from_wkb(row["geometry"])
                        match_info = choose_spatial_match(geom, spatial_tree, spatial_ids, spatial_heights, spatial_floors)
                    matching[match_info["method"]] += 1
                    row.update({
                        "overture_match_method": match_info["method"],
                        "overture_feature_id": None,
                        "overture_source_record_id": None, "overture_source_version": None,
                        "overture_height_m": None, "overture_num_floors": None,
                        "overture_min_height_m": None, "overture_min_floor": None,
                        "overture_roof_height_m": None, "overture_roof_shape": None,
                        "overture_roof_direction_deg": None,
                        "overture_feature_type": None, "overture_building_id": None,
                        "overture_overlap_iou": match_info["iou"],
                        "spatial_candidate_feature_id": match_info["feature_id"],
                        "spatial_candidate_height_m": match_info["height"],
                        "spatial_candidate_num_floors": match_info["floors"],
                    })
                    if match_info["method"] == "strong_spatial_overlap_candidate":
                        if row.get("height_parse_status") == "parsed" and match_info["height"] is None:
                            conflicts["spatial_candidate_missing_overture_height_not_identity_confirmed"] += 1
                        if row.get("building_levels_parse_status") == "parsed" and match_info["floors"] is None:
                            conflicts["spatial_candidate_missing_overture_floors_not_identity_confirmed"] += 1
                final_batch.append(row)
                if len(final_batch) >= args.batch_size:
                    write_batch(output_writer, final_batch, schema)
        write_batch(output_writer, final_batch, schema)
        output_writer.close()

    feature_count = pq.ParquetFile(args.output).metadata.num_rows
    if feature_count != output_rows:
        raise RuntimeError(f"output row mismatch: expected {output_rows}, got {feature_count}")
    examples_doc = {"type": "FeatureCollection", "features": examples}
    args.examples.write_text(json.dumps(examples_doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    # Final coverage counts are computed by streaming the written artifact.
    field_counts = Counter()
    feature_kinds = Counter()
    geometry_types = Counter()
    unique_keys: set[tuple[str, int]] = set()
    parsed_fields = (
        "height_m", "min_height_m", "building_levels", "building_min_level", "roof_height_m",
        "roof_levels", "roof_shape", "roof_direction_deg",
    )
    status_fields = (
        "height_parse_status", "min_height_parse_status", "building_levels_parse_status",
        "building_min_level_parse_status", "roof_height_parse_status", "roof_levels_parse_status",
        "roof_direction_parse_status",
    )
    for batch in pq.ParquetFile(args.output).iter_batches(batch_size=8192,
            columns=["osm_type", "osm_id", "entity_kind", "geometry", *parsed_fields, *status_fields]):
        for row in batch.to_pylist():
            key = (row["osm_type"], row["osm_id"])
            if key in unique_keys:
                raise RuntimeError(f"duplicate OSM source entity in output: {key}")
            unique_keys.add(key)
            feature_kinds[row["entity_kind"]] += 1
            if row["geometry"]:
                geom = shapely.from_wkb(row["geometry"])
                geometry_types[geom.geom_type] += 1
            for field in parsed_fields:
                if row[field] is not None:
                    field_counts[field] += 1
            for field in status_fields:
                field_counts[f"status:{field}:{row[field]}"] += 1
    manifest = {
        "status": "city_boundary_clipped_osm_building_and_parts_source_snapshot",
        "source": {
            "file": str(args.pbf.relative_to(ROOT)), "bytes": args.pbf.stat().st_size,
            "sha256": sha256(args.pbf), "pinned_source_manifest": str(args.pbf_manifest.relative_to(ROOT)),
            "source_url": source_manifest.get("source", {}).get("url"),
            "source_dataset": source_manifest.get("source", {}).get("dataset"),
            "source_last_modified": source_manifest.get("source", {}).get("source_last_modified"),
            "osm_replication_timestamp": source_manifest.get("source", {}).get("osm_replication_timestamp"),
            "data_last_object_timestamp": source_info.get("data_last_object_timestamp"),
            "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors",
            "filter_expressions": list(FILTERS), "filtered_subset_bytes": filtered_subset_bytes,
            "filtered_subset_sha256": filtered_hash, "filtered_subset_removed_after_run": True,
            "preclip_object_counts": {"nodes": source_info.get("node_count"), "ways": source_info.get("way_count"), "relations": source_info.get("relation_count")},
        },
        "boundary": {
            "file": str(args.boundary.relative_to(ROOT)), "relation_id": boundary_manifest.get("relation_id"),
            "relation_version": boundary_manifest.get("relation_version"),
            "relation_timestamp_utc": boundary_manifest.get("relation_timestamp_utc"),
            "sha256": boundary_manifest.get("output_sha256"),
            "legal_boundary_certification": boundary_manifest.get("boundary_is_authoritative_legal_source", False),
            "clip_rule": "Keep polygonal entity if it intersects the pinned OSM relation boundary; preserve source_geometry WKB unchanged, clip render/city geometry to boundary; touch-only and non-polygonal are not footprints.",
        },
        "overture_comparison": {
            "building_file": str(args.overture.relative_to(ROOT)), "building_sha256": overture_manifest.get("output_sha256"),
            "building_parts_file": str(args.overture_parts.relative_to(ROOT)),
            "building_parts_sha256": overture_parts_manifest.get("output_sha256"),
            "release": overture_manifest.get("source_release"), "source_osm_records_by_type": dict(overture_osm_source_records),
            "matching_rule": "First parse Overture sources[].record_id (w/r/n<ID>@version) to exact OSM typed ID; version is reported separately. If absent, only a unique spatial candidate with IoU>=0.85, area ratio 0.80..1.25 and >=0.05 lead over second candidate is recorded; this is candidate-only, never a replacement identity.",
            "semantic_note": "OSM numeric tags and Overture facts are retained as distinct source attributes. An exact source ID proves source-record linkage, not survey accuracy. In this pinned OSM-sourced Moscow BuildingPart sample, exact-ID rows repeat OSM height/min_height values (ground-to-top and bottom offset); this conflicts with the generic Overture v2.0 prose describing height as lowest-to-highest distance. Do not blindly derive top=min_height+height for these rows; use the source-linked OSM interpretation and retain both raw values. Other source datasets require separate semantic validation.",
            "matching_counts": dict(matching), "new_source_fields_vs_overture_missing": dict(new_sources),
            "conflict_counts": dict(conflicts),
        },
        "counts": {
            "selected_way_entities_preclip": counts["selected_way_entities"],
            "tagged_way_tags": dict(tag_counts), "relation_entities_preclip": dict(relation_counts),
            "ways_outside_boundary": counts["ways_outside_boundary"],
            "ways_without_reconstructable_geometry": counts["ways_without_reconstructable_geometry"],
            "open_way_entities": counts["open_way_entities"],
            "relations_without_boundary_polygon": counts["relations_without_boundary_polygon"],
            "clipped_feature_count": feature_count, "unique_osm_entity_count": len(unique_keys),
            "duplicate_osm_entity_count": 0, "feature_kind_counts": dict(feature_kinds),
            "clipped_geometry_type_counts": dict(geometry_types), "parsed_source_value_counts": dict(field_counts),
            "multilevel_relation_examples": len(examples),
        },
        "output": {"file": args.output.name, "bytes": args.output.stat().st_size, "sha256": sha256(args.output),
                   "examples_file": args.examples.name, "examples_sha256": sha256(args.examples),
                   "unique_id_key": ["osm_type", "osm_id"], "source_geometry_preserved": True},
        "quality": {
            "source_height_tags_are_survey_measurements": False,
            "source_height_statement": "These are OSM community-mapped tags; source:height metadata is preserved when present but does not independently verify a survey.",
            "height_unit_policy": "Bare scalar defaults to metres. Parse explicit m aliases, km, mi, nmi, yd, ft/feet, inches and combined feet-inch apostrophe notation; normalize to metres. Unsupported syntax (including missing space before a suffix) remains in raw tag with null parsed value.",
            "height_unit_policy_supported": ["m (default)", "km", "mi", "nmi", "yd", "ft/foot/feet", "in/inch/inches", "feet-and-inches apostrophe notation"],
            "floor_unit_policy": "building:levels, building:min_level, roof:levels accept bare scalar counts only; length units and ranges are retained raw but not normalized.",
            "roof_direction_policy": "Numeric roof:direction in degrees in [0,360] is parsed; compass strings and unsupported syntax are retained raw with null normalized value.",
            "source_priority_note": "No OSM or Overture source attribute is overwritten by this adapter. Consumers should prioritize the selected source's explicit value, then explicit levels with an application conversion policy; no fill or survey promotion occurs here.",
            "overture_building_part_vertical_semantics": "Generic Overture BuildingPart v2.0 prose says height is lowest-to-highest distance and min_height is bottom offset. In this release's OSM-sourced exact-ID sample, height and min_height preserve OSM height (ground-to-top) and min_height (bottom offset); render [min_height,height] for these source-linked rows. Treat generic non-OSM sourced BuildingPart height semantics as unresolved until validated. roof_height is part of the total height, not an additional height to add.",
            "osm_vertical_semantics": "OSM Simple 3D Buildings height is ground-to-top; min_height is the bottom offset, so facade thickness is height - min_height when both exist. roof:height describes roof rise within total height and must not be added to height.",
            "coverage_note": "This is the set of returned OSM building-tagged ways, relation members, and selected relations that intersect the chosen OSM boundary; it is not an exhaustive building inventory. Missing tags do not prove absence.",
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "reproduction": {
            "command": "`rtk .runtime/geodata-env/bin/python sources/city/osm_building_parts.py` (defaults are pinned repo artifacts)",
            "filter_command_template": f"{args.osmium} tags-filter --no-progress --output <temp.pbf> {args.pbf} " + " ".join(FILTERS),
        },
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": feature_count, "sha256": manifest["output"]["sha256"],
                      "source_new_fields": dict(new_sources), "matching_counts": dict(matching),
                      "relation_counts": dict(relation_counts), "examples": len(examples)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
