"""Bounded UV facade texture on a source footprint, for selected reconstruction.

The repeated image is generated reference art, not a photo unwrap. This module
does not assign source sections to geometric parts of the building.
"""

from __future__ import annotations

from array import array
import json
import math
import struct
import sys
from typing import Any

from shapely import orient_polygons
from shapely.geometry import MultiPolygon, Polygon

from sources.models.geometry import ModelMesh, polygon_parts


def _edge_metres(first: tuple[float, float], second: tuple[float, float]) -> float:
    mean_lat = math.radians((first[1] + second[1]) / 2)
    east = (second[0] - first[0]) * 111_320 * math.cos(mean_lat)
    north = (second[1] - first[1]) * 111_320
    return math.hypot(east, north)


def textured_glb(geometry: Polygon | MultiPolygon, origin_lon: float,
                 origin_lat: float, height_m: float, png: bytes) -> tuple[bytes, dict[str, Any]]:
    if sys.byteorder != "little" or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Textured GLB requires little-endian host and PNG texture")
    if not 0 < height_m <= 300 or not 0 < len(png) <= 10_000_000:
        raise ValueError("Unbounded reconstruction height or texture")
    mesh = ModelMesh(origin_lon, origin_lat, height_m)
    mesh.add(geometry)
    positions = array("f", mesh.positions)
    texcoords = array("f", [0.0] * (len(positions) // 3 * 2))
    roof = list(mesh.roof)
    walls: list[int] = []
    for raw in polygon_parts(geometry):
        part = orient_polygons(raw)
        for ring in (part.exterior, *part.interiors):
            perimeter_u = 0.0
            pairs = [(float(lon), float(lat)) for lon, lat, *_ in ring.coords]
            for first, second in zip(pairs, pairs[1:]):
                distance = _edge_metres(first, second)
                if distance <= 0.001:
                    continue
                next_u = perimeter_u + distance / 9.0  # three schematic bays per tile
                uv = [(perimeter_u, 0.0), (next_u, 0.0),
                      (next_u, height_m / 3.0), (perimeter_u, height_m / 3.0)]
                corners = [(first, 0.65), (second, 0.65),
                           (second, 0.65 + height_m), (first, 0.65 + height_m)]
                start = len(positions) // 3
                for (lon_lat, level), (u, v) in zip(corners, uv):
                    source_idx = mesh.vertices[(lon_lat[0], lon_lat[1], level)]
                    positions.extend(mesh.positions[source_idx * 3:source_idx * 3 + 3])
                    texcoords.extend((u, v))
                walls.extend((start, start + 1, start + 2,
                              start, start + 2, start + 3))
                perimeter_u = next_u
    if not walls or len(positions) // 3 > 65_535:
        raise ValueError("Textured wall geometry exceeds bounded vertices")
    roof_indices = array("H", roof).tobytes()
    wall_indices = array("H", walls).tobytes()
    chunks = [positions.tobytes(), texcoords.tobytes(), roof_indices,
              wall_indices, png]
    buffer = bytearray()
    views = []
    for index, chunk in enumerate(chunks):
        buffer.extend(b"\0" * (-len(buffer) % 4))
        offset = len(buffer)
        buffer.extend(chunk)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(chunk)}
        if index in (0, 1):
            view["target"] = 34962
        elif index in (2, 3):
            view["target"] = 34963
        views.append(view)
    buffer.extend(b"\0" * (-len(buffer) % 4))
    document = {
        "asset": {"version": "2.0", "generator": "Novostroy selected facade reconstruction",
                  "extras": {"reconstruction": True,
                             "texture_basis": "generated_photo_informed_repeat_unoriented",
                             "not_approved_design": True, "source_ids_redacted": True}},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [
            {"attributes": {"POSITION": 0}, "indices": 2, "material": 0, "mode": 4},
            {"attributes": {"POSITION": 0, "TEXCOORD_0": 1},
             "indices": 3, "material": 1, "mode": 4}]}],
        "materials": [
            {"name": "schematic_roof", "doubleSided": True,
             "extensions": {"KHR_materials_unlit": {}},
             "pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.53, 0.54, 1],
                                      "metallicFactor": 0, "roughnessFactor": 1}},
            {"name": "reconstructed_facade_repeat", "doubleSided": True,
             "extensions": {"KHR_materials_unlit": {}},
             "pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                      "metallicFactor": 0, "roughnessFactor": 1}}],
        "textures": [{"sampler": 0, "source": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987,
                      "wrapS": 10497, "wrapT": 10497}],
        "images": [{"bufferView": 4, "mimeType": "image/png"}],
        "buffers": [{"byteLength": len(buffer)}], "bufferViews": views,
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions) // 3,
             "type": "VEC3", "min": [min(positions[i::3]) for i in range(3)],
             "max": [max(positions[i::3]) for i in range(3)]},
            {"bufferView": 1, "componentType": 5126, "count": len(texcoords) // 2,
             "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123, "count": len(roof), "type": "SCALAR"},
            {"bufferView": 3, "componentType": 5123, "count": len(walls), "type": "SCALAR"}],
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    blob = (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(buffer))
            + struct.pack("<I4s", len(encoded), b"JSON") + encoded
            + struct.pack("<I4s", len(buffer), b"BIN\0") + buffer)
    return blob, {"roof_triangles": len(roof) // 3, "wall_triangles": len(walls) // 3,
                  "courtyards": mesh.holes, "texture_repeat_story_m": 3.0,
                  "texture_repeat_bay_m": 9.0}
