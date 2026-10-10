"""Integration checks against the pinned local Valhalla pedestrian graph."""

from __future__ import annotations

import importlib.util
import unittest

from backend import nearby


@unittest.skipUnless(
    importlib.util.find_spec("valhalla") is not None,
    "requires PyValhalla; run with .runtime/geodata-env/bin/python",
)
class NearbyGraphIntegrationTests(unittest.TestCase):
    def test_zilart_walk_route_avoids_known_ferry_edge(self) -> None:
        index = nearby.NearbyIndex.from_local_files()
        row = next(row for row in index.records
                   if row["osm_source_id"] == "osm:node:5055723244:clinic")
        route = index._route((37.63091196467479, 55.69662455), row)

        # With the pinned graph the old 1.74-km route used OSM ferry way
        # 1410350899. A walk-only alternative is about 2.5 km.
        self.assertEqual(route["status"], "routed")
        self.assertGreater(route["distance_m"], 2400)
        self.assertIsInstance(route["estimated_minutes"], int)


if __name__ == "__main__":
    unittest.main()
