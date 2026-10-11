"""Exact source identity and qualified display-name decisions."""

from __future__ import annotations

import unittest

from sources.property.developer_builder_enrichment import project_builder


class BuilderEnrichmentTest(unittest.TestCase):
    def test_aggregator_claim_stays_qualified_and_legal_name_unknown(self) -> None:
        point = {"source_group": "proplanirovki", "publisher_project_id": "project-a"}
        assertion = {"project-a": {"builder_assertion": "Level Group",
                                   "catalog_response_sha256": "a" * 64}}
        row = project_builder(point, assertion)
        self.assertEqual(row["developer_display_name"], "Level Group")
        self.assertEqual(row["developer_display_basis"], "aggregator_asserted_unverified")
        self.assertIsNone(row["developer_legal_name"])
        with self.assertRaisesRegex(ValueError, "no catalog builder"):
            project_builder({**point, "publisher_project_id": "project-b"}, assertion)

    def test_exact_official_override_has_separate_basis(self) -> None:
        point = {"source_group": "proplanirovki", "publisher_project_id": "project-a"}
        assertion = {"project-a": {"builder_assertion": "Other",
                                   "catalog_response_sha256": "a" * 64}}
        override = {"developer_brand": "Level Group", "target_registry_id":
                    "registry-" + "b" * 24}
        row = project_builder(point, assertion, override)
        self.assertEqual(row["developer_display_name"], "Level Group")
        self.assertEqual(row["developer_display_basis"], "official_project_brand")
        self.assertIsNone(row["developer_legal_name"])

    def test_first_party_brand_is_not_a_legal_entity(self) -> None:
        for source, label in (("a101", "А101"), ("fsk", "ФСК")):
            with self.subTest(source=source):
                row = project_builder({"source_group": source,
                                       "source_evidence_sha256": "f" * 64}, {})
                self.assertEqual(row["developer_display_name"], label)
                self.assertEqual(row["developer_display_basis"], "first_party_source_brand")
                self.assertIsNone(row["developer_legal_name"])


if __name__ == "__main__":
    unittest.main()
