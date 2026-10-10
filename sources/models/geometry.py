"""Small selected-object GLBs from mapped polygons, with no source identifiers."""

from __future__ import annotations

import json
import math
import struct
import sys
from array import array
from typing import Any

from shapely import constrained_delaunay_triangles, orient_polygons
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from pipeline.build_tiles import ecef, enu_transform

BASE_HEIGHT_M = 0.65


def polygon_parts(geometry: Polygon | MultiPolygon) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    raise ValueError("Source geometry is not Polygon or MultiPolygon")


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _color(rgb: tuple[int, int, int]) -> list[float]:
    return [*(_linear(channel) for channel in rgb), 1.0]


class ModelMesh:
    """Exact footprint roof and optional polygon-ring walls in local ECEF/ENU."""

    def __init__(self, origin_lon: float, origin_lat: float, height_m: float | None) -> None:
        frame = enu_transform(origin_lon, origin_lat)
        self.origin = frame[12:15]
        self.axes = (frame[0:3], frame[4:7], frame[8:11])
        self.height_m = height_m
        self.positions = array("f")
        self.roof = array("I")
        self.walls = array("I")
        self.vertices: dict[tuple[float, float, float], int] = {}
        self.polygon_count = 0
        self.holes = 0
        self.triangles = 0

    def _vertex(self, lon: float, lat: float, height_m: float) -> int:
        key = (lon, lat, height_m)
        if key in self.vertices:
            return self.vertices[key]
        point = ecef(lon, lat, height_m)
        delta = [point[index] - self.origin[index] for index in range(3)]
        east, north, up = (sum(axis[index] * delta[index] for index in range(3)) for axis in self.axes)
        number = len(self.positions) // 3
        self.positions.extend((east, up, -north))
        self.vertices[key] = number
        return number

    def add(self, geometry: Polygon | MultiPolygon, *, top_height_m: float | None = None,
            base_height_m: float = BASE_HEIGHT_M) -> None:
        if not geometry.is_valid or geometry.is_empty or geometry.area <= 0:
            raise ValueError("Mapped footprint is empty or invalid")
        target_height = (BASE_HEIGHT_M + (self.height_m or 0)) if top_height_m is None else top_height_m
        draw_walls = self.height_m is not None or top_height_m is not None
        if draw_walls and not 0 <= base_height_m < target_height:
            raise ValueError("Part top must exceed its mapped base height")
        for raw in polygon_parts(geometry):
            polygon = orient_polygons(raw)
            covered = 0.0
            count = 0
            for triangle in constrained_delaunay_triangles(polygon).geoms:
                if not polygon.covers(triangle):
                    continue
                points = [(float(x), float(y)) for x, y, *_ in list(triangle.exterior.coords)[:3]]
                signed = ((points[1][0] - points[0][0]) * (points[2][1] - points[0][1])
                          - (points[1][1] - points[0][1]) * (points[2][0] - points[0][0]))
                if abs(signed) <= 1e-20:
                    continue
                if signed < 0:
                    points[1], points[2] = points[2], points[1]
                self.roof.extend(self._vertex(x, y, target_height) for x, y in points)
                covered += triangle.area
                count += 1
            if not count or abs(covered - polygon.area) > max(1e-13, polygon.area * 1e-4):
                raise ValueError("Polygon roof triangulation lost source area or courtyard")
            if draw_walls:
                for ring in (polygon.exterior, *polygon.interiors):
                    points = [(float(x), float(y)) for x, y, *_ in ring.coords]
                    for first, second in zip(points, points[1:]):
                        if first == second:
                            continue
                        a = self._vertex(*first, base_height_m)
                        b = self._vertex(*second, base_height_m)
                        c = self._vertex(*second, target_height)
                        d = self._vertex(*first, target_height)
                        self.walls.extend((a, b, c, a, c, d))
            self.polygon_count += 1
            self.holes += len(polygon.interiors)
            self.triangles += count
        if not self.roof:
            raise ValueError("No polygon roof triangles")


    def to_glb(self) -> bytes:
        if sys.byteorder != "little":
            raise ValueError("GLB writer needs little-endian host")
        positions = self.positions.tobytes()
        index_type = 5123 if len(self.positions) // 3 <= 65535 else 5125
        roof = (array("H", self.roof) if index_type == 5123 else self.roof).tobytes()
        walls = (array("H", self.walls) if index_type == 5123 else self.walls).tobytes()
        chunks = [positions, roof, walls]
        offsets: list[int] = []
        binary = bytearray()
        for chunk in chunks:
            binary.extend(b"\x00" * (-len(binary) % 4))
            offsets.append(len(binary))
            binary.extend(chunk)
        binary.extend(b"\x00" * (-len(binary) % 4))
        views = [{"buffer": 0, "byteOffset": offsets[index], "byteLength": len(chunk),
                  "target": 34962 if index == 0 else 34963}
                 for index, chunk in enumerate(chunks) if chunk]
        accessors: list[dict[str, Any]] = [
            {"bufferView": 0, "componentType": 5126, "count": len(self.positions) // 3,
             "type": "VEC3", "min": [min(self.positions[i::3]) for i in range(3)],
             "max": [max(self.positions[i::3]) for i in range(3)]},
            {"bufferView": 1, "componentType": index_type, "count": len(self.roof), "type": "SCALAR"},
        ]
        primitives = [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0, "mode": 4}]
        materials = [{"name": "mapped_construction_roof", "doubleSided": True,
                      "extensions": {"KHR_materials_unlit": {}},
                      "pbrMetallicRoughness": {"baseColorFactor": _color((190, 165, 123)),
                                               "metallicFactor": 0, "roughnessFactor": 1}}]
        if walls:
            accessors.append({"bufferView": 2, "componentType": index_type,
                              "count": len(self.walls), "type": "SCALAR"})
            primitives.append({"attributes": {"POSITION": 0}, "indices": 2,
                               "material": 1, "mode": 4})
            materials.append({"name": "mapped_construction_walls", "doubleSided": True,
                              "extensions": {"KHR_materials_unlit": {}},
                              "pbrMetallicRoughness": {"baseColorFactor": _color((158, 145, 127)),
                                                       "metallicFactor": 0, "roughnessFactor": 1}})
        document = {
            "asset": {"version": "2.0", "generator": "Novostroy mapped construction candidate builder",
                      "extras": {"geometry_type": "mapped_construction_footprint",
                                 "height_policy": "flat_outline" if self.height_m is None else "schematic_source_tag_or_floors_estimate",
                                 "surface_base_ellipsoid_m": BASE_HEIGHT_M,
                                 "source_ids_redacted": True}},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": primitives}], "materials": materials,
            "buffers": [{"byteLength": len(binary)}], "bufferViews": views,
            "accessors": accessors,
        }
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        encoded += b" " * (-len(encoded) % 4)
        return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(binary))
                + struct.pack("<I4s", len(encoded), b"JSON") + encoded
                + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def validated_part_profile(parent_key: tuple[str, int, int],
                           parent: Polygon | MultiPolygon,
                           candidates: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Admit only explicit same-version parent/part facts with near-total coverage.

    Current construction candidates have no such profile; this gate is for a
    future, separately reviewed part source, never donor/nearest geometry.
    """
    if not 2 <= len(candidates) <= 12 or not parent.is_valid or parent.area <= 0:
        return None
    profiles: list[dict[str, Any]] = []
    for row in candidates:
        if (tuple(row.get("parent_osm_key", ())) != parent_key
                or row.get("validation_profile") != "clean_multilevel_parts_v1"
                or row.get("rights_status") != "cleared"):
            return None
        geometry = row.get("geometry")
        top = row.get("height_top_m")
        base = row.get("vertical_base_m")
        if (not isinstance(geometry, (Polygon, MultiPolygon)) or not geometry.is_valid or geometry.area <= 0
                or not isinstance(top, (int, float)) or not isinstance(base, (int, float))
                or not math.isfinite(top) or not math.isfinite(base)
                or not 0 <= base < top <= 400):
            return None
        if geometry.difference(parent).area > parent.area * 0.0002:
            return None
        profiles.append({"geometry": geometry, "height_top_m": float(top),
                         "vertical_base_m": float(base)})
    if unary_union([row["geometry"] for row in profiles]).intersection(parent).area / parent.area < 0.99:
        return None
    if max(row["height_top_m"] for row in profiles) - min(row["height_top_m"] for row in profiles) < 2:
        return None
    for i, first in enumerate(profiles):
        for second in profiles[i + 1:]:
            if (min(first["height_top_m"], second["height_top_m"])
                    > max(first["vertical_base_m"], second["vertical_base_m"]) + 0.1
                    and first["geometry"].intersection(second["geometry"]).area > parent.area * 0.0002):
                return None
    return profiles
