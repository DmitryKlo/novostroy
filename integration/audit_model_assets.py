"""Read-only audit of one staged mapped-construction model asset release.

Run with the geodata environment so the source GeoParquet geometry can be
decoded. This audit reads the run manifest, assessments, evaluations, public
manifest, GLBs, and pinned OSM candidate parquet. It never activates a release,
opens a network connection, or writes audit output to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.build_tiles import ecef, enu_transform


DEFAULT_RUN_DIR = ROOT / "data/models/models-v1-9fabfcc68bd1811f"
DEFAULT_COMPARE_RUN_DIR = ROOT / "data/models/models-v1-b553758cd13765aa"
DEFAULT_ASSET_ROOT = ROOT / "assets"
DEFAULT_OSM_PARQUET = ROOT / "data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.parquet"
BASE_HEIGHT_M = 0.65
FILE_NAME_RE = re.compile(r"^model_([0-9a-f]{64})\.(glb|tileset\.json)$")
SOURCE_RECORD_RE = re.compile(r"^(way|relation)/(\d+)/v(\d+)$")
PRIVATE_KEYS = {
    "source_id", "source_ids", "source_record_id", "record_id", "osm_id", "osm_ids",
    "provider_id", "feature_id_mapping", "registry_id", "candidate_id", "raw_tags",
    "private_path", "private_root", "source_tile_index", "location_source",
}
PRIVATE_PATH_RE = re.compile(
    r"(?i)(?:/home/|/tmp/|/var/|/mnt/|/root/|/\.runtime/|(?:^|/)data/(?:property|models)/|[a-z]:\\)"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected JSON object: {path.name}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    require(all(isinstance(row, dict) for row in rows), f"Bad JSONL object: {path.name}")
    return rows


def check_public_strings(value: Any, context: str) -> None:
    if isinstance(value, dict):
        leaked = PRIVATE_KEYS.intersection(value)
        require(not leaked, f"Private metadata keys {sorted(leaked)} in {context}")
        for child in value.values():
            check_public_strings(child, context)
    elif isinstance(value, list):
        for child in value:
            check_public_strings(child, context)
    elif isinstance(value, str):
        require(not PRIVATE_PATH_RE.search(value), f"Private filesystem path in {context}")


def glb_document(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    require(len(payload) >= 20, f"Short GLB: {path.name}")
    magic, version, total_length = struct.unpack_from("<4sII", payload, 0)
    require((magic, version, total_length) == (b"glTF", 2, len(payload)),
            f"Bad GLB header: {path.name}")
    cursor = 12
    document: dict[str, Any] | None = None
    binary: bytes | None = None
    while cursor < len(payload):
        require(cursor + 8 <= len(payload), f"Truncated GLB chunk header: {path.name}")
        length, kind = struct.unpack_from("<I4s", payload, cursor)
        cursor += 8
        require(cursor + length <= len(payload), f"Truncated GLB chunk: {path.name}")
        chunk = payload[cursor:cursor + length]
        cursor += length
        if kind == b"JSON":
            require(document is None, f"Duplicate GLB JSON chunk: {path.name}")
            document = json.loads(chunk.decode("utf-8").rstrip(" \x00"))
        elif kind == b"BIN\x00":
            require(binary is None, f"Duplicate GLB BIN chunk: {path.name}")
            binary = chunk
        else:
            raise AssertionError(f"Unexpected GLB chunk {kind!r}: {path.name}")
    require(cursor == len(payload) and document is not None and binary is not None,
            f"Missing GLB chunks: {path.name}")
    require(len(document.get("buffers", [])) == 1, f"Expected one GLB buffer: {path.name}")
    buffer = document["buffers"][0]
    require("uri" not in buffer and int(buffer["byteLength"]) <= len(binary),
            f"External or invalid GLB buffer: {path.name}")
    return document, binary


def accessor_values(document: dict[str, Any], binary: bytes, accessor_index: int,
                    *, components: int, scalar_format: str) -> list[tuple[float, ...]]:
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    require(0 <= accessor_index < len(accessors), "GLB accessor index out of range")
    accessor = accessors[accessor_index]
    require("sparse" not in accessor and accessor.get("type") == ("VEC3" if components == 3 else "SCALAR"),
            "Unexpected sparse or accessor type")
    require(accessor.get("componentType") == ({"f": 5126, "H": 5123, "I": 5125}[scalar_format]),
            "Unexpected accessor component type")
    view_index = int(accessor["bufferView"])
    require(0 <= view_index < len(views), "GLB bufferView index out of range")
    view = views[view_index]
    require(view.get("buffer", 0) == 0, "GLB accessor references nonzero buffer")
    component_bytes = struct.calcsize("<" + scalar_format)
    item_bytes = component_bytes * components
    stride = int(view.get("byteStride", item_bytes))
    require(stride == item_bytes, "Interleaved GLB accessors are not expected")
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    count = int(accessor["count"])
    end = start + count * stride
    require(start >= 0 and end <= len(binary), "GLB accessor exceeds binary chunk")
    unpacker = struct.Struct("<" + scalar_format * components)
    return [unpacker.unpack_from(binary, start + i * stride) for i in range(count)]


def source_to_enu(point: tuple[float, float], origin_lon: float,
                  origin_lat: float) -> tuple[float, float]:
    frame = enu_transform(origin_lon, origin_lat)
    origin = frame[12:15]
    east_axis, north_axis = frame[0:3], frame[4:7]
    xyz = ecef(float(point[0]), float(point[1]), 0.0)
    delta = [xyz[i] - origin[i] for i in range(3)]
    east = sum(east_axis[i] * delta[i] for i in range(3))
    north = sum(north_axis[i] * delta[i] for i in range(3))
    return east, north


def projected_polygon(polygon: Polygon, origin_lon: float,
                      origin_lat: float) -> Polygon:
    exterior = [source_to_enu((x, y), origin_lon, origin_lat)
                for x, y, *_ in polygon.exterior.coords]
    holes = [[source_to_enu((x, y), origin_lon, origin_lat)
              for x, y, *_ in ring.coords] for ring in polygon.interiors]
    return Polygon(exterior, holes)


def projected_source(geometry: Polygon | MultiPolygon, origin_lon: float,
                     origin_lat: float) -> Polygon | MultiPolygon:
    if isinstance(geometry, Polygon):
        return projected_polygon(geometry, origin_lon, origin_lat)
    require(isinstance(geometry, MultiPolygon), "Unsupported source geometry")
    return MultiPolygon([projected_polygon(part, origin_lon, origin_lat)
                         for part in geometry.geoms])


def interior_count(geometry: Polygon | MultiPolygon) -> int:
    if isinstance(geometry, Polygon):
        return len(geometry.interiors)
    return sum(len(part.interiors) for part in geometry.geoms)


def glb_geometry(document: dict[str, Any], binary: bytes, path: Path,
                 *, volume_expected: bool, expected_height: float | None) -> dict[str, Any]:
    check_public_strings(document, path.name)
    asset = document.get("asset", {})
    require(asset.get("version") == "2.0", f"GLB version mismatch: {path.name}")
    extras = asset.get("extras", {})
    require(extras.get("source_ids_redacted") is True, f"No source-ID redaction marker: {path.name}")
    require("KHR_materials_unlit" in document.get("extensionsUsed", []),
            f"Expected unlit material: {path.name}")
    require(not document.get("images") and not document.get("textures"),
            f"Unexpected embedded image/texture: {path.name}")
    materials = document.get("materials", [])
    expected_materials = {"mapped_construction_roof"} | ({"mapped_construction_walls"} if volume_expected else set())
    require({material.get("name") for material in materials} == expected_materials,
            f"Unexpected public materials: {path.name}")
    for material in materials:
        pbr = material.get("pbrMetallicRoughness", {})
        require("baseColorTexture" not in pbr, f"Unexpected texture reference: {path.name}")
        factor = pbr.get("baseColorFactor", [])
        require(len(factor) == 4 and all(math.isfinite(float(v)) for v in factor),
                f"Nonfinite material factor: {path.name}")

    mesh_rows = document.get("meshes", [])
    require(len(mesh_rows) == 1, f"Expected one mesh: {path.name}")
    primitives = mesh_rows[0].get("primitives", [])
    require(len(primitives) == (2 if volume_expected else 1), f"Primitive count mismatch: {path.name}")
    positions = accessor_values(document, binary, int(primitives[0]["attributes"]["POSITION"]),
                                components=3, scalar_format="f")
    require(bool(positions), f"Empty vertex array: {path.name}")
    require(all(math.isfinite(value) for point in positions for value in point),
            f"Nonfinite vertex: {path.name}")
    accessor = document["accessors"][int(primitives[0]["attributes"]["POSITION"])]
    actual_min = [min(point[i] for point in positions) for i in range(3)]
    actual_max = [max(point[i] for point in positions) for i in range(3)]
    require(len(accessor.get("min", [])) == 3 and len(accessor.get("max", [])) == 3,
            f"No POSITION bounds: {path.name}")
    for i in range(3):
        require(abs(float(accessor["min"][i]) - actual_min[i]) <= 1e-5
                and abs(float(accessor["max"][i]) - actual_max[i]) <= 1e-5,
                f"Accessor bounds disagree with decoded vertices: {path.name}")

    primitive_triangles: list[int] = []
    roof_triangles: list[Polygon] = []
    wall_triangle_count = 0
    for primitive_index, primitive in enumerate(primitives):
        require(primitive.get("mode", 4) == 4, f"Non-triangle GLB primitive: {path.name}")
        require(int(primitive.get("material", -1)) < len(materials), f"Invalid material index: {path.name}")
        indices = accessor_values(document, binary, int(primitive["indices"]),
                                  components=1,
                                  scalar_format={5123: "H", 5125: "I"}[int(document["accessors"][int(primitive["indices"])]["componentType"])])
        require(len(indices) % 3 == 0, f"Index count not divisible by three: {path.name}")
        flat_indices = [int(row[0]) for row in indices]
        require(all(0 <= index < len(positions) for index in flat_indices),
                f"Index outside vertex array: {path.name}")
        primitive_triangles.append(len(indices) // 3)
        if primitive_index == 0:
            for i in range(0, len(flat_indices), 3):
                points = [(positions[index][0], -positions[index][2])
                          for index in flat_indices[i:i + 3]]
                triangle = Polygon(points)
                require(triangle.is_valid and triangle.area > 0,
                        f"Degenerate roof triangle: {path.name}")
                roof_triangles.append(triangle)
        else:
            wall_triangle_count = len(indices) // 3

    if expected_height is None:
        require(not volume_expected, f"Unexpected volume for flat model: {path.name}")
        require(abs(actual_min[1] - BASE_HEIGHT_M) <= 0.08
                and abs(actual_max[1] - BASE_HEIGHT_M) <= 0.08,
                f"Flat GLB vertical bounds unexpected: {path.name}")
    else:
        require(volume_expected, f"Missing volume for height-bearing model: {path.name}")
        require(1 <= expected_height <= 400, f"Out-of-range modeled height for {path.name}")
        require(abs(actual_min[1] - BASE_HEIGHT_M) <= 0.08
                and abs(actual_max[1] - (BASE_HEIGHT_M + expected_height)) <= 0.08,
                f"Volume GLB height bounds disagree: {path.name}")
        require(wall_triangle_count > 0, f"No side walls in volume GLB: {path.name}")
    return {"positions": positions, "roof_triangles": roof_triangles,
            "primitive_triangles": primitive_triangles,
            "wall_triangle_count": wall_triangle_count,
            "min": actual_min, "max": actual_max}


def public_glb_hashes(run_dir: Path, asset_root: Path) -> tuple[str, dict[str, str]]:
    manifest = load_json(run_dir / "manifest.json")
    run_id = str(manifest["run_id"])
    public_dir = asset_root / str(manifest["public_root"])
    public_manifest_path = public_dir / "public_manifest.json"
    require(sha256_file(public_manifest_path) == manifest["public_manifest_sha256"],
            f"Comparison public manifest checksum mismatch: {run_id}")
    public_manifest = load_json(public_manifest_path)
    hashes: dict[str, str] = {}
    for row in public_manifest.get("files", []):
        relative = str(row.get("path", ""))
        if not relative.endswith(".glb"):
            continue
        path = public_dir / relative
        require(path.is_file() and sha256_file(path) == row["sha256"],
                f"Comparison GLB checksum mismatch: {run_id}/{relative}")
        hashes[relative] = row["sha256"]
    return run_id, hashes


def public_asset_hashes(run_dir: Path, asset_root: Path) -> tuple[str, dict[str, str], int]:
    """Verify and return the immutable public GLB/tileset file set, excluding its manifest."""
    manifest = load_json(run_dir / "manifest.json")
    run_id = str(manifest["run_id"])
    public_dir = asset_root / str(manifest["public_root"])
    public_manifest_path = public_dir / "public_manifest.json"
    require(sha256_file(public_manifest_path) == manifest["public_manifest_sha256"],
            f"Public manifest checksum mismatch: {run_id}")
    public_manifest = load_json(public_manifest_path)
    require(public_manifest.get("run_id") == run_id
            and public_manifest.get("public_redacted_source_ids") is True
            and public_manifest.get("license") == "ODbL-1.0"
            and public_manifest.get("attribution") == "© OpenStreetMap contributors",
            f"Public manifest rights/run gate failed: {run_id}")
    hashes: dict[str, str] = {}
    total_bytes = 0
    for row in public_manifest.get("files", []):
        relative = str(row.get("path", ""))
        match = FILE_NAME_RE.fullmatch(relative)
        require(relative == Path(relative).name and ".." not in Path(relative).parts
                and match is not None and match.group(1) == row.get("sha256"),
                f"Unsafe or non-content-addressed public filename: {run_id}/{relative}")
        path = public_dir / relative
        require(path.is_file() and path.stat().st_size == int(row["bytes"])
                and sha256_file(path) == row["sha256"],
                f"Public asset file hash/size mismatch: {run_id}/{relative}")
        require(relative not in hashes, f"Duplicate public asset path: {run_id}/{relative}")
        hashes[relative] = row["sha256"]
        total_bytes += int(row["bytes"])
    actual = {path.name for path in public_dir.iterdir()
              if path.is_file() and path.name != "public_manifest.json"}
    require(actual == set(hashes), f"Public asset directory differs from manifest: {run_id}")
    return run_id, hashes, total_bytes


def geometry_coordinate_count(geometry: Polygon | MultiPolygon) -> int:
    polygons = [geometry] if isinstance(geometry, Polygon) else list(geometry.geoms)
    return sum(len(polygon.exterior.coords)
               + sum(len(ring.coords) for ring in polygon.interiors)
               for polygon in polygons)


def geometry_coordinates_finite_2d(geometry: Polygon | MultiPolygon) -> bool:
    polygons = [geometry] if isinstance(geometry, Polygon) else list(geometry.geoms)
    rings = [ring for polygon in polygons
             for ring in (polygon.exterior, *polygon.interiors)]
    return all(len(point) == 2 and math.isfinite(float(point[0]))
               and math.isfinite(float(point[1]))
               for ring in rings for point in ring.coords)


def audit_previews(run_dir: Path, asset_root: Path, osm_parquet: Path,
                   compare_run_dir: Path) -> dict[str, Any]:
    """Audit all new preview geometries, reusing a previously decoded mesh audit."""
    run_manifest = load_json(run_dir / "manifest.json")
    run_id = str(run_manifest["run_id"])
    require(run_id == run_dir.name, "Preview run directory/manifest mismatch")
    assessments_path = run_dir / "assessments.jsonl"
    evaluations_path = run_dir / "source_evaluations.jsonl"
    require(sha256_file(assessments_path) == run_manifest["assessments_sha256"],
            "Preview assessment checksum mismatch")
    require(sha256_file(evaluations_path) == run_manifest["source_evaluations_sha256"],
            "Preview evaluation checksum mismatch")
    assessments = load_jsonl(assessments_path)
    evaluations = load_jsonl(evaluations_path)
    require(len(assessments) == len(evaluations) == 2317,
            "Preview assessment/evaluation count mismatch")
    ready = [row for row in assessments if row.get("asset_readiness") == "ready_to_render"]
    require(len(ready) == 756, "Preview ready count mismatch")
    current_by_id = {row["candidate_id"]: row for row in assessments}
    evaluation_by_id = {row["candidate_id"]: row for row in evaluations}
    require(len(current_by_id) == len(assessments) and len(evaluation_by_id) == len(evaluations),
            "Duplicate preview candidate/evaluation ID")
    old_manifest = load_json(compare_run_dir / "manifest.json")
    old_assessment_path = compare_run_dir / "assessments.jsonl"
    require(sha256_file(old_assessment_path) == old_manifest["assessments_sha256"],
            "Previous assessment checksum mismatch")
    previous = {row["candidate_id"]: row for row in load_jsonl(old_assessment_path)}
    require(len(previous) == 2317, "Previous assessment count mismatch")

    old_id, old_files, old_bytes = public_asset_hashes(compare_run_dir, asset_root)
    new_id, new_files, new_bytes = public_asset_hashes(run_dir, asset_root)
    require(new_files == old_files,
            f"Current public GLB/tileset set differs from prior fully audited run {old_id}")
    require(len(new_files) == 1784 and new_bytes == old_bytes == 2_222_318,
            "Current/prior public asset file count or byte sum differs")

    source_hash = sha256_file(osm_parquet)
    require(source_hash == run_manifest.get("input_hashes", {}).get("osm_construction_parquet"),
            "Preview OSM source parquet hash differs from this run")
    source_rows = pq.read_table(osm_parquet, columns=[
        "osm_type", "osm_id", "osm_version", "geometry", "rights_status",
        "rights_license", "attribution", "source_record_id",
    ]).to_pylist()
    source_index = {(row["osm_type"], int(row["osm_id"]), int(row["osm_version"])): row
                    for row in source_rows}
    require(len(source_index) == 756 and len(source_rows) == 756,
            "Preview OSM source keys are not unique/exhaustive")

    type_counts: Counter[str] = Counter()
    old_preview_count = 0
    missing_new_preview = 0
    max_coordinate_pairs = 0
    total_coordinate_pairs = 0
    hole_count = 0
    old_to_new_fields = ("candidate_id", "decision", "asset_readiness", "depiction",
                         "entity_type", "geometry_as_of", "geometry_quality", "model_quality",
                         "location", "bbox", "height_evidence", "reason_codes", "preview_footprint")
    for row in ready:
        candidate_id = row["candidate_id"]
        old = previous[candidate_id]
        require(all(row.get(field) == old.get(field) for field in old_to_new_fields),
                f"Non-preview ready assessment field changed: {candidate_id}")
        new_model = dict(row["selected_model"] or {})
        old_model = dict(old["selected_model"] or {})
        new_url = str(new_model.pop("tileset_url", ""))
        old_url = str(old_model.pop("tileset_url", ""))
        require(new_model == old_model and Path(new_url).name == Path(old_url).name,
                f"Selected public model differs from prior run: {candidate_id}")
        require(new_url.startswith(f"/assets/{run_manifest['public_root']}/"),
                f"Preview selected model points outside its staged root: {candidate_id}")

        evaluation = evaluation_by_id[candidate_id]
        source_match = SOURCE_RECORD_RE.fullmatch(str(evaluation.get("source_record_id", "")))
        require(source_match is not None and evaluation.get("source_group") == "osm_construction",
                f"Malformed preview source identity: {candidate_id}")
        key = (source_match.group(1), int(source_match.group(2)), int(source_match.group(3)))
        source = source_index.get(key)
        require(source is not None and source["source_record_id"] == evaluation["source_record_id"],
                f"Preview/source OSM identity mismatch: {candidate_id}")
        require(source["rights_status"] == "cleared" and source["rights_license"] == "ODbL-1.0"
                and source["attribution"] == "© OpenStreetMap contributors",
                f"Preview source rights gate failed: {candidate_id}")
        require(hashlib.sha256(source["geometry"]).hexdigest() == evaluation["geometry_sha256"],
                f"Preview source geometry checksum mismatch: {candidate_id}")
        source_geometry = wkb.loads(source["geometry"])
        preview_data = row.get("preview_geometry")
        if preview_data is None:
            missing_new_preview += 1
            continue
        require(preview_data.get("type") in {"Polygon", "MultiPolygon"},
                f"Unexpected preview geometry type: {candidate_id}")
        preview_geometry = shape(preview_data)
        require(isinstance(preview_geometry, (Polygon, MultiPolygon))
                and preview_geometry.is_valid and not preview_geometry.is_empty
                and geometry_coordinates_finite_2d(preview_geometry),
                f"Invalid/nonfinite preview geometry: {candidate_id}")
        require(preview_geometry.geom_type == source_geometry.geom_type
                and preview_geometry.equals_exact(source_geometry, 0.0),
                f"Preview does not preserve exact source coordinate sequences: {candidate_id}")
        require(interior_count(preview_geometry) == interior_count(source_geometry),
                f"Preview hole count differs from source: {candidate_id}")
        coordinate_pairs = geometry_coordinate_count(preview_geometry)
        max_coordinate_pairs = max(max_coordinate_pairs, coordinate_pairs)
        total_coordinate_pairs += coordinate_pairs
        hole_count += interior_count(preview_geometry)
        type_counts[preview_geometry.geom_type] += 1
        if old.get("preview_footprint") is not None:
            old_preview_count += 1
            exterior = (list(preview_geometry.exterior.coords)
                        if isinstance(preview_geometry, Polygon) and not preview_geometry.interiors
                        else None)
            require(exterior is not None and old["preview_footprint"] == [list(point) for point in exterior],
                    f"Legacy simple preview changed or disagrees: {candidate_id}")

    nonready_with_preview = sum(row.get("preview_geometry") is not None for row in assessments
                                if row.get("asset_readiness") != "ready_to_render")
    require(missing_new_preview == 0 and nonready_with_preview == 0,
            f"Preview coverage/privacy mismatch: missing={missing_new_preview}, nonready={nonready_with_preview}")
    require(type_counts == Counter({"Polygon": 738, "MultiPolygon": 18}),
            f"Preview geometry type counts differ: {dict(type_counts)}")
    require(old_preview_count == 725 and 0 < max_coordinate_pairs <= 277 and hole_count == 12,
            "Preview coverage/coordinate/hole totals differ from expected source geometry")
    return {
        "run_id": run_id,
        "compared_to_run": old_id,
        "result": "PASS",
        "read_only": True,
        "active_pointer_changed": False,
        "ready_previews": len(ready) - missing_new_preview,
        "preview_geometry_types": dict(type_counts),
        "exact_source_coordinate_sequences": len(ready) - missing_new_preview,
        "legacy_preview_footprints_preserved": old_preview_count,
        "old_null_previews_now_exact": len(ready) - old_preview_count,
        "nonready_preview_geometries": nonready_with_preview,
        "total_holes_preserved": hole_count,
        "max_coordinate_pairs_per_preview": max_coordinate_pairs,
        "total_coordinate_pairs": total_coordinate_pairs,
        "public_asset_files_identical": len(new_files),
        "public_asset_bytes_identical": new_bytes,
        "source_osm_parquet_sha256": source_hash,
        "rights_gate": "ODbL-1.0, source rows cleared, attribution preserved",
    }


def audit(run_dir: Path, asset_root: Path, osm_parquet: Path,
          compare_run_dir: Path | None = None) -> dict[str, Any]:
    run_manifest = load_json(run_dir / "manifest.json")
    run_id = str(run_manifest["run_id"])
    require(run_id == run_dir.name and run_id.startswith("models-v1-"),
            "Run directory/manifest identity mismatch")
    public_root = str(run_manifest["public_root"])
    require(public_root == f"public-project-models-v1-{run_id.removeprefix('models-v1-')}",
            "Unexpected public root naming")
    public_dir = asset_root / public_root
    require(public_dir.is_dir(), f"Missing staged public root: {public_root}")

    assessments_path = run_dir / "assessments.jsonl"
    evaluations_path = run_dir / "source_evaluations.jsonl"
    require(sha256_file(assessments_path) == run_manifest["assessments_sha256"],
            "Assessment checksum mismatch")
    require(sha256_file(evaluations_path) == run_manifest["source_evaluations_sha256"],
            "Evaluation checksum mismatch")
    assessments = load_jsonl(assessments_path)
    evaluations = load_jsonl(evaluations_path)
    require(len(assessments) == run_manifest["assessments_count"] == 2317,
            "Assessment count mismatch")
    require(len(evaluations) == run_manifest["source_evaluations_count"] == 2317,
            "Evaluation count mismatch")
    actual_decisions = Counter(row["decision"] for row in assessments)
    require(dict(actual_decisions) == run_manifest["decision_counts"], "Decision count mismatch")
    assessments_by_id = {row["candidate_id"]: row for row in assessments}
    evaluations_by_id = {row["candidate_id"]: row for row in evaluations}
    require(len(assessments_by_id) == len(assessments) and len(evaluations_by_id) == len(evaluations),
            "Duplicate assessment/evaluation ID")
    ready = [row for row in assessments if row.get("asset_readiness") == "ready_to_render"]
    require(len(ready) == 756, "Ready assessment count mismatch")
    require(all(row["decision"] == "ready" and row["entity_type"] == "mapped_construction"
                and row.get("selected_model") for row in ready), "Invalid ready assessment")
    require(all(row.get("selected_model") is None for row in assessments
                if row.get("asset_readiness") != "ready_to_render"),
            "Non-ready assessment unexpectedly selects a model")

    public_manifest_path = public_dir / "public_manifest.json"
    require(sha256_file(public_manifest_path) == run_manifest["public_manifest_sha256"],
            "Public manifest checksum mismatch")
    public_manifest = load_json(public_manifest_path)
    require(public_manifest.get("run_id") == run_id
            and public_manifest.get("schema_version") == "public-project-models/1",
            "Public manifest run/schema mismatch")
    require(public_manifest.get("public_redacted_source_ids") is True
            and public_manifest.get("license") == "ODbL-1.0"
            and public_manifest.get("attribution") == "© OpenStreetMap contributors"
            and public_manifest.get("model_count") == 756,
            "Public manifest rights/model-count gate failed")
    check_public_strings(public_manifest, "public_manifest.json")
    file_entries = public_manifest.get("files", [])
    entries_by_path = {row["path"]: row for row in file_entries}
    require(len(entries_by_path) == len(file_entries) == 1784, "Public file manifest count/uniqueness mismatch")
    expected_paths: set[str] = set()
    file_bytes = 0
    file_hashes: Counter[str] = Counter()
    for entry in file_entries:
        relative = str(entry["path"])
        require(relative == Path(relative).name and ".." not in Path(relative).parts,
                f"Nonlocal public manifest path: {relative}")
        match = FILE_NAME_RE.fullmatch(relative)
        require(match is not None and match.group(1) == entry["sha256"],
                f"Content-addressed name/hash mismatch: {relative}")
        path = public_dir / relative
        require(path.is_file(), f"Manifest file missing: {relative}")
        require(path.stat().st_size == int(entry["bytes"]), f"Byte size mismatch: {relative}")
        require(sha256_file(path) == entry["sha256"], f"Content hash mismatch: {relative}")
        expected_paths.add(relative)
        file_bytes += int(entry["bytes"])
        file_hashes[match.group(2)] += 1
    actual_paths = {path.name for path in public_dir.iterdir() if path.is_file() and path.name != "public_manifest.json"}
    require(actual_paths == expected_paths, "Unmanifested or missing public files")
    ext_counts = Counter("tileset" if path.endswith(".tileset.json") else "glb" for path in expected_paths)
    require(ext_counts == {"tileset": 756, "glb": 1028}, f"Public asset extension counts differ: {dict(ext_counts)}")

    source_rows = pq.read_table(osm_parquet, columns=[
        "osm_type", "osm_id", "osm_version", "geometry", "raw_tags", "rights_status",
        "rights_license", "attribution", "source_record_id",
    ]).to_pylist()
    require(len(source_rows) == 756, "OSM source parquet row count mismatch")
    source_index = {(row["osm_type"], int(row["osm_id"]), int(row["osm_version"])): row
                    for row in source_rows}
    require(len(source_index) == 756, "Duplicate typed/versioned OSM source key")
    source_parquet_hash = sha256_file(osm_parquet)
    expected_source_hash = run_manifest.get("input_hashes", {}).get("osm_construction_parquet")
    require(source_parquet_hash == expected_source_hash,
            "OSM source parquet differs from the staged run input")

    counters: Counter[str] = Counter()
    source_height_counts: Counter[str] = Counter()
    glb_cache: dict[str, tuple[dict[str, Any], bytes, dict[str, Any]]] = {}
    maximum_area_error = 0.0
    maximum_height_error = 0.0
    maximum_region_error_rad = 0.0
    hole_total = 0
    triangle_total = 0
    processed_tile_names: set[str] = set()
    processed_glb_names: set[str] = set()

    for assessment in ready:
        candidate_id = assessment["candidate_id"]
        evaluation = evaluations_by_id[candidate_id]
        require(evaluation.get("source_group") == "osm_construction"
                and evaluation.get("decision") == "ready", f"Not an OSM-ready evaluation: {candidate_id}")
        source_match = SOURCE_RECORD_RE.fullmatch(str(evaluation.get("source_record_id", "")))
        require(source_match is not None, f"Malformed private source identity for {candidate_id}")
        source_key = (source_match.group(1), int(source_match.group(2)), int(source_match.group(3)))
        source = source_index.get(source_key)
        require(source is not None and source["source_record_id"] == evaluation["source_record_id"],
                f"Assessment/source identity mismatch for {candidate_id}")
        require(source["rights_status"] == "cleared" and source["rights_license"] == "ODbL-1.0"
                and source["attribution"] == "© OpenStreetMap contributors",
                f"Source rights gate failed for {candidate_id}")
        counters["source_rights_cleared"] += 1
        raw_geometry = source["geometry"]
        require(hashlib.sha256(raw_geometry).hexdigest() == evaluation["geometry_sha256"],
                f"Source geometry checksum mismatch for {candidate_id}")
        geometry = wkb.loads(raw_geometry)
        require(isinstance(geometry, (Polygon, MultiPolygon)) and geometry.is_valid
                and not geometry.is_empty and geometry.area > 0,
                f"Invalid source footprint for {candidate_id}")
        height_basis = assessment.get("height_evidence", {}).get("render_height_basis")
        tags = json.loads(source["raw_tags"] or "{}")
        if height_basis == "mapped_height_tag":
            require(tags.get("height") is not None, f"Height basis lacks source tag: {candidate_id}")
        elif height_basis == "mapped_floors_times_3m":
            require(tags.get("building:levels") is not None, f"Floor basis lacks source tag: {candidate_id}")
        else:
            require(height_basis is None, f"Unrecognized rendered height basis: {candidate_id}")
        source_height_counts[height_basis or "flat_only"] += 1

        selected = assessment["selected_model"]
        rights = selected.get("rights_evidence", {})
        require(selected.get("kind") == "mapped_construction"
                and selected.get("attribution") == "© OpenStreetMap contributors"
                and selected.get("public_attribution") == "© OpenStreetMap contributors; ODbL 1.0"
                and rights == {"license": "ODbL-1.0", "display_cleared": True,
                               "attribution_required": True},
                f"Assessment rights gate failed for {candidate_id}")
        url = str(selected.get("tileset_url", ""))
        prefix = f"/assets/{public_root}/"
        require(url.startswith(prefix) and "?" not in url and "#" not in url,
                f"Unexpected staged asset URL: {candidate_id}")
        tile_name = url[len(prefix):]
        require(tile_name in entries_by_path and tile_name.endswith(".tileset.json"),
                f"Tileset missing from public manifest: {candidate_id}")
        require(tile_name not in processed_tile_names, f"Duplicate asset selected by two records: {tile_name}")
        processed_tile_names.add(tile_name)
        tile_path = public_dir / tile_name
        tile = load_json(tile_path)
        check_public_strings(tile, tile_name)
        content_sha = hashlib.sha256(tile_path.read_bytes()).hexdigest()
        require(content_sha == selected["content_hash"] == tile_name.removeprefix("model_").removesuffix(".tileset.json"),
                f"Selected content hash mismatch for {candidate_id}")
        require(tile.get("asset", {}).get("version") == "1.1"
                and tile.get("asset", {}).get("tilesetVersion") == "mapped-construction-models-v1",
                f"Unexpected tileset version: {tile_name}")
        extras = tile.get("extras", {})
        require(extras.get("source_ids_redacted") is True
                and extras.get("not_approved_project_model") is True
                and extras.get("attribution") == "© OpenStreetMap contributors",
                f"Tileset redaction/attribution gate failed: {tile_name}")
        root = tile.get("root", {})
        transform = root.get("transform", [])
        require(len(transform) == 16 and all(math.isfinite(float(v)) for v in transform),
                f"Invalid ENU transform: {tile_name}")
        bounds = root.get("boundingVolume", {}).get("region", [])
        require(len(bounds) == 6 and all(math.isfinite(float(v)) for v in bounds),
                f"Invalid root region: {tile_name}")
        west, south, east, north = geometry.bounds
        expected_bounds = [math.radians(west), math.radians(south), math.radians(east),
                           math.radians(north), 0.0,
                           BASE_HEIGHT_M + (float(assessment["height_evidence"]["render_height_m"] or 0)) + 2.0]
        for actual, expected in zip(bounds, expected_bounds):
            error = abs(float(actual) - expected)
            maximum_region_error_rad = max(maximum_region_error_rad, error)
            require(error <= 1e-10, f"Tileset region disagrees with source bounds: {tile_name}")
        model_height_raw = assessment["height_evidence"].get("render_height_m")
        model_height = float(model_height_raw) if model_height_raw is not None else None
        children = root.get("children", [])
        require(len(children) == (1 if model_height is not None else 0),
                f"Unexpected volume-child count: {tile_name}")
        flat_uri = root.get("content", {}).get("uri")
        require(isinstance(flat_uri, str) and flat_uri.endswith(".glb")
                and flat_uri in entries_by_path, f"Flat GLB missing: {tile_name}")
        uris = [flat_uri]
        if children:
            child_region = children[0].get("boundingVolume", {}).get("region", [])
            require(child_region == bounds, f"Volume child bounds differ from root: {tile_name}")
            volume_uri = children[0].get("content", {}).get("uri")
            require(isinstance(volume_uri, str) and volume_uri.endswith(".glb")
                    and volume_uri in entries_by_path, f"Volume GLB missing: {tile_name}")
            uris.append(volume_uri)
        require(assessment.get("depiction") == extras.get("depiction"),
                f"Assessment/tileset depiction mismatch: {tile_name}")

        flat_shape: Polygon | MultiPolygon | None = None
        for uri in uris:
            processed_glb_names.add(uri)
            if uri not in glb_cache:
                glb_path = public_dir / uri
                document, binary = glb_document(glb_path)
                glb_cache[uri] = (document, binary,
                                  glb_geometry(document, binary, glb_path,
                                               volume_expected=(uri != flat_uri),
                                               expected_height=model_height if uri != flat_uri else None))
            document, binary, decoded = glb_cache[uri]
            volume_expected = uri != flat_uri
            require((len(document["meshes"][0]["primitives"]) == 2) == volume_expected,
                    f"GLB flat/volume role mismatch: {uri}")
            expected_glb_height = model_height if volume_expected else None
            if expected_glb_height is None:
                vertical_error = max(abs(decoded["min"][1] - BASE_HEIGHT_M),
                                     abs(decoded["max"][1] - BASE_HEIGHT_M))
            else:
                vertical_error = max(abs(decoded["min"][1] - BASE_HEIGHT_M),
                                     abs(decoded["max"][1] - (BASE_HEIGHT_M + expected_glb_height)))
            maximum_height_error = max(maximum_height_error, vertical_error)
            if not volume_expected:
                flat_shape = unary_union(decoded["roof_triangles"])
                require(flat_shape.is_valid and flat_shape.area > 0,
                        f"Decoded flat mesh is invalid: {uri}")
                origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
                expected_shape = projected_source(geometry, origin_lon, origin_lat)
                area_error = float(flat_shape.symmetric_difference(expected_shape).area)
                maximum_area_error = max(maximum_area_error, area_error)
                require(area_error <= max(0.05, float(expected_shape.area) * 1e-6),
                        f"Decoded GLB footprint differs from source after (x,-z,y): {uri}; area error={area_error:.6g}")
                source_holes = interior_count(expected_shape)
                decoded_holes = interior_count(flat_shape)
                metrics = evaluation.get("geometry_metrics", {})
                require(decoded_holes == source_holes == int(metrics.get("holes", -1)),
                        f"Courtyard/hole loss in flat mesh: {uri}")
                require(decoded["primitive_triangles"][0] == int(metrics.get("flat_triangles", -1)),
                        f"Flat roof triangle count mismatch: {uri}")
                hole_total += decoded_holes
                triangle_total += decoded["primitive_triangles"][0]
                counters["flat_mesh_source_matches"] += 1
        require(flat_shape is not None, f"No decoded flat footprint: {tile_name}")
        counters["records_audited"] += 1
        counters["volume_models"] += model_height is not None
        counters["flat_only_models"] += model_height is None

    require(processed_tile_names == {name for name in expected_paths if name.endswith(".tileset.json")},
            "Not all 756 public tilesets were linked to one ready assessment")
    require(processed_glb_names == {name for name in expected_paths if name.endswith(".glb")},
            "A public GLB is not referenced or was not decoded")
    require(source_height_counts == Counter({"mapped_height_tag": 32,
                                             "mapped_floors_times_3m": 241,
                                             "flat_only": 483}),
            f"Source height basis counts differ: {dict(source_height_counts)}")
    require(counters["records_audited"] == counters["flat_mesh_source_matches"] == 756,
            "Not all flat meshes were decoded and compared")
    require(maximum_height_error <= 0.08, "Decoded GLB vertical bounds exceed tolerance")
    require(counters["volume_models"] == 273 and counters["flat_only_models"] == 483,
            "Volume/flat-only count mismatch")
    require(hole_total == int(run_manifest["geometry_counts"]["holes"]) == 12,
            "Courtyard total differs from staged manifest")
    require(triangle_total == int(run_manifest["geometry_counts"]["flat_triangles"]) == 6531,
            "Triangle total differs from staged manifest")

    comparison: dict[str, Any] = {"compared_to_run": None, "glb_hash_set_identical": None,
                                  "matching_glb_files": None}
    if compare_run_dir is not None:
        compare_id, compare_hashes = public_glb_hashes(compare_run_dir, asset_root)
        current_glb_hashes = {path: entry["sha256"] for path, entry in entries_by_path.items()
                              if path.endswith(".glb")}
        require(current_glb_hashes == compare_hashes,
                f"GLB content-addressed set differs from {compare_id}")
        comparison = {"compared_to_run": compare_id, "glb_hash_set_identical": True,
                      "matching_glb_files": len(current_glb_hashes)}

    return {
        "run_id": run_id,
        "public_root": public_root,
        "run_checked_at": run_manifest.get("checked_at"),
        "result": "PASS",
        "read_only": True,
        "active_pointer_changed": False,
        "assessments": len(assessments),
        "ready_models_audited": counters["records_audited"],
        "public_tilesets": ext_counts["tileset"],
        "unique_glb_files": ext_counts["glb"],
        "volume_models": counters["volume_models"],
        "flat_only_models": counters["flat_only_models"],
        "source_rights_cleared": counters["source_rights_cleared"],
        "flat_mesh_source_matches_after_y_up_fix_x_minus_z_y": counters["flat_mesh_source_matches"],
        "decoded_flat_triangles": triangle_total,
        "decoded_holes_preserved": hole_total,
        "max_symmetric_difference_area_m2": maximum_area_error,
        "max_vertical_bounds_error_m": maximum_height_error,
        "max_region_bounds_error_rad": maximum_region_error_rad,
        "public_files_manifested": len(expected_paths),
        "public_files_bytes": file_bytes,
        "public_manifest_sha256": sha256_file(public_manifest_path),
        "source_osm_parquet_sha256": source_parquet_hash,
        "private_metadata_or_path_leaks": 0,
        "rights_gate": "ODbL-1.0, source rows cleared, attribution preserved, display_cleared=true",
        "geometry_set_comparison": comparison,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--osm-parquet", type=Path, default=DEFAULT_OSM_PARQUET)
    parser.add_argument("--compare-run-dir", type=Path, default=DEFAULT_COMPARE_RUN_DIR,
                        help="Compare the staged GLB content set with another immutable run")
    parser.add_argument("--preview-only", action="store_true",
                        help="Audit GeoJSON previews and require byte-identical public assets")
    args = parser.parse_args()
    if args.preview_only:
        result = audit_previews(args.run_dir, args.asset_root, args.osm_parquet,
                                args.compare_run_dir)
    else:
        result = audit(args.run_dir, args.asset_root, args.osm_parquet, args.compare_run_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
