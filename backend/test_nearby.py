"""Contract checks for bounded nearby lookup and honest walking routes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import nearby


def _encoded_shape(points: list[tuple[float, float]]) -> str:
    output: list[str] = []
    last_lat = last_lon = 0
    for lon, lat in points:
        current_lat, current_lon = round(lat * 1_000_000), round(lon * 1_000_000)
        for delta in (current_lat - last_lat, current_lon - last_lon):
            value = (delta << 1) if delta >= 0 else ((-delta << 1) - 1)
            while value >= 32:
                output.append(chr((32 | (value & 31)) + 63))
                value >>= 5
            output.append(chr(value + 63))
        last_lat, last_lon = current_lat, current_lon
    return "".join(output)


class FakeActor:
    def __init__(self, _config: dict, *, offset: float = 0.0, ferry: bool = False,
                 lengths: dict[float, float] | None = None) -> None:
        self.calls = 0
        self.offset = offset
        self.ferry = ferry
        self.lengths = lengths or {}
        self.last_request: dict | None = None

    def route(self, request: dict) -> dict:
        self.calls += 1
        self.last_request = request
        first, second = request["locations"]
        points = [(first["lon"] + self.offset, first["lat"]),
                  (second["lon"] + self.offset, second["lat"])]
        length = self.lengths.get(round(second["lon"], 4), 0.6)
        return {"status": 0, "trip": {"legs": [{"shape": _encoded_shape(points),
            "summary": {"length": length, "time": 420, "has_ferry": self.ferry}}]}}


def _row(identifier: str, category: str, lon: float, lat: float) -> dict:
    return {"id": "poi_" + identifier, "osm_source_id": f"osm:node:{identifier}:{category}",
            "category": category, "name": identifier, "coordinate": (lon, lat),
            "coordinate_basis": "source_point", "access_kind": "poi_node_access_unverified",
            "osm_version": 1, "osm_timestamp": "2026-10-08T00:00:00Z"}


class NearbyTests(unittest.TestCase):
    def test_production_tile_override_keeps_source_config_file_unchanged(self) -> None:
        captured: list[dict] = []
        row = _row("station", "metro_station", 37.51, 55.61)
        index = nearby.NearbyIndex([row], dataset_id="test", osm_snapshot_at="2026-10-08",
            actor_factory=lambda config: captured.append(config) or FakeActor(config))
        source_config = nearby.GRAPH_CONFIG.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"NOVOSTROY_VALHALLA_TILE_DIR": directory}):
                index._route((37.5, 55.6), row)
            self.assertEqual(captured[0]["mjolnir"]["tile_dir"], directory)
        self.assertEqual(nearby.GRAPH_CONFIG.read_bytes(), source_config)
        self.assertIs(captured[0]["service_limits"]["allow_hard_exclusions"], True)
        self.assertEqual(index.actor.last_request["costing_options"]["pedestrian"]["exclude_ferries"], True)

    def test_nearest_scans_skipped_intermediate_rings(self) -> None:
        origin = (37.005, 55.005)
        rows = [_row(str(index), "school", 37.005 + index * 0.01, 55.005)
                for index in (1, 2, 3, 4, 5, 6, 7)]
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08")
        selected = index._nearest(origin, {"school"}, 5)
        expected = sorted(rows, key=lambda row: nearby._distance_m(origin, row["coordinate"]))[:5]
        self.assertEqual([row["id"] for row in selected], [row["id"] for row in expected])
        self.assertIn("poi_3", [row["id"] for row in selected])

    def test_two_per_kind_route_cache_and_verified_clinic_replacement(self) -> None:
        origin = (37.5, 55.6)
        rows = []
        for category in ("metro_station", "school", "clinic"):
            rows.extend(_row(f"{category}{i}", category, 37.5001 + i * 0.0001, 55.6001)
                        for i in range(7))
        verified = rows[-1]
        fake = FakeActor({})
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
            oms_records={verified["osm_source_id"]: {"osm_version": 1, "official_snapshot_at": "2026-10-10"}},
            actor_factory=lambda _config: fake)
        result = index.point(*origin, point_id="mappt_test", education="school")
        self.assertEqual([len(layer["items"]) for layer in result["layers"].values()], [2, 2, 2])
        self.assertEqual(index.actor_calls, 21)
        self.assertEqual(fake.calls, 21)
        self.assertEqual(result["layers"]["medical"]["selection_policy"],
                         "nearest_local_plus_verified_oms")
        self.assertTrue(result["layers"]["medical"]["required_clinic_included"])
        self.assertTrue(result["layers"]["medical"]["verified_oms_added_as_exception"])
        self.assertEqual(sum(item["free_oms"]["status"] == "verified_program_participant"
                             for item in result["layers"]["medical"]["items"]), 1)
        self.assertEqual(result["free_clinic_guarantee"]["status"], "met")
        self.assertIs(index.point(*origin, point_id="mappt_test", education="school"), result)
        self.assertEqual(fake.calls, 21)

    def test_naturally_nearby_oms_is_included_without_exception(self) -> None:
        rows = [_row("verified", "clinic", 37.5001, 55.6),
                _row("other", "clinic", 37.5002, 55.6)]
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   oms_records={rows[0]["osm_source_id"]: {
                                       "osm_version": 1, "official_snapshot_at": "2026-10-10"}},
                                   actor_factory=lambda config: FakeActor(config))
        result = index.point(37.5, 55.6, point_id="mappt_test", education="all")
        medical = result["layers"]["medical"]
        self.assertTrue(medical["required_clinic_included"])
        self.assertFalse(medical["verified_oms_added_as_exception"])
        self.assertEqual(medical["selection_policy"], "nearest_two_walkable_clinics")
        self.assertEqual(result["free_clinic_guarantee"]["status"], "met")

    def test_education_all_separates_schools_and_kindergartens(self) -> None:
        rows = [_row(f"school{i}", "school", 37.5001 + i * 0.0001, 55.6)
                for i in range(5)] + [_row(f"kindergarten{i}", "kindergarten", 37.5001 + i * 0.0001, 55.6001)
                                     for i in range(5)]
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   actor_factory=lambda config: FakeActor(config))
        result = index.point(37.5, 55.6, point_id="mappt_test", education="all")
        self.assertEqual([item["category"] for item in result["layers"]["education"]["items"]],
                         ["school", "school", "kindergarten", "kindergarten"])

    def test_verified_oms_choice_uses_walk_not_only_air_distance(self) -> None:
        rows = [_row(str(i), "clinic", 37.5001 + i * 0.0001, 55.6)
                for i in range(7)]
        oms = {rows[i]["osm_source_id"]: {"osm_version": 1,
                "official_snapshot_at": "2026-10-10"} for i in (5, 6)}
        actor = FakeActor({}, lengths={37.5006: 2.0, 37.5007: 1.0})
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   oms_records=oms, actor_factory=lambda _config: actor)
        medical = index.point(37.5, 55.6, point_id="mappt_test", education="all")["layers"]["medical"]
        self.assertEqual(len(medical["items"]), 2)
        self.assertEqual(medical["items"][-1]["id"], rows[6]["id"])
        self.assertTrue(medical["required_clinic_included"])
        self.assertEqual(medical["items"][-1]["route"]["distance_m"], 1000)

    def test_unrouted_nearby_verified_site_is_not_displaced_by_far_route(self) -> None:
        rows = [_row(str(i), "clinic", 37.5001 + i * 0.0001, 55.6)
                for i in range(7)]
        oms = {rows[i]["osm_source_id"]: {"osm_version": 1,
                "official_snapshot_at": "2026-10-10"} for i in (5, 6)}

        class FerryAtCloserVerified(FakeActor):
            def route(self, request: dict) -> dict:
                result = super().route(request)
                result["trip"]["legs"][0]["summary"]["has_ferry"] = (
                    round(request["locations"][1]["lon"], 4) == 37.5006)
                return result

        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   oms_records=oms,
                                   actor_factory=lambda _config: FerryAtCloserVerified({}))
        medical = index.point(37.5, 55.6, point_id="mappt_test", education="all")["layers"]["medical"]
        closer = next(item for item in medical["items"] if item["id"] == rows[5]["id"])
        self.assertEqual(closer["route"]["status"], "contains_ferry")
        self.assertIsNone(closer["route"]["estimated_minutes"])
        self.assertNotIn(rows[6]["id"], [item["id"] for item in medical["items"]])

    def test_walking_distance_ranks_nearby_candidates_and_minutes_are_integer(self) -> None:
        origin = (37.5, 55.6)
        rows = [_row(str(i), "school", 37.5001 + i * 0.0001, 55.6) for i in range(3)]
        fake = FakeActor({}, lengths={37.5001: 1.2, 37.5002: 0.4, 37.5003: 0.8})
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   actor_factory=lambda _config: fake)
        result = index.point(*origin, point_id="mappt_test", education="school")
        items = result["layers"]["education"]["items"]
        self.assertEqual([item["name"] for item in items], ["1", "2"])
        self.assertEqual([item["route"]["distance_m"] for item in items], [400, 800])
        self.assertTrue(all(isinstance(item["route"]["estimated_minutes"], int) for item in items))

    def test_ferry_route_is_not_presented_as_walk_and_alternative_is_selected(self) -> None:
        origin = (37.5, 55.6)
        rows = [_row("near", "school", 37.5001, 55.6),
                _row("other", "school", 37.5002, 55.6)]

        class ConditionalFerry(FakeActor):
            def route(self, request: dict) -> dict:
                result = super().route(request)
                result["trip"]["legs"][0]["summary"]["has_ferry"] = request["locations"][1]["lon"] == 37.5001
                return result

        fake = ConditionalFerry({})
        index = nearby.NearbyIndex(rows, dataset_id="test", osm_snapshot_at="2026-10-08",
                                   actor_factory=lambda _config: fake)
        result = index.point(*origin, point_id="mappt_test", education="school")
        items = result["layers"]["education"]["items"]
        self.assertEqual(items[0]["route"]["status"], "contains_ferry")
        self.assertIsNone(items[0]["route"]["estimated_minutes"])
        self.assertIsNone(items[0]["route"]["geometry"])
        self.assertEqual(items[1]["route"]["status"], "routed")
        self.assertEqual(fake.calls, 2)

    def test_unlinked_and_snap_failure_never_fabricate_walk_time(self) -> None:
        point = _row("station", "metro_station", 37.51, 55.61)
        fake = FakeActor({}, offset=0.01)
        index = nearby.NearbyIndex([point], dataset_id="test", osm_snapshot_at="2026-10-08",
                                   actor_factory=lambda _config: fake)
        result = index.point(37.5, 55.6, point_id="mappt_test", education="all")
        item = result["layers"]["metro"]["items"][0]
        self.assertEqual(item["route"]["status"], "snap_exceeds_threshold")
        self.assertIsNone(item["route"]["geometry"])
        self.assertIsNone(item["route"]["estimated_minutes"])
        self.assertEqual(item["metro_access_link"], "unknown")

    def test_estimated_origin_access_is_runtime_fallback_after_origin_snap_failure(self) -> None:
        origin = (37.5, 55.6)
        target = _row("school", "school", 37.501, 55.601)
        graph_point = (37.5, 55.60025)
        segment = nearby.WalkSegment(
            "osm:way:101", ((37.499, 55.60025), (37.501, 55.60025)),
            {"highway": "footway", "surface": "asphalt"}, graph_point)

        class OriginFailureThenGraphRoute(FakeActor):
            def __init__(self) -> None:
                super().__init__({})
                self.locate_calls = 0

            def route(self, request: dict) -> dict:
                first = request["locations"][0]
                if (first["lon"], first["lat"]) == origin:
                    self.calls += 1
                    return {"status": 1, "trip": {}}
                return super().route(request)

            def locate(self, request: dict) -> list[dict]:
                self.locate_calls += 1
                return [{"edges": [{"way_id": 101, "correlated_lon": graph_point[0],
                                     "correlated_lat": graph_point[1]}]}]

        actor = OriginFailureThenGraphRoute()
        index = nearby.NearbyIndex(
            [target], dataset_id="local-test", osm_snapshot_at="2026-10-08",
            actor_factory=lambda _config: actor,
            walk_way_lookup=lambda edges: [segment] if edges else [],
            barrier_lookup=lambda _origin: [], roads_revision="roads-test-v1",
            barriers_revision="water-test-v1",
            barrier_coverage="known_osm_water_and_waterways_only",
        )

        route = index._route(origin, target)

        self.assertEqual(route["status"], "routed")
        self.assertEqual(route["routing_basis"], "local_pedestrian_graph_with_estimated_origin_connector")
        self.assertTrue(route["origin_access"]["approximate"])
        self.assertRegex(route["origin_access"]["source_segment_id"], r"^segment_[0-9a-f]{16}$")
        self.assertEqual(route["geometry"], route["network_geometry"])
        self.assertEqual(route["snap_distance_m"], [0, 0])
        self.assertGreater(route["distance_m"], route["network_distance_m"])
        self.assertEqual(actor.locate_calls, 1)

    def test_direct_success_has_priority_and_target_snap_failure_does_not_connector(self) -> None:
        origin = (37.5, 55.6)
        target = _row("school", "school", 37.501, 55.601)
        segment = nearby.WalkSegment("osm:way:101", ((37.5, 55.6), (37.501, 55.6)),
                                     {"highway": "footway"}, (37.5001, 55.6))

        class TargetSnapFailure(FakeActor):
            def locate(self, _request: dict) -> list[dict]:
                self.locate_calls += 1
                return [{"edges": [{"way_id": 101, "correlated_lon": 37.5001,
                                     "correlated_lat": 55.6}]}]

            def __init__(self) -> None:
                super().__init__({}, offset=0.001)
                self.locate_calls = 0

            def route(self, request: dict) -> dict:
                self.calls += 1
                first, second = request["locations"]
                points = [(first["lon"], first["lat"]),
                          (second["lon"] + 0.001, second["lat"])]
                return {"status": 0, "trip": {"legs": [{"shape": _encoded_shape(points),
                    "summary": {"length": 0.5, "time": 300, "has_ferry": False}}]}}

        direct_actor = FakeActor({})
        direct_index = nearby.NearbyIndex(
            [target], dataset_id="direct-test", osm_snapshot_at="2026-10-08",
            actor_factory=lambda _config: direct_actor,
            walk_way_lookup=lambda _edges: (_ for _ in ()).throw(AssertionError("must not locate")),
            barrier_lookup=lambda _origin: [], roads_revision="roads-v1", barriers_revision="water-v1")
        direct = direct_index._route(origin, target)
        self.assertEqual(direct["status"], "routed")
        self.assertNotIn("origin_access", direct)
        self.assertNotIn("_duration_seconds", direct)

        failing_actor = TargetSnapFailure()
        failing_index = nearby.NearbyIndex(
            [target], dataset_id="target-snap-test", osm_snapshot_at="2026-10-08",
            actor_factory=lambda _config: failing_actor,
            walk_way_lookup=lambda _edges: [segment], barrier_lookup=lambda _origin: [],
            roads_revision="roads-v1", barriers_revision="water-v1")
        failed = failing_index._route(origin, target)
        self.assertEqual(failed["status"], "snap_exceeds_threshold")
        self.assertGreater(failed["snap_distance_m"][1], nearby.SNAP_LIMIT_M)
        self.assertEqual(failing_actor.locate_calls, 0)

    def test_runtime_connector_keeps_unavailable_when_no_local_segment_matches(self) -> None:
        origin = (37.5, 55.6)
        target = _row("school", "school", 37.501, 55.601)

        class DisconnectedActor:
            def __init__(self, _config: dict) -> None:
                self.locate_calls = 0

            def route(self, _request: dict) -> dict:
                return {"status": 1, "trip": {}}

            def locate(self, _request: dict) -> list[dict]:
                self.locate_calls += 1
                return [{"edges": [{"way_id": 101, "correlated_lon": 37.5002,
                                     "correlated_lat": 55.6002}]}]

        actor = DisconnectedActor({})
        index = nearby.NearbyIndex(
            [target], dataset_id="no-segment-test", osm_snapshot_at="2026-10-08",
            actor_factory=lambda _config: actor,
            walk_way_lookup=lambda _edges: [], barrier_lookup=lambda _origin: [],
            roads_revision="roads-v1", barriers_revision="water-v1")

        route = index._route(origin, target)

        self.assertEqual(route["status"], "no_route")
        self.assertIsNone(route["estimated_minutes"])
        self.assertNotIn("origin_access", route)
        self.assertEqual(actor.locate_calls, 1)

    def test_explicit_metro_link_routes_to_entrance_without_moving_station(self) -> None:
        station = _row("station", "metro_station", 37.51, 55.61)
        fake = FakeActor({})
        access = (37.509, 55.609)
        index = nearby.NearbyIndex([station], dataset_id="test", osm_snapshot_at="2026-10-08",
            metro_links={station["osm_source_id"]: [access]}, actor_factory=lambda _config: fake)
        item = index.point(37.5, 55.6, point_id="mappt_test", education="all")["layers"]["metro"]["items"][0]
        self.assertEqual(item["coordinate"], {"lon": 37.51, "lat": 55.61})
        self.assertEqual(item["route"]["endpoint_coordinate"], {"lon": access[0], "lat": access[1]})
        self.assertEqual(item["metro_access_link"], "explicit_osm_relation_membership")
        self.assertEqual(item["route"]["target_access_kind"], "entrance_known")

    def test_metro_station_group_uses_best_linked_exit_and_keeps_distinct_station(self) -> None:
        first = _row("exit-a", "metro_station", 37.5000, 55.6000)
        second = _row("exit-b", "metro_station", 37.5001, 55.6000)
        distinct = _row("other-line", "metro_station", 37.5002, 55.6000)
        for row in (first, second, distinct):
            row["name"] = "Площадь Ильича"
        actor = FakeActor({}, lengths={37.5005: 1.0, 37.5006: 0.3, 37.5007: 0.8})
        index = nearby.NearbyIndex(
            [first, second, distinct], dataset_id="test", osm_snapshot_at="2026-10-08",
            metro_group_ids={first["osm_source_id"]: "osm:relation:station-a",
                             second["osm_source_id"]: "osm:relation:station-a",
                             distinct["osm_source_id"]: "osm:relation:station-b"},
            metro_links={first["osm_source_id"]: [(37.5005, 55.6)],
                         second["osm_source_id"]: [(37.5006, 55.6)]},
            actor_factory=lambda _config: actor)

        items = index.point(37.5, 55.6, point_id="mappt_test", education="all")["layers"]["metro"]["items"]

        self.assertEqual(len(items), 2)
        self.assertEqual({item["station_member_count"] for item in items}, {1, 2})
        grouped = next(item for item in items if item["station_member_count"] == 2)
        self.assertEqual(grouped["station_identity_basis"], "explicit_osm_stop_area_relation")
        self.assertFalse(grouped["station_identity_uncertain"])
        self.assertEqual(grouped["route"]["endpoint_coordinate"], {"lon": 37.5006, "lat": 55.6})
        # Equal names plus nearby pins do not override conflicting explicit identities.
        self.assertEqual({item["station_identity_basis"] for item in items},
                         {"explicit_osm_stop_area_relation"})

    def test_metro_name_fallback_only_groups_very_local_matches_and_marks_uncertainty(self) -> None:
        first = _row("local-a", "metro_station", 37.5, 55.6)
        duplicate = _row("local-b", "metro_station", 37.5, 55.6001)
        another = _row("local-c", "metro_station", 37.5, 55.601)
        for row in (first, duplicate, another):
            row["name"] = "Сокол"
        index = nearby.NearbyIndex([first, duplicate, another], dataset_id="test",
            osm_snapshot_at="2026-10-08", actor_factory=lambda config: FakeActor(config))

        items = index.point(37.5, 55.6, point_id="mappt_test", education="all")["layers"]["metro"]["items"]

        self.assertEqual(len(items), 2)
        local_group = next(item for item in items if item["station_member_count"] == 2)
        self.assertEqual(local_group["station_identity_basis"],
                         "exact_name_and_local_distance_uncertain")
        self.assertTrue(local_group["station_identity_uncertain"])
        distant = next(item for item in items if item["station_member_count"] == 1)
        self.assertTrue(distant["station_identity_uncertain"])

    def test_sparse_coverage_and_geometry_cap(self) -> None:
        index = nearby.NearbyIndex([], dataset_id="test", osm_snapshot_at="2026-10-08")
        result = index.point(37.5, 55.6, point_id="mappt_test", education="all")
        self.assertTrue(all(not layer["items"] for layer in result["layers"].values()))
        self.assertEqual(result["free_clinic_guarantee"]["status"], "unavailable")
        self.assertEqual(index.actor_calls, 0)
        points = [[37.0 + i * 0.00001, 55.0 + (i % 2) * 0.00001] for i in range(1200)]
        simplified, tolerance = nearby._bounded_route_geometry(points)
        self.assertLessEqual(len(simplified), nearby.MAX_ROUTE_POINTS)
        self.assertEqual(simplified[0], points[0])
        self.assertEqual(simplified[-1], points[-1])
        self.assertGreater(tolerance, 0)

    def test_snapshot_dedup_and_stale_oms_rejected(self) -> None:
        index = nearby.NearbyIndex.from_local_files()
        self.assertEqual(len(index.records), 7126)
        self.assertEqual(sum(row["category"] == "clinic" for row in index.records), 1848)
        self.assertEqual(sum(row["osm_source_id"] == "osm:way:28573158:clinic"
                             for row in index.records), 1)
        self.assertIn("osm:node:278692957:metro_station", index.metro_links)
        self.assertGreaterEqual(len(index.oms_records), 1)
        document = json.loads(nearby.OMS_SIDECAR.read_text(encoding="utf-8"))
        for record in document["records"]:
            record["effective_year"] = 2025
        with tempfile.TemporaryDirectory() as temp_dir:
            sidecar = Path(temp_dir) / "oms.json"
            manifest = Path(temp_dir) / "oms.manifest.json"
            payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
            sidecar.write_bytes(payload)
            manifest.write_text(json.dumps({"output_sha256": hashlib.sha256(payload).hexdigest()}), encoding="utf-8")
            with patch.object(nearby, "OMS_SIDECAR", sidecar), patch.object(nearby, "OMS_MANIFEST", manifest):
                stale = nearby.NearbyIndex.from_local_files()
            self.assertEqual(stale.oms_records, {})


if __name__ == "__main__":
    unittest.main()
