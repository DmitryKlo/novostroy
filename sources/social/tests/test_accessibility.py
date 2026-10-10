from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from sources.social.accessibility import (
    AccessPoint,
    ValhallaClient,
    ValhallaError,
    accessibility_index,
    deduplicate_osm_features,
    nearest_poi,
    poi_access_point,
    social_payload_for_complex,
)
from sources.social.osm_adapter import classify_osm_tags


ROOT = Path(__file__).resolve().parents[3]
SOCIAL_DATA = ROOT / "data/social"
ACCEPTANCE_PATH = ROOT / "quality/acceptance_cases.json"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_records() -> list[dict]:
    return _load_json(SOCIAL_DATA / "osm_overpass_poi_normalized.json")


def _routes_for(category: str, duration_s: float) -> dict[str, list[dict]]:
    return {
        category: [
            {"method": "routed", "provider": "fixture", "duration_s": duration_s}
        ]
    }


class DeduplicationTests(unittest.TestCase):
    def test_overlapping_pharmacy_and_multicategory_samples_deduplicate_to_50(self):
        pharmacy = _load_json(SOCIAL_DATA / "osm_overpass_pharmacy_normalized.json")
        multi_category = _sample_records()
        combined = [*pharmacy, *multi_category]

        unique = deduplicate_osm_features(combined)

        self.assertEqual(len(combined), 55)
        self.assertEqual(len(unique), 50)
        self.assertEqual(len({_key(record) for record in unique}), 50)
        self.assertEqual(Counter(record["category"] for record in unique)["pharmacy"], 5)

    def test_newest_osm_revision_wins_without_mutating_input(self):
        older = {"osm_type": "node", "osm_id": 7, "osm_version": 2, "tags_raw": {"name": "old"}}
        newer = {"osm_type": "node", "osm_id": 7, "osm_version": 3, "tags_raw": {"name": "new"}}

        [result] = deduplicate_osm_features([older, newer])

        self.assertEqual(result["tags_raw"]["name"], "new")
        self.assertEqual(older["tags_raw"]["name"], "old")

    def test_osm_type_is_part_of_deduplication_key(self):
        records = [
            {"osm_type": "node", "osm_id": 1},
            {"osm_type": "way", "osm_id": 1},
            {"osm_type": "relation", "osm_id": 1},
        ]
        self.assertEqual(len(deduplicate_osm_features(records)), 3)

    def test_records_without_source_identity_are_kept_separately(self):
        records = [{"category": "education"}, {"category": "education"}]
        self.assertEqual(len(deduplicate_osm_features(records)), 2)


def _key(record: dict) -> tuple[str, str]:
    return str(record["osm_type"]), str(record["osm_id"])


class NearestPoiTests(unittest.TestCase):
    def test_distance_is_straight_line_meters_and_not_walk_time(self):
        origin = AccessPoint((0.0, 0.0), "entrance_known", "building/access-1")
        poi = {
            "source": "openstreetmap",
            "source_id": "node/11",
            "osm_type": "node",
            "osm_id": 11,
            "category": "pharmacy",
            "entrance": None,
            "location": {
                "type": "Point",
                "coordinates": [0.001, 0.0],
                "role": "osm_node",
                "approximate": False,
            },
        }

        result = nearest_poi(origin, [poi], category="pharmacy")

        self.assertIsNotNone(result)
        self.assertEqual(result["metric"], "straight_line")
        self.assertEqual(result["unit"], "m")
        self.assertAlmostEqual(result["straight_line_m"], 111.195, places=2)
        self.assertEqual(result["to_access_kind"], "mapped_point_access_unknown")
        self.assertFalse(result["entrance_known"])
        self.assertTrue(result["approximate"])
        self.assertNotIn("duration_s", result)
        self.assertNotIn("walk_time_minutes", result)

    def test_known_entrance_is_distinguished_from_source_point(self):
        poi = {
            "source_id": "way/12",
            "entrance": {
                "access_kind": "entrance_known",
                "coordinates": [37.61, 55.75],
            },
            "location": {
                "coordinates": [37.62, 55.76],
                "role": "overpass_center",
                "approximate": True,
            },
        }

        access = poi_access_point(poi)

        self.assertEqual(access.kind, "entrance_known")
        self.assertEqual(access.coordinates, (37.61, 55.75))

    def test_overpass_center_is_explicitly_approximate(self):
        poi = {
            "source_id": "way/12",
            "location": {
                "coordinates": [37.62, 55.76],
                "role": "overpass_center",
                "approximate": True,
            },
        }

        access = poi_access_point(poi)

        self.assertEqual(access.kind, "center_approx")
        result = nearest_poi(AccessPoint((37.61, 55.75), "entrance_known"), [poi])
        self.assertEqual(result["to_access_kind"], "center_approx")
        self.assertTrue(result["approximate"])

    def test_missing_location_is_not_given_a_coordinate(self):
        self.assertIsNone(poi_access_point({"source_id": "node/1", "location": None}))
        self.assertIsNone(
            nearest_poi(AccessPoint((37.6, 55.7), "entrance_known"), [{"category": "pharmacy"}])
        )

    def test_nearest_can_filter_by_category_and_ties_are_stable(self):
        origin = AccessPoint((0.0, 0.0), "entrance_known")
        records = [
            {
                "osm_type": "node",
                "osm_id": 2,
                "category": "education",
                "location": {"coordinates": [0.001, 0.0], "role": "osm_node"},
            },
            {
                "osm_type": "node",
                "osm_id": 1,
                "category": "pharmacy",
                "location": {"coordinates": [0.001, 0.0], "role": "osm_node"},
            },
        ]

        result = nearest_poi(origin, records, category="pharmacy")

        self.assertEqual(result["osm_id"], 1)
        self.assertEqual(result["category"], "pharmacy")


class AccessibilityIndexTests(unittest.TestCase):
    def test_unknown_category_coverage_returns_null_not_zero(self):
        result = accessibility_index(
            ["education"],
            poi_coverage={"education": "unknown"},
            route_coverage={"education": "unknown"},
            source_version="osm:demo-snapshot",
        )

        self.assertIsNone(result["value"])
        self.assertEqual(result["coverage"]["status"], "unknown")
        self.assertEqual(result["coverage"]["coverage_ratio"], 0.0)

    def test_partial_category_or_route_coverage_returns_null(self):
        result = accessibility_index(
            ["education", "pharmacy"],
            poi_coverage={"education": "complete", "pharmacy": "partial"},
            route_coverage={"education": "complete", "pharmacy": "complete"},
            routes_by_category=_routes_for("education", 300),
            source_version="osm:test+valhalla:test",
        )

        self.assertIsNone(result["value"])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["coverage"]["observed_categories"], ["education"])
        self.assertEqual(result["coverage"]["coverage_ratio"], 0.5)

    def test_fully_observed_routes_use_the_documented_formula(self):
        result = accessibility_index(
            ["education", "pharmacy"],
            poi_coverage={"education": "complete", "pharmacy": "complete"},
            route_coverage={"education": "complete", "pharmacy": "complete"},
            routes_by_category={
                "education": [{"method": "routed", "duration_s": 300}],
                "pharmacy": [{"method": "routed", "duration_s": 600}],
            },
            threshold_minutes=15,
            source_version="osm:2026-10-09+valhalla:graph-v1",
        )

        self.assertAlmostEqual(result["category_scores"]["education"], 0.666667)
        self.assertAlmostEqual(result["category_scores"]["pharmacy"], 0.333333)
        self.assertEqual(result["value"], 0.5)
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["coverage"]["coverage_ratio"], 1.0)
        self.assertEqual(result["coverage"]["method"], "routed")

    def test_weights_keep_the_full_denominator(self):
        result = accessibility_index(
            ["education", "pharmacy"],
            poi_coverage={"education": "complete", "pharmacy": "complete"},
            route_coverage={"education": "complete", "pharmacy": "complete"},
            routes_by_category={"education": [{"method": "routed", "duration_s": 0}]},
            weights={"education": 2, "pharmacy": 1},
            source_version="routes:v1",
        )

        self.assertEqual(result["category_scores"], {"education": 1.0, "pharmacy": 0.0})
        self.assertAlmostEqual(result["value"], 0.666667)

    def test_complete_coverage_and_verified_absence_scores_zero(self):
        result = accessibility_index(
            ["childcare"],
            poi_coverage={"childcare": "complete"},
            route_coverage={"childcare": "complete"},
            routes_by_category={"childcare": []},
            source_version="osm:empty-complete-category+valhalla:graph-v1",
        )

        self.assertEqual(result["category_scores"]["childcare"], 0.0)
        self.assertEqual(result["value"], 0.0)

    def test_sample_or_unrecognized_coverage_is_not_complete(self):
        result = accessibility_index(
            ["pharmacy"],
            poi_coverage={"pharmacy": "sample"},
            route_coverage={"pharmacy": "complete"},
            routes_by_category={"pharmacy": [{"method": "routed", "duration_s": 60}]},
            source_version="osm:sample+route:v1",
        )

        self.assertIsNone(result["value"])
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_missing_source_version_does_not_emit_a_reproducible_index(self):
        result = accessibility_index(
            ["education"],
            poi_coverage={"education": "complete"},
            route_coverage={"education": "complete"},
            routes_by_category=_routes_for("education", 300),
        )

        self.assertIsNone(result["value"])
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertIsNone(result["coverage"]["source_version"])

    def test_straight_line_metric_cannot_be_used_as_route_time(self):
        with self.assertRaisesRegex(ValueError, "straight-line distance is not travel time"):
            accessibility_index(
                ["pharmacy"],
                poi_coverage={"pharmacy": "complete"},
                route_coverage={"pharmacy": "complete"},
                routes_by_category={
                    "pharmacy": [
                        {
                            "method": "straight_line",
                            "straight_line_m": 100,
                            "duration_s": 60,
                        }
                    ]
                },
                source_version="test",
            )


class SocialPayloadTests(unittest.TestCase):
    def test_payload_exposes_real_sample_pois_but_keeps_index_unknown(self):
        records = _sample_records()
        origin = AccessPoint((37.61, 55.755), "center_approx", "demo-coordinate")

        payload = social_payload_for_complex(
            origin,
            records,
            ["pharmacy", "education", "missing_category"],
            source_version="osm-normalized-sha256:e3d4f0dcea718b820a23cb1f9644297bb989f2ed05ec1aa084db6092f3e7ff0e",
        )

        self.assertIsNone(payload["index"])
        self.assertEqual(payload["coverage"]["status"], "unknown")
        self.assertEqual(payload["coverage"]["coverage_completeness"], "unknown")
        self.assertEqual(payload["coverage"]["method"], "straight_line")
        self.assertEqual(payload["poi_source"]["coverage"], "sample")
        self.assertEqual(
            [poi["category"] for poi in payload["nearest_pois"]],
            ["pharmacy", "education"],
        )
        self.assertTrue(all(poi["metric"] == "straight_line" for poi in payload["nearest_pois"]))
        self.assertTrue(all(poi["approximate"] for poi in payload["nearest_pois"]))
        self.assertTrue(all("duration_s" not in poi for poi in payload["nearest_pois"]))

    def test_payload_does_not_claim_category_coverage_from_observed_pois(self):
        payload = social_payload_for_complex(
            AccessPoint((37.61, 55.755), "center_approx"),
            _sample_records(),
            ["pharmacy"],
        )
        self.assertEqual(
            payload["coverage"]["observed_categories"],
            ["education", "green_space", "grocery", "healthcare", "pharmacy", "sports"],
        )
        self.assertEqual(payload["coverage"]["status"], "unknown")
        self.assertIsNone(payload["index"])


class _MockResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


def _valhalla_payload(length_km: float = 1.25, duration_s: float = 620) -> dict:
    return {"trip": {"status": 0, "summary": {"length": length_km, "time": duration_s}}}


class ValhallaTests(unittest.TestCase):
    def test_construction_and_local_calculations_make_no_network_call(self):
        origin = AccessPoint((37.6, 55.7), "entrance_known")
        poi = {
            "category": "pharmacy",
            "location": {"coordinates": [37.601, 55.7], "role": "osm_node"},
        }
        with patch("sources.social.accessibility.urlopen") as open_url:
            ValhallaClient("http://valhalla.internal:8002")
            nearest_poi(origin, [poi])
            accessibility_index(
                ["pharmacy"],
                poi_coverage={"pharmacy": "unknown"},
                route_coverage={"pharmacy": "unknown"},
                source_version="osm:test",
            )
        open_url.assert_not_called()

    def test_explicit_route_posts_pedestrian_request_and_uses_provider_metrics(self):
        origin = AccessPoint((37.6, 55.7), "entrance_known")
        destination = AccessPoint((37.61, 55.71), "entrance_known")
        client = ValhallaClient("http://valhalla.internal:8002", timeout_s=4)

        with patch(
            "sources.social.accessibility.urlopen",
            return_value=_MockResponse(_valhalla_payload()),
        ) as open_url:
            result = client.route(origin, destination)

        request = open_url.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "http://valhalla.internal:8002/route")
        self.assertEqual(body["costing"], "pedestrian")
        self.assertEqual(body["units"], "kilometers")
        self.assertEqual(result["distance_m"], 1250.0)
        self.assertEqual(result["duration_s"], 620.0)
        self.assertEqual(result["method"], "routed")
        self.assertFalse(result["approximate"])

    def test_approximate_endpoint_requires_explicit_opt_in_and_is_flagged(self):
        origin = AccessPoint((37.6, 55.7), "entrance_known")
        center = AccessPoint((37.61, 55.71), "center_approx")
        client = ValhallaClient("http://valhalla.internal:8002")
        with patch(
            "sources.social.accessibility.urlopen",
            return_value=_MockResponse(_valhalla_payload()),
        ) as open_url:
            with self.assertRaisesRegex(ValueError, "known entrances"):
                client.route(origin, center)
            open_url.assert_not_called()
            result = client.route(origin, center, allow_approximate=True)
        self.assertTrue(result["approximate"])

    def test_status_error_and_invalid_payload_are_reported(self):
        client = ValhallaClient("http://valhalla.internal/route")
        origin = AccessPoint((37.6, 55.7), "entrance_known")
        target = AccessPoint((37.61, 55.71), "entrance_known")
        bad_status = {"trip": {"status": 442, "status_message": "No path"}}
        with patch(
            "sources.social.accessibility.urlopen",
            return_value=_MockResponse(bad_status),
        ):
            with self.assertRaises(ValhallaError):
                client.route(origin, target)
        with patch(
            "sources.social.accessibility.urlopen",
            return_value=_MockResponse({"not_trip": {}}),
        ):
            with self.assertRaisesRegex(ValhallaError, "invalid route response"):
                client.route(origin, target)

    def test_endpoint_must_be_operator_configured_http_url_without_credentials(self):
        with self.assertRaises(ValueError):
            ValhallaClient("javascript:alert(1)")
        with self.assertRaises(ValueError):
            ValhallaClient("https://user:secret@valhalla.internal/route")
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(ValhallaClient.from_environment())
        with patch.dict("os.environ", {"NOVOSTROY_VALHALLA_ENDPOINT": "http://localhost:8002"}):
            self.assertEqual(
                ValhallaClient.from_environment().endpoint,
                "http://localhost:8002/route",
            )


class SocialAcceptanceCaseTests(unittest.TestCase):
    """Execute each SC-081..SC-100 social-data invariant without offer fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.cases = {
            case["case_id"]: case
            for case in _load_json(ACCEPTANCE_PATH)["cases"]
            if case["group"] == "missing_data"
        }
        cls.records = _sample_records()

    def _has_predicate(self, case_id: str, predicate: str) -> None:
        case = self.cases[case_id]
        self.assertIn(
            predicate,
            [
                item["predicate"]
                for item in case["expected"]["expected_invariants"]
            ],
        )

    def test_sc081_to_sc100_predicates_hold_for_source_and_index(self):
        self.assertEqual(len(self.cases), 20)
        for case_id in [f"SC-{number:03d}" for number in range(81, 101)]:
            with self.subTest(case_id=case_id):
                case = self.cases[case_id]
                predicate = case["expected"]["expected_invariants"][0]["predicate"]
                if case_id in {"SC-081", "SC-082", "SC-090", "SC-097"}:
                    result = accessibility_index(
                        ["healthcare"],
                        poi_coverage={"healthcare": "sample" if case_id == "SC-097" else "unknown"},
                        route_coverage={"healthcare": "unknown"},
                        source_version="osm:bounded-sample",
                    )
                    self.assertIsNone(result["value"])
                    if case_id == "SC-081":
                        self._has_predicate(case_id, "unknown_not_zero")
                    elif case_id == "SC-082":
                        self._has_predicate(case_id, "missing_is_unknown")
                    elif case_id == "SC-090":
                        self._has_predicate(case_id, "unknown_not_zero")
                    else:
                        self._has_predicate(case_id, "sample_not_citywide")
                elif case_id == "SC-083":
                    result = accessibility_index(
                        ["healthcare", "education"],
                        poi_coverage={"healthcare": "complete", "education": "partial"},
                        route_coverage={"healthcare": "complete", "education": "complete"},
                        routes_by_category={"healthcare": [{"method": "routed", "duration_s": 60}]},
                        source_version="osm:v1+valhalla:v1",
                    )
                    self.assertIsNone(result["value"])
                    self.assertEqual(result["coverage"]["coverage_ratio"], 0.5)
                    self._has_predicate(case_id, "null_below_declared_coverage_threshold")
                elif case_id == "SC-084":
                    result = accessibility_index(
                        ["childcare"],
                        poi_coverage={"childcare": "complete"},
                        route_coverage={"childcare": "complete"},
                        routes_by_category={"childcare": []},
                        source_version="osm:v1+valhalla:v1",
                    )
                    self.assertEqual(result["value"], 0.0)
                    self._has_predicate(case_id, "zero_only_if_query_coverage_is_complete")
                elif case_id in {"SC-085", "SC-086", "SC-087", "SC-098", "SC-100"}:
                    if case_id in {"SC-085", "SC-086"}:
                        record = next(
                            row
                            for row in self.records
                            if row["tags_raw"].get("amenity") == "clinic"
                        )
                    elif case_id == "SC-087":
                        record = next(
                            row
                            for row in self.records
                            if row["tags_raw"].get("amenity") == "school"
                        )
                    else:
                        record = next(row for row in self.records if row["category"] in {"healthcare", "education"})
                    self.assertIsNone(record["quality"])
                    if case_id in {"SC-085", "SC-086"}:
                        self.assertIsNone(record["ownership"])
                    if case_id == "SC-085":
                        self._has_predicate(case_id, "unknown_not_assumed_public_or_private")
                    elif case_id == "SC-086":
                        self._has_predicate(case_id, "unknown_not_zero_rating")
                    elif case_id == "SC-087":
                        self._has_predicate(case_id, "unknown_not_inferred_from_category")
                    elif case_id == "SC-098":
                        self._has_predicate(case_id, "do_not_create_rating")
                    else:
                        self._has_predicate(case_id, "category_is_not_quality_evidence")
                elif case_id == "SC-088":
                    record = next(row for row in self.records if row["osm_type"] == "node")
                    self.assertIsNone(record["entrance"])
                    self.assertEqual(poi_access_point(record).kind, "mapped_point_access_unknown")
                    self._has_predicate(case_id, "source_point_is_not_assumed_entrance")
                elif case_id == "SC-089":
                    record = next(row for row in self.records if row["osm_type"] in {"way", "relation"})
                    self.assertEqual(record["location"]["role"], "overpass_center")
                    self.assertTrue(record["location"]["approximate"])
                    self.assertEqual(poi_access_point(record).kind, "center_approx")
                    self._has_predicate(case_id, "true_for_overpass_center")
                elif case_id in {"SC-091", "SC-092", "SC-093", "SC-094"}:
                    record = self.records[0]
                    self.assertIsNone(record["district_id"])
                    self.assertIsNone(record["district_boundary_version"])
                    expected = predicate
                    self._has_predicate(case_id, expected)
                elif case_id in {"SC-095", "SC-096"}:
                    tags = (
                        {"amenity": "pharmacy", "shop": "supermarket"}
                        if "conflicting" in case["name"]
                        else {"amenity": "unmapped_facility"}
                    )
                    self.assertEqual(classify_osm_tags(tags), (None, None))
                    self._has_predicate(case_id, "unclassified_not_guessed")
                elif case_id == "SC-099":
                    record = self.records[0]
                    self.assertFalse(any("ml" in key.lower() for key in record))
                    self.assertIsNone(record["ownership"])
                    self._has_predicate(case_id, "no_ml_generated_label")
                else:
                    self.fail(f"no executable handler for {case_id}: {predicate}")


if __name__ == "__main__":
    unittest.main()
