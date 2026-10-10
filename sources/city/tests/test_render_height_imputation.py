import importlib.util
import unittest
from pathlib import Path

import shapely
from shapely.geometry import Polygon

MODULE_PATH = Path(__file__).resolve().parents[1] / "impute_render_heights.py"
SPEC = importlib.util.spec_from_file_location("impute_render_heights", MODULE_PATH)
impute = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(impute)


def local_rectangle(x: float, y: float, width: float = 12.0, depth: float = 8.0) -> Polygon:
    """Small rectangle around Moscow, with approximate meter-to-degree conversion."""
    lon = 37.62 + x / 62000.0
    lat = 55.75 + y / 111000.0
    dx = width / 62000.0
    dy = depth / 111000.0
    return Polygon([(lon, lat), (lon + dx, lat), (lon + dx, lat + dy), (lon, lat + dy), (lon, lat)])


def feature(source_id: str, geometry: Polygon, height: float | None, building_class: str | None = "apartments") -> dict:
    return {
        "id": source_id,
        "wkb": shapely.to_wkb(geometry),
        "height": height,
        "floors": None,
        "class": building_class,
        "subtype": "residential" if building_class == "apartments" else None,
        "is_underground": False,
        "has_parts": False,
        "source_dataset": "OpenStreetMap",
    }


class RenderHeightImputationTests(unittest.TestCase):
    def test_canonical_shape_aligns_translated_equal_footprints(self) -> None:
        left = impute.metric_geometries([shapely.to_wkb(local_rectangle(0, 0))])[0]
        right = impute.metric_geometries([shapely.to_wkb(local_rectangle(30, 15))])[0]
        left_shape, _ = impute.canonical_shape(left)
        _, right_variants = impute.canonical_shape(right)
        self.assertGreater(impute.normalized_shape_iou(left_shape, right_variants), 0.999)

    def test_canonical_geometry_key_ignores_ring_direction(self) -> None:
        polygon = local_rectangle(0, 0)
        reversed_polygon = Polygon(list(polygon.exterior.coords)[::-1])
        left = impute.metric_geometries([shapely.to_wkb(polygon)])[0]
        right = impute.metric_geometries([shapely.to_wkb(reversed_polygon)])[0]
        self.assertEqual(impute.geometry_key(left), impute.geometry_key(right))

    def test_same_normalized_square_does_not_pass_absolute_size_gates(self) -> None:
        target = feature("target", local_rectangle(0, 0, 4, 4), None)
        donor = feature("tower", local_rectangle(7, 0, 40, 40), 220.0)
        donor_rows = [donor]
        donor_local, donor_geoms, tree, exact_map, _ = impute.donor_index(donor_rows)
        filtered = [donor_rows[i] for i in donor_local]
        target_geom = impute.metric_geometries([target["wkb"]])[0]
        result, _ = impute.estimate(target, target_geom, filtered, donor_geoms, tree, exact_map)
        self.assertIsNone(result)

    def test_class_mismatch_does_not_copy_height(self) -> None:
        target = feature("garage-target", local_rectangle(0, 0), None, "garages")
        donor = feature("apartment-donor", local_rectangle(15, 0), 25.0, "apartments")
        donor_rows = [donor]
        donor_local, donor_geoms, tree, exact_map, _ = impute.donor_index(donor_rows)
        filtered = [donor_rows[i] for i in donor_local]
        target_geom = impute.metric_geometries([target["wkb"]])[0]
        result, _ = impute.estimate(target, target_geom, filtered, donor_geoms, tree, exact_map)
        self.assertIsNone(result)

    def test_minimum_height_is_used_across_all_suitable_donors(self) -> None:
        target = feature("target", local_rectangle(0, 0), None)
        donors = [
            feature("donor-9m", local_rectangle(15, 0), 9.0),
            feature("donor-12m", local_rectangle(-15, 0), 12.0),
        ]
        donor_local, donor_geoms, tree, exact_map, _ = impute.donor_index(donors)
        filtered = [donors[i] for i in donor_local]
        target_geom = impute.metric_geometries([target["wkb"]])[0]
        result, _ = impute.estimate(target, target_geom, filtered, donor_geoms, tree, exact_map)
        self.assertIsNotNone(result)
        self.assertEqual(result["estimated_render_height_m"], 9.0)
        self.assertEqual(result["donor_count"], 2)


if __name__ == "__main__":
    unittest.main()
