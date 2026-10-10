"""Source-only checks for official developer observations and coverage gates."""

from __future__ import annotations

import json
import unittest

from sources.property.developers_brusnika import (
    discover_brusnika_pages,
    parse_brusnika_page,
)
from sources.property.developers_granel import (
    discover_granel_pages,
    parse_granel_page,
)


WHEN = "2026-10-10T12:00:00+00:00"
SHA = "a" * 64


class GranelParserTests(unittest.TestCase):
    def test_project_page_keeps_geometry_and_facade_unknown(self) -> None:
        url = "https://granelle.ru/objects/mypriority-mnevniki/"
        html = """<html><head><title>ЖК «Мнёвники от Гранель»: квартиры</title>
          <script type='application/ld+json'>
          {"@graph":[{"@type":"ApartmentComplex","name":"Мнёвники от Гранель"}]}
          </script></head><body>463 квартиры
          <img alt='Совершенство, воплощенное в архитектуре'
           src='https://storage.yandexcloud.net/granelleprod/media/projects_content/business_features/image/a.jpg'>
          </body></html>"""
        row = parse_granel_page(html, url, WHEN, SHA)[0]
        self.assertEqual(row["record_type"], "project")
        self.assertEqual(row["publisher_project_id"], "mypriority-mnevniki")
        self.assertIsNone(row["address"])
        self.assertEqual(row["source_fields"]["catalog_total_claimed"], 463)
        self.assertEqual(row["media"][0]["role"], "unknown")

    def test_public_api_preserves_full_payment_basis_and_unit_identity(self) -> None:
        url = "https://granelle.ru/api/flats/?limit=24&offset=24&project=mypriority-mnevniki"
        payload = {
            "count": 463,
            "next": "http://granelle.ru/api/flats/?limit=24&offset=48&project=mypriority-mnevniki",
            "results": [
                {"id": 51229, "type": "flat", "status": 1,
                 "project": "Мнёвники от Гранель", "project_slug": "mypriority-mnevniki",
                 "building": "1", "section": 7, "floor": 19, "floor_count": 27,
                 "number": "1452", "rooms": 0, "area": 28.06,
                 "price": 20740592.62, "current_price": "17777650.81",
                 "show_price_discounted": True, "profit_full_payment": 14,
                 "profit_full_payment_until_date": "2026-10-31T23:00:00+03:00",
                 "plan_png": "https://storage.yandexcloud.net/granelleprod/media/properties/property/plan/a.png",
                 "floor_plan": "https://storage.yandexcloud.net/granelleprod/media/buildings/floor/plan/b.svg"},
                {"id": 51230, "type": "flat", "status": 0,
                 "project": "Мнёвники от Гранель", "project_slug": "mypriority-mnevniki",
                 "current_price": "15000000", "area": 28.0},
            ],
        }
        rows = parse_granel_page(json.dumps(payload), url, WHEN, SHA)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["publisher_unit_id"], "51229")
        self.assertEqual(rows[0]["publisher_project_id"], "mypriority-mnevniki")
        self.assertEqual(rows[0]["price_rub"], 17777651)
        self.assertEqual(rows[0]["source_updated_at"], None)
        self.assertIn("100%", rows[0]["price_basis"])
        self.assertEqual(rows[0]["source_fields"]["catalog_total_claimed"], 463)
        self.assertEqual({item["role"] for item in rows[0]["media"]}, {"apartment_plan", "floor_plan"})
        self.assertEqual(rows[0]["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertIsNone(rows[1]["price_rub"])
        self.assertEqual(rows[1]["source_fields"]["availability_scope"], "unverified_or_inactive")
        self.assertEqual(discover_granel_pages(json.dumps(payload), url)[0]["url"],
                         "https://granelle.ru/api/flats/?limit=24&offset=48&project=mypriority-mnevniki")

    def test_catalog_keeps_mixed_scope_and_remaining_count(self) -> None:
        html = """<html><body>
          <a href='/flats/mypriority-mnevniki/50707'><h2 class='title_A'>Студия 28.19 м²</h2>
            <span class='priceLabel_A'>от 16 232 550 ₽</span><div class='project_A'>Мнёвники от Гранель</div>
            <li>корпус 1</li><li>этаж 2 из 27</li>
            <img src='https://storage.yandexcloud.net/granelleprod/media/properties/property/plan/a.svg'>
          </a>
          <a href='/flats/mytishi/65129'><h2 class='title_A'>3-комн. 76.69 м²</h2>
            <span class='priceLabel_A'>от 16 158 264 ₽</span><div class='project_A'>Гранель Мытищи</div>
            <li>корпус 1</li><li>этаж 8 из 22</li>
          </a><div>Показать еще 24 из 461</div>
          <script>var x={pagination:{next:"http:\\u002F\\u002Fgranelle.ru\\u002Fapi\\u002Fflats\\u002F?limit=24&offset=24&project=mypriority-mnevniki"}}</script>
          </body></html>"""
        rows = parse_granel_page(html, "https://granelle.ru/flats/", WHEN, SHA)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["price_rub"], 16232550)
        self.assertEqual(rows[0]["floor"], 2)
        self.assertEqual(rows[0]["source_fields"]["catalog_total_claimed"], 463)
        self.assertEqual(rows[1]["source_fields"]["location_scope"], "out_of_scope")
        self.assertIsNone(rows[1]["address"])
        refs = discover_granel_pages(html, "https://granelle.ru/flats/")
        self.assertTrue(any(ref["page_kind"] == "catalog" and "/api/flats/" in ref["url"] for ref in refs))


class BrusnikaParserTests(unittest.TestCase):
    def test_project_api_crosswalk_keeps_marker_and_imagery_unverified(self) -> None:
        url = "https://moskva.brusnika.ru/api/projects/"
        payload = [
            {"id": 83, "slug": "izdanie", "name": "Квартал «Издание»",
             "url": "https://moskva.brusnika.ru/projects/izdanie/",
             "commercial_complex_id": 46, "longitude": "37.748086", "latitude": "55.736973",
             "preview": "https://cdn.brusnika.ru/media/complex/preview/a.jpg",
             "card_slides": [{"original": "https://cdn.brusnika.ru/media/realty/complex_card_slide/original/b.jpg"}]},
            {"id": 11, "slug": "perviy", "name": "Первый квартал",
             "url": "https://moskva.brusnika.ru/projects/perviy/"},
            {"id": 92, "slug": "gertzena", "name": "Квартал Герцена",
             "url": "https://unrelated.example/projects/gertzena/"},
        ]
        rows = parse_brusnika_page(json.dumps(payload), url, WHEN, SHA)
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row["publisher_project_id"], "83")
        self.assertEqual(row["source_fields"]["commercial_complex_id"], 46)
        self.assertEqual(row["source_fields"]["location_scope"], "unknown")
        self.assertIsNone(row["source_fields"]["scope_evidence_basis"])
        self.assertEqual(row["source_fields"]["marketing_project_marker"]["coordinate_basis"],
                         "publisher_project_map_marker_not_building_location")
        self.assertIsNone(row["address"])
        self.assertEqual({item["role"] for item in row["media"]}, {"unknown"})
        self.assertTrue(all(item["rights_status"] == "unknown" for item in row["media"]))
        self.assertEqual(rows[1]["source_fields"]["location_scope"], "out_of_scope")
        self.assertEqual(discover_brusnika_pages(json.dumps(payload), url),
                         [{"url": payload[0]["url"], "page_kind": "project", "project_hint": "izdanie"}])

    def test_project_detail_keeps_city_scope_separate_from_building_address(self) -> None:
        url = "https://moskva.brusnika.ru/projects/metronom/"
        html = """<html><head><title>Квартал «Метроном» – официальный сайт проекта компании Брусника в Москве</title>
          <meta name='description' content='Квартал «Метроном» – первый проект Брусники в Москве.'>
          </head><body><img alt='Квартал «Метроном»'
          src='https://cdn.brusnika.ru/media/complex/preview/a.jpg'></body></html>"""
        row = parse_brusnika_page(html, url, WHEN, SHA)[0]
        self.assertEqual(row["publisher_project_id"], "79")
        self.assertEqual(row["project_name"], "Квартал «Метроном»")
        self.assertEqual(row["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertIsNone(row["address"])
        self.assertEqual(row["media"][0]["role"], "unknown")

    def test_project_page_does_not_turn_architecture_image_into_facade_photo(self) -> None:
        url = "https://moskva.brusnika.ru/projects/dom-a/"
        html = """<html><head><title>Дом «А» - официальный сайт проекта</title></head><body>
          74 квартиры <img alt='Здание'
          src='https://cdn.brusnika.ru/media/complex/preview/a.jpg'></body></html>"""
        row = parse_brusnika_page(html, url, WHEN, SHA)[0]
        self.assertEqual(row["publisher_project_id"], "87")
        self.assertEqual(row["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual(row["source_fields"]["catalog_total_claimed"], 74)
        self.assertEqual(row["media"][0]["role"], "unknown")
        self.assertIsNone(row["address"])

    def test_public_api_excludes_booked_and_region_project(self) -> None:
        url = "https://moskva.brusnika.ru/api/filter/flats/"
        payload = {"count": 424,
                   "next": "https://moskva.brusnika.ru/api/filter/flats/?limit=8&offset=8",
                   "results": [
                       {"flat_id": 49980, "pk": 53937, "page_url": "https://moskva.brusnika.ru/flat/49980/",
                        "complex": 87, "complex_name": "Дом «А»", "building": 363,
                        "building_number": "1", "building_floors_count": 21,
                        "section_number": "3", "floor": 2, "rooms": 2, "square": 84.5,
                        "price": 61290000.0, "price_marketing": "57610000.00",
                        "show_price_marketing": False, "is_booked": False,
                        "main_flat_image": "https://cdn.brusnika.ru/media/flat/flat_image_file/a.svg"},
                       {"flat_id": 40000, "page_url": "https://moskva.brusnika.ru/flat/40000/",
                        "complex": 11, "complex_name": "Первый квартал", "building": 391,
                        "price": 19000000, "price_marketing": "16000000",
                        "show_price_marketing": True, "is_booked": True},
                   ]}
        rows = parse_brusnika_page(json.dumps(payload), url, WHEN, SHA)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["publisher_unit_id"], "49980")
        self.assertEqual(rows[0]["publisher_project_id"], "87")
        self.assertEqual(rows[0]["publisher_building_id"], "363")
        self.assertEqual(rows[0]["price_rub"], 61290000)
        self.assertEqual(rows[0]["source_fields"]["location_scope"], "in_scope_moscow")
        self.assertEqual(rows[0]["media"][0]["role"], "apartment_plan")
        self.assertIsNone(rows[1]["price_rub"])
        self.assertEqual(rows[1]["source_fields"]["location_scope"], "out_of_scope")
        self.assertEqual(discover_brusnika_pages(json.dumps(payload), url)[0]["url"], payload["next"])

    def test_unit_page_preserves_explicit_plan_and_unknown_address(self) -> None:
        html = """<html><head><title>Квартира в Доме «А» - Брусника Москва</title></head><body>
          <div>Этаж 4 из 21</div><strong>126,6 м²</strong>
          <div class='mainPriceWrapper_A'><span>101 800 000</span> руб.</div>
          <p>№59 • ‎ДМА01.1-Ж3.4.5</p><div>Секция 3</div><div>Дом 1</div>
          <a href='/flat?complex=87&amp;building=363'>74 квартиры</a>
          <img alt='Планировка' src='https://cdn.brusnika.ru/media/flat/flat_image_file/a.svg'>
          <img alt='Дом «А»' src='https://cdn.brusnika.ru/media/complex/preview/b.jpg'>
          </body></html>"""
        rows = parse_brusnika_page(html, "https://moskva.brusnika.ru/flat/50252/", WHEN, SHA)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["floor"], row["area_m2"], row["price_rub"]), (4, 126.6, 101800000))
        self.assertEqual(row["source_fields"]["article"], "ДМА01.1-Ж3.4.5")
        self.assertEqual(row["publisher_project_id"], "87")
        self.assertIsNone(row["address"])
        self.assertIsNone(row["source_updated_at"])
        self.assertEqual([(item["role"], item["scope"]) for item in row["media"]],
                         [("apartment_plan", "unit"), ("unknown", "project")])
        self.assertEqual(row["rights_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
