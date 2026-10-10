"""Focused offline audit of city v10 shading against v9 triangle geometry.

This checks material parity, normals, immutable geometry and public links. It
cannot prove the rendered color match without a browser/GPU frame.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration.audit_public_assets import (  # noqa: E402
    assert_no_private_gltf_keys,
    glb_chunks,
    referenced_assets,
    sha256,
    without_private_gltf_keys,
)

ASSETS = ROOT / "assets"
PUBLIC = ASSETS / "public-citywide-v10"
PRIVATE = ASSETS / "citywide-v10"
PRIOR = ASSETS / "public-citywide-v9"


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def same_bytes(first: Path, second: Path) -> bool:
    return first.samefile(second) or sha256(first) == sha256(second)


def accessor_data(document: dict[str, Any], binary: bytes, index: int) -> bytes:
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    assert "byteStride" not in view
    width = {"SCALAR": 1, "VEC3": 3}[accessor["type"]]
    size = {5120: 1, 5122: 2, 5125: 4, 5126: 4}[accessor["componentType"]]
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    end = start + accessor["count"] * width * size
    assert end <= len(binary)
    return binary[start:end]


def normal_array(document: dict[str, Any], binary: bytes, index: int) -> np.ndarray:
    accessor = document["accessors"][index]
    assert accessor["type"] == "VEC3"
    raw = accessor_data(document, binary, index)
    component = accessor["componentType"]
    if component == 5120:
        assert accessor.get("normalized") is True
        return np.maximum(np.frombuffer(raw, dtype="i1").reshape(-1, 3) / 127.0, -1.0)
    if component == 5122:
        assert accessor.get("normalized") is True
        return np.maximum(np.frombuffer(raw, dtype="<i2").reshape(-1, 3) / 32767.0, -1.0)
    assert component == 5126 and not accessor.get("normalized", False)
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 3).astype(np.float64)


def audit_coarse(name: str, detail_factor: list[float]) -> tuple[int, int]:
    current, binary = glb_chunks(PUBLIC / name)
    previous, previous_binary = glb_chunks(PRIOR / name)
    assert_no_private_gltf_keys(current, name)
    assert current["asset"]["extras"]["source_count"] == previous["asset"]["extras"]["source_count"]
    assert "KHR_materials_unlit" not in current.get("extensionsUsed", [])
    assert len(current["materials"]) == len(previous["materials"]) == 2
    for material in current["materials"]:
        assert "KHR_materials_unlit" not in material.get("extensions", {})
        pbr = material["pbrMetallicRoughness"]
        assert pbr["baseColorFactor"] == detail_factor
        assert pbr["metallicFactor"] == 0 and pbr["roughnessFactor"] == 1
        assert material["doubleSided"] is True
    current_primitives = current["meshes"][0]["primitives"]
    previous_primitives = previous["meshes"][0]["primitives"]
    assert len(current_primitives) == len(previous_primitives) == 2
    total_faces = 0
    degenerate = 0
    for number, (new, old) in enumerate(zip(current_primitives, previous_primitives, strict=True)):
        assert new["mode"] == old["mode"] == 4
        assert new["material"] == old["material"] == number
        assert set(new["attributes"]) == {"POSITION", "NORMAL"}
        position_index = new["attributes"]["POSITION"]
        normal_index = new["attributes"]["NORMAL"]
        positions = np.frombuffer(accessor_data(current, binary, position_index),
                                  dtype="<f4").reshape(-1, 3).astype(np.float64)
        old_positions = np.frombuffer(accessor_data(previous, previous_binary,
                                                    old["attributes"]["POSITION"]),
                                      dtype="<f4").reshape(-1, 3).astype(np.float64)
        normals = normal_array(current, binary, normal_index)
        assert len(normals) == len(positions)
        indices = np.frombuffer(accessor_data(current, binary, new["indices"]),
                                dtype="<u4").reshape(-1, 3)
        old_indices = np.frombuffer(accessor_data(previous, previous_binary, old["indices"]),
                                    dtype="<u4").reshape(-1, 3)
        assert len(indices) and int(indices.max()) < len(positions)
        assert indices.shape == old_indices.shape
        # v10 may split a shared vertex so adjacent wall faces can each own a
        # proper lit normal. Every indexed triangle must still have exactly the
        # same float32 coordinates, order, and count as v9.
        assert np.array_equal(positions[indices], old_positions[old_indices]), name
        used = np.unique(indices)
        used_normals = normals[used]
        assert np.isfinite(used_normals).all()
        lengths = np.linalg.norm(used_normals, axis=1)
        assert float(lengths.min()) > 0.97 and float(lengths.max()) < 1.03
        vertices = positions[indices]
        faces = np.cross(vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0])
        magnitude = np.linalg.norm(faces, axis=1)
        degenerate += int((magnitude < 1e-7).sum())
        valid = magnitude >= 1e-7
        assert valid.any()
        face_directions = faces[valid] / magnitude[valid, None]
        vertex_directions = normals[indices[valid]]
        agreement = np.einsum("ij,ikj->ik", face_directions, vertex_directions)
        # Averaging normals at sharp corners may reduce dot products, but should
        # not systematically reverse faces after changing to lit shading.
        assert float((agreement > 0).mean()) > 0.99, (name, number, float((agreement > 0).mean()))
        if number == 0:
            assert float((face_directions[:, 1] > 0).mean()) > 0.99
        total_faces += len(indices)
    return total_faces, degenerate


def audit(*, require_active: bool = False) -> None:
    pointer = read_json(ASSETS / "manifest.json")
    active = pointer["city_tileset_url"] == "/assets/public-citywide-v10/tileset.json"
    assert active or not require_active, "City v10 pointer is not active"
    if active:
        assert pointer["ground_tileset_url"] == "/assets/public-ground-v3/tileset.json"
        assert "public-citywide-v9" in pointer["previous_public_roots"]
    public_manifest = read_json(PUBLIC / "manifest.json")
    private_manifest = read_json(PRIVATE / "manifest.json")
    assert public_manifest["public_redacted_source_ids"] is True
    assert public_manifest["private_manifest_sha256"] == sha256(PRIVATE / "manifest.json")
    assert private_manifest["source_rows"] == 564_031
    tree = read_json(PUBLIC / "tileset.json")
    assert tree == without_private_gltf_keys(read_json(PRIVATE / "tileset.json"))
    assert_no_private_gltf_keys(tree, "city v10 root")
    refs, externals = referenced_assets(tree, PUBLIC)
    assert len(refs) == 3608 and len(externals) == 212
    assert all((PUBLIC / path).is_file() for path in refs | externals)
    assert {path.name for path in PUBLIC.glob("*.glb")} == {path.name for path in refs}
    details = [path.name for path in refs if path.name.startswith("cell_")]
    coarse = [path.name for path in refs if path.name.startswith("massing_region_")]
    assert len(details) == 3396 and len(coarse) == 212
    for name in details:
        assert same_bytes(PUBLIC / name, PRIOR / name)
    prior_tree = read_json(PRIOR / "tileset.json")
    assert len(tree["root"]["children"]) == len(prior_tree["root"]["children"]) == 212
    for current, old in zip(tree["root"]["children"], prior_tree["root"]["children"], strict=True):
        assert current == old
    for path in externals:
        assert read_json(PUBLIC / path) == read_json(PRIOR / path)
    detail_document, _ = glb_chunks(PUBLIC / details[0])
    detail_factor = detail_document["materials"][4]["pbrMetallicRoughness"]["baseColorFactor"]
    total_faces = degenerate = 0
    for name in coarse:
        faces, bad = audit_coarse(name, detail_factor)
        total_faces += faces
        degenerate += bad
    assert degenerate == 0, degenerate
    print(f"PASS {'active' if active else 'staged'} city v10 lighting: 212 lit coarse "
          f"GLBs / {total_faces} exact v9 triangle positions, normals finite and "
          f"aligned; 3396 details unchanged; public refs/privacy verified")


if __name__ == "__main__":
    audit(require_active="--require-active" in sys.argv[1:])
