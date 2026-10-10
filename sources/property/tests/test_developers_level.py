"""Source-linked Level catalog parsing and safety gates."""

import json
import tempfile
import unittest
from pathlib import Path

from sources.property.developer_harvest import Harvest
from sources.property.developers_level import discover_level_pages, parse_level_page


class LevelCatalogTest(unittest.TestCase):
    def test_source_next_ids_price_scope_and_media(self) -> None:
        url = "https://level.ru/api/filter/?city=moscow&limit=8&offset=8"
        page = json.dumps({
            "count": 1291, "count_without_alterplans": 1278,
            "next": "http://level.ru/api/filter/?city=moscow&limit=8&offset=16",
            "results": [
                {"pk": 10, "realty_pk": "10-0", "project_id": 36,
                 "project": "Проект", "project_short_slug": "project",
                 "building_id": 20, "building_name": "Корпус 1", "section_id": 4,
                 "floor": 2, "number": "101", "area": 36.2, "room": 1,
                 "price": 17000000, "show_price": True,
                 "plan": "https://cdn.level.ru/plan.svg"},
                {"pk": 11, "realty_pk": "10-1", "project_id": 36,
                 "building_id": 20, "price": 18000000, "show_price": False},
            ],
        })
        self.assertEqual(discover_level_pages(page, url), [{
            "url": "https://level.ru/api/filter/?city=moscow&limit=8&offset=16",
            "page_kind": "catalog",
        }])
        rows = parse_level_page(page, url, "2026-10-10T17:01:45Z", "sha")
        self.assertEqual([row["record_type"] for row in rows],
                         ["unit", "unit", "project", "corpus"])
        self.assertEqual([row["publisher_unit_id"] for row in rows[:2]], ["10", "11"])
        self.assertEqual(rows[0]["price_rub"], 17000000)
        self.assertIsNone(rows[1]["price_rub"])
        self.assertEqual(rows[0]["source_fields"]["location_scope"], "unknown")
        self.assertEqual(rows[0]["source_fields"]["catalog_count_without_alterplans"], 1278)
        self.assertEqual(rows[0]["media"][0]["role"], "apartment_plan")
        self.assertFalse(rows[0]["media"][0]["public_display_allowed"])

    def test_unrelated_next_never_enters_queue(self) -> None:
        url = "https://level.ru/api/filter/?city=moscow&limit=8&offset=8"
        page = json.dumps({"results": [], "next": "https://other.example/api/filter/?city=moscow&limit=8&offset=16"})
        self.assertEqual(discover_level_pages(page, url), [])

    def test_completion_counts_slots_and_distinct_units_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = root / "seeds.json"
            first_url = "https://level.ru/api/filter/?city=moscow&limit=8&offset=0"
            second_url = "https://level.ru/api/filter/?city=moscow&limit=8&offset=8"
            seed.write_text(json.dumps({"schema_version": "developer-harvest-seeds/1",
                                        "providers": {"level": {"allowed_hosts": ["level.ru"],
                                                                "seeds": [{"url": first_url,
                                                                           "page_kind": "catalog"}]}}}),
                            encoding="utf-8")
            base = {"project_id": 36, "building_id": 20, "show_price": True,
                    "price": 10000000}
            first = {"count": 3, "count_without_alterplans": 2,
                     "results": [{**base, "pk": 1, "plan": "https://cdn.level.ru/original.svg"},
                                 {**base, "pk": 2}],
                     "next": "http://level.ru/api/filter/?city=moscow&limit=8&offset=8"}
            second = {"count": 3, "count_without_alterplans": 2,
                      "results": [{**base, "pk": 1,
                                   "plan": "https://cdn.level.ru/alternate.svg"}],
                      "next": None}
            captures = []
            for index, (url, payload) in enumerate(((first_url, first),
                                                    (second_url, second))):
                path = root / f"page{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                captures.append({"provider": "level", "url": url,
                                 "page_kind": "catalog", "observed_at": "2026-10-10T17:00:00Z",
                                 "path": path.name})
            capture_manifest = root / "captures.json"
            capture_manifest.write_text(json.dumps({
                "schema_version": "developer-harvest-captures/1", "captures": captures,
            }), encoding="utf-8")
            harvest = Harvest(seed, root / "state")
            try:
                self.assertEqual(harvest.ingest_captures(capture_manifest), 2)
                counts = harvest.status()["providers"]["level"]
                self.assertEqual(counts["catalog_source_unit_slots"], 3)
                self.assertEqual(counts["catalog_distinct_unit_ids"], 2)
                self.assertEqual(counts["catalog_count_without_alterplans_claimed"], 2)
                self.assertTrue(counts["catalog_exhausted"])
                self.assertEqual(counts["catalog_pages_remaining"], 0)
            finally:
                harvest.close()


if __name__ == "__main__":
    unittest.main()
