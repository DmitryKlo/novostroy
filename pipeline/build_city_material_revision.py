"""Match v9 footprint overview lighting to its unchanged detailed volumes.

This v10 revision keeps every rendered footprint triangle unchanged. Vertices
shared by incompatible wall faces are split only to assign correct normals;
the exact detail GLBs and lazy external region trees are reused byte for byte.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import struct
import sys
import tempfile
from array import array
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT
from pipeline.build_footprint_city import ROOF_COLOR, sha256
from pipeline.build_tiles import save_bytes, save_json

BASE = ASSET_ROOT / "citywide-v9"
OUTPUT = ASSET_ROOT / "citywide-v10"
DETAIL_MATERIAL_INDEX = 4
LIGHTING_POLICY = "lit_pbr_matches_unchanged_detailed_city_massing_material"
MIN_WALL_NORMAL_FACE_DOT = 0.1


def _chunks(payload: bytes) -> tuple[dict[str, Any], bytes]:
    if len(payload) < 28 or struct.unpack_from("<4sII", payload) != (b"glTF", 2, len(payload)):
        raise ValueError("Invalid overview GLB header")
    json_size, json_kind = struct.unpack_from("<I4s", payload, 12)
    binary_header = 20 + json_size
    binary_size, binary_kind = struct.unpack_from("<I4s", payload, binary_header)
    if json_kind != b"JSON" or binary_kind != b"BIN\x00" or binary_header + 8 + binary_size != len(payload):
        raise ValueError("Invalid overview GLB chunks")
    return json.loads(payload[20:binary_header]), payload[binary_header + 8:]


def _pack(document: dict[str, Any], binary: bytes) -> bytes:
    json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_part += b" " * (-len(json_part) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + 8 + len(binary))
            + struct.pack("<I4s", len(json_part), b"JSON") + json_part
            + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def _array(document: dict[str, Any], binary: bytes, index: int,
           kind: str, component: int) -> array:
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    if (accessor["componentType"] != component or accessor["type"] != kind
            or accessor.get("byteOffset", 0) != 0 or view.get("byteStride") is not None):
        raise ValueError("Unexpected overview accessor layout")
    components = 3 if kind == "VEC3" else 1
    size = 4 * accessor["count"] * components
    if view["byteLength"] != size:
        raise ValueError("Unexpected overview buffer view length")
    offset = view["byteOffset"]
    values = array("f" if component == 5126 else "I")
    values.frombytes(binary[offset:offset + size])
    return values


def _lit_geometry(positions: array, wall_indices: array) -> tuple[array, array, bytes, bytes, int, int]:
    """Split only conflicting wall vertices and retain all triangle coordinates."""
    original_count = len(positions) // 3
    revised_positions = array("f", positions)
    revised_indices = array("I")
    groups: dict[int, list[dict[str, Any]]] = {}
    degenerate_faces = 0
    if len(wall_indices) % 6:
        raise ValueError("Expected two wall triangles per source edge")
    for start in range(0, len(wall_indices), 6):
        a, b, c, second_a, second_c, d = wall_indices[start:start + 6]
        if (a != second_a or c != second_c
                or max(a, b, c, d) >= original_count):
            raise ValueError("Wall index exceeds overview positions")
        p = [positions[3 * b + axis] - positions[3 * a + axis] for axis in range(3)]
        q = [positions[3 * c + axis] - positions[3 * a + axis] for axis in range(3)]
        cross = (p[1] * q[2] - p[2] * q[1],
                 p[2] * q[0] - p[0] * q[2],
                 p[0] * q[1] - p[1] * q[0])
        if not all(math.isfinite(value) for value in cross):
            raise ValueError("Nonfinite wall face")
        length = math.hypot(*cross)
        if length < 1e-8:
            degenerate_faces += 1
            raise ValueError("v9 contains a degenerate wall triangle")
        face = tuple(value / length for value in cross)

        def assign(vertex: int) -> int:
            options = groups.setdefault(vertex, [])
            chosen: dict[str, Any] | None = None
            chosen_dot = -2.0
            for candidate in options:
                summed = tuple(candidate["sum"][axis] + face[axis] for axis in range(3))
                norm = math.hypot(*summed)
                if norm < 1e-8:
                    continue
                proposed = tuple(value / norm for value in summed)
                if (sum(proposed[axis] * face[axis] for axis in range(3)) < MIN_WALL_NORMAL_FACE_DOT
                        or any(sum(proposed[axis] * old[axis] for axis in range(3)) < MIN_WALL_NORMAL_FACE_DOT
                               for old in candidate["faces"])):
                    continue
                similarity = sum(candidate["sum"][axis] * face[axis] for axis in range(3))
                if similarity > chosen_dot:
                    chosen = candidate
                    chosen_dot = similarity
            if chosen is None:
                index = vertex if not options else len(revised_positions) // 3
                if options:
                    revised_positions.extend(positions[3 * vertex:3 * vertex + 3])
                chosen = {"index": index, "sum": list(face), "faces": [face]}
                options.append(chosen)
            else:
                chosen["faces"].append(face)
                for axis in range(3):
                    chosen["sum"][axis] += face[axis]
            return chosen["index"]

        ia, ib, ic, id_ = (assign(vertex) for vertex in (a, b, c, d))
        revised_indices.extend((ia, ib, ic, ia, ic, id_))
    vertex_count = len(revised_positions) // 3
    roof = bytes((0, 127, 0)) * vertex_count
    wall = bytearray()
    wall_vectors: dict[int, tuple[float, float, float]] = {}
    for options in groups.values():
        for entry in options:
            vector = entry["sum"]
            length = math.hypot(*vector)
            if length < 1e-8 or not math.isfinite(length):
                raise ValueError("Invalid wall vertex normal")
            wall_vectors[entry["index"]] = tuple(value / length for value in vector)
    for vertex in range(vertex_count):
        vector = wall_vectors.get(vertex, (0.0, 1.0, 0.0))
        wall.extend(struct.pack("<bbb", *(max(-127, min(127, round(127 * value)))
                                        for value in vector)))
    return (revised_positions, revised_indices, roof, bytes(wall),
            vertex_count - original_count, degenerate_faces)


def revise_overview(payload: bytes) -> tuple[bytes, dict[str, int]]:
    if sys.byteorder != "little":
        raise ValueError("Overview revision requires little-endian host")
    document, original_binary = _chunks(payload)
    primitives = document["meshes"][0]["primitives"]
    if (len(primitives) != 2 or [item["material"] for item in primitives] != [0, 1]
            or any(item["attributes"] != {"POSITION": 0} for item in primitives)
            or document.get("extensionsUsed") != ["KHR_materials_unlit"]):
        raise ValueError("Expected untouched v9 overview material structure")
    positions = _array(document, original_binary, 0, "VEC3", 5126)
    roof_indices = _array(document, original_binary, 1, "SCALAR", 5125)
    wall_indices = _array(document, original_binary, 2, "SCALAR", 5125)
    if not roof_indices or not wall_indices:
        raise ValueError("Overview has empty surfaces")
    revised_positions, revised_wall_indices, roof_normals, wall_normals, split_vertices, degenerate_faces = (
        _lit_geometry(positions, wall_indices))
    new_position_blob = revised_positions.tobytes()
    roof_index_blob = roof_indices.tobytes()
    wall_index_blob = revised_wall_indices.tobytes()
    binary = bytearray(new_position_blob + roof_index_blob + wall_index_blob)
    document["bufferViews"][0].update({"byteOffset": 0, "byteLength": len(new_position_blob)})
    document["bufferViews"][1].update({"byteOffset": len(new_position_blob),
                                         "byteLength": len(roof_index_blob)})
    document["bufferViews"][2].update({"byteOffset": len(new_position_blob) + len(roof_index_blob),
                                         "byteLength": len(wall_index_blob)})
    document["accessors"][0]["count"] = len(revised_positions) // 3
    for primitive, normal_blob in zip(primitives, (roof_normals, wall_normals), strict=True):
        binary.extend(b"\x00" * (-len(binary) % 4))
        view_index = len(document["bufferViews"])
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary),
                                         "byteLength": len(normal_blob), "target": 34962})
        accessor_index = len(document["accessors"])
        document["accessors"].append({"bufferView": view_index, "componentType": 5120,
                                       "normalized": True, "count": len(revised_positions) // 3,
                                       "type": "VEC3"})
        primitive["attributes"]["NORMAL"] = accessor_index
        binary.extend(normal_blob)
    document["buffers"][0]["byteLength"] = len(binary) + (-len(binary) % 4)
    document.pop("extensionsUsed")
    for material in document["materials"]:
        material.pop("extensions")
        material["pbrMetallicRoughness"]["baseColorFactor"] = ROOF_COLOR.copy()
    document["asset"]["generator"] = "Novostroy footprint volume lighting revision"
    document["asset"]["extras"].update({
        "lighting_policy": LIGHTING_POLICY,
        "normal_encoding": "signed_byte_normalized_per_primitive",
        "material_reference": "unchanged_detail_material_4_city_massing",
    })
    return _pack(document, bytes(binary)), {
        "vertices": len(revised_positions) // 3,
        "roof_triangles": len(roof_indices) // 3,
        "wall_triangles": len(wall_indices) // 3,
        "split_vertices": split_vertices,
        "degenerate_wall_faces": degenerate_faces,
        "normal_bytes": len(roof_normals) + len(wall_normals),
    }


def build(base: Path = BASE, output: Path = OUTPUT) -> dict[str, Any]:
    base_manifest_path = base / "manifest.json"
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    if (base_manifest.get("version") != "9" or base_manifest.get("reused_v8_detail_glbs") != 3396
            or base_manifest.get("region_overview_cell_box_count") != 0):
        raise ValueError("Expected complete immutable citywide-v9 base")
    base_hash = sha256(base_manifest_path)
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if saved.get("version") != "10" or saved.get("base_v9_manifest_sha256") != base_hash:
            raise ValueError("Existing v10 differs from its reviewed v9 base")
        return {"status": "no_op", **saved}
    detail_paths = sorted(base.glob("cell_*.glb"))
    overview_paths = sorted(base.glob("massing_region_*.glb"))
    external_paths = sorted(base.glob("region_*.tileset.json"))
    if (len(detail_paths), len(overview_paths), len(external_paths)) != (3396, 212, 212):
        raise ValueError("Base city hierarchy is incomplete")
    detail_document, _ = _chunks(detail_paths[0].read_bytes())
    detail_material = detail_document["materials"][DETAIL_MATERIAL_INDEX]["pbrMetallicRoughness"]
    if (detail_material.get("baseColorFactor") != ROOF_COLOR
            or detail_material.get("metallicFactor") != 0
            or detail_material.get("roughnessFactor") != 1):
        raise ValueError("Detailed city massing material changed")
    with tempfile.TemporaryDirectory(prefix=".citywide-v10.", dir=ASSET_ROOT) as temporary:
        stage = Path(temporary)
        for path in (*detail_paths, *external_paths, base / "source_tile_index.jsonl",
                     base / "part_tile_index.jsonl"):
            os.link(path, stage / path.name)
        metrics = {item["region"]: item for item in json.loads(
            (base / "region_overview_metrics.json").read_text(encoding="utf-8"))}
        total_bytes = 0
        maximum_bytes = 0
        normal_bytes = 0
        split_vertices = 0
        degenerate_faces = 0
        for position, path in enumerate(overview_paths, 1):
            revised, counts = revise_overview(path.read_bytes())
            save_bytes(stage / path.name, revised)
            metrics[path.name]["bytes"] = len(revised)
            metrics[path.name]["normal_bytes"] = counts["normal_bytes"]
            metrics[path.name]["split_vertices"] = counts["split_vertices"]
            total_bytes += len(revised)
            maximum_bytes = max(maximum_bytes, len(revised))
            normal_bytes += counts["normal_bytes"]
            split_vertices += counts["split_vertices"]
            degenerate_faces += counts["degenerate_wall_faces"]
            if position % 25 == 0:
                print(f"revised city overview lighting {position}/{len(overview_paths)}", flush=True)
        tree = json.loads((base / "tileset.json").read_text(encoding="utf-8"))
        tree["asset"]["tilesetVersion"] = "citywide-individual-footprint-massing-v10"
        tree["extras"].update({"region_overview_revision": "citywide-v10",
                               "region_overview_lighting_policy": LIGHTING_POLICY,
                               "detail_revision": "citywide-v8_byte_identical"})
        root_blob = (json.dumps(tree, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        save_bytes(stage / "tileset.json", root_blob)
        save_json(stage / "region_overview_metrics.json", list(metrics.values()))
        manifest = dict(base_manifest)
        manifest.update({"version": "10", "base_v9_manifest_sha256": base_hash,
                         "region_overview_bytes": total_bytes,
                         "region_overview_max_bytes": maximum_bytes,
                         "region_overview_roof_base_color_factor": ROOF_COLOR,
                         "region_overview_wall_base_color_factor": ROOF_COLOR,
                         "region_overview_lighting_policy": LIGHTING_POLICY,
                         "region_overview_normal_bytes": normal_bytes,
                         "region_overview_degenerate_wall_faces": degenerate_faces,
                         "coarse_triangle_coordinates_identical_to_v9": True,
                         "region_overview_split_vertices": split_vertices,
                         "reused_v9_detail_glbs": len(detail_paths),
                         "root_tileset_raw_bytes": len(root_blob),
                         "root_tileset_gzip5_bytes": len(gzip.compress(root_blob, compresslevel=5)),
                         "tileset_url": f"/assets/{output.name}/tileset.json"})
        save_json(stage / "manifest.json", manifest)
        stage.replace(output)
    return {"status": "built", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.base, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
