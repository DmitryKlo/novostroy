from __future__ import annotations

import unittest

from sources.property.importer import import_records, validate_records


RIGHTS = {
    "source_id": "synthetic_test_feed",
    "rights_status": "cleared",
    "access_basis": "isolated_integration_test_only",
    "permitted_storage": True,
    "permitted_display": True,
    "test_only": True,
}


def record(**overrides):
    value = {
        "schema_version": "property-intermediate/0.1",
        "record_type": "offer",
        "data_mode": "real",
        "offer_id": "SYNTHETIC-INTEGRATION-OFFER-001",
        "unit_id": "SYNTHETIC-INTEGRATION-UNIT-001",
        "complex_id": "SYNTHETIC-INTEGRATION-COMPLEX-001",
        "district_id": None,
        "rooms": 2,
        "area_sqm": 55.0,
        "floor": 4,
        "price": {
            "amount_rub": 12345678,
            "basis": "asking_total",
            "observed_at": "2026-10-09T12:00:00+03:00",
        },
        "availability": None,
        "source": {
            "name": "SYNTHETIC INTEGRATION TEST ONLY",
            "quality": "dated_asking",
            "record_mode": "developer_offer",
            "observed_at": "2026-10-09T12:00:00+03:00",
            "source_updated_at": None,
            "rights_status": "cleared",
            "source_id": RIGHTS["source_id"],
            "source_record_id": "SYNTHETIC-INTEGRATION-OFFER-001",
            "source_fields": {"synthetic": True, "fixture_id": "test-only"},
            "source_rights": {
                "access_basis": RIGHTS["access_basis"],
                "local_storage": "test_only_schema",
                "public_display": "test_client_only",
                "redistribution": "not_applicable",
                "rights_status": "cleared",
            },
        },
        "facts": {
            "offer.price_per_sqm_rub": {
                "value": {"numerator_rub": 12345678, "denominator_sqm": "55.0"},
                "unit": "RUB/m2",
                "status": "derived",
                "scope": "offer",
                "observed_at": "2026-10-09T12:00:00+03:00",
                "provenance": {
                    "source_id": RIGHTS["source_id"],
                    "source_record_id": "SYNTHETIC-INTEGRATION-OFFER-001",
                    "source_field": None,
                    "source_updated_at": None,
                    "rights_status": "cleared",
                    "method": "exact_ratio",
                    "inputs": [
                        {"source_field": "price/value", "value": 12345678},
                        {"source_field": "area/value", "value": "55.0"},
                    ],
                    "formula": "offer.price_total_rub / unit.area_sqm",
                },
            },
            "building.cctv": {
                "value": None,
                "unit": "boolean",
                "status": "unknown",
                "scope": "building_or_complex",
                "observed_at": "2026-10-09T12:00:00+03:00",
                "provenance": {
                    "source_id": RIGHTS["source_id"],
                    "source_record_id": "SYNTHETIC-INTEGRATION-OFFER-001",
                    "source_field": None,
                    "source_updated_at": None,
                    "rights_status": "cleared",
                },
            },
        },
    }
    value.update(overrides)
    return value


class ImportValidationTests(unittest.TestCase):
    def test_default_is_dry_run_and_marks_test_records(self):
        result = import_records([record()], RIGHTS, service="property_test_unit", test_only=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(result["test_only"])
        self.assertEqual(result["record_count"], 1)
        clean = validate_records([record()], RIGHTS, test_only=True)[0]
        self.assertIn("SYNTHETIC INTEGRATION TEST ONLY", clean["source"]["name"])
        self.assertEqual(clean["source"]["source_fields"]["fixture_id"], "test-only")
        self.assertEqual(clean["facts"]["offer.price_per_sqm_rub"]["value"]["denominator_sqm"], "55.0")
        self.assertEqual(clean["facts"]["building.cctv"]["status"], "unknown")

    def test_demo_fixture_cannot_enter_real_import(self):
        demo = record(data_mode="demo")
        with self.assertRaisesRegex(ValueError, "real-mode canonical offer"):
            validate_records([demo], RIGHTS, test_only=True)

    def test_uncleared_record_cannot_be_imported(self):
        unknown = record()
        unknown["source"]["rights_status"] = "unknown"
        with self.assertRaisesRegex(ValueError, "not explicitly cleared"):
            validate_records([unknown], RIGHTS, test_only=True)

    def test_production_requires_explicit_storage_and_display_rights(self):
        production_rights = {
            "source_id": "developer_feed",
            "rights_status": "cleared",
            "access_basis": "signed_contract_123",
            "permitted_storage": True,
            "permitted_display": False,
        }
        production_record = record()
        production_record["source"]["source_id"] = "developer_feed"
        production_record["source"]["source_rights"]["access_basis"] = "signed_contract_123"
        with self.assertRaisesRegex(ValueError, "explicit storage and public-display"):
            validate_records([production_record], production_rights)

    def test_missing_or_misclassified_price_is_rejected(self):
        no_price = record(price=None)
        with self.assertRaisesRegex(ValueError, "must have source quality unknown"):
            validate_records([no_price], RIGHTS, test_only=True)
        per_sqm = record(price={"amount_rub": 123, "basis": "per_sqm", "observed_at": "2026-10-09T12:00:00Z"})
        with self.assertRaisesRegex(ValueError, "price.basis is unsupported"):
            validate_records([per_sqm], RIGHTS, test_only=True)

    def test_source_fields_and_ids_are_required_and_unique(self):
        bad = record()
        bad["source"].pop("source_record_id")
        with self.assertRaisesRegex(ValueError, "source_record_id"):
            validate_records([bad], RIGHTS, test_only=True)
        repeated = record()
        with self.assertRaisesRegex(ValueError, "duplicate source record ID"):
            validate_records([repeated, repeated], RIGHTS, test_only=True)

    def test_rejects_unknown_with_value_and_derived_without_provenance(self):
        bad_unknown = record()
        bad_unknown["facts"]["building.cctv"]["value"] = False
        with self.assertRaisesRegex(ValueError, "requires null value"):
            validate_records([bad_unknown], RIGHTS, test_only=True)
        bad_derived = record()
        bad_derived["facts"]["offer.price_per_sqm_rub"]["provenance"].pop("formula")
        with self.assertRaisesRegex(ValueError, "needs input provenance and formula"):
            validate_records([bad_derived], RIGHTS, test_only=True)

    def test_real_import_rejects_illustrative_facts(self):
        production_rights = {
            "source_id": "developer_feed",
            "rights_status": "cleared",
            "access_basis": "signed_contract_123",
            "permitted_storage": True,
            "permitted_display": True,
        }
        production_record = record()
        production_record["source"]["source_id"] = "developer_feed"
        production_record["source"]["source_rights"]["access_basis"] = "signed_contract_123"
        for existing_fact in production_record["facts"].values():
            existing_fact["provenance"]["source_id"] = "developer_feed"
        production_record["facts"]["unit.floor"] = {
            "value": 4,
            "unit": "floor",
            "status": "illustrative",
            "scope": "unit",
            "observed_at": "2026-10-09T12:00:00+03:00",
            "provenance": {
                "source_id": "developer_feed",
                "source_record_id": "SYNTHETIC-INTEGRATION-OFFER-001",
                "source_field": "floor",
                "source_updated_at": None,
                "rights_status": "cleared",
            },
        }
        with self.assertRaisesRegex(ValueError, "cannot be imported as real"):
            validate_records([production_record], production_rights)


if __name__ == "__main__":
    unittest.main()
