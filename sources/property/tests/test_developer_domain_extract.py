"""Bounded offline extraction from audited entry-page HTML."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from sources.property.developer_domain_extract import build, extract


class DomainExtractTests(TestCase):
    def test_jsonld_price_and_image_remain_page_evidence(self) -> None:
        html = ('<script type="application/ld+json">'
                '{"@type":"Product","name":"Test house",'
                '"image":"/image.jpg","offers":{"price":"12345678",'
                '"priceCurrency":"RUB"}}</script>'
                '<script>const price="88 888 888 ₽";</script>'
                '<p>от 12.4 млн ₽</p>').encode()
        result = extract(html, "https://example.org/projects/test/")
        self.assertEqual(result["jsonld_entities"][0]["types"], ["Product"])
        self.assertEqual(result["jsonld_entities"][0]["offers"][0]["price"], "12345678")
        self.assertEqual(result["jsonld_media_refs"][0]["url"],
                         "https://example.org/image.jpg")
        self.assertFalse(result["jsonld_media_refs"][0]["public_display_allowed"])
        self.assertTrue(result["html_text_price_pattern_present"])

    def test_hash_gate_and_manifest(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'<script type="application/ld+json">{"@type":"Organization"}</script>'
            sha = hashlib.sha256(raw).hexdigest()
            (root / "raw").mkdir()
            (root / "raw" / f"{sha}.body").write_bytes(raw)
            row = {"host": "example.org", "final_url": "https://example.org/",
                   "at": "2026-10-10T00:00:00Z", "body_sha256": sha}
            (root / "requests.jsonl").write_text(json.dumps(row) + "\n")
            manifest = build(root)
            self.assertEqual(manifest["entry_pages"], 1)
            self.assertEqual(manifest["jsonld_entities"], 1)
            (root / "raw" / f"{sha}.body").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA mismatch"):
                build(root)

