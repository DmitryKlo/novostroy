"""Build reproducible 3D Tiles 1.1 GLB content from local data.

Geometry is synthetic massing. City boxes approximate the footprint bounds;
unknown heights use an explicit 15 m display fallback. Ground is WGS84
ellipsoid height zero, not surveyed terrain. Facades are entirely procedural.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import tempfile
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.store import ASSET_ROOT, CITY_PATH, DEMO_PATH

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
CITY_CELL_DEGREES = 0.01
FALLBACK_HEIGHT_M = 15.0


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != data:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def save_json(path: Path, data: Any) -> None:
    save_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def ecef(lon: float, lat: float, height: float = 0) -> tuple[float, float, float]:
    lam, phi = math.radians(lon), math.radians(lat)
    radius = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
    return ((radius + height) * math.cos(phi) * math.cos(lam),
            (radius + height) * math.cos(phi) * math.sin(lam),
            (radius * (1 - WGS84_E2) + height) * math.sin(phi))


def enu_transform(lon: float, lat: float) -> list[float]:
    lam, phi = math.radians(lon), math.radians(lat)
    x, y, z = ecef(lon, lat)
    return [
        -math.sin(lam), math.cos(lam), 0, 0,
        -math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi), 0,
        math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi), 0,
        x, y, z, 1,
    ]


def enu_relative_transform(child_lon: float, child_lat: float, parent_lon: float, parent_lat: float) -> list[float]:
    """Child ENU frame expressed in a parent ENU frame for nested tiles."""
    parent = enu_transform(parent_lon, parent_lat)
    child = enu_transform(child_lon, child_lat)
    axes = [parent[0:3], parent[4:7], parent[8:11]]
    columns = [child[0:3], child[4:7], child[8:11], [child[i] - parent[i] for i in range(12, 15)]]
    result = []
    for vector in columns:
        result.extend(sum(axis[i] * vector[i] for i in range(3)) for axis in axes)
        result.append(0 if len(result) < 15 else 1)
    return result


def relative_xy(lon: float, lat: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    # Small-cell local tangent approximation in metres, adequate for display massing.
    return ((lon - origin_lon) * 111320 * math.cos(math.radians(origin_lat)),
            (lat - origin_lat) * 111132)


def png_texture() -> bytes:
    """Tiny hand-generated wall pattern, so no licensed texture is bundled."""
    size = 16
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            mortar = x % 8 == 0 or y % 8 == 0
            raw.extend((168, 160, 150, 255) if mortar else (212, 200, 180, 255))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


class Mesh:
    def __init__(self) -> None:
        self.groups: dict[int, tuple[list[float], list[float], list[float], list[int]]] = {}
        self.feature_ids: dict[int, list[int]] = {}

    def box(self, cx: float, cy: float, bottom: float, width: float, depth: float, height: float, material: int, feature_id: int = 0) -> None:
        if width <= 0 or depth <= 0 or height <= 0:
            return
        x0, x1 = cx - width / 2, cx + width / 2
        y0, y1 = cy - depth / 2, cy + depth / 2
        z0, z1 = bottom, bottom + height
        # Faces in ENU coordinates; GLB is emitted in the glTF Y-up frame.
        faces = [
            ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0)),
            ([(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)], (0, 1, 0)),
            ([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0)),
            ([(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)], (-1, 0, 0)),
            ([(x0, y1, z1), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1)], (0, 0, 1)),
            ([(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)], (0, 0, -1)),
        ]
        positions, normals, uvs, indices = self.groups.setdefault(material, ([], [], [], []))
        feature_ids = self.feature_ids.setdefault(material, [])
        for corners, normal in faces:
            start = len(positions) // 3
            for (east, north, up), uv in zip(corners, [(0, 0), (1, 0), (1, 1), (0, 1)]):
                positions.extend((east, up, -north))
                normals.extend((normal[0], normal[2], -normal[1]))
                uvs.extend(uv)
                feature_ids.append(feature_id)
            indices.extend((start, start + 1, start + 2, start, start + 2, start + 3))

    def triangle(self, corners: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]], material: int, feature_id: int) -> None:
        (a, b, c) = corners
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        normal = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2], ab[0] * ac[1] - ab[1] * ac[0])
        length = math.sqrt(sum(value * value for value in normal))
        if length < 1e-9:
            return
        normal = tuple(value / length for value in normal)
        positions, normals, uvs, indices = self.groups.setdefault(material, ([], [], [], []))
        ids = self.feature_ids.setdefault(material, [])
        start = len(positions) // 3
        for east, north, up in corners:
            positions.extend((east, up, -north))
            normals.extend((normal[0], normal[2], -normal[1]))
            uvs.extend((east / 10, north / 10))
            ids.append(feature_id)
        indices.extend((start, start + 1, start + 2))


MATERIALS = [
    {"name": "procedural_walls", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "baseColorTexture": {"index": 0}, "metallicFactor": 0, "roughnessFactor": 0.95}, "doubleSided": True},
    {"name": "roof", "pbrMetallicRoughness": {"baseColorFactor": [0.28, 0.32, 0.39, 1], "metallicFactor": 0, "roughnessFactor": 0.9}, "doubleSided": True},
    {"name": "windows", "pbrMetallicRoughness": {"baseColorFactor": [0.12, 0.31, 0.44, 1], "metallicFactor": 0.35, "roughnessFactor": 0.22}, "doubleSided": True},
    {"name": "balcony", "pbrMetallicRoughness": {"baseColorFactor": [0.55, 0.58, 0.61, 1], "metallicFactor": 0.1, "roughnessFactor": 0.8}, "doubleSided": True},
    {"name": "city_massing", "pbrMetallicRoughness": {"baseColorFactor": [0.36, 0.42, 0.46, 1], "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True},
    {"name": "cartographic_roads", "pbrMetallicRoughness": {"baseColorFactor": [0.78, 0.80, 0.79, 1], "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True},
    {"name": "cartographic_parks", "pbrMetallicRoughness": {"baseColorFactor": [0.62, 0.77, 0.64, 1], "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True},
    {"name": "cartographic_water", "pbrMetallicRoughness": {"baseColorFactor": [0.60, 0.76, 0.88, 1], "metallicFactor": 0, "roughnessFactor": 1}, "doubleSided": True},
]


def glb(mesh: Mesh, extras: dict[str, Any]) -> bytes:
    buffer = bytearray()
    views = []
    accessors = []
    primitives = []

    def append(blob: bytes, target: int | None = None) -> int:
        while len(buffer) % 4:
            buffer.append(0)
        view = {"buffer": 0, "byteOffset": len(buffer), "byteLength": len(blob)}
        if target is not None:
            view["target"] = target
        views.append(view)
        buffer.extend(blob)
        return len(views) - 1

    def accessor(values: list[float] | list[int], components: int, integer: bool = False, is_index: bool = False) -> int:
        if integer:
            view = append(struct.pack(f"<{len(values)}I", *values), 34963 if is_index else 34962)
            entry = {"bufferView": view, "componentType": 5125, "count": len(values), "type": "SCALAR"}
        else:
            view = append(struct.pack(f"<{len(values)}f", *values), 34962)
            entry = {"bufferView": view, "componentType": 5126, "count": len(values) // components,
                     "type": {2: "VEC2", 3: "VEC3"}[components]}
            if components == 3 and values:
                entry["min"] = [min(values[i::3]) for i in range(3)]
                entry["max"] = [max(values[i::3]) for i in range(3)]
        accessors.append(entry)
        return len(accessors) - 1

    for material, (positions, normals, uvs, indices) in sorted(mesh.groups.items()):
        feature_ids = mesh.feature_ids[material]
        primitives.append({
            "attributes": {"POSITION": accessor(positions, 3), "NORMAL": accessor(normals, 3), "TEXCOORD_0": accessor(uvs, 2), "_FEATURE_ID_0": accessor(feature_ids, 1, integer=True)},
            "indices": accessor(indices, 1, integer=True, is_index=True), "material": material, "mode": 4,
            "extensions": {"EXT_mesh_features": {"featureIds": [{"featureCount": max(feature_ids) + 1, "attribute": 0}]}}
        })
    texture_view = append(png_texture())
    document = {
        "asset": {"version": "2.0", "generator": "Novostroy procedural tile builder", "extras": extras},
        "extensionsUsed": ["EXT_mesh_features"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "extras": extras}],
        "meshes": [{"primitives": primitives}], "materials": MATERIALS,
        "textures": [{"source": 0, "sampler": 0}], "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}],
        "images": [{"bufferView": texture_view, "mimeType": "image/png", "name": "synthetic wall texture"}],
        "buffers": [{"byteLength": len(buffer)}], "bufferViews": views, "accessors": accessors,
    }
    json_part = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_part += b" " * ((-len(json_part)) % 4)
    buffer.extend(b"\x00" * ((-len(buffer)) % 4))
    return struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_part) + 8 + len(buffer)) + struct.pack("<I4s", len(json_part), b"JSON") + json_part + struct.pack("<I4s", len(buffer), b"BIN\x00") + buffer


def region(west: float, south: float, east: float, north: float, top: float) -> list[float]:
    return [math.radians(west), math.radians(south), math.radians(east), math.radians(north), 0, top]


def facade_mesh(lod: int, style: int) -> Mesh:
    mesh = Mesh()
    width, depth = 38 + style * 8, 28 + style * 5
    floors = 12 + style * 3
    height = floors * 3.2
    mesh.box(0, 0, 0, width, depth, height, 0)
    mesh.box(0, 0, height, width + 1.6, depth + 1.6, 1.2, 1)
    if lod >= 1:
        mesh.box(0, -depth / 2 - 2, 0, 12, 4, 3.5, 3)
        mesh.box(0, depth / 2 + 1, height - 3.2, width / 2, 2, 2, 1)
    if lod >= 2:
        for floor in range(1, floors):
            z = floor * 3.2 - 1.3
            for x in range(-int(width // 2) + 4, int(width // 2) - 2, 5):
                mesh.box(x, -depth / 2 - 0.07, z, 2.3, 0.14, 1.7, 2)
                mesh.box(x, depth / 2 + 0.07, z, 2.3, 0.14, 1.7, 2)
            for y in range(-int(depth // 2) + 4, int(depth // 2) - 2, 5):
                mesh.box(-width / 2 - 0.07, y, z, 0.14, 2.3, 1.7, 2)
                mesh.box(width / 2 + 0.07, y, z, 0.14, 2.3, 1.7, 2)
            if floor % 3 == 0:
                mesh.box(-width / 4, -depth / 2 - 1, z - 1.1, 7, 2, 0.25, 3)
    return mesh


def build_facades() -> None:
    catalogue = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    for style, item in enumerate(catalogue["complexes"][:3]):
        lon, lat = item["location"]
        folder = ASSET_ROOT / "facades" / item["complex_id"]
        height = (12 + style * 3) * 3.2 + 1.2
        root = None
        for lod in (2, 1, 0):
            filename = f"lod{lod}.glb"
            save_bytes(folder / filename, glb(facade_mesh(lod, style), {
                "complex_id": item["complex_id"], "model_quality": "synthetic_facade",
                "geometry_type": "procedural_synthetic_facade", "lod": lod,
                "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
            }))
            tile = {
                "boundingVolume": {"box": [0, 0, height / 2, 36, 0, 0, 0, 30, 0, 0, 0, height / 2 + 2]},
                "geometricError": [140, 35, 0][lod],
                "refine": "REPLACE", "content": {"uri": filename},
            }
            if root is not None:
                tile["children"] = [root]
            root = tile
        assert root is not None
        root["transform"] = enu_transform(lon, lat)
        save_json(folder / "tileset.json", {
            "asset": {"version": "1.1", "tilesetVersion": "synthetic-v1", "extras": {"model_quality": "synthetic_facade", "complex_id": item["complex_id"]}},
            "geometricError": 200, "root": root,
            "extras": {"attribution": "Generated synthetic geometry and texture", "ground_height_policy": "WGS84 ellipsoid 0 m"},
        })


def polygon_bounds(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    def points(value: Any):
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
                yield float(value[0]), float(value[1])
            else:
                for child in value:
                    yield from points(child)

    coords = list(points(geometry["coordinates"]))
    if not coords:
        raise ValueError("Empty geometry")
    return min(x for x, _ in coords), min(y for _, y in coords), max(x for x, _ in coords), max(y for _, y in coords)


def build_city(city_path: Path = CITY_PATH) -> int:
    if not city_path.is_file():
        return 0
    document = json.loads(city_path.read_text(encoding="utf-8"))
    features = document.get("features", [])
    is_demo = document.get("metadata", {}).get("data_mode") == "demo"
    source_attribution = ("Synthetic local demo" if is_demo else
                          "© OpenStreetMap contributors; ODbL 1.0; Overture Maps")
    source_name = "Synthetic local demo" if is_demo else "Overture Maps normalized local export"
    buckets: dict[tuple[int, int], list[tuple[dict[str, Any], tuple[float, float, float, float]]]] = defaultdict(list)
    for feature in features:
        try:
            bounds = polygon_bounds(feature["geometry"])
        except (KeyError, TypeError, ValueError):
            continue
        center_lon, center_lat = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        buckets[(math.floor(center_lon / CITY_CELL_DEGREES), math.floor(center_lat / CITY_CELL_DEGREES))].append((feature, bounds))
    children = []
    index = []
    all_bounds = []
    for (ix, iy), members in sorted(buckets.items()):
        tile_id = f"cell_{ix}_{iy}"
        west = min(b[0] for _, b in members)
        south = min(b[1] for _, b in members)
        east = max(b[2] for _, b in members)
        north = max(b[3] for _, b in members)
        all_bounds.append((west, south, east, north))
        origin_lon, origin_lat = (west + east) / 2, (south + north) / 2
        mesh = Mesh()
        highest = FALLBACK_HEIGHT_M
        for feature_index, (feature, bounds) in enumerate(members):
            props = feature.get("properties", {})
            measured = props.get("height_m")
            valid_height = isinstance(measured, (int, float)) and math.isfinite(measured) and measured > 0
            height = float(measured) if valid_height else FALLBACK_HEIGHT_M
            highest = max(highest, height)
            cx, cy = relative_xy((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, origin_lon, origin_lat)
            width = max(1, relative_xy(bounds[2], origin_lat, bounds[0], origin_lat)[0])
            depth = max(1, relative_xy(origin_lon, bounds[3], origin_lon, bounds[1])[1])
            mesh.box(cx, cy, 0, width, depth, height, 4, feature_id=feature_index)
            index.append({
                "source_id": props.get("source_id") or feature.get("id"),
                "tile": tile_id + ".glb", "height_m": height,
                "height_policy": "source_height_m" if valid_height else "display_fallback_15m",
                "geometry_policy": "axis_aligned_footprint_bounds",
                "source_release": props.get("source_release"),
                "source_attribution": source_attribution,
            })
        save_bytes(ASSET_ROOT / "city" / (tile_id + ".glb"), glb(mesh, {
            "geometry_type": "synthetic_demo_massing" if is_demo else
                             "synthetic_massing_from_public_footprints",
            "source": source_name, "source_count": len(members),
            "source_ids": [f.get("properties", {}).get("source_id") or f.get("id") for f, _ in members],
            "ground_height_policy": "WGS84 ellipsoid 0 m; approximate",
        }))
        children.append({
            "boundingVolume": {"region": region(west, south, east, north, highest + 5)},
            "geometricError": 0, "refine": "REPLACE",
            "transform": enu_transform(origin_lon, origin_lat),
            "content": {"uri": tile_id + ".glb"},
        })
    if not children:
        return 0
    save_json(ASSET_ROOT / "city" / "tileset.json", {
        "asset": {"version": "1.1", "tilesetVersion": "synthetic-demo-v1" if is_demo else "overture-massing-v1"},
        "geometricError": 5000,
        "root": {
            "boundingVolume": {"region": region(min(b[0] for b in all_bounds), min(b[1] for b in all_bounds), max(b[2] for b in all_bounds), max(b[3] for b in all_bounds), max(i["height_m"] for i in index) + 5)},
            "geometricError": 1000, "refine": "REPLACE", "children": children,
        },
        "extras": {"attribution": source_attribution, "geometry_policy": "axis_aligned_footprint_bounds", "unknown_height_policy": "display_fallback_15m", "ground_height_policy": "WGS84 ellipsoid 0 m; approximate"},
    })
    digest = hashlib.sha256()
    with city_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    save_json(ASSET_ROOT / "city" / "source_tile_index.json", {
        "version": "1", "normalized_source": str(city_path.resolve()),
        "normalized_sha256": digest.hexdigest(), "features": index,
    })
    return len(index)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-city", action="store_true")
    parser.add_argument("--city-input", type=Path)
    parser.add_argument("--force-sample-city", action="store_true", help="Replace a completed citywide selection with sample tiles")
    args = parser.parse_args()
    build_facades()
    manifest_path = ASSET_ROOT / "manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    citywide_selected = previous.get("city_tileset_url") == "/assets/citywide/tileset.json" and (ASSET_ROOT / "citywide/manifest.json").is_file()
    if citywide_selected and args.city_input is not None and not args.force_sample_city:
        parser.error("Citywide tiles are selected; pass --force-sample-city to replace their selection")
    if args.skip_city or (citywide_selected and not args.force_sample_city):
        city_count = int(previous.get("city_buildings", 0))
        city_tileset_url = previous.get("city_tileset_url", "/assets/city/tileset.json")
    else:
        city_count = build_city(args.city_input or CITY_PATH)
        city_tileset_url = "/assets/city/tileset.json"
    save_json(manifest_path, {"version": "1", "facade_models": 3, "city_buildings": city_count, "city_tileset_url": city_tileset_url})
    print(f"3 facade tilesets and {city_count} city buildings built in {ASSET_ROOT}")


if __name__ == "__main__":
    main()
