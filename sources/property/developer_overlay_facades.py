"""Batched near facade reconstructions with one texture per exact project and tile.

The roof and unstyled walls remain one shared mesh. Styled walls use the same
source polygon and schematic height, with an appearance-only reviewed project
material. A b3dm feature table retains the opaque registry ID for every corpus.
"""

from __future__ import annotations

from array import array
import hashlib
import json
import struct
import sys
from typing import Any

from sources.models.geometry import BASE_HEIGHT_M, ModelMesh
from sources.property.developer_map import _canonical
from sources.property.textured_model import textured_glb


MAX_TILE_BYTES = 10_000_000


def _chunk(glb: bytes) -> tuple[dict[str, Any], bytes]:
    if len(glb) < 28 or glb[:4] != b"glTF":
        raise ValueError("Invalid styled GLB")
    version, length = struct.unpack_from("<II", glb, 4)
    json_length, json_type = struct.unpack_from("<I4s", glb, 12)
    binary_at = 20 + json_length
    binary_length, binary_type = struct.unpack_from("<I4s", glb, binary_at)
    if (version != 2 or length != len(glb) or json_type != b"JSON" or
            binary_type != b"BIN\x00"):
        raise ValueError("Styled GLB chunk layout is invalid")
    return (json.loads(glb[20:binary_at]),
            glb[binary_at + 8:binary_at + 8 + binary_length])


def _view(document: dict[str, Any], binary: bytes, accessor_id: int) -> bytes:
    accessor = document["accessors"][accessor_id]
    view = document["bufferViews"][accessor["bufferView"]]
    if accessor.get("byteOffset", 0) != 0 or view.get("buffer") != 0:
        raise ValueError("Unsupported styled GLB accessor layout")
    start = view.get("byteOffset", 0)
    end = start + view["byteLength"]
    if end > len(binary):
        raise ValueError("Styled GLB accessor exceeds binary chunk")
    return binary[start:end]


def _source_walls(geometry: Any, origin_lon: float, origin_lat: float,
                  height_m: float, png: bytes) -> tuple[array, array, array]:
    blob, _ = textured_glb(
        geometry, origin_lon, origin_lat, height_m, png,
        texture_basis="generated_project_style_repeat_unoriented")
    document, binary = _chunk(blob)
    positions = array("f")
    positions.frombytes(_view(document, binary, 0))
    uvs = array("f")
    uvs.frombytes(_view(document, binary, 1))
    indices = array("H")
    indices.frombytes(_view(document, binary, 3))
    if (len(positions) // 3 != len(uvs) // 2 or not indices or
            max(indices) >= len(positions) // 3):
        raise ValueError("Styled source walls have invalid indices")
    return positions, uvs, indices


def _pack_glb(mesh: ModelMesh, generic_batches: list[int],
              styled: dict[str, dict[str, Any]]) -> bytes:
    if sys.byteorder != "little":
        raise ValueError("Batched facade GLB requires little-endian host")
    binary = bytearray()
    views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []

    def add(data: bytes, component: int, count: int, kind: str,
            *, target: int, minimum: list[float] | None = None,
            maximum: list[float] | None = None) -> int:
        binary.extend(b"\x00" * (-len(binary) % 4))
        offset = len(binary)
        binary.extend(data)
        view_id = len(views)
        views.append({"buffer": 0, "byteOffset": offset,
                      "byteLength": len(data), "target": target})
        result = {"bufferView": view_id, "componentType": component,
                  "count": count, "type": kind}
        if minimum is not None:
            result["min"] = minimum
        if maximum is not None:
            result["max"] = maximum
        accessors.append(result)
        return len(accessors) - 1

    primitives: list[dict[str, Any]] = []
    materials = [
        {"name": "source_roof", "doubleSided": True,
         "extensions": {"KHR_materials_unlit": {}},
         "pbrMetallicRoughness": {"baseColorFactor": [0.63, 0.57, 0.45, 1],
                                  "metallicFactor": 0, "roughnessFactor": 1}},
        {"name": "generic_schematic_wall", "doubleSided": True,
         "extensions": {"KHR_materials_unlit": {}},
         "pbrMetallicRoughness": {"baseColorFactor": [0.55, 0.50, 0.43, 1],
                                  "metallicFactor": 0, "roughnessFactor": 1}},
    ]
    images: list[dict[str, Any]] = []
    textures: list[dict[str, int]] = []
    texture_by_hash: dict[str, int] = {}
    if not mesh.positions or len(mesh.positions) // 3 != len(generic_batches):
        raise ValueError("Generic roof batch IDs do not cover all vertices")
    positions = array("f", mesh.positions)
    position_id = add(positions.tobytes(), 5126, len(positions) // 3, "VEC3",
                      target=34962,
                      minimum=[min(positions[i::3]) for i in range(3)],
                      maximum=[max(positions[i::3]) for i in range(3)])
    batch_id = add(array("H", generic_batches).tobytes(), 5123,
                   len(generic_batches), "SCALAR", target=34962,
                   minimum=[min(generic_batches)], maximum=[max(generic_batches)])
    index_type = 5123 if len(generic_batches) <= 65535 else 5125
    if mesh.roof:
        indices = array("H" if index_type == 5123 else "I", mesh.roof)
        roof_id = add(indices.tobytes(), index_type, len(indices), "SCALAR", target=34963)
        primitives.append({"attributes": {"POSITION": position_id, "_BATCHID": batch_id},
                           "indices": roof_id, "material": 0, "mode": 4})
    if mesh.walls:
        indices = array("H" if index_type == 5123 else "I", mesh.walls)
        wall_id = add(indices.tobytes(), index_type, len(indices), "SCALAR", target=34963)
        primitives.append({"attributes": {"POSITION": position_id, "_BATCHID": batch_id},
                           "indices": wall_id, "material": 1, "mode": 4})

    for style_id, group in sorted(styled.items()):
        positions = group["positions"]
        uvs = group["uvs"]
        batches = group["batches"]
        indices = group["indices"]
        if (not indices or len(positions) // 3 != len(uvs) // 2 or
                len(positions) // 3 != len(batches) or
                max(indices) >= len(batches)):
            raise ValueError("Batched facade wall arrays are inconsistent")
        style_position_id = add(positions.tobytes(), 5126, len(batches), "VEC3",
                                target=34962,
                                minimum=[min(positions[i::3]) for i in range(3)],
                                maximum=[max(positions[i::3]) for i in range(3)])
        uv_id = add(uvs.tobytes(), 5126, len(batches), "VEC2", target=34962)
        styled_batch_id = add(array("H", batches).tobytes(), 5123,
                              len(batches), "SCALAR", target=34962,
                              minimum=[min(batches)], maximum=[max(batches)])
        styled_index_type = 5123 if len(batches) <= 65535 else 5125
        styled_indices = array("H" if styled_index_type == 5123 else "I", indices)
        styled_index_id = add(styled_indices.tobytes(), styled_index_type,
                              len(styled_indices), "SCALAR", target=34963)
        png = group["png"]
        png_sha = hashlib.sha256(png).hexdigest()
        texture_id = texture_by_hash.get(png_sha)
        if texture_id is None:
            image_view_id = len(views)
            binary.extend(b"\x00" * (-len(binary) % 4))
            image_offset = len(binary)
            binary.extend(png)
            views.append({"buffer": 0, "byteOffset": image_offset,
                          "byteLength": len(png)})
            image_id = len(images)
            images.append({"bufferView": image_view_id, "mimeType": "image/png"})
            texture_id = len(textures)
            textures.append({"sampler": 0, "source": image_id})
            texture_by_hash[png_sha] = texture_id
        material_id = len(materials)
        materials.append({
            "name": f"project_style_{style_id}", "doubleSided": True,
            "extensions": {"KHR_materials_unlit": {}},
            "pbrMetallicRoughness": {"baseColorTexture": {"index": texture_id},
                                     "metallicFactor": 0, "roughnessFactor": 1},
        })
        primitives.append({"attributes": {"POSITION": style_position_id,
                                           "TEXCOORD_0": uv_id,
                                           "_BATCHID": styled_batch_id},
                           "indices": styled_index_id, "material": material_id,
                           "mode": 4})

    binary.extend(b"\x00" * (-len(binary) % 4))
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "Novostroy grouped facade reconstruction",
                  "extras": {"reconstruction": True, "source_ids_redacted": True,
                             "style_basis": "project_style_inference"}},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}], "materials": materials,
        "buffers": [{"byteLength": len(binary)}], "bufferViews": views,
        "accessors": accessors,
    }
    if textures:
        document.update({"images": images, "textures": textures,
                         "samplers": [{"magFilter": 9729, "minFilter": 9987,
                                       "wrapS": 10497, "wrapT": 10497}]})
    encoded = _canonical(document)
    encoded += b" " * (-len(encoded) % 4)
    return (struct.pack("<4sII", b"glTF", 2,
                        12 + 8 + len(encoded) + 8 + len(binary))
            + struct.pack("<I4s", len(encoded), b"JSON") + encoded
            + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def styled_batched_tile(items: list[dict[str, Any]], origin_lon: float,
                        origin_lat: float,
                        styles: dict[str, dict[str, Any]]) -> bytes:
    """Return one b3dm with fallback roof/walls and project-grouped style walls."""
    if not items or len(items) > 256:
        raise ValueError("Invalid styled spatial tile size")
    registry_ids = [item["registry_id"] for item in items]
    if len(set(registry_ids)) != len(registry_ids):
        raise ValueError("Repeated registry identity in styled spatial tile")
    mesh = ModelMesh(origin_lon, origin_lat, None)
    generic_batches: list[int] = []
    styled: dict[str, dict[str, Any]] = {}
    modes: list[str] = []
    heights: list[float | None] = []
    style_ids: list[str | None] = []
    for batch_id, item in enumerate(items):
        mesh.vertices.clear()
        before = len(mesh.positions) // 3
        before_walls = len(mesh.walls)
        height = item.get("volume_height_m")
        material = styles.get(item["publisher_project_id"]) if height else None
        mesh.add(item["geometry"],
                 top_height_m=(BASE_HEIGHT_M + height if height else None))
        after = len(mesh.positions) // 3
        generic_batches.extend([batch_id] * (after - before))
        if material is not None:
            del mesh.walls[before_walls:]
            style_id = material["style_id"]
            group = styled.setdefault(style_id, {
                "positions": array("f"), "uvs": array("f"),
                "indices": array("I"), "batches": [], "png": material["png"],
            })
            if group["png"] != material["png"]:
                raise ValueError("One style ID maps to multiple project textures")
            source_positions, source_uvs, source_indices = _source_walls(
                item["geometry"], origin_lon, origin_lat, height, material["png"])
            offset = len(group["positions"]) // 3
            group["positions"].extend(source_positions)
            group["uvs"].extend(source_uvs)
            group["indices"].extend(index + offset for index in source_indices)
            group["batches"].extend([batch_id] * (len(source_positions) // 3))
        modes.append("source_footprint_schematic_volume" if height else "source_footprint_flat")
        heights.append(height)
        style_ids.append(material["style_id"] if material is not None else None)
    glb = _pack_glb(mesh, generic_batches, styled)
    feature_table = _canonical({"BATCH_LENGTH": len(items)})
    feature_table += b" " * (-(28 + len(feature_table)) % 8)
    batch_table = _canonical({"registry_id": registry_ids,
                              "render_mode": modes, "height_m": heights,
                              "facade_style_id": style_ids})
    batch_table += b" " * (-(28 + len(feature_table) + len(batch_table)) % 8)
    total = 28 + len(feature_table) + len(batch_table) + len(glb)
    if total > MAX_TILE_BYTES:
        raise ValueError("Styled spatial tile exceeds bounded size")
    return (struct.pack("<4sIIIIII", b"b3dm", 1, total, len(feature_table), 0,
                        len(batch_table), 0) + feature_table + batch_table + glb)
