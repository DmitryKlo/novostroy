"""Verify the unlit color correction changes metadata but not ground vertices."""

from __future__ import annotations

import json
import struct
import unittest

from shapely.geometry import Polygon

from pipeline.build_ground import GroundMesh
from pipeline.build_ground_color_revision import (
    DISPLAY_SRGB, LINEAR_FACTOR, _recolor_glb, linear_to_srgb,
)


class GroundColorTests(unittest.TestCase):
    def test_linear_factor_round_trip_and_unchanged_glb_bin(self) -> None:
        self.assertEqual(tuple(linear_to_srgb(value) for value in LINEAR_FACTOR[:3]),
                         DISPLAY_SRGB)
        self.assertLess(LINEAR_FACTOR[0], 0.5)
        mesh = GroundMesh(37.6, 55.745)
        mesh.add(Polygon([(37.6, 55.745), (37.6001, 55.745),
                          (37.6001, 55.7451), (37.6, 55.7451)]))
        before = mesh.to_glb(geometry_type="exact_mapped_building_ground_surface")
        after = _recolor_glb(before)
        old_length = struct.unpack_from("<I", before, 12)[0]
        new_length = struct.unpack_from("<I", after, 12)[0]
        old_doc = json.loads(before[20:20 + old_length])
        new_doc = json.loads(after[20:20 + new_length])
        self.assertEqual(before[20 + old_length:], after[20 + new_length:])
        self.assertEqual(new_doc["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"],
                         LINEAR_FACTOR)
        self.assertEqual(new_doc["asset"]["extras"]["display_color_srgb"], "#AAAFB2")
        old_doc["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] = LINEAR_FACTOR
        old_doc["asset"]["extras"].update({"display_color_srgb": "#AAAFB2",
                                            "base_color_factor_color_space": "linear"})
        self.assertEqual(old_doc, new_doc)


if __name__ == "__main__":
    unittest.main()
