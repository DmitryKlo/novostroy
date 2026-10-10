"""Small geometry checks for permanent mapped building ground."""

from __future__ import annotations

import json
import struct
import unittest

from shapely.geometry import MultiPolygon, Polygon

from pipeline.build_ground import GROUND_OFFSET_M, GroundMesh
from pipeline.build_progressive_city import RasterSilhouette
from pipeline.build_tiles import ecef, enu_transform, region


class GroundSurfaceTests(unittest.TestCase):
    def test_exact_surface_preserves_courtyard_and_wgs84_height(self) -> None:
        shell = [(37.6000, 55.7450), (37.6004, 55.7450),
                 (37.6004, 55.7454), (37.6000, 55.7454)]
        hole = [(37.6001, 55.7451), (37.6003, 55.7451),
                (37.6003, 55.7453), (37.6001, 55.7453)]
        inner = Polygon(hole)
        second = Polygon([(37.6005, 55.7450), (37.6006, 55.7450),
                          (37.6006, 55.7451), (37.6005, 55.7451)])
        mesh = GroundMesh(37.6003, 55.7452)
        mesh.add(MultiPolygon([Polygon(shell, [hole]), second]))
        self.assertEqual((mesh.features, mesh.holes, mesh.multipolygons), (1, 1, 1))
        by_index = {index: coordinate for coordinate, index in mesh.vertices.items()}
        for start in range(0, len(mesh.indices), 3):
            triangle = Polygon([by_index[index] for index in mesh.indices[start:start + 3]])
            self.assertLess(triangle.intersection(inner).area, 1e-18)
        blob = mesh.to_glb(geometry_type="exact_mapped_building_ground_surface")
        json_length = struct.unpack_from("<I", blob, 12)[0]
        document = json.loads(blob[20:20 + json_length])
        self.assertEqual(document["asset"]["extras"]["ground_offset_m"], GROUND_OFFSET_M)
        self.assertEqual(document["accessors"][1]["count"] // 3, mesh.triangles)
        self.assertNotIn("source_ids", blob.decode("latin1"))
        binary = blob[28 + json_length:]
        lon, lat = by_index[0]
        east, up, south = struct.unpack_from("<3f", binary)
        transform = enu_transform(37.6003, 55.7452)
        actual = [transform[12 + axis] + east * transform[axis]
                  - south * transform[4 + axis] + up * transform[8 + axis]
                  for axis in range(3)]
        expected = ecef(lon, lat, GROUND_OFFSET_M)
        self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 0.01)

    def test_distant_raster_does_not_add_false_dot_for_subpixel_footprint(self) -> None:
        overview = RasterSilhouette(region(37.5, 55.7, 37.7, 55.8, 2),
                                    max_dimension_px=64, mark_subpixel=False,
                                    surface_height_m=GROUND_OFFSET_M)
        tiny = Polygon([(37.6, 55.75), (37.600001, 55.75),
                        (37.600001, 55.750001), (37.6, 55.750001)])
        self.assertFalse(overview.polygon(tiny))
        self.assertEqual(overview.subpixel_features, 1)
        self.assertFalse(any(overview.mask))
        overview.polygon(Polygon([(37.55, 55.72), (37.58, 55.72),
                                  (37.58, 55.74), (37.55, 55.74)]))
        overview.footprints = 1
        blob = overview.to_glb()
        json_length = struct.unpack_from("<I", blob, 12)[0]
        document = json.loads(blob[20:20 + json_length])
        self.assertEqual(document["asset"]["extras"]["ground_height_policy"],
                         "WGS84 ellipsoid +0.45 m")


if __name__ == "__main__":
    unittest.main()
