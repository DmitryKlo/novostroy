"""Replace solid region cell boxes with sparse individual footprint volumes.

The existing detailed 3D cells and source-height index remain immutable. This
offline builder reads only their pinned building snapshot, builds one simplified
overview mesh per region, and keeps the lazy detailed subtrees.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
from array import array
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq
from shapely import constrained_delaunay_triangles, make_valid, orient_polygons, wkb
from shapely.geometry import Polygon

from backend.store import ASSET_ROOT
from pipeline.build_citywide import polygon_parts
from pipeline.build_progressive_city import SOURCE, sha256
from pipeline.build_tiles import ecef, enu_transform, save_bytes, save_json

BASE = ASSET_ROOT / "citywide-v8"
OUTPUT = ASSET_ROOT / "citywide-v9"
SAMPLE = ASSET_ROOT / ".citywide-v9-sample"
SIMPLIFY_M = 2.0
OVERVIEW_POLICY = "individual_simplified_footprint_volumes_no_cell_aggregation"
ROOF_COLOR = [0.36, 0.42, 0.46, 1.0]
WALL_COLOR = [0.25, 0.30, 0.34, 1.0]


class FootprintVolumeMesh:
    """Indexed, unlit top and wall surfaces, with no cell-filling geometry."""

    def __init__(self, origin_lon: float, origin_lat: float) -> None:
        frame = enu_transform(origin_lon, origin_lat)
        self.origin = frame[12:15]
        self.axes = (frame[0:3], frame[4:7], frame[8:11])
        self.positions = array("f")
        self.roof_indices = array("I")
        self.wall_indices = array("I")
        self.vertex_ids: dict[tuple[float, float, float], int] = {}
        self.features = 0
        self.roof_triangles = 0
        self.wall_triangles = 0
        self.holes = 0
        self.max_height_m = 0.0
        self.area_m2 = 0.0
        self.origin_lat = origin_lat

    def _vertex(self, lon: float, lat: float, height_m: float) -> int:
        key = (lon, lat, height_m)
        found = self.vertex_ids.get(key)
        if found is not None:
            return found
        world = ecef(lon, lat, height_m)
        delta = [world[index] - self.origin[index] for index in range(3)]
        east, north, up = (sum(axis[index] * delta[index] for index in range(3))
                           for axis in self.axes)
        number = len(self.positions) // 3
        self.positions.extend((east, up, -north))
        self.vertex_ids[key] = number
        return number

    def _polygon(self, polygon: Polygon, height_m: float) -> None:
        polygon = orient_polygons(polygon)
        roofs = 0
        covered = 0.0
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            if not polygon.covers(triangle):
                continue
            corners = [(float(x), float(y)) for x, y, *_ in list(triangle.exterior.coords)[:3]]
            signed = ((corners[1][0] - corners[0][0]) * (corners[2][1] - corners[0][1])
                      - (corners[1][1] - corners[0][1]) * (corners[2][0] - corners[0][0]))
            if abs(signed) <= 1e-20:
                continue
            if signed < 0:
                corners[1], corners[2] = corners[2], corners[1]
            self.roof_indices.extend(self._vertex(lon, lat, height_m) for lon, lat in corners)
            roofs += 1
            covered += triangle.area
        if not roofs or abs(covered - polygon.area) > max(1e-13, polygon.area * 1e-4):
            raise ValueError("Overview roof lost source footprint area")
        self.roof_triangles += roofs
        self.holes += len(polygon.interiors)
        self.area_m2 += polygon.area * 111132 * 111320 * math.cos(math.radians(self.origin_lat))
        for ring in (polygon.exterior, *polygon.interiors):
            corners = [(float(x), float(y)) for x, y, *_ in ring.coords]
            for first, second in zip(corners, corners[1:]):
                if first == second:
                    continue
                a = self._vertex(*first, 0.0)
                b = self._vertex(*second, 0.0)
                c = self._vertex(*second, height_m)
                d = self._vertex(*first, height_m)
                self.wall_indices.extend((a, b, c, a, c, d))
                self.wall_triangles += 2

    def add(self, geometry: Any, height_m: float) -> None:
        if not math.isfinite(height_m) or height_m <= 0:
            raise ValueError("Overview height differs from reviewed source index")
        valid = make_valid(geometry) if not geometry.is_valid else geometry
        parts = list(polygon_parts(valid))
        if not parts:
            raise ValueError("Source building has no footprint polygon")
        made = 0
        for part in parts:
            simplified = part.simplify(SIMPLIFY_M / 111132, preserve_topology=True)
            if simplified.is_empty or simplified.area <= 0:
                simplified = part
            for polygon in polygon_parts(simplified):
                self._polygon(polygon, height_m)
                made += 1
        if not made:
            raise ValueError("Source building produced no overview volume")
        self.features += 1
        self.max_height_m = max(self.max_height_m, height_m)

    def to_glb(self) -> bytes:
        if sys.byteorder != "little" or not self.roof_indices or not self.wall_indices:
            raise ValueError("Empty overview or unsupported host endian")
        positions = self.positions.tobytes()
        roof = self.roof_indices.tobytes()
        walls = self.wall_indices.tobytes()
        binary = positions + roof + walls
        binary += b"\x00" * (-len(binary) % 4)
        bounds_min = [min(self.positions[axis::3]) for axis in range(3)]
        bounds_max = [max(self.positions[axis::3]) for axis in range(3)]
        document = {
            "asset": {"version": "2.0", "generator": "Novostroy individual footprint volume builder",
                      "extras": {"geometry_type": "simplified_individual_footprint_volumes",
                                 "source_count": self.features,
                                 "simplify_tolerance_m": SIMPLIFY_M,
                                 "height_source": "reviewed_private_city_source_index"}},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [
                {"attributes": {"POSITION": 0}, "indices": 1, "material": 0, "mode": 4},
                {"attributes": {"POSITION": 0}, "indices": 2, "material": 1, "mode": 4},
            ]}],
            "materials": [
                {"name": "individual_footprint_roof", "doubleSided": True,
                 "extensions": {"KHR_materials_unlit": {}},
                 "pbrMetallicRoughness": {"baseColorFactor": ROOF_COLOR,
                                          "metallicFactor": 0, "roughnessFactor": 1}},
                {"name": "individual_footprint_wall", "doubleSided": True,
                 "extensions": {"KHR_materials_unlit": {}},
                 "pbrMetallicRoughness": {"baseColorFactor": WALL_COLOR,
                                          "metallicFactor": 0, "roughnessFactor": 1}},
            ],
            "buffers": [{"byteLength": len(binary)}],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962},
                {"buffer": 0, "byteOffset": len(positions), "byteLength": len(roof), "target": 34963},
                {"buffer": 0, "byteOffset": len(positions) + len(roof),
                 "byteLength": len(walls), "target": 34963},
            ],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": len(self.positions) // 3,
                 "type": "VEC3", "min": bounds_min, "max": bounds_max},
                {"bufferView": 1, "componentType": 5125, "count": len(self.roof_indices),
                 "type": "SCALAR"},
                {"bufferView": 2, "componentType": 5125, "count": len(self.wall_indices),
                 "type": "SCALAR"},
            ],
        }
        json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_part += b" " * (-len(json_part) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + 8 + len(binary))
                + struct.pack("<I4s", len(json_part), b"JSON") + json_part
                + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def _source_index(path: Path) -> tuple[dict[str, tuple[int, int, float]], dict[tuple[int, int], int]]:
    mapping: dict[str, tuple[int, int, float]] = {}
    counts: dict[tuple[int, int], int] = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            _, x, y = row["tile"].removesuffix(".glb").split("_")
            region = (int(x) // 5, int(y) // 5)
            if row["source_id"] in mapping:
                raise ValueError("Duplicate reviewed city source ID")
            mapping[row["source_id"]] = (*region, float(row["height_m"]))
            counts[region] = counts.get(region, 0) + 1
    if len(mapping) != 564031 or len(counts) != 212:
        raise ValueError("Reviewed city source-height index changed")
    return mapping, counts


def _stage_source(source: Path, index: dict[str, tuple[int, int, float]],
                  connection: sqlite3.Connection,
                  sample_region: tuple[int, int] | None = None) -> int:
    connection.execute("CREATE TABLE footprint (rx INTEGER, ry INTEGER, source_id TEXT, height REAL, geometry BLOB)")
    connection.execute("CREATE INDEX footprint_region ON footprint(rx,ry,source_id)")
    total = 0
    for batch in pq.ParquetFile(source).iter_batches(batch_size=4096, columns=["id", "geometry"]):
        rows = []
        for row in batch.to_pylist():
            record = index.get(row["id"])
            if record is None or not row["geometry"]:
                raise ValueError("Pinned geometry differs from reviewed source-height index")
            rx, ry, height = record
            if sample_region is None or sample_region == (rx, ry):
                rows.append((rx, ry, row["id"], height, row["geometry"]))
                total += 1
        with connection:
            connection.executemany("INSERT INTO footprint VALUES (?,?,?,?,?)", rows)
    return total


def _region_id(tile: dict[str, Any]) -> tuple[int, int]:
    name = tile["content"]["uri"]
    _, rx, ry = name.removesuffix(".glb").split("_")
    return int(rx), int(ry)


def _set_v9_tree_policy(tree: dict[str, Any]) -> None:
    tree["asset"]["tilesetVersion"] = "citywide-individual-footprint-massing-v9"
    tree["extras"].pop("volume_and_detail_revision", None)
    tree["extras"].update({"region_overview_geometry": OVERVIEW_POLICY,
                           "region_simplify_tolerance_m": SIMPLIFY_M,
                           "region_overview_has_cell_boxes": False,
                           "geometry_policy": "individual source footprint roof and wall volumes; no cell aggregation",
                           "lazy_metadata_policy": "individual_footprint_regions_with_lazy_external_exact_detail_tilesets",
                           "region_overview_revision": "citywide-v9",
                           "detail_revision": "citywide-v8_byte_identical"})


def refresh_unpublished_metadata(private: Path = OUTPUT,
                                 public: Path = ASSET_ROOT / "public-citywide-v9") -> dict[str, Any]:
    """Correct only the staged v9 policy labels before pointer publication."""
    pointer = json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    if pointer.get("city_tileset_url") == "/assets/public-citywide-v9/tileset.json":
        raise ValueError("An active immutable v9 release cannot be refreshed")
    manifest = json.loads((private / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("version") != "9" or manifest.get("overview_policy") != OVERVIEW_POLICY
            or not (public / "manifest.json").is_file()):
        raise ValueError("Expected unpublished staged v9 candidate")
    tree = json.loads((private / "tileset.json").read_text(encoding="utf-8"))
    _set_v9_tree_policy(tree)
    root_blob = (json.dumps(tree, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest.pop("revised_coarse_region_tiles", None)
    manifest.update({"root_tileset_raw_bytes": len(root_blob),
                     "root_tileset_gzip5_bytes": len(gzip.compress(root_blob, compresslevel=5)),
                     "region_overview_roof_base_color_factor": ROOF_COLOR,
                     "region_overview_wall_base_color_factor": WALL_COLOR})
    save_bytes(private / "tileset.json", root_blob)
    save_json(private / "manifest.json", manifest)
    save_bytes(public / "tileset.json", root_blob)
    from pipeline.publish_public_assets import _public_manifest

    save_json(public / "manifest.json", _public_manifest(
        manifest, url=f"/assets/{public.name}/tileset.json",
        private_manifest_sha256=sha256(private / "manifest.json"),
        attribution=tree["extras"]["attribution"]))
    return {"private_manifest_sha256": sha256(private / "manifest.json"),
            "public_manifest_sha256": sha256(public / "manifest.json"),
            "root_tileset_raw_bytes": len(root_blob)}


def _build_region(tile: dict[str, Any], connection: sqlite3.Connection,
                  expected: int) -> tuple[bytes, dict[str, Any]]:
    rx, ry = _region_id(tile)
    bounds = tile["boundingVolume"]["region"]
    west, south, east, north = (math.degrees(value) for value in bounds[:4])
    origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
    mesh = FootprintVolumeMesh(origin_lon, origin_lat)
    rows = connection.execute("SELECT geometry,height FROM footprint WHERE rx=? AND ry=? ORDER BY source_id",
                              (rx, ry))
    for geometry, height in rows:
        mesh.add(wkb.loads(geometry), float(height))
    if mesh.features != expected or mesh.max_height_m > bounds[5] + 1e-6:
        raise ValueError("Region source count or conservative height bound changed")
    payload = mesh.to_glb()
    bbox_area_m2 = ((east - west) * 111320 * math.cos(math.radians(origin_lat))
                    * (north - south) * 111132)
    metrics = {"region": f"massing_region_{rx}_{ry}.glb", "source_count": mesh.features,
               "roof_triangles": mesh.roof_triangles, "wall_triangles": mesh.wall_triangles,
               "vertices": len(mesh.positions) // 3, "bytes": len(payload),
               "holes": mesh.holes, "max_height_m": mesh.max_height_m,
               "summed_footprint_area_m2": round(mesh.area_m2, 3),
               "region_bbox_area_m2": round(bbox_area_m2, 3),
               "summed_coverage_fraction": round(mesh.area_m2 / bbox_area_m2, 6)}
    return payload, metrics


def build(source: Path = SOURCE, base: Path = BASE, output: Path = OUTPUT,
          sample_region: tuple[int, int] | None = None) -> dict[str, Any]:
    base_manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if (base_manifest.get("version") != "8" or base_manifest.get("partial")
            or base_manifest.get("source_sha256") != sha256(source)
            or base_manifest.get("source_rows") != 564031):
        raise ValueError("Expected complete reviewed v8 and pinned source geometry")
    base_hash = sha256(base / "manifest.json")
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("base_v8_manifest_sha256") != base_hash or saved.get("overview_policy") != OVERVIEW_POLICY:
            raise ValueError("Existing v9 differs from reviewed v8 base")
        return {"status": "no_op", **saved}
    source_index, counts = _source_index(base / "source_tile_index.jsonl")
    tree = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
    parents = tree["root"]["children"]
    if len(parents) != 212:
        raise ValueError("Base city region count changed")
    with tempfile.TemporaryDirectory(prefix=".citywide-v9-work.", dir=ASSET_ROOT) as temporary:
        connection = sqlite3.connect(Path(temporary) / "footprints.sqlite3")
        staged = _stage_source(source, source_index, connection, sample_region)
        expected_total = counts[sample_region] if sample_region else 564031
        if staged != expected_total:
            raise ValueError("Source geometry rows did not match reviewed source index")
        if sample_region:
            parent = next(tile for tile in parents if _region_id(tile) == sample_region)
            payload, metric = _build_region(parent, connection, counts[sample_region])
            sample_dir = SAMPLE
            sample_dir.mkdir(parents=True, exist_ok=True)
            save_bytes(sample_dir / metric["region"], payload)
            save_json(sample_dir / "manifest.json", {"version": "v9-sample", "sample_region": sample_region,
                                                     "source_sha256": base_manifest["source_sha256"],
                                                     "base_v8_manifest_sha256": base_hash,
                                                     "overview_policy": OVERVIEW_POLICY, **metric})
            connection.close()
            return {"status": "sample", **metric}
        with tempfile.TemporaryDirectory(prefix=".citywide-v9-stage.", dir=ASSET_ROOT) as stage_name:
            stage = Path(stage_name)
            for path in base.iterdir():
                if path.name in {"tileset.json", "manifest.json"} or path.name.startswith("region_"):
                    continue
                if not path.is_file():
                    raise ValueError(f"Unexpected v8 asset: {path}")
                os.link(path, stage / path.name)
            metrics = []
            external_raw = 0
            external_gzip5 = 0
            for number, parent in enumerate(parents, 1):
                rx, ry = _region_id(parent)
                payload, metric = _build_region(parent, connection, counts[rx, ry])
                save_bytes(stage / metric["region"], payload)
                external_name = parent["children"][0]["content"]["uri"]
                external = json.loads((base / external_name).read_text(encoding="utf-8"))
                if external["root"]["content"]["uri"] != f"region_{rx}_{ry}.glb":
                    raise ValueError("Base lazy region no longer contains reviewed coarse tile")
                external["root"]["content"] = {"uri": metric["region"]}
                save_json(stage / external_name, external)
                external_blob = (json.dumps(external, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n").encode("utf-8")
                external_raw += len(external_blob)
                external_gzip5 += len(gzip.compress(external_blob, compresslevel=5))
                parent["content"] = {"uri": metric["region"]}
                metrics.append(metric)
                if number % 20 == 0:
                    print(f"built individual massing regions {number}/{len(parents)}", flush=True)
            connection.close()
            _set_v9_tree_policy(tree)
            root_blob = (json.dumps(tree, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            save_json(stage / "tileset.json", tree)
            save_json(stage / "region_overview_metrics.json", metrics)
            manifest = {key: value for key, value in base_manifest.items()
                        if key not in {"base_v7_manifest_sha256", "ground_first_city_policy",
                                       "reused_v7_volume_detail_glbs", "revised_coarse_region_tiles"}}
            manifest.update({"version": "9", "tileset_url": f"/assets/{output.name}/tileset.json",
                             "base_v8_manifest_sha256": base_hash,
                             "overview_policy": OVERVIEW_POLICY,
                             "region_simplify_tolerance_m": SIMPLIFY_M,
                             "region_overview_glbs": len(metrics),
                             "region_overview_source_rows": sum(row["source_count"] for row in metrics),
                             "region_overview_roof_triangles": sum(row["roof_triangles"] for row in metrics),
                             "region_overview_wall_triangles": sum(row["wall_triangles"] for row in metrics),
                             "region_overview_bytes": sum(row["bytes"] for row in metrics),
                             "region_overview_max_bytes": max(row["bytes"] for row in metrics),
                             "region_overview_cell_box_count": 0,
                             "region_overview_roof_base_color_factor": ROOF_COLOR,
                             "region_overview_wall_base_color_factor": WALL_COLOR,
                             "reused_v8_detail_glbs": sum(1 for _ in stage.glob("cell_*.glb")),
                             "root_tileset_raw_bytes": len(root_blob),
                             "root_tileset_gzip5_bytes": len(gzip.compress(root_blob, compresslevel=5)),
                             "external_tilesets_raw_bytes": external_raw,
                             "external_tilesets_gzip5_bytes": external_gzip5})
            if (manifest["reused_v8_detail_glbs"] != 3396
                    or manifest["region_overview_source_rows"] != 564031):
                raise ValueError("City v9 omitted a reviewed source/detail tile")
            save_json(stage / "manifest.json", manifest)
            stage.replace(output)
    return {"status": "built", **manifest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--sample-region", nargs=2, type=int, metavar=("RX", "RY"))
    parser.add_argument("--refresh-unpublished-metadata", action="store_true")
    arguments = parser.parse_args()
    result = (refresh_unpublished_metadata(arguments.output)
              if arguments.refresh_unpublished_metadata else build(
                  arguments.source, arguments.base, arguments.output,
                  tuple(arguments.sample_region) if arguments.sample_region else None))
    print(json.dumps(result,
                     ensure_ascii=False, indent=2))
