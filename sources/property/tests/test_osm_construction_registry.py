import importlib.util
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from sources.property.osm_construction_registry import (
    classify_construction_tags,
    normalize_osm_area,
    source_record_id,
)


class OSMConstructionRegistryTests(unittest.TestCase):
    def test_only_explicit_apartments_and_residential_construction_are_candidates(self):
        self.assertEqual(
            classify_construction_tags(
                {"building": "construction", "construction": "apartments"}
            ),
            "building=construction AND construction=apartments",
        )
        self.assertEqual(
            classify_construction_tags(
                {"building": "construction", "construction": "residential"}
            ),
            "building=construction AND construction=residential",
        )

    def test_generic_nonresidential_and_inferred_tags_are_excluded(self):
        excluded = [
            {"building": "construction"},
            {"building": "construction", "construction": "school"},
            {"building": "construction", "construction": "dormitory"},
            {"building": "construction", "construction": "appartments"},
            {"building": "apartments", "construction": "apartments"},
            {"building": "residential", "construction": "apartments"},
            {"construction": "apartments"},
        ]
        for tags in excluded:
            with self.subTest(tags=tags):
                self.assertIsNone(classify_construction_tags(tags))

    def test_source_record_id_includes_osm_type_id_and_version(self):
        self.assertEqual(source_record_id("way", 123, 4), "way/123/v4")
        self.assertEqual(source_record_id("relation", 987, 2), "relation/987/v2")

    def test_invalid_osm_identifiers_are_rejected(self):
        for values in [("node", 1, 1), ("way", 0, 1), ("relation", 1, 0)]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    source_record_id(*values)

    @unittest.skipUnless(
        importlib.util.find_spec("shapely"),
        "install optional OSM dependencies to test geometry clipping",
    )
    def test_normalizer_clips_geometry_and_keeps_only_source_given_fields(self):
        from shapely import from_wkb
        from shapely.geometry import box

        area = SimpleNamespace(
            tags={
                "addr:street": "Синтетическая улица",
                "building": "construction",
                "construction": "apartments",
                "name": "СИНТЕТИЧЕСКИЙ DEMO ЖК",
            },
            timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
            from_way=lambda: True,
            orig_id=lambda: 777,
            version=2,
        )
        record = normalize_osm_area(
            area,
            box(37.0, 55.0, 37.2, 55.2),
            box(37.1, 55.1, 37.3, 55.3),
            observed_at="2026-10-09T12:00:00Z",
        )

        self.assertIsNotNone(record)
        assert record is not None
        geometry = from_wkb(record["geometry"])
        self.assertEqual(geometry.bounds, (37.1, 55.1, 37.2, 55.2))
        self.assertEqual(record["source_record_id"], "way/777/v2")
        self.assertEqual(record["record_mode"], "registry_status")
        self.assertEqual(record["data_mode"], "real")
        self.assertEqual(record["name"], "СИНТЕТИЧЕСКИЙ DEMO ЖК")
        self.assertEqual(json.loads(record["address_json"]), {"addr:street": "Синтетическая улица"})
        self.assertEqual(json.loads(record["raw_tags"]), area.tags)
        self.assertEqual(record["rights_status"], "cleared")
        self.assertNotIn("offer_id", record)
        self.assertNotIn("price", record)


if __name__ == "__main__":
    unittest.main()
