from __future__ import annotations

import unittest

try:
    from shapely.geometry import Point, Polygon

    from sources.social.build_citywide_surfaces import clip_polygon_geometry, feature_class

    GEODATA_AVAILABLE = True
except ModuleNotFoundError:
    GEODATA_AVAILABLE = False


@unittest.skipUnless(GEODATA_AVAILABLE, "run geodata tests in .runtime/geodata-env")
class CitywideSurfaceTests(unittest.TestCase):
    def test_boundary_clip_preserves_polygon_hole(self) -> None:
        source = Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
            [[(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)]],
        )
        boundary = Polygon([(1, 1), (9, 1), (9, 9), (1, 9), (1, 1)])

        result = clip_polygon_geometry(source, boundary)

        self.assertIsNotNone(result)
        geometry, hole_count = result
        self.assertEqual(hole_count, 1)
        self.assertFalse(geometry.covers(Point(5, 5)))
        self.assertTrue(geometry.covers(Point(2, 2)))

    def test_surface_class_comes_from_tags_and_geometry_kind(self) -> None:
        self.assertEqual(feature_class({"leisure": "park"}, "MultiPolygon"), ("parks", "park"))
        self.assertEqual(feature_class({"natural": "water", "water": "reservoir"}, "Polygon"), ("water", "reservoir"))
        self.assertEqual(feature_class({"waterway": "river"}, "LineString"), ("waterways", "river"))
        self.assertIsNone(feature_class({"leisure": "track"}, "Polygon"))


if __name__ == "__main__":
    unittest.main()
