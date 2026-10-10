"""Focused offline checks for the bounded official-domain entry audit."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sources.property.developer_domain_audit import build_plan, page_signals, run
from sources.property.developer_harvest import FetchResult


class DomainAuditTests(TestCase):
    def test_plan_limits_urls_and_respects_prior_denial(self) -> None:
        rows = [
            {"supported": True, "evidence_id": "one", "label": "A",
             "official_domain_url": "https://example.org",
             "project_or_catalog_entry_url": "https://example.org/flats/"},
            {"supported": True, "evidence_id": "two", "label": "B",
             "official_domain_url": "https://example.org/other",
             "project_or_catalog_entry_url": "https://elsewhere.org/flats/"},
            {"supported": True, "evidence_id": "three", "label": "PIK",
             "official_domain_url": "https://www.pik.ru/",
             "project_or_catalog_entry_url": "https://www.pik.ru/flats/"},
            {"supported": False, "evidence_id": "four", "label": "Unknown",
             "official_domain_url": "https://unknown.org/"},
        ]
        plan = build_plan(rows)
        self.assertEqual(plan["evidence_rows"], 4)
        self.assertEqual(plan["supported_rows"], 3)
        self.assertEqual(len(plan["hosts"]), 2)
        host = next(item for item in plan["hosts"] if item["host"] == "example.org")
        self.assertEqual(host["urls"], ["https://example.org/", "https://example.org/flats/"])
        denied = next(item for item in plan["hosts"] if item["host"] == "pik.ru")
        self.assertEqual(denied["status"], "prior_401_stop")

    def test_signals_do_not_mistake_script_price_for_page_text(self) -> None:
        body = ('<script>const promo="99 999 999 ₽";</script>'
                '<script type="application/ld+json">{"@type":"Organization"}</script>'
                '<a href="/flats/">Apartments</a>'
                '<meta property="og:image" content="https://img.example.org/one.jpg">').encode()
        result = page_signals(body, "https://example.org/")
        self.assertFalse(result["html_text_price_pattern_present"])
        self.assertEqual(result["jsonld_types"], ["Organization"])
        self.assertEqual(result["source_linked_catalog_candidates"],
                         ["https://example.org/flats/"])
        self.assertEqual(result["source_image_refs"],
                         ["https://img.example.org/one.jpg"])

    def test_resume_and_host_denial_without_second_request(self) -> None:
        rows = [{"supported": True, "evidence_id": "one", "label": "A",
                 "official_domain_url": "https://example.org/",
                 "project_or_catalog_entry_url": "https://example.org/flats/"}]
        with TemporaryDirectory() as temp:
            output = Path(temp)
            with patch("sources.property.developer_domain_audit.curl_fetch",
                       return_value=FetchResult(403, b"", {}, "https://example.org/",
                                                "http_403")) as fetch, \
                    patch("sources.property.developer_domain_audit.time.sleep"):
                first = run(build_plan(rows), output, live=True, max_domains=10, delay=1)
                second = run(build_plan(rows), output, live=True, max_domains=10, delay=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(first["blocked_hosts"], ["example.org"])
            self.assertEqual(second["requests"], 1)
            self.assertEqual(list((output / "raw").glob("*")) if
                             (output / "raw").exists() else [], [])
