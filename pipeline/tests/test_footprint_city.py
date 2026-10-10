"""Checks for the sparse regional massing that replaces solid cell boxes."""

from __future__ import annotations

import json
import struct
import unittest

from shapely.geometry import Polygon

from pipeline.build_footprint_city import FootprintVolumeMesh, OVERVIEW_POLICY


class IndividualRegionVolumes(unittest.TestCase):
    def test_courtyard_stays_empty_and_no_bottom_plane_is_emitted(self) -> None:
        shell = [(37.6000, 55.7450), (37.6010, 55.7450),
                 (37.6010, 55.7460), (37.6000, 55.7460)]
        hole = [(37.6003, 55.7453), (37.6007, 55.7453),
                (37.6007, 55.7457), (37.6003, 55.7457)]
        courtyard = Polygon(hole)
        mesh = FootprintVolumeMesh(37.6005, 55.7455)
        mesh.add(Polygon(shell, [hole]), 24.0)
        self.assertEqual((mesh.features, mesh.holes, mesh.max_height_m), (1, 1, 24.0))
        positions = {number: point for point, number in mesh.vertex_ids.items()}
        for offset in range(0, len(mesh.roof_indices), 3):
            coords = [positions[number][:2] for number in mesh.roof_indices[offset:offset + 3]]
            self.assertLess(Polygon(coords).intersection(courtyard).area, 1e-18)
            self.assertTrue(all(positions[number][2] == 24.0
                                for number in mesh.roof_indices[offset:offset + 3]))
        for offset in range(0, len(mesh.wall_indices), 3):
            heights = {positions[number][2] for number in mesh.wall_indices[offset:offset + 3]}
            self.assertEqual(heights, {0.0, 24.0})
        self.assertGreater(mesh.wall_triangles, 0)
        self.assertLess(mesh.area_m2, 10000)
        blob = mesh.to_glb()
        length = struct.unpack_from("<I", blob, 12)[0]
        document = json.loads(blob[20:20 + length])
        self.assertEqual(document["asset"]["extras"]["geometry_type"],
                         "simplified_individual_footprint_volumes")
        self.assertEqual(len(document["meshes"][0]["primitives"]), 2)
        self.assertEqual(document["extensionsUsed"], ["KHR_materials_unlit"])
        self.assertNotIn("_FEATURE_ID_0", blob.decode("latin1"))
        self.assertNotIn("cell_ids", blob.decode("latin1"))
        self.assertIn("individual", OVERVIEW_POLICY)


if __name__ == "__main__":
    unittest.main()
