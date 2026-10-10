from __future__ import annotations

import json
import unittest

from sources.property.developers_fsk import discover_fsk_pages, parse_fsk_page


OBSERVED = "2026-10-10T11:00:00+03:00"
HASH = "a" * 64


class FskDeveloperParserTests(unittest.TestCase):
    def test_catalog_emits_only_visible_unit_cards_and_parses_price_context(self) -> None:
        html = """<!doctype html><html><head>
          <link rel="canonical" href="https://fsk.ru/flats/list">
          <script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"ФСК"}</script>
          </head><body>
          <nav class="menu-container"><a href="/promo">Акции</a><a href="/flats">Квартиры</a></nav>
          <div class="flats-grid">
            <a class="card" data-testid="flats-link-detail-1" href="/rimskiy/flats/132159">
              <div class="card-flat">1-комнатная, 38,1 м²</div>
              <p class="flat-project-text">Римский</p>
              <div class="card-info-params"><span>7 корпус</span><span class="card-floor-number">8/11</span></div>
              <div class="card-price"><div class="card-price-main"><span class="price-text">9 863 442 ₽</span></div>
              <span class="old-price">11 000 000 ₽</span></div>
              <div class="card-promo"><div class="card-promo-item">Скидка при 100% оплате</div></div>
              <div class="card-image"><img data-src="https://cdn.fsk.ru/flat.jpg" alt=""></div>
            </a>
            <a class="card" href="/rimskiy/flats/132160">
              <div class="card-flat">Студия, 23.4 м²</div><p class="flat-project-text">Римский</p>
              <div class="card-info-params"><span>7 корпус</span><span class="card-floor-number">4/11</span></div>
              <div class="card-price-main"><span class="price-text">8 100 000 ₽</span></div>
            </a>
            <a class="card" href="https://not-fsk.example/rimskiy/flats/1">bad</a>
          </div><button>Показать ещё 20 из 1153</button>
          </body></html>"""
        rows = parse_fsk_page(html, "https://fsk.ru/flats/list", OBSERVED, HASH)
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual((first["record_type"], first["project_name"], first["publisher_unit_id"]),
                         ("unit", "Римский", "132159"))
        self.assertEqual((first["corpus"], first["publisher_building_id"], first["floor"]),
                         ("7", None, 8))
        self.assertEqual((first["rooms"], first["area_m2"], first["price_rub"]),
                         (1, 38.1, 9863442))
        self.assertEqual(first["price_basis"], "conditional_total")
        self.assertEqual(first["promo_conditions"], ["Скидка при 100% оплате"])
        self.assertEqual(second["rooms"], 0)
        self.assertEqual(second["price_basis"], "asking_total")
        self.assertEqual(first["source_fields"]["catalog_visible_unit_card_count"], 2)
        self.assertEqual(first["source_fields"]["catalog_more_button"]["remaining_count"], 1153)
        self.assertEqual(first["source_fields"]["catalog_more_button"]["next_chunk_size"], 20)
        self.assertIsNone(first["source_updated_at"])
        self.assertEqual(first["rights_status"], "unknown")
        self.assertEqual(first["media"][0]["role"], "unknown")

    def test_discovery_uses_unit_links_in_catalog_and_contextual_project_link(self) -> None:
        catalog = """<div class="menu-container"><a href="/promo">Промо</a></div>
          <div class="flats-grid"><a class="card" href="/rimskiy/flats/123">Лот</a></div>
          <footer><a href="/rimskiy">Римский</a></footer>"""
        refs = discover_fsk_pages(catalog, "https://fsk.ru/flats/list")
        self.assertEqual([ref["url"] for ref in refs], ["https://fsk.ru/rimskiy/flats/123"])
        listing = """<nav class="complex-navbar"><a class="complex-logo" href="/rimskiy">Римский</a></nav>
          <div class="recommendations"><a class="card" href="/rimskiy/flats/456">Другой лот</a></div>"""
        refs = discover_fsk_pages(listing, "https://fsk.ru/rimskiy/flats/123")
        self.assertEqual([ref["page_kind"] for ref in refs], ["project"])
        self.assertEqual(refs[0]["project_hint"], "Римский")

    def test_catalog_discovery_uses_observed_cards_and_frontend_api_defaults(self) -> None:
        catalog = """<div class="flats-grid">
          <a class="card" href="/rimskiy/flats/123">Лот</a>
          <a class="card" href="/rimskiy/flats/124">Лот</a>
          </div><button>Показать ещё 20 из 1153</button>"""
        refs = discover_fsk_pages(catalog, "https://fsk.ru/flats/list")
        api = next(ref for ref in refs if "/api/v3/flats?" in ref["url"])
        self.assertEqual(api["page_kind"], "catalog")
        self.assertEqual(api["url"], "https://fsk.ru/api/v3/flats?city=1&offset=0&limit=2&sort=price&order=1")
        self.assertEqual(api["discovery_source"], "fsk_frontend_catalog_request")
        no_button = discover_fsk_pages(catalog.split("<button>")[0], "https://fsk.ru/flats/list")
        self.assertFalse(any("/api/v3/flats?" in ref["url"] for ref in no_button))

    def test_catalog_api_response_parses_unit_facts_and_paginates_observed_offset(self) -> None:
        item = {
            "_id": "6579ba96605be765367bad6b", "externalId": "203017",
            "price": 18317588, "priceWoDiscount": 20581560, "discount": 11,
            "priceOnRequest": False, "prePrice": False, "areaTotal": 32.3,
            "number": "467", "rooms": 0, "floorNumber": 36,
            "plan": "https://cdn.fsk.ru/production/unit.sizes.svg",
            "planFurniture": "https://cdn.fsk.ru/production/unit.furniture.svg",
            "project": {"_id": "project-id", "title": "Скай Гарден", "slug": "skygarden"},
            "corpus": {"_id": "corpus-id", "number": "3", "dateDelivery": "2027-06-30"},
            "section": {"_id": "section-id", "number": "2", "floorsCount": 44},
            "labels": [{"title": "Скидка"}, {"title": "Семейная ипотека от 4%"}],
            "financials": [{"title": "Платите как хотите"}],
        }
        payload = {"total": 1171, "limit": 20, "page": 1, "hasNextPage": True,
                   "items": [item]}
        source = "https://fsk.ru/api/v3/flats?city=1&offset=18&limit=20&sort=price&order=1"
        rows = parse_fsk_page(json.dumps(payload, ensure_ascii=False), source, OBSERVED, HASH)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["project_name"], row["corpus"], row["section"], row["unit_number"]),
                         ("Скай Гарден", "3", "2", 467))
        self.assertEqual((row["publisher_project_id"], row["publisher_building_id"],
                          row["publisher_unit_id"]), ("project-id", "corpus-id", "203017"))
        self.assertEqual((row["floor"], row["rooms"], row["area_m2"], row["price_rub"]),
                         (36, 0, 32.3, 18317588))
        self.assertEqual(row["price_basis"], "asking_total")
        self.assertIsNone(row["promo_conditions"])
        self.assertEqual(row["source_fields"]["catalog_total_claimed"], 1171)
        self.assertEqual([asset["source_field"] for asset in row["media"]],
                         ["json:items[0].plan", "json:items[0].planFurniture"])
        refs = discover_fsk_pages(json.dumps(payload), source)
        self.assertEqual(refs, [{"url": "https://fsk.ru/api/v3/flats?city=1&sort=price&order=1&offset=19&limit=20",
                                 "page_kind": "catalog",
                                 "discovery_source": "fsk_frontend_offset_pagination"}])

    def test_detail_page_keeps_unit_and_floor_plans_scoped_and_price_conditional(self) -> None:
        html = """<link rel="canonical" href="https://fsk.ru/arhitektor/flats/176276">
          <script type="application/ld+json">{"@type":"Product","description":"Квартира в ЖК Архитектор от застройщика ФСК","offers":{"price":"24845540","priceCurrency":"RUB"}}</script>
          <div class="v8-breadcrumbs"><a class="breadcrumb-link" href="/arhitektor">Архитектор</a></div>
          <h1>2-комнатная, 43,3 м², № 1257</h1>
          <div class="flat-corpus-section-floor">3 корпус, 2 секция, 44 этаж</div>
          <div class="price-value">24 845 540 ₽</div><div class="price-line-th">32 691 500 ₽</div>
          <div class="flat-promo-item">Живите сейчас — платите потом</div>
          <div class="flat-plan-image -plan"><img data-src="https://cdn.fsk.ru/plan.svg"></div>
          <div class="flat-plan-image -floor"><img data-src="https://cdn.fsk.ru/floor.svg"></div>
          <div class="flat-plan-image -genplan"><img data-src="https://cdn.fsk.ru/genplan.jpg"></div>"""
        rows = parse_fsk_page(html, "https://fsk.ru/arhitektor/flats/176276", OBSERVED, HASH)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["project_name"], row["publisher_unit_id"], row["unit_number"]),
                         ("Архитектор", "176276", 1257))
        self.assertEqual((row["corpus"], row["section"], row["floor"]), ("3", "2", 44))
        self.assertEqual(row["price_basis"], "asking_total")
        self.assertEqual([asset["role"] for asset in row["media"]],
                         ["apartment_plan", "floor_plan"])
        self.assertEqual([asset["scope"] for asset in row["media"]], ["unit", "floor"])
        self.assertEqual(row["source_fields"]["jsonld_offer"]["priceCurrency"], "RUB")

    def test_project_gallery_requires_explicit_bound_architecture_label(self) -> None:
        html = """<link rel="canonical" href="https://fsk.ru/rimskiy">
          <h1>ЖК Римский</h1><div class="gallery-item">
          <img src="https://cdn.fsk.ru/facade.jpg"><span>Архитектура</span><b>Реальное фото</b>
          </div><img src="https://cdn.fsk.ru/hero.jpg" alt="Римский">"""
        rows = parse_fsk_page(html, "https://fsk.ru/rimskiy", OBSERVED, HASH)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_name"], "ЖК Римский")
        self.assertEqual(len(rows[0]["media"]), 1)
        self.assertEqual(rows[0]["media"][0]["role"], "facade_image")
        self.assertEqual(rows[0]["media"][0]["visual_kind"], "photo")
        self.assertEqual(rows[0]["media"][0]["scope"], "project")

    def test_project_id_and_city_are_bound_to_source_project_and_street_address(self) -> None:
        html = '''<link rel="canonical" href="https://fsk.ru/arhitektor">
          <script type="application/ld+json">{"@type":"Place","address":
          {"@type":"PostalAddress","addressLocality":"Москва и МО",
           "streetAddress":"Москва, ул. Академика Волгина, д. 2"}}</script>
          <h1>ЖК «Архитектор»</h1>
          <div class="project" _id="6050c6c4cb6cdb4761623f99"></div>'''
        row = parse_fsk_page(html, "https://fsk.ru/arhitektor", OBSERVED, HASH)[0]
        self.assertEqual(row["publisher_project_id"], "6050c6c4cb6cdb4761623f99")
        self.assertEqual(row["source_fields"]["publisher_project_id_basis"],
                         "html:div.project[_id]")
        self.assertEqual(row["source_fields"]["location_scope"], "in_scope_moscow")
        mixed_only = html.replace("Москва, ул. Академика Волгина, д. 2", "ул. Тестовая, д. 2")
        row = parse_fsk_page(mixed_only, "https://fsk.ru/arhitektor", OBSERVED, HASH)[0]
        self.assertEqual(row["source_fields"]["location_scope"], "unknown")
        city_prefix = html.replace("Москва, ул. Академика Волгина, д. 2",
                                   "г. Москва, ул. Академика Волгина, д. 2")
        row = parse_fsk_page(city_prefix, "https://fsk.ru/arhitektor", OBSERVED, HASH)[0]
        self.assertEqual(row["source_fields"]["location_scope"], "in_scope_moscow")
        ambiguous = html.replace("</div>", "</div><div class=\"project\" _id=\"f" + "0" * 23 + "\"></div>")
        row = parse_fsk_page(ambiguous, "https://fsk.ru/arhitektor", OBSERVED, HASH)[0]
        self.assertIsNone(row["publisher_project_id"])

    def test_external_or_non_https_links_and_assets_are_not_emitted(self) -> None:
        html = """<a class="card" href="http://fsk.ru/rimskiy/flats/1">bad</a>
          <a class="card" href="https://evil.example/rimskiy/flats/2">bad</a>
          <a class="card" href="/rimskiy/flats/3"><div class="card-flat">Студия, 20 м²</div>
          <div class="card-image"><img data-src="https://evil.example/plan.svg"></div></a>"""
        refs = discover_fsk_pages(html, "https://fsk.ru/flats/list")
        self.assertEqual([ref["url"] for ref in refs], ["https://fsk.ru/rimskiy/flats/3"])
        row = parse_fsk_page(html, "https://fsk.ru/flats/list", OBSERVED, HASH)[0]
        self.assertEqual(row["media"], [])


if __name__ == "__main__":
    unittest.main()
