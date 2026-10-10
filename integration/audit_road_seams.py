"""Measure matching road centerline endpoints across one proven tile seam.

Uses private source-ID mapping only inside this offline auditor. Public GLB
geometry is the measured payload; no source IDs are published by this script.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_public_assets import glb_chunks  # noqa: E402
from integration.audit_road_bounds import matrix  # noqa: E402

ASSETS = ROOT / "assets"
LEFT = "cell_3761_5575.glb"
RIGHT = "cell_3762_5575.glb"
SEAM_LONGITUDE = math.radians(37.62)


def tile_transform(tree: dict, uri: str) -> np.ndarray:
    root = tree["root"]
    for region in root["children"]:
        for cell in region["children"]:
            if cell.get("content", {}).get("uri") == uri:
                return matrix(root.get("transform")) @ matrix(region.get("transform")) \
                    @ matrix(cell.get("transform"))
    raise AssertionError(f"Missing road tile {uri}")


def accessor(document: dict, binary: bytes, index: int) -> np.ndarray:
    item = document["accessors"][index]
    view = document["bufferViews"][item["bufferView"]]
    assert "byteStride" not in view
    dtype = {5126: "<f4", 5125: "<u4"}[item["componentType"]]
    width = {"VEC3": 3, "SCALAR": 1}[item["type"]]
    offset = view.get("byteOffset", 0) + item.get("byteOffset", 0)
    values = np.frombuffer(binary, dtype=dtype, count=item["count"] * width, offset=offset)
    return values.reshape(-1, width) if width != 1 else values


def endpoints(public: Path, private: Path, transform: np.ndarray) -> dict[str, np.ndarray]:
    document, binary = glb_chunks(public)
    private_document, private_binary = glb_chunks(private)
    assert binary == private_binary
    sources = private_document["asset"]["extras"]["source_ids"]
    primitive = document["meshes"][0]["primitives"][0]
    positions = accessor(document, binary, primitive["attributes"]["POSITION"])
    feature_ids = accessor(document, binary, primitive["attributes"]["_FEATURE_ID_0"])
    assert len(positions) == len(feature_ids) and len(positions) % 6 == 0
    quads = positions.reshape(-1, 6, 3).astype(np.float64)
    ids = feature_ids.reshape(-1, 6)
    assert np.all(ids == ids[:, :1])
    start = (quads[:, 0] + quads[:, 1]) / 2
    end = (quads[:, 2] + quads[:, 5]) / 2
    centers = np.concatenate((start, end))
    source_numbers = np.tile(ids[:, 0], 2)
    corrected = np.column_stack((centers[:, 0], -centers[:, 2], centers[:, 1],
                                 np.ones(len(centers))))
    world = (corrected @ transform.T)[:, :3]
    longitude = np.arctan2(world[:, 1], world[:, 0])
    near = np.abs(longitude - SEAM_LONGITUDE) < math.radians(0.0005)
    grouped: dict[str, list[np.ndarray]] = {}
    for number, point in zip(source_numbers[near], world[near], strict=True):
        grouped.setdefault(sources[int(number)], []).append(point)
    return {source: np.array(points) for source, points in grouped.items()}


def audit(*, version: int = 2, require_active: bool = False) -> None:
    pointer = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    if require_active:
        assert pointer["roads_tileset_url"] == f"/assets/public-roads-v{version}/tileset.json"
    public = ASSETS / f"public-roads-v{version}"
    private = ASSETS / f"roads-v{version}"
    tree = json.loads((public / "tileset.json").read_text(encoding="utf-8"))
    first = endpoints(public / LEFT, private / LEFT, tile_transform(tree, LEFT))
    second = endpoints(public / RIGHT, private / RIGHT, tile_transform(tree, RIGHT))
    common = sorted(set(first) & set(second))
    gaps = []
    for source in common:
        distances = np.linalg.norm(first[source][:, None, :] - second[source][None, :, :], axis=2)
        gaps.append(float(distances.min()))
    assert len(gaps) >= 12, len(gaps)
    if version >= 2:
        assert max(gaps) < 0.1, max(gaps)
    print(f"PASS road v{version} seam: {len(gaps)} shared ways; "
          f"median {statistics.median(gaps):.3f} m, max {max(gaps):.3f} m")


if __name__ == "__main__":
    options = sys.argv[1:]
    version = 1 if "--baseline-v1" in options else 2
    audit(version=version, require_active="--require-active" in options)
