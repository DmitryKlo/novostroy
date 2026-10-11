"""Grouped facade tiles retain source geometry and per-building batch identity."""

from __future__ import annotations

import json
import struct
import unittest

from shapely.geometry import Polygon

from sources.property.developer_overlay_facades import styled_batched_tile


def _square(west: float) -> Polygon:
    return Polygon([(west, 55.0), (west + 0.0001, 55.0),
                    (west + 0.0001, 55.0001), (west, 55.0001)])


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")


class GroupedFacadesTest(unittest.TestCase):
    def test_one_project_texture_is_shared_by_two_building_features(self) -> None:
        items = [
            {"registry_id": "registry-first", "publisher_project_id": "project-a",
             "geometry": _square(37.0), "volume_height_m": 30.0},
            {"registry_id": "registry-second", "publisher_project_id": "project-a",
             "geometry": _square(37.0002), "volume_height_m": 36.0},
            {"registry_id": "registry-third", "publisher_project_id": "project-b",
             "geometry": _square(37.0004), "volume_height_m": 24.0},
        ]
        blob = styled_batched_tile(items, 37.00025, 55.00005,
                                   {"project-a": {"style_id": "style_a", "png": PNG}})
        magic, version, length, feature_length, _, batch_length, _ = struct.unpack_from(
            "<4sIIIIII", blob, 0)
        self.assertEqual((magic, version, length), (b"b3dm", 1, len(blob)))
        feature = json.loads(blob[28:28 + feature_length])
        self.assertEqual(feature["BATCH_LENGTH"], 3)
        batch_at = 28 + feature_length
        batch = json.loads(blob[batch_at:batch_at + batch_length])
        self.assertEqual(batch["registry_id"], ["registry-first", "registry-second",
                                                 "registry-third"])
        glb_at = batch_at + batch_length
        self.assertEqual(blob[glb_at:glb_at + 4], b"glTF")
        json_length = struct.unpack_from("<I", blob, glb_at + 12)[0]
        document = json.loads(blob[glb_at + 20:glb_at + 20 + json_length])
        self.assertEqual(len(document["images"]), 1)
        self.assertEqual(len(document["textures"]), 1)
        self.assertEqual(len(document["meshes"][0]["primitives"]), 3)
        wall = document["meshes"][0]["primitives"][-1]
        self.assertIn("_BATCHID", wall["attributes"])
        self.assertIn("TEXCOORD_0", wall["attributes"])
        accessor = document["accessors"][wall["attributes"]["_BATCHID"]]
        self.assertEqual((accessor["min"], accessor["max"]), ([0], [1]))

    def test_distinct_project_materials_reuse_identical_png_bytes(self) -> None:
        items = [
            {"registry_id": "registry-first", "publisher_project_id": "project-a",
             "geometry": _square(37.0), "volume_height_m": 30.0},
            {"registry_id": "registry-second", "publisher_project_id": "project-b",
             "geometry": _square(37.0002), "volume_height_m": 36.0},
        ]
        blob = styled_batched_tile(items, 37.00015, 55.00005, {
            "project-a": {"style_id": "style_a", "png": PNG},
            "project-b": {"style_id": "style_b", "png": PNG},
        })
        _, _, _, f_len, _, b_len, _ = struct.unpack_from("<4sIIIIII", blob, 0)
        batch = json.loads(blob[28 + f_len:28 + f_len + b_len])
        self.assertEqual(batch["facade_style_id"], ["style_a", "style_b"])
        glb_at = 28 + f_len + b_len
        j_len = struct.unpack_from("<I", blob, glb_at + 12)[0]
        document = json.loads(blob[glb_at + 20:glb_at + 20 + j_len])
        self.assertEqual(len(document["images"]), 1)
        self.assertEqual(len(document["textures"]), 1)
        self.assertEqual(len(document["meshes"][0]["primitives"]), 3)
        self.assertEqual(document["materials"][2]["name"], "project_style_style_a")
        self.assertEqual(document["materials"][3]["name"], "project_style_style_b")
        self.assertEqual(document["materials"][2]["pbrMetallicRoughness"]["baseColorTexture"],
                         document["materials"][3]["pbrMetallicRoughness"]["baseColorTexture"])


if __name__ == "__main__":
    unittest.main()
