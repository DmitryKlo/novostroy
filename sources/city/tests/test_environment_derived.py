from __future__ import annotations

import unittest

from sources.city.environment_derived import (
    METHOD,
    count_features_within_m,
    distance_to_geometry_m,
    geometry_centroid_lonlat,
    nearest_feature_distance,
)


class EnvironmentDerivedTests(unittest.TestCase):
    def test_distance_to_nearby_point_is_metric_and_repeatable(self) -> None:
        origin = [37.0, 55.0]
        target = {"type": "Point", "coordinates": [37.0, 55.001]}
        distance = distance_to_geometry_m(origin, target)
        self.assertGreater(distance, 110)
        self.assertLess(distance, 112)

    def test_line_uses_segment_projection_not_only_vertices(self) -> None:
        line = {"type": "LineString", "coordinates": [[37.0, 55.0], [37.01, 55.0]]}
        distance = distance_to_geometry_m([37.005, 55.001], line)
        self.assertGreater(distance, 110)
        self.assertLess(distance, 112)

    def test_polygon_inside_and_hole_have_correct_zero_boundary_semantics(self) -> None:
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [[37.0, 55.0], [37.01, 55.0], [37.01, 55.01], [37.0, 55.01], [37.0, 55.0]],
                [[37.004, 55.004], [37.006, 55.004], [37.006, 55.006], [37.004, 55.006], [37.004, 55.004]],
            ],
        }
        self.assertEqual(distance_to_geometry_m([37.002, 55.002], polygon), 0)
        self.assertGreater(distance_to_geometry_m([37.005, 55.005], polygon), 60)

    def test_nearest_keeps_source_reference_and_method(self) -> None:
        features = [
            {"type": "Feature", "id": "far", "geometry": {"type": "Point", "coordinates": [37.0, 55.002]}, "properties": {"class": "secondary"}},
            {"type": "Feature", "id": "near", "geometry": {"type": "Point", "coordinates": [37.0, 55.001]}, "properties": {"class": "primary", "source_release": "pinned"}},
        ]
        result = nearest_feature_distance([37.0, 55.0], features, predicate=lambda feature: feature["properties"]["class"] == "primary")
        self.assertEqual(result["reference_feature_id"], "near")
        self.assertEqual(result["method"], METHOD)
        self.assertTrue(result["approximate"])

    def test_count_reports_source_absence_semantics(self) -> None:
        features = [
            {"type": "Feature", "id": "near", "geometry": {"type": "Point", "coordinates": [37.0, 55.001]}, "properties": {"category": "playground"}},
            {"type": "Feature", "id": "far", "geometry": {"type": "Point", "coordinates": [37.0, 55.01]}, "properties": {"category": "playground"}},
        ]
        result = count_features_within_m([37.0, 55.0], features, 200, predicate=lambda feature: feature["properties"]["category"] == "playground")
        self.assertEqual(result["value"], 1)
        self.assertEqual(result["mapped_feature_ids"], ["near"])
        self.assertIn("not confirmed", result["absence_semantics"])

    def test_polygon_centroid_handles_closed_ring(self) -> None:
        polygon = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
        self.assertEqual(geometry_centroid_lonlat(polygon), (1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
