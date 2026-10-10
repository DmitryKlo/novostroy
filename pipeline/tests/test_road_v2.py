"""Road ribbons from adjacent cells must meet at one WGS84 location."""

from __future__ import annotations

import math
import unittest

from pipeline.build_roads import exact_local_point, padded_road_region
from pipeline.build_tiles import ecef, enu_transform, relative_xy


def world_point(origin: tuple[float, float], local: tuple[float, float, float]) -> tuple[float, float, float]:
    frame = enu_transform(*origin)
    return tuple(frame[12 + axis] + sum(frame[4 * column + axis] * local[column]
                                       for column in range(3)) for axis in range(3))


class RoadV2SeamTests(unittest.TestCase):
    def test_shared_cell_boundary_uses_one_world_centerline(self) -> None:
        # An actual clipped source endpoint shared by two adjacent road cells.
        lon, lat = 37.62, 55.754307932
        west_origin = (37.615, 55.755)
        east_origin = (37.625, 55.755)
        exact_west = world_point(west_origin, exact_local_point(lon, lat, *west_origin))
        exact_east = world_point(east_origin, exact_local_point(lon, lat, *east_origin))
        self.assertLess(math.dist(exact_west, exact_east), 1e-6)
        self.assertLess(math.dist(exact_west, ecef(lon, lat, 1.6)), 1e-6)

        def old_point(origin: tuple[float, float]) -> tuple[float, float, float]:
            east, north = relative_xy(lon, lat, *origin)
            return world_point(origin, (east, north, 1.6))

        self.assertGreater(math.dist(old_point(west_origin), old_point(east_origin)), 1.3)

    def test_cell_bounds_include_ribbon_outside_clipped_centerline(self) -> None:
        west, south, east, north = 37.61, 55.75, 37.62, 55.76
        detail = padded_road_region(west, south, east, north, coarse=False)
        coarse = padded_road_region(west, south, east, north, coarse=True)
        self.assertLess(math.degrees(detail[0]), west - 0.00011)
        self.assertGreater(math.degrees(detail[2]), east + 0.00011)
        self.assertLess(math.degrees(coarse[0]), math.degrees(detail[0]))
        self.assertGreater(math.degrees(coarse[3]), math.degrees(detail[3]))


if __name__ == "__main__":
    unittest.main()
