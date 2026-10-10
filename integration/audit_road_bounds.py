"""Check rendered road ribbon vertices against the published v2 tile bounds.

Run offline after road-v2 staging, then add ``--require-active`` after the
pointer switch. Checks GLB geometry and metadata, not browser seam rendering.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    without_private_gltf_keys,
)

ASSETS = ROOT / "assets"
PUBLIC = ASSETS / "public-roads-v2"
PRIVATE = ASSETS / "roads-v2"
WGS84_A = 6_378_137.0
WGS84_E2 = 0.00669437999014


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def matrix(value: list[float] | None) -> np.ndarray:
    return np.asarray(value, dtype=np.float64).reshape(4, 4).T if value else np.eye(4)


def cartographic(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x, y, z = points.T
    longitude = np.arctan2(y, x)
    radius = np.hypot(x, y)
    latitude = np.arctan2(z, radius * (1 - WGS84_E2))
    for _ in range(6):
        normal = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(latitude) ** 2)
        latitude = np.arctan2(z + WGS84_E2 * normal * np.sin(latitude), radius)
    normal = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(latitude) ** 2)
    height = radius / np.cos(latitude) - normal
    return longitude, latitude, height


def rendered_bounds(path: Path, transform: np.ndarray) -> tuple[float, ...]:
    document, binary = glb_chunks(path)
    assert_no_private_gltf_keys(document, path.name)
    accessor = document["accessors"][document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    view = document["bufferViews"][accessor["bufferView"]]
    assert accessor["componentType"] == 5126 and accessor["type"] == "VEC3"
    assert "byteStride" not in view
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    local = np.frombuffer(binary, dtype="<f4", count=accessor["count"] * 3,
                          offset=offset).reshape(-1, 3).astype(np.float64)
    assert np.isfinite(local).all() and len(local)
    # Cesium's glTF Y-up correction maps (x,y,z) to ENU (x,-z,y).
    corrected = np.column_stack((local[:, 0], -local[:, 2], local[:, 1],
                                 np.ones(len(local))))
    world = (corrected @ transform.T)[:, :3]
    lon, lat, height = cartographic(world)
    return (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()),
            float(height.min()), float(height.max()))


def overflow_m(actual: tuple[float, ...], declared: list[float]) -> float:
    west, south, east, north, low, high = actual
    scale_x = WGS84_A * math.cos((south + north) / 2)
    scale_y = WGS84_A
    return max(0.0, (declared[0] - west) * scale_x,
               (east - declared[2]) * scale_x,
               (declared[1] - south) * scale_y,
               (north - declared[3]) * scale_y,
               declared[4] - low, high - declared[5])


def audit(*, require_active: bool = False) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    active = pointer["roads_tileset_url"] == "/assets/public-roads-v2/tileset.json"
    assert active or not require_active, "Road v2 pointer is not active"
    public_manifest = read_json(PUBLIC / "manifest.json")
    private_manifest = read_json(PRIVATE / "manifest.json")
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["source_rows"] == private_manifest["source_rows"] == 463_288
    public_tree = read_json(PUBLIC / "tileset.json")
    private_tree = read_json(PRIVATE / "tileset.json")
    assert public_tree == without_private_gltf_keys(private_tree)
    assert_no_private_gltf_keys(public_tree, "road v2 root")
    references: set[str] = set()
    checked = 0
    worst_overflow = 0.0
    pending: list[tuple[dict[str, Any], np.ndarray]] = [(public_tree["root"], np.eye(4))]
    while pending:
        tile, parent = pending.pop()
        effective = parent @ matrix(tile.get("transform"))
        for child in tile.get("children", []):
            pending.append((child, effective))
        content = tile.get("content")
        if content is None:
            continue
        uri = content["uri"]
        assert isinstance(uri, str) and uri.endswith(".glb") and "/" not in uri and ".." not in uri
        assert uri not in references
        references.add(uri)
        path = PUBLIC / uri
        assert path.is_file() and (PRIVATE / uri).is_file()
        actual = rendered_bounds(path, effective)
        declared = tile["boundingVolume"]["region"]
        overflow = overflow_m(actual, declared)
        worst_overflow = max(worst_overflow, overflow)
        assert overflow <= 0.25, (uri, round(overflow, 3), actual, declared)
        checked += 1
    assert len(public_tree["root"]["children"]) == public_manifest["coarse_region_tiles"]
    assert sum(uri.startswith("cell_") for uri in references) == public_manifest["detailed_tiles"]
    assert checked == len(references)
    assert {path.name for path in PUBLIC.glob("*.glb")} == references
    print(f"PASS {'active' if active else 'staged'} roads v2: {checked} GLB ribbons "
          f"inside declared bounds; worst overflow {worst_overflow:.3f} m; public refs/privacy verified")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
