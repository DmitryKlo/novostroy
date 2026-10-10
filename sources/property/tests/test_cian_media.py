"""Offline, source-label-only checks for private CIAN media references."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from sources.property.cian_media import parse_cian_media


LISTING = "https://www.cian.ru/sale/flat/325479540/"
OBSERVED = "2026-10-10T08:17:06Z"


def item(index: int, caption: str, **extra: object) -> dict[str, object]:
    return {"url": f"https://img.example.test/{index}.jpg", "caption": caption,
            "source_field": f"media_items[{index}]", **extra}


class CianMediaTests(unittest.TestCase):
    def parse(self, entries: list[dict[str, object]], **extra: object) -> list[dict]:
        payload = {"page_kind": "unit_listing", "listing_id": "325479540", "media_items": entries}
        return parse_cian_media(payload, source_url=LISTING, observed_at=OBSERVED,
                                rights_status="unknown", **extra)

    def test_explicit_item_labels_and_facade_visual_kind(self) -> None:
        records = self.parse([
            item(0, "Планировка квартиры"),
            item(1, "План этажа"),
            item(2, "Рендер фасада"),
            item(3, "Фото фасада"),
            item(4, "Фасад корпуса"),
            item(5, "Фото квартиры"),
            item(6, "ЖК Шагал"),
            item(7, "3D-тур"),
            item(8, "Планировка"),
            item(9, "Apartment floor plan"),
        ], allowed_media_hosts={"img.example.test"})
        self.assertEqual([record["role"] for record in records], [
            "apartment_plan", "floor_plan", "facade_image", "facade_image",
            "facade_image", "photo", "unknown", "unknown", "apartment_plan", "apartment_plan",
        ])
        self.assertEqual([record["visual_kind"] for record in records[2:5]],
                         ["render", "photo", "unknown"])
        self.assertTrue(all(record["approved_media_host"] for record in records))
        self.assertTrue(all(record["public_display_allowed"] is False and
                            record["fetch_status"] == "not_requested" and
                            record["rights_status"] == "unknown" for record in records))
        self.assertEqual(records[0]["listing_id"], "325479540")
        self.assertEqual(records[0]["source_url"], "https://img.example.test/0.jpg")
        self.assertEqual(records[0]["observed_at"], "2026-10-10T08:17:06+00:00")

    @unittest.skipUnless(Path("data/property/cian_public_sample_20261010.json").is_file(),
                         "local CIAN observation sample absent")
    def test_unbound_real_pilot_labels_do_not_create_media(self) -> None:
        sample = Path("data/property/cian_public_sample_20261010.json")
        document = json.loads(sample.read_text(encoding="utf-8"))
        observation = document["observations"][0]
        self.assertIn("План этажа", observation["visible_media_labels"])
        for observation in document["observations"]:
            with self.subTest(page=observation["source_url"]):
                self.assertEqual(parse_cian_media(observation,
                    source_url=observation["source_url"], observed_at=document["observed_at"],
                    rights_status="unknown"), [])

    def test_missing_binding_or_conflicting_labels_stays_unknown(self) -> None:
        records = self.parse([
            {"url": "https://img.example.test/a.jpg", "caption": "План этажа"},
            item(1, "План этажа и планировка квартиры"),
            item(2, "ЖК Шагал", section_label="Галерея комплекса"),
            item(3, "Apartment floor plan and whole floor plan"),
        ])
        self.assertEqual([record["role"] for record in records], ["unknown"] * 4)
        self.assertEqual(records[0]["classification_basis"], "missing_source_field")
        self.assertEqual(records[1]["classification_basis"], "conflicting_labels")
        self.assertTrue(all(record["approved_media_host"] is False for record in records))

    def test_authorised_feed_layoutphoto_is_explicit_plan_evidence(self) -> None:
        records = parse_cian_media({"media_items": [{
            "url": "https://img.example.test/layout.jpg",
            "source_field": "Offer/LayoutPhoto/FullUrl",
            "rights_status": "cleared",
            "publisher_updated_at": "2026-10-09T16:20:00Z",
        }, {
            "url": "https://img.example.test/gallery.jpg",
            "source_field": "Offer/Photos/Photo/FullUrl",
        }]}, source_url=LISTING, observed_at=OBSERVED, rights_status="cleared",
            allowed_media_hosts={"img.example.test"})
        self.assertEqual(records[0]["role"], "apartment_plan")
        self.assertEqual(records[0]["classification_basis"], "source_field")
        self.assertEqual(records[0]["rights_status"], "cleared")
        self.assertFalse(records[0]["public_display_allowed"])
        self.assertEqual(records[0]["publisher_updated_at"], "2026-10-09T16:20:00+00:00")
        self.assertEqual((records[1]["role"], records[1]["visual_kind"]), ("photo", "unknown"))

    def test_html_needs_rights_and_only_uses_own_image_label(self) -> None:
        html = ('<div>План этажа</div><img src="https://img.example.test/a.jpg" alt="ЖК Шагал">'
                '<img src="https://img.example.test/b.jpg" alt="План этажа">')
        with self.assertRaisesRegex(ValueError, "HTML parsing requires"):
            parse_cian_media(html, source_url=LISTING, observed_at=OBSERVED,
                             rights_status="unknown")
        records = parse_cian_media(html, source_url=LISTING, observed_at=OBSERVED,
                                   rights_status="demo")
        self.assertEqual([record["role"] for record in records], ["unknown", "floor_plan"])
        self.assertEqual(records[1]["source_field"], "html:img[1]")
        self.assertTrue(all(record["public_display_allowed"] is False for record in records))

    def test_unsafe_urls_and_unverified_asset_rights_are_rejected(self) -> None:
        for url in ("http://img.example.test/a.jpg", "https://127.0.0.1/a.jpg",
                    "https://localhost/a.jpg", "https://0177.0.0.1/a.jpg",
                    "https://user:pass@img.example.test/a.jpg",
                    "https://img.example.test/a.jpg?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.parse([{"url": url, "caption": "План этажа", "source_field": "media[0]"}])
        with self.assertRaisesRegex(ValueError, "unsupported by source rights"):
            self.parse([item(0, "План этажа", rights_status="cleared")])
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_cian_media({"media_items": []}, source_url=LISTING,
                             observed_at="2026-10-10", rights_status="unknown")


if __name__ == "__main__":
    unittest.main()
