"""Check batched tile identity and geometry isolation."""

from __future__ import annotations

import json
import struct
import unittest

from shapely.geometry import Polygon

from sources.models.geometry import ModelMesh
from sources.property.developer_overlay_tiles import (
    _glb_with_batch_ids, _tile_features, batched_tile,
)


def _decode_b3dm(blob: bytes) -> tuple[dict, dict, dict, bytes]:
    magic, version, total, ft_json, ft_bin, bt_json, bt_bin = struct.unpack_from(
        "<4sIIIIII", blob)
    assert (magic, version, total, ft_bin, bt_bin) == (b"b3dm", 1, len(blob), 0, 0)
    ft = json.loads(blob[28:28 + ft_json])
    bt_at = 28 + ft_json
    bt = json.loads(blob[bt_at:bt_at + bt_json])
    glb = blob[bt_at + bt_json:]
    assert glb[:4] == b"glTF" and len(glb) == struct.unpack_from("<I", glb, 8)[0]
    json_len = struct.unpack_from("<I", glb, 12)[0]
    document = json.loads(glb[20:20 + json_len])
    binary = glb[20 + json_len + 8:]
    return ft, bt, document, binary


class BatchedDeveloperTilesTest(unittest.TestCase):
    def test_two_buildings_have_distinct_styleable_batch_ids(self) -> None:
        left = Polygon([(37.0, 55.0), (37.0001, 55.0),
                        (37.0001, 55.0001), (37.0, 55.0001)])
        right = Polygon([(37.0001, 55.0), (37.0002, 55.0),
                         (37.0002, 55.0001), (37.0001, 55.0001)])
        items = [{"registry_id": "registry-" + "a" * 24,
                  "geometry": left, "volume_height_m": 30.0},
                 {"registry_id": "registry-" + "b" * 24,
                  "geometry": right, "volume_height_m": None}]
        blob = _tile_features(items, detail=True, origin_lon=37.0001, origin_lat=55.00005)
        ft, bt, gltf, binary = _decode_b3dm(blob)
        self.assertEqual(ft["BATCH_LENGTH"], 2)
        self.assertEqual(bt["registry_id"], [item["registry_id"] for item in items])
        self.assertEqual(bt["render_mode"], ["source_footprint_schematic_volume",
                                             "source_footprint_flat"])
        accessors = gltf["accessors"]
        batch_accessor_id = gltf["meshes"][0]["primitives"][0]["attributes"]["_BATCHID"]
        for primitive in gltf["meshes"][0]["primitives"]:
            self.assertEqual(primitive["attributes"]["_BATCHID"], batch_accessor_id)
        accessor = accessors[batch_accessor_id]
        view = gltf["bufferViews"][accessor["bufferView"]]
        offset = view["byteOffset"]
        ids = struct.unpack_from("<" + "H" * accessor["count"], binary, offset)
        self.assertEqual(set(ids), {0, 1})
        self.assertEqual(list(ids[:ids.index(1)]), [0] * ids.index(1))
        self.assertEqual(list(ids[ids.index(1):]), [1] * (len(ids) - ids.index(1)))

    def test_batch_ids_must_cover_every_vertex(self) -> None:
        mesh = ModelMesh(37.0, 55.0, None)
        mesh.add(Polygon([(37, 55), (37.0001, 55), (37.0001, 55.0001)]))
        with self.assertRaisesRegex(ValueError, "cover exactly"):
            _glb_with_batch_ids(mesh.to_glb(), [0])
        with self.assertRaisesRegex(ValueError, "match feature table"):
            batched_tile(mesh, ["registry-" + "a" * 24, "registry-" + "b" * 24],
                         ["source_footprint_flat"] * 2, [None, None], [0] *
                         (len(mesh.positions) // 3))


if __name__ == "__main__":
    unittest.main()
