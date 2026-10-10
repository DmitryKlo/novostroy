"""Tests for bounded social batch enrichment and the expanded POI taxonomy."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from sources.social.accessibility import AccessPoint
from sources.social.enrichment import enrich_social_batch
from sources.social.taxonomy import SOCIAL_CATEGORIES, classify_criteria_tags


ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "data/social/osm_overpass_poi_normalized.json"
SAMPLE_HASH = "e3d4f0dcea718b820a23cb1f9644297bb989f2ed05ec1aa084db6092f3e7ff0e"


def _poi(
    osm_type: str,
    osm_id: int,
    lon: float,
    lat: float,
    tags: dict[str, Any],
    *,
    entrance: bool = False,
    station_name: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": "openstreetmap",
        "source_kind": "osm",
        "source_id": f"{osm_type}/{osm_id}",
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_timestamp": "2026-10-09T09:00:00Z",
        "observed_at": "2026-10-09T09:21:12Z",
        "tags_raw": tags,
        "location": {
            "type": "Point",
            "coordinates": [lon, lat],
            "role": "osm_node",
            "approximate": False,
        },
        "entrance": None,
    }
    if entrance:
        row["entrance"] = {
            "coordinates": [lon, lat],
            "access_kind": "entrance_known",
        }
    if station_name is not None:
        row["metro_station_name"] = station_name
        row["metro_station_source_id"] = "node/900"
        row["metro_station_link_source_id"] = "relation/901"
    return row


class FakePedestrianRouter:
    def __init__(self, durations: dict[str, float] | None = None):
        self.durations = durations or {}
        self.calls: list[tuple[AccessPoint, AccessPoint, bool]] = []

    def route(
        self,
        origin: AccessPoint,
        destination: AccessPoint,
        *,
        allow_approximate: bool = False,
    ) -> dict[str, Any]:
        self.calls.append((origin, destination, allow_approximate))
        if (origin.kind != "entrance_known" or destination.kind != "entrance_known") and not allow_approximate:
            raise ValueError("approximate access point not allowed")
        source_id = destination.source_id or ""
        return {
            "method": "routed",
            "provider": "fake-test-router",
            "duration_s": self.durations.get(source_id, 600),
            "distance_m": 800,
            "observed_at": "2026-10-09T10:00:00Z",
        }


class FakeTransitRouter:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[tuple[AccessPoint, AccessPoint, tuple[str, ...], bool]] = []

    def route_transit(
        self,
        origin: AccessPoint,
        destination: AccessPoint,
        *,
        allowed_modes: tuple[str, ...],
        require_direct: bool,
    ) -> dict[str, Any]:
        self.calls.append((origin, destination, allowed_modes, require_direct))
        return dict(self.response)


class SocialTaxonomyTests(unittest.TestCase):
    def test_category_taxonomy_is_narrow_and_stable(self):
        self.assertEqual(
            classify_criteria_tags({"amenity": "clinic"}),
            ("clinic",),
        )
        self.assertEqual(
            classify_criteria_tags({"amenity": "hospital"}),
            (),
        )
        self.assertEqual(
            classify_criteria_tags({"shop": "convenience"}),
            (),
        )
        self.assertEqual(
            classify_criteria_tags(
                {"public_transport": "platform", "bus": "yes", "tram": "yes"}
            ),
            ("bus_stop", "tram_stop"),
        )
        self.assertEqual(
            classify_criteria_tags({"railway": "subway_entrance"}),
            ("metro_entrance",),
        )
        self.assertEqual(len(SOCIAL_CATEGORIES), len(set(SOCIAL_CATEGORIES)))

    def test_existing_normalized_50_row_sample_is_consumed_without_rewrite(self):
        rows = json.loads(SAMPLE.read_text(encoding="utf-8"))
        result = enrich_social_batch(
            [{"complex_id": "cx-1", "access_point": AccessPoint((37.61, 55.755), "center_approx")}],
            rows,
            poi_source_version=f"osm-sha256:{SAMPLE_HASH}",
            poi_coverage="sample",
        )
        self.assertEqual(len(rows), 50)
        self.assertEqual(result[0]["complex_id"], "cx-1")
        self.assertIsNone(result[0]["facts"]["walk_time_clinic_s"]["value"])
        self.assertEqual(result[0]["facts"]["walk_time_clinic_s"]["status"], "unknown")
        self.assertEqual(
            result[0]["coverage"]["poi_source_version"],
            f"osm-sha256:{SAMPLE_HASH}",
        )
        self.assertEqual(result[0]["coverage"]["poi_coverage"], "sample")
        self.assertIsNone(result[0]["coverage"]["accessibility_index"])
        criteria = json.loads(
            (ROOT / "docs/criteria/social.json").read_text(encoding="utf-8")
        )["criteria"]
        self.assertEqual({row["key"] for row in criteria}, set(result[0]["facts"]))
        self.assertTrue(all(row["coverage_requirement"] == "complete" for row in criteria))


class SocialBatchTests(unittest.TestCase):
    def setUp(self):
        self.origin = {
            "complex_id": "complex-a",
            "access_point": AccessPoint((37.6000, 55.7500), "entrance_known", "complex-a/main-entry"),
        }

    def test_no_router_means_no_route_calls_and_no_straight_line_time(self):
        poi = _poi("node", 10, 37.6010, 55.7500, {"amenity": "pharmacy"})
        output = enrich_social_batch(
            [self.origin],
            [poi],
            poi_source_version="osm:test-v1",
            poi_coverage="unknown",
        )[0]
        fact = output["facts"]["walk_time_pharmacy_s"]
        self.assertIsNone(fact["value"])
        self.assertEqual(fact["status"], "unknown")
        self.assertIn("distance_police_straight_line_m", output["facts"])
        self.assertNotIn("walk_time_minutes", output["facts"])
        self.assertIsNone(output["facts"]["distance_police_straight_line_m"]["value"])

    def test_explicit_pedestrian_routes_are_seconds_and_sample_scoped(self):
        pharmacy = _poi("node", 10, 37.6010, 55.7500, {"amenity": "pharmacy"}, entrance=True)
        router = FakePedestrianRouter({"node/10": 480})
        result = enrich_social_batch(
            [self.origin],
            [pharmacy],
            pedestrian_router=router,
            poi_source_version="osm:test-v1",
            poi_coverage="unknown",
            max_pedestrian_requests=1,
        )[0]
        fact = result["facts"]["walk_time_pharmacy_s"]
        self.assertEqual(fact["value"], 480.0)
        self.assertEqual(fact["unit"], "s")
        self.assertEqual(fact["status"], "derived")
        self.assertEqual(fact["provenance"]["coverage_status"], "unknown")
        self.assertEqual(fact["scope"], "nearest_routed_pharmacy_within_candidate_scope")
        self.assertEqual(len(router.calls), 1)

    def test_approximate_source_point_requires_explicit_opt_in(self):
        pharmacy = _poi("node", 10, 37.6010, 55.7500, {"amenity": "pharmacy"})
        router = FakePedestrianRouter()
        first = enrich_social_batch(
            [self.origin],
            [pharmacy],
            pedestrian_router=router,
            max_pedestrian_requests=1,
        )[0]
        self.assertEqual(router.calls, [])
        self.assertIsNone(first["facts"]["walk_time_pharmacy_s"]["value"])

        second = enrich_social_batch(
            [self.origin],
            [pharmacy],
            pedestrian_router=router,
            max_pedestrian_requests=1,
            allow_approximate_routes=True,
        )[0]
        self.assertEqual(len(router.calls), 1)
        fact = second["facts"]["walk_time_pharmacy_s"]
        self.assertEqual(fact["provenance"]["target_access_kind"], "mapped_point_access_unknown")
        self.assertTrue(fact["provenance"]["approximate"])

    def test_distance_field_is_straight_line_and_sample_labeled(self):
        police = _poi("node", 11, 37.6010, 55.7500, {"amenity": "police"})
        result = enrich_social_batch(
            [self.origin],
            [police],
            poi_source_version="osm:test-v1",
            poi_coverage="unknown",
        )[0]
        fact = result["facts"]["distance_police_straight_line_m"]
        self.assertGreater(fact["value"], 0)
        self.assertEqual(fact["unit"], "m")
        self.assertEqual(fact["status"], "derived")
        self.assertEqual(fact["scope"], "nearest_observed_police_station_in_source_sample")
        self.assertEqual(fact["provenance"]["method"], "haversine_straight_line")
        self.assertIsNone(result["facts"]["walk_time_clinic_s"]["value"])

    def test_metro_station_name_requires_explicit_station_join_and_pedestrian_route(self):
        entrance = _poi(
            "node", 20, 37.601, 55.750, {"railway": "subway_entrance"}, station_name="Станция А"
        )
        router = FakePedestrianRouter({"node/20": 300})
        result = enrich_social_batch(
            [self.origin],
            [entrance],
            pedestrian_router=router,
            max_pedestrian_requests=1,
            poi_source_version="osm:test-v1",
            poi_coverage="unknown",
        )[0]
        self.assertEqual(result["facts"]["nearest_metro_name"]["value"], "Станция А")
        self.assertEqual(
            result["facts"]["nearest_metro_name"]["provenance"]["source_record_id"],
            "relation/901",
        )
        self.assertEqual(result["facts"]["walk_time_metro_s"]["value"], 300.0)
        self.assertEqual(result["facts"]["walk_time_metro_s"]["provenance"]["target_access_kind"], "entrance_known")

        unlinked = _poi("node", 21, 37.602, 55.750, {"railway": "subway_entrance"})
        result = enrich_social_batch(
            [self.origin],
            [unlinked],
            pedestrian_router=FakePedestrianRouter(),
            max_pedestrian_requests=1,
            poi_coverage="unknown",
        )[0]
        self.assertIsNone(result["facts"]["nearest_metro_name"]["value"])

    def test_direct_transit_is_not_inferred_from_nearby_stop(self):
        metro = _poi("node", 20, 37.601, 55.750, {"railway": "subway_entrance"})
        bus = _poi("node", 30, 37.6005, 55.750, {"highway": "bus_stop"})
        result = enrich_social_batch(
            [self.origin],
            [metro, bus],
            poi_coverage="unknown",
        )[0]
        self.assertIsNone(result["facts"]["walk_time_bus_stop_s"]["value"])
        self.assertIsNone(result["facts"]["metro_direct_route"]["value"])
        self.assertIsNone(result["facts"]["metro_transit_bus_tram_time_s"]["value"])

    def test_only_explicit_provider_direct_zero_transfer_bus_tram_route_is_accepted(self):
        metro = _poi("node", 20, 37.601, 55.750, {"railway": "subway_entrance"})
        direct = FakeTransitRouter(
            {
                "direct": True,
                "direct_checked": True,
                "transfer_count": 0,
                "modes": ["pedestrian", "tram"],
                "duration_s": 900,
                "provider": "fake-transit",
                "observed_at": "2026-10-09T10:00:00Z",
            }
        )
        result = enrich_social_batch(
            [self.origin],
            [metro],
            transit_router=direct,
            max_transit_requests=1,
            allow_approximate_routes=False,
        )[0]
        self.assertEqual(result["facts"]["metro_transit_bus_tram_time_s"]["value"], 900.0)
        self.assertTrue(result["facts"]["metro_direct_route"]["value"])
        self.assertIn("tram", result["facts"]["metro_transit_modes"]["value"])
        self.assertTrue(direct.calls[0][3])

        indirect = FakeTransitRouter(
            {
                "direct": True,
                "direct_checked": True,
                "transfer_count": 1,
                "modes": ["bus", "metro"],
                "duration_s": 1200,
            }
        )
        result = enrich_social_batch(
            [self.origin],
            [metro],
            transit_router=indirect,
            max_transit_requests=1,
        )[0]
        self.assertIsNone(result["facts"]["metro_transit_bus_tram_time_s"]["value"])

    def test_oms_and_dms_are_independent_sourced_facts(self):
        clinic = _poi("node", 40, 37.601, 55.750, {"amenity": "clinic"}, entrance=True)
        router = FakePedestrianRouter()
        source_provenance = {
            "source_id": "official-moscow-oms-program",
            "source_record_id": "clinic-code-123",
            "source_field": "appendix3.oms_participation",
            "source_updated_at": "2026-01-01",
            "rights_status": "official public document",
        }
        metadata = {
            "node/40": {
                "ownership_type": {"value": "private", "unit": None, "status": "observed", "observed_at": "2026-01-02", "provenance": source_provenance},
                "oms_participation": {"value": True, "unit": None, "status": "observed", "observed_at": "2026-01-02", "provenance": source_provenance},
                "has_paid_services": {"value": True, "unit": None, "status": "observed", "observed_at": "2026-01-02", "provenance": source_provenance},
                "dms_networks": {"value": [{"insurer": "test insurer", "product": "test plan"}], "unit": None, "status": "observed", "observed_at": "2026-01-02", "provenance": {**source_provenance, "source_id": "official-insurer-directory"}},
            }
        }
        result = enrich_social_batch(
            [self.origin],
            [clinic],
            pedestrian_router=router,
            max_pedestrian_requests=1,
            allow_approximate_routes=False,
            clinic_metadata_by_source_id=metadata,
            poi_coverage="complete",
            category_coverage={"clinic": "complete"},
        )[0]
        facts = result["facts"]
        self.assertEqual(facts["nearest_clinic_ownership_type"]["value"], "private")
        self.assertTrue(facts["nearest_clinic_oms_participation"]["value"])
        self.assertTrue(facts["nearest_clinic_has_paid_services"]["value"])
        self.assertEqual(facts["nearest_clinic_dms_networks"]["value"][0]["product"], "test plan")
        self.assertNotEqual(
            facts["nearest_clinic_oms_participation"]["provenance"]["source_id"],
            facts["nearest_clinic_dms_networks"]["provenance"]["source_id"],
        )

    def test_request_limits_are_validated(self):
        with self.assertRaises(ValueError):
            enrich_social_batch([self.origin], [], max_pedestrian_requests=-1)
        with self.assertRaises(ValueError):
            enrich_social_batch([self.origin], [], candidate_limit_per_category=0)


if __name__ == "__main__":
    unittest.main()
