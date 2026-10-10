"""Tests for bounded approximate access to the local pedestrian graph."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import origin_access
from backend.origin_access import (
    Barrier,
    ConnectorPolicy,
    Origin,
    WalkSegment,
    connector_cache_key,
    route_with_origin_access,
)


def _valid_route(start: tuple[float, float]) -> dict:
    return {
        "status": "routed",
        "distance_m": 800,
        "estimated_minutes": 12,
        "geometry": [list(start), [37.501, 55.701]],
        "snap_distance_m": [2, 4],
    }


def _segment(source_id: str, graph_point: tuple[float, float], *,
             tags: dict | None = None) -> WalkSegment:
    lon, lat = graph_point
    return WalkSegment(source_id, ((lon - 0.001, lat), (lon + 0.001, lat)),
                       tags or {"highway": "footway"}, graph_point)


class OriginAccessTests(unittest.TestCase):
    def test_estimated_connector_is_separate_and_included_in_total(self) -> None:
        origin = Origin((37.5, 55.7))
        graph_point = (37.5, 55.70027)  # about 30 m north of the selected point
        calls: list[tuple[float, float]] = []

        def network_route(access: tuple[float, float]) -> dict:
            calls.append(access)
            return _valid_route(access)

        result = route_with_origin_access(
            origin, [_segment("osm:way:101", graph_point)], [], network_route)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["routing_basis"], "local_pedestrian_graph_with_estimated_origin_connector")
        access = result["origin_access"]
        self.assertEqual(access["status"], "estimated")
        self.assertTrue(access["approximate"])
        self.assertEqual(access["source_segment_id"], "osm:way:101")
        self.assertEqual(access["geometry"][0], [37.5, 55.7])
        self.assertGreater(access["distance_m"], 25)
        self.assertLess(access["distance_m"], 35)
        self.assertEqual(result["network_geometry"][0], list(graph_point))
        self.assertEqual(result["distance_m"], 800 + access["distance_m"])
        self.assertEqual(result["estimated_minutes"], 12 + access["estimated_minutes"])
        self.assertEqual(calls, [graph_point])
        self.assertNotIn("geometry", result)  # UI draws connector and network as separate layers

    def test_disconnected_nearest_candidate_falls_through_to_next_graph_segment(self) -> None:
        origin = Origin((37.5, 55.7))
        disconnected = _segment("osm:way:near", (37.5, 55.70008))
        connected = _segment("osm:way:next", (37.5, 55.70016))
        calls: list[tuple[float, float]] = []

        def network_route(access: tuple[float, float]) -> dict:
            calls.append(access)
            return ({"status": "no_route"} if access == disconnected.graph_coordinate
                    else _valid_route(access))

        result = route_with_origin_access(origin, [disconnected, connected], [], network_route)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["origin_access"]["source_segment_id"], "osm:way:next")
        self.assertEqual(calls, [disconnected.graph_coordinate, connected.graph_coordinate])

    def test_known_water_and_rail_barriers_reject_crossing_connector(self) -> None:
        origin = Origin((37.5, 55.7))
        candidate = _segment("osm:way:over-barrier", (37.5, 55.70027))
        water = Barrier("osm:way:water", "water", {
            "type": "Polygon", "coordinates": [[
                [37.4998, 55.70010], [37.5002, 55.70010],
                [37.5002, 55.70020], [37.4998, 55.70020],
                [37.4998, 55.70010],
            ]],
        })
        route_calls: list[tuple[float, float]] = []
        result = route_with_origin_access(
            origin, [candidate], [water], lambda point: route_calls.append(point) or _valid_route(point))
        self.assertIsNone(result)
        self.assertEqual(route_calls, [])

        rail = Barrier("osm:way:rail", "railway", {
            "type": "LineString", "coordinates": [[37.4999, 55.70013], [37.5001, 55.70013]],
        })
        result = route_with_origin_access(
            origin, [candidate], [rail], lambda point: _valid_route(point))
        self.assertIsNone(result)

    def test_unsuitable_private_or_distant_segments_and_no_route_return_null(self) -> None:
        origin = Origin((37.5, 55.7))
        candidates = [
            _segment("motorway", (37.5, 55.70005), tags={"highway": "motorway"}),
            _segment("private", (37.5, 55.70006),
                     tags={"highway": "residential", "access": "private"}),
            _segment("far", (37.5, 55.701), tags={"highway": "footway"}),
        ]
        calls: list[tuple[float, float]] = []
        result = route_with_origin_access(
            origin, candidates, [], lambda point: calls.append(point) or _valid_route(point))
        self.assertIsNone(result)
        self.assertEqual(calls, [])

        local = _segment("disconnected", (37.5, 55.70005))
        result = route_with_origin_access(origin, [local], [], lambda _point: {"status": "no_route"})
        self.assertIsNone(result)

    def test_100m_bound_covers_setback_but_rejects_longer_approaches(self) -> None:
        origin = Origin((37.712921802, 55.693160959))
        graph_point = (37.713845, 55.693679)  # local footway correlation, about 82 m away
        street = WalkSegment(
            "osm:way:1071637324", ((37.7137, 55.6935), (37.7140, 55.6938)),
            {"highway": "footway"}, graph_point)
        result = route_with_origin_access(origin, [street], [], _valid_route)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result["origin_access"]["distance_m"], 75)
        self.assertLessEqual(result["origin_access"]["distance_m"], 100)

        too_far_point = (37.713845, 55.69450)
        too_far = WalkSegment(
            "osm:way:too-far", ((37.7137, 55.6943), (37.7140, 55.6946)),
            {"highway": "footway"}, too_far_point)
        self.assertIsNone(route_with_origin_access(origin, [too_far], [], _valid_route))
        with self.assertRaises(ValueError):
            route_with_origin_access(origin, [street], [], _valid_route,
                policy=ConnectorPolicy(max_access_distance_m=100.01))

    def test_total_minutes_round_exact_seconds_once(self) -> None:
        origin = Origin((37.5, 55.7))
        street = _segment("osm:way:short", (37.5, 55.700261))
        result = route_with_origin_access(origin, [street], [], lambda point: {
            "status": "routed", "distance_m": 100, "estimated_minutes": 1,
            "_duration_seconds": 35.0, "geometry": [list(point), [37.501, 55.701]],
            "snap_distance_m": [0, 0],
        })
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["origin_access"]["estimated_minutes"], 1)
        self.assertEqual(result["network_estimated_minutes"], 1)
        self.assertEqual(result["estimated_minutes"], 1)

    def test_candidate_count_is_bounded_and_cache_revision_invalidates(self) -> None:
        origin = Origin((37.5, 55.7))
        candidates = [_segment(f"way:{index}", (37.5, 55.70001 + index * 0.00001))
                      for index in range(7)]
        calls: list[tuple[float, float]] = []
        result = route_with_origin_access(
            origin, candidates, [], lambda point: calls.append(point) or {"status": "no_route"},
            policy=ConnectorPolicy(max_candidates=3))
        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)

        base = ("dataset-v1", (37.5, 55.7), "osm:node:clinic")
        current = connector_cache_key(base, roads_revision="roads-v2", barriers_revision="barriers-v1")
        other_roads = connector_cache_key(base, roads_revision="roads-v3", barriers_revision="barriers-v1")
        other_barriers = connector_cache_key(base, roads_revision="roads-v2", barriers_revision="barriers-v2")
        self.assertNotEqual(current, other_roads)
        self.assertNotEqual(current, other_barriers)
        self.assertIn("origin-access-connector-v2", current)
        with patch.object(origin_access, "CONNECTOR_POLICY_VERSION", "next-policy"):
            changed_policy = connector_cache_key(
                base, roads_revision="roads-v2", barriers_revision="barriers-v1")
        self.assertNotEqual(current, changed_policy)

    def test_footprint_uses_nearest_boundary_not_center_as_connector_start(self) -> None:
        ring = ((37.4999, 55.6999), (37.5001, 55.6999), (37.5001, 55.7001),
                (37.4999, 55.7001), (37.4999, 55.6999))
        origin = Origin((37.5, 55.7), ring)
        # Road lies north; the closest outline point is on the north edge.
        graph_point = (37.5, 55.70030)
        result = route_with_origin_access(
            origin, [_segment("osm:way:north", graph_point)], [], _valid_route)
        self.assertIsNotNone(result)
        assert result is not None
        connector_start = result["origin_access"]["geometry"][0]
        self.assertGreaterEqual(connector_start[0], 37.4999)
        self.assertLessEqual(connector_start[0], 37.5001)
        self.assertAlmostEqual(connector_start[1], 55.7001, places=6)


if __name__ == "__main__":
    unittest.main()
