"""Unit detail projection retains source terms without claiming a live offer."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sources.property import developer_enrichment_units as details


class DeveloperEnrichmentUnitsTest(unittest.TestCase):
    def test_source_fields_are_dated_assertions_not_current_availability(self) -> None:
        with TemporaryDirectory() as tmp:
            menu_dir = Path(tmp) / "menu1"
            menu_dir.mkdir()
            unit = {"unit_id": "unit_one", "registry_id": "registry_one",
                    "source_group": "a101", "publisher_unit_id": "123",
                    "publisher_project_id": "project_one",
                    "observed_at": "2026-10-10T10:00:00Z", "source_updated_at": None,
                    "source_evidence_sha256": "a" * 64}
            (menu_dir / "units.jsonl").write_text(json.dumps(unit) + "\n")
            raw = {"observed_at": unit["observed_at"], "source_updated_at": None,
                   "publisher_project_id": "project_one",
                   "source_fields": {"a101_price_fields_rub": {"actual_price": 5_000_000,
                                                                "special_price": 4_800_000},
                                     "a101_status_code": 1,
                                     "availability_assertion": "listed"},
                   "promo_conditions": None}
            with patch.object(details, "MENU_ROOT", Path(tmp)), patch.object(
                    details, "_source_rows", return_value=({("a101", "123"): raw}, {})):
                row = next(details.unit_detail_rows("menu1"))
                self.assertEqual(row["source_price_fields_rub"]["special_price"], 4_800_000)
                self.assertEqual(row["availability_claim"], "listed")
                self.assertFalse(row["availability_confirmed_current"])
                self.assertFalse(row["payment_terms_confirmed_unconditional"])
                raw["observed_at"] = "2026-10-09T10:00:00Z"
                with self.assertRaisesRegex(ValueError, "date changed"):
                    list(details.unit_detail_rows("menu1"))


if __name__ == "__main__":
    unittest.main()
