"""A lighting fix must leave published footprint geometry untouched."""

from __future__ import annotations

import math
import unittest

from shapely.geometry import Polygon

from pipeline.build_city_material_revision import _array, _chunks, revise_overview
from pipeline.build_footprint_city import FootprintVolumeMesh, ROOF_COLOR


class CityMaterialRevisionTests(unittest.TestCase):
    def test_revised_overview_has_detail_lighting_and_identical_geometry(self) -> None:
        mesh = FootprintVolumeMesh(37.615, 55.755)
        mesh.add(Polygon([(37.6150, 55.7550), (37.6151, 55.7550),
                          (37.6151, 55.7551), (37.6150, 55.7551)]), 16.0)
        original, original_binary = _chunks(mesh.to_glb())
        revised_blob, counts = revise_overview(mesh.to_glb())
        revised, revised_binary = _chunks(revised_blob)
        old_positions = _array(original, original_binary, 0, "VEC3", 5126)
        new_positions = _array(revised, revised_binary, 0, "VEC3", 5126)
        self.assertEqual(new_positions[:len(old_positions)], old_positions)
        for accessor in (1, 2):
            before = _array(original, original_binary, accessor, "SCALAR", 5125)
            after = _array(revised, revised_binary, accessor, "SCALAR", 5125)
            self.assertEqual(len(before), len(after))
            for old_index, new_index in zip(before, after, strict=True):
                self.assertEqual(old_positions[3 * old_index:3 * old_index + 3],
                                 new_positions[3 * new_index:3 * new_index + 3])
        self.assertNotIn("extensionsUsed", revised)
        for primitive in revised["meshes"][0]["primitives"]:
            self.assertIn("NORMAL", primitive["attributes"])
            normal = revised["accessors"][primitive["attributes"]["NORMAL"]]
            self.assertEqual(normal["componentType"], 5120)
            self.assertTrue(normal["normalized"])
            self.assertEqual(normal["count"], counts["vertices"])
        for material in revised["materials"]:
            self.assertNotIn("extensions", material)
            self.assertEqual(material["pbrMetallicRoughness"]["baseColorFactor"], ROOF_COLOR)
        self.assertEqual(counts["degenerate_wall_faces"], 0)
        self.assertGreater(counts["wall_triangles"], 0)
        self.assertTrue(math.isfinite(counts["normal_bytes"]))


if __name__ == "__main__":
    unittest.main()
