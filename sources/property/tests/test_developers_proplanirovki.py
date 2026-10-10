from __future__ import annotations

import json
import unittest
from pathlib import Path

from sources.property.developers_proplanirovki import (
    discover_proplanirovki_pages,
    parse_proplanirovki_page,
)


OBSERVED = "2026-10-10T15:00:00+00:00"
HASH = "b" * 64


class ProplanirovkiParserTests(unittest.TestCase):
    def test_catalog_page_keeps_aggregator_minimum_and_discovers_details_and_next_page(self) -> None:
        url = "https://proplanirovki.ru/api/catalog?region=msk&page=1&limit=2"
        payload = {
            "page": 1, "limit": 2, "total": 5,
            "items": [
                {"id": "a" * 24, "name": "Тестовый ЖК", "address": "Москва, ул. Тестовая",
                 "district": "САО", "builder": "Застройщик из карточки",
                 "cover": "https://cdn-dataout.trendagent.ru/images/test.jpg",
                 "priceFrom": 10_000_000, "pricePerSqmFrom": 250_000,
                 "areaFrom": 40, "areaTo": 80, "apartments": 12,
                 "roomsPrices": [{"label": "1-к.кв", "priceFrom": 10_000_000}],
                 "deadlineFrom": "2027-12-31T12:00:00.000Z"},
                {"id": "c" * 24, "name": "Областной ЖК", "address": "г. Химки, ул. Полевая",
                 "district": "ГО Химки, МО", "builder": "ООО СЗ",
                 "priceFrom": 9_000_000},
            ],
        }

        rows = parse_proplanirovki_page(json.dumps(payload), url, OBSERVED, HASH)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_fields"]["source_project_id"], "a" * 24)
        self.assertIsNone(rows[0].get("publisher_project_id"))
        self.assertEqual(rows[0]["project_name"], "Тестовый ЖК")
        self.assertEqual(rows[0]["price_from_rub"], 10_000_000)
        self.assertEqual(rows[0]["price_per_sqm_from_rub_m2"], 250_000)
        self.assertEqual(rows[0]["source_fields"]["aggregator_builder_label"], "Застройщик из карточки")
        self.assertEqual(rows[0]["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual(rows[1]["source_fields"]["location_scope"], "out_of_scope")
        self.assertEqual(rows[0]["media"][0]["role"], "project_cover")
        self.assertEqual(rows[0]["rights_status"], "unknown")

        refs = discover_proplanirovki_pages(json.dumps(payload), url)
        self.assertEqual(refs[0], {"url": "https://proplanirovki.ru/api/catalog/" + "a" * 24,
                                   "page_kind": "project"})
        self.assertEqual(refs[1]["url"], "https://proplanirovki.ru/api/catalog/" + "c" * 24)
        self.assertEqual(refs[2], {"url": "https://proplanirovki.ru/api/catalog?region=msk&page=2&limit=2",
                                   "page_kind": "catalog"})

    def test_project_payload_emits_project_corpus_and_unit_with_separate_source_dates(self) -> None:
        project_id = "d" * 24
        building_id = "e" * 24
        unit_id = "f" * 24
        url = f"https://proplanirovki.ru/api/catalog/{project_id}"
        payload = {
            "id": project_id, "name": "Эко Тест", "address": "деревня Столбово, проспект Куприна",
            "region": "msk", "district": "Новая Москва", "class": "standard",
            "renderer": ["https://cdn-dataout.trendagent.ru/images/render.jpg"],
            "plan": ["https://cdn-dataout.trendagent.ru/images/site-plan.jpg"],
            "coords": [37.4739, 55.5400],
            "buildings": [{"id": building_id, "name": "13", "queue": "2",
                           "deadline": "2026-12-31T12:00:00.000Z",
                           "geo": {"type": "Polygon", "coordinates": [[
                               [37.4738, 55.5399], [37.4740, 55.5399],
                               [37.4740, 55.5401], [37.4738, 55.5399]]]}}],
            "apartmentsTotal": 1, "updatedAt": "2026-09-14T07:17:39.607Z",
            "apartments": [{"id": unit_id, "buildingId": building_id,
                            "rooms": 0, "roomCode": 0, "roomLabel": "Студия",
                            "floor": 5, "floors": 12, "number": "708",
                            "areaTotal": 24.49, "price": 8_877_910, "priceBase": None,
                            "plan": ["https://cdn-dataout.trendagent.ru/images/unit-plan.png"],
                            "deadline": "2026-12-31T12:00:00.000Z", "finishing": "Без стен",
                            "saleStatus": None, "updatedAt": "2026-09-14T07:18:30.698Z"}],
        }

        rows = parse_proplanirovki_page(json.dumps(payload), url, OBSERVED, HASH)
        self.assertEqual([row["record_type"] for row in rows], ["project", "corpus", "unit"])
        project, corpus, unit = rows
        self.assertEqual(project["publisher_project_id"], project_id)
        self.assertEqual(project["source_updated_at"], "2026-09-14T07:17:39.607Z")
        self.assertEqual(project["coordinates"], {"longitude": 37.4739, "latitude": 55.54})
        self.assertEqual(project["source_fields"]["source_updated_at_basis"], "explicit_project")
        self.assertEqual(project["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual({item["role"] for item in project["media"]},
                         {"project_rendering", "project_plan"})
        self.assertEqual(corpus["publisher_building_id"], building_id)
        self.assertEqual(corpus["corpus"], "13")
        self.assertEqual(corpus["structure"], "2")
        self.assertTrue(corpus["source_fields"]["source_geo_present"])
        self.assertEqual(corpus["source_geometry"], payload["buildings"][0]["geo"])
        self.assertEqual(corpus["source_fields"]["geometry_basis"], "aggregator_unverified")
        self.assertEqual(corpus["source_fields"]["geometry_validation_status"], "accepted")
        self.assertEqual(corpus["source_fields"]["source_updated_at_basis"], "inherited_project")
        self.assertEqual(unit["publisher_unit_id"], unit_id)
        self.assertEqual(unit["publisher_project_id"], project_id)
        self.assertEqual(unit["publisher_building_id"], building_id)
        self.assertEqual((unit["unit_number"], unit["floor"], unit["area_m2"], unit["price_rub"]),
                         ("708", 5, 24.49, 8_877_910))
        self.assertEqual(unit["source_updated_at"], "2026-09-14T07:18:30.698Z")
        self.assertEqual(unit["source_fields"]["source_updated_at_basis"], "explicit_unit")
        self.assertEqual(unit["source_fields"]["price_field_used"], "apartments[].price")
        self.assertEqual(unit["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual(unit["source_fields"]["availability_claim"],
                         "listed in aggregator project inventory; current availability not independently verified")
        self.assertEqual(unit["media"][0]["role"], "apartment_plan")
        self.assertEqual(unit["media"][0]["rights_status"], "unknown")

    def test_invalid_source_geometry_and_inherited_unit_date_are_explicit(self) -> None:
        project_id, building_id, unit_id = "p" * 24, "b" * 24, "u" * 24
        url = f"https://proplanirovki.ru/api/catalog/{project_id}"
        payload = {
            "id": project_id, "name": "Тест", "address": "Москва, ул. Полевая",
            "district": "Москва", "coords": [37.5, 55.5],
            "updatedAt": "2026-09-14T07:17:39.607Z",
            "buildings": [{"id": building_id, "name": "1", "geo": {
                "type": "Polygon", "coordinates": [[
                    [37.499, 55.499], [37.501, 55.501],
                    [37.499, 55.501], [37.501, 55.499], [37.499, 55.499],
                ]]}}],
            "apartments": [{"id": unit_id, "buildingId": building_id,
                            "number": "1", "areaTotal": 30, "price": 10_000_000}],
        }
        project, corpus, unit = parse_proplanirovki_page(json.dumps(payload), url, OBSERVED, HASH)
        self.assertIsNone(corpus["source_geometry"])
        self.assertEqual(corpus["source_fields"]["geometry_validation_status"], "invalid_topology")
        self.assertEqual(unit["source_updated_at"], project["source_updated_at"])
        self.assertEqual(unit["source_fields"]["source_updated_at_basis"], "inherited_project")

    def test_real_cached_project_geometry_if_available(self) -> None:
        cache = (Path(__file__).resolve().parents[3] / ".runtime/research/developer-harvest/"
                 "developer-harvest-65f70f242a8ebc8c/cache")
        if not cache.is_dir():
            self.skipTest("local captured public API cache is absent")
        observations = cache.parent / "observations.jsonl"
        selected = None
        with observations.open(encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("record_type") == "project" and row.get("project_name") == "Бунинские кварталы" \
                        and row.get("publisher_project_id"):
                    selected = row
                    break
        if selected is None:
            self.skipTest("captured project is absent")
        body = (cache / f"{selected['raw_sha256']}.body").read_text(encoding="utf-8")
        rows = parse_proplanirovki_page(body, selected["source_page_url"], OBSERVED,
                                        selected["raw_sha256"])
        invalid = [row for row in rows if row["record_type"] == "corpus"
                   and row["corpus"] == "5.2"]
        self.assertEqual(len(invalid), 1)
        self.assertIsNone(invalid[0]["source_geometry"])
        self.assertEqual(invalid[0]["source_fields"]["geometry_validation_status"],
                         "invalid_topology")
        self.assertTrue(any(row["record_type"] == "corpus" and row.get("source_geometry")
                            for row in rows))

    def test_ambiguous_location_and_unlinked_or_foreign_pages_remain_untrusted(self) -> None:
        project_id = "g" * 24
        url = f"https://proplanirovki.ru/api/catalog/{project_id}"
        payload = {"id": project_id, "name": "Неясный ЖК", "address": "ул. Лесная",
                   "region": "msk", "district": "Лесной", "apartments": [], "buildings": []}
        rows = parse_proplanirovki_page(json.dumps(payload), url, OBSERVED, HASH)
        self.assertEqual(rows[0]["source_fields"]["location_scope"], "unknown")
        self.assertEqual(rows[0]["source_fields"]["location_scope_basis"],
                         "aggregator locality does not establish city-versus-region scope")
        self.assertEqual(parse_proplanirovki_page(json.dumps(payload),
                                                   "http://proplanirovki.ru/api/catalog/" + project_id,
                                                   OBSERVED, HASH), [])
        self.assertEqual(discover_proplanirovki_pages(json.dumps(payload),
                                                       "https://other.example/api/catalog?region=msk"), [])

        conflict_id = "h" * 24
        conflict = {"id": conflict_id, "name": "Спорный ЖК", "address": "Москва, ул. Тестовая",
                    "region": "msk", "district": "ГО Химки, МО", "apartments": [], "buildings": []}
        conflict_rows = parse_proplanirovki_page(
            json.dumps(conflict), f"https://proplanirovki.ru/api/catalog/{conflict_id}", OBSERVED, HASH)
        self.assertEqual(conflict_rows[0]["source_fields"]["location_scope"], "unknown")
        self.assertEqual(conflict_rows[0]["source_fields"]["location_scope_basis"],
                         "aggregator address, district, and region conflict")


if __name__ == "__main__":
    unittest.main()
