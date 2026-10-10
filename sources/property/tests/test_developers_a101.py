from __future__ import annotations

import json
import unittest

from sources.property.developers_a101 import discover_a101_pages, parse_a101_page


OBSERVED = "2026-10-10T15:00:00+00:00"
HASH = "a" * 64
CATALOG_URL = "https://a101.ru/kvartiry/"


class A101DeveloperParserTests(unittest.TestCase):
    def test_api_page_parses_unit_without_inventing_location_or_rights(self) -> None:
        payload = {
            "count": 4807,
            "next": "https://a101.ru/api/flats/?limit=18&offset=36&order=actual_price",
            "results": [{
                "id": 41001,
                "project_id": 17,
                "building_id": 901,
                "project_commercial": "Бунинские кварталы",
                "project_slug": "buninskiye-kvartaly",
                "building": "9.1",
                "number": 516,
                "floor": "15",
                "room": 0,
                "room_name": "Студия",
                "area": 21.8,
                "actual_price": 10756992,
                "price": 10900000,
                "big_layout": "https://cdn.a101.ru/plans/flat.svg",
                "visualization_slides": [],
            }],
        }
        rows = parse_a101_page(json.dumps(payload),
                               "https://a101.ru/api/flats/?limit=18&offset=18&order=actual_price",
                               OBSERVED, HASH)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["record_type"], row["publisher_unit_id"], row["publisher_project_id"]),
                         ("unit", "41001", "buninskiye-kvartaly"))
        self.assertEqual(row["source_fields"]["a101_project_id"], "17")
        self.assertEqual((row["rooms"], row["area_m2"], row["price_rub"]),
                         ("Студия", 21.8, 10756992))
        self.assertEqual(row["price_basis"], "unknown")
        self.assertEqual(row["promo_conditions"]["source_price_rub"], 10900000)
        self.assertIsNone(row["address"])
        self.assertIsNone(row["source_updated_at"])
        self.assertEqual(row["rights_status"], "unknown")
        self.assertEqual(row["media"][0]["role"], "apartment_plan")
        self.assertEqual(row["source_fields"]["location_scope"], "unknown")
        refs = discover_a101_pages(json.dumps(payload),
                                   "https://a101.ru/api/flats/?limit=18&offset=18&order=actual_price")
        self.assertEqual(refs, [{"url": payload["next"], "page_kind": "catalog"}])

    def test_html_discovery_keeps_project_links_and_ignores_unit_detail_nav(self) -> None:
        html = """<a href="/projects/">Все проекты</a>
          <a href="/projects/buninskiye-kvartaly">Бунинские кварталы</a>
          <a href="/projects/buninskiye-kvartaly/">same project with slash</a>
          <a href="https://a101.ru/projects/desnarechie/">Деснаречье</a>
          <a href="/kvartiry/49707">Лот не нужен: карточка уже в SSR</a>
          <a href="http://a101.ru/projects/not-https">bad</a>
          <a href="https://elsewhere.example/projects/foreign">bad</a>
          <a href="https://a101.ru@elsewhere.example/projects/credential">bad</a>"""
        refs = discover_a101_pages(html, CATALOG_URL)
        self.assertEqual(
            refs,
            [
                {"url": "https://a101.ru/projects/", "page_kind": "project"},
                {"url": "https://a101.ru/projects/buninskiye-kvartaly",
                 "page_kind": "project", "project_hint": "buninskiye-kvartaly"},
                {"url": "https://a101.ru/projects/desnarechie/",
                 "page_kind": "project", "project_hint": "desnarechie"},
            ],
        )
        self.assertTrue(all("address_hint" not in ref for ref in refs))

    def test_project_page_does_not_fan_out_to_filtered_catalog_links(self) -> None:
        html = """<a href="/kvartiry/?order=actual_price&room=0">Studio catalog</a>
          <a href="/kvartiry/?order=actual_price&room=1">One-room catalog</a>
          <a href="/projects/another-project">Other project navigation</a>"""
        self.assertEqual(discover_a101_pages(
            html, "https://a101.ru/projects/buninskiye-kvartaly"), [])

    def test_project_record_keeps_project_address_and_coords_separate_from_office(self) -> None:
        slug = "buninskiye-kvartaly"
        nodes: list[object] = [
            None,
            {"data": 2},
            {f"getProjectPageData_{slug}": 3},
            {"pageStartRes": 4},
            {
                "id": 8,
                "slug": slug,
                "name": "Бунинские кварталы",
                "commercial_name": "Жилой комплекс «Бунинские кварталы»",
                "commercial_short_name": "Бунинские кварталы",
                "address": "г. Москва, район Коммунарка",
                "coords": "55.541143,37.475495",
                "office": 5,
            },
            {"address": "г. Москва, ул. Вишнёвая, 91/1",
             "coordinates": "55.54593,37.512326"},
        ]
        html = ('<script id="__NUXT_DATA__" type="application/json">'
                + json.dumps(nodes, ensure_ascii=False)
                + '</script>')
        rows = parse_a101_page(html, f"https://a101.ru/projects/{slug}", OBSERVED, HASH)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["record_type"], "project")
        self.assertEqual(row["publisher_project_id"], slug)
        self.assertEqual(row["city"], "Москва")
        self.assertEqual(row["address"], "г. Москва, район Коммунарка")
        self.assertEqual(row["coordinates"], {"latitude": 55.541143, "longitude": 37.475495})
        self.assertEqual(row["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual(row["source_fields"]["project_coordinates_scope"], "project")
        self.assertEqual(row["rights_status"], "unknown")

    def test_visible_cash_label_is_used_only_for_single_offer_detail(self) -> None:
        offer = {
            "id": 52001,
            "building": "3",
            "area": 40,
            "project_commercial": "Тестовый проект",
            "room": 2,
            "room_name": "Евро-двухкомнатная",
            "actual_price": 10000000,
            "price": 12000000,
        }
        # Minimal Nuxt references; numeric properties exceed this node-table size
        # and therefore are direct scalar values, while container objects use refs.
        nodes: list[object] = [
            None,
            {"data": 2},
            {"getLotPageData_52001": 3},
            {"pageDataRes": 4},
            offer,
        ]
        html = ('<script id="__NUXT_DATA__" type="application/json">'
                + json.dumps(nodes, ensure_ascii=False)
                + '</script><body>Оплата 100% 10 000 000 ₽</body>')
        rows = parse_a101_page(html, "https://a101.ru/kvartiry/52001", OBSERVED, HASH)
        # Nuxt indexing is intentionally tested through the parsed detail record.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_basis"], "cash_100_percent")
        self.assertEqual(rows[0]["promo_conditions"]["displayed_selector"], "Оплата 100%")


if __name__ == "__main__":
    unittest.main()
