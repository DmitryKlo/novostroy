from pathlib import Path
import json
import unittest

from sources.property.adapters import normalize_bnmap_prices, normalize_registry_tsv, parse_developer_yrl

try:
    import jsonschema
except ImportError:  # runtime adapter has no jsonschema dependency
    jsonschema = None


HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
OBSERVED_AT = "2026-10-09T10:00:00+03:00"
DEMO_RIGHTS = {
    "access_basis": "synthetic_fixture",
    "local_storage": "test_only",
    "public_display": "not_applicable",
    "redistribution": "not_applicable",
}
REAL_TSV_RIGHTS = {
    "access_basis": "public_endpoint_sample",
    "local_storage": "audit_only",
    "public_display": "not_cleared",
    "redistribution": "not_cleared",
    "rights_status": "unknown",
}


class DeveloperYrlTests(unittest.TestCase):
    def parse_fixture(self, **overrides):
        options = {
            "source_id": "synthetic_developer_fixture",
            "rights": DEMO_RIGHTS,
            "data_mode": "demo",
            "observed_at": OBSERVED_AT,
        }
        options.update(overrides)
        return parse_developer_yrl(FIXTURES / "developer_feed.synthetic.xml", **options)

    def test_parses_offer_preserves_source_fields_and_separates_ids(self):
        records = self.parse_fixture()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["data_mode"], "demo")
        self.assertEqual(record["record_type"], "offer")
        self.assertEqual(record["offer_id"], "SYNTHETIC-DEMO-OFFER-001")
        self.assertEqual(record["unit_id"], "SYNTHETIC-DEMO-UNIT-001")
        self.assertEqual(record["complex_id"], "SYNTHETIC-DEMO-PROJECT-001")
        self.assertEqual(record["building_id"], "SYNTHETIC-DEMO-BUILDING-001")
        self.assertEqual(record["price"], {
            "amount_rub": 9876543210,
            "basis": "asking_total",
            "observed_at": OBSERVED_AT,
        })
        self.assertEqual(record["area_sqm"], 55.5)
        self.assertEqual(record["source"]["source_updated_at"], "2026-10-09T09:00:00+03:00")
        self.assertEqual(record["source"]["source_fields"]["@internal-id"], "SYNTHETIC-DEMO-OFFER-001")
        self.assertEqual(len(record["source"]["source_fields"]["{http://webmaster.yandex.ru/schemas/feed/realty/2010-06}image"]), 2)
        self.assertEqual(record["source"]["rights_status"], "demo")
        self.assertEqual(record["source"]["record_mode"], "demo")

    @unittest.skipIf(jsonschema is None, "jsonschema is optional for the pure-Python adapter")
    def test_demo_offer_validates_against_canonical_contract(self):
        contract = json.loads((HERE.parents[2] / "contracts" / "schemas.json").read_text(encoding="utf-8"))
        offer_schema = {
            "$schema": contract["$schema"],
            "$defs": contract["$defs"],
            "$ref": "#/$defs/Offer",
        }
        jsonschema.validate(self.parse_fixture()[0], offer_schema)

    def test_price_per_area_is_marked_as_per_sqm(self):
        xml = '''<realty-feed><offer internal-id="SYNTHETIC-DEMO-2">
        <newbuilding-id>SYNTHETIC-DEMO-PROJECT-2</newbuilding-id>
        <price><value>12 345,67</value><currency>rub</currency><unit>кв. м</unit></price>
        </offer></realty-feed>'''.encode("utf-8")
        [record] = parse_developer_yrl(
            xml,
            source_id="synthetic",
            rights=DEMO_RIGHTS,
            data_mode="demo",
            observed_at=OBSERVED_AT,
        )
        self.assertIsNone(record["price"])
        self.assertEqual(record["normalized"]["price"]["amount"], "12345.67")
        self.assertEqual(record["normalized"]["price"]["basis"], "per_sqm")
        self.assertEqual(record["normalized"]["price"]["currency"], "RUB")

    def test_explicit_rights_and_correct_mode_are_required(self):
        with self.assertRaisesRegex(ValueError, "rights must provide"):
            parse_developer_yrl(
                b"<realty-feed/>",
                source_id="synthetic",
                rights={},
                data_mode="demo",
            )
        with self.assertRaisesRegex(ValueError, "synthetic fixtures must use data_mode='demo'"):
            parse_developer_yrl(
                b"<realty-feed/>",
                source_id="synthetic",
                rights=DEMO_RIGHTS,
                data_mode="real",
            )

    def test_rejects_missing_offer_id_doctype_and_secret_urls(self):
        with self.assertRaisesRegex(ValueError, "internal-id"):
            parse_developer_yrl(
                b"<realty-feed><offer><price><value>1</value></price></offer></realty-feed>",
                source_id="synthetic",
                rights=DEMO_RIGHTS,
                data_mode="demo",
            )
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            parse_developer_yrl(
                b"<!DOCTYPE x [<!ENTITY e 'x'>]><realty-feed/>",
                source_id="synthetic",
                rights=DEMO_RIGHTS,
                data_mode="demo",
            )
        with self.assertRaisesRegex(ValueError, "credential-like"):
            self.parse_fixture(source_url="https://example.invalid/feed?pbi=secret")

    def test_rejects_duplicate_feed_offer_ids(self):
        xml = b'''<realty-feed>
        <offer internal-id="DUPLICATE"/><offer internal-id="DUPLICATE"/>
        </realty-feed>'''
        with self.assertRaisesRegex(ValueError, "must be unique"):
            parse_developer_yrl(
                xml,
                source_id="synthetic",
                rights=DEMO_RIGHTS,
                data_mode="demo",
                observed_at=OBSERVED_AT,
            )

    def test_empty_real_feed_needs_explicit_nonsynthetic_rights(self):
        rights = {
            "access_basis": "authorized_developer_feed",
            "local_storage": "contract_permits_processing",
            "public_display": "not_cleared",
            "redistribution": "not_cleared",
            "rights_status": "unknown",
        }
        self.assertEqual(
            parse_developer_yrl(
                b"<realty-feed/>",
                source_id="authorized_developer_feed",
                rights=rights,
                data_mode="real",
                observed_at=OBSERVED_AT,
            ),
            [],
        )

    def test_facts_keep_field_provenance_unknowns_explicit_false_and_exact_ratio(self):
        record = parse_developer_yrl(
            FIXTURES / "property_facts.synthetic.xml",
            source_id="synthetic_property_facts",
            rights=DEMO_RIGHTS,
            data_mode="demo",
            observed_at=OBSERVED_AT,
        )[0]
        facts = record["facts"]
        ratio = facts["offer.price_per_sqm_rub"]
        self.assertEqual(ratio["value"], {"numerator_rub": 10000000, "denominator_sqm": "40.5000000000000001"})
        self.assertEqual(ratio["unit"], "RUB/m2")
        self.assertEqual(ratio["status"], "illustrative")
        self.assertEqual(ratio["provenance"]["method"], "exact_ratio")
        self.assertEqual(len(ratio["provenance"]["inputs"]), 2)
        self.assertEqual(facts["unit.area_sqm"]["unit"], "m²")
        self.assertEqual(facts["unit.area_sqm"]["value"], "40.5000000000000001")
        self.assertEqual(facts["unit.area_sqm"]["provenance"]["source_field"], "area/value")
        self.assertEqual(facts["unit.floor"]["value"], 7)
        self.assertEqual(facts["unit.rooms"]["value"], 1)
        self.assertEqual(facts["unit.bathroom_count"]["value"], 2)
        self.assertEqual(facts["unit.balcony"]["value"], "лоджия")
        self.assertEqual(facts["building.lift_present"]["value"], False)
        self.assertEqual(facts["building.lift_present"]["status"], "illustrative")
        self.assertEqual(facts["building.closed_territory"]["value"], False)
        self.assertEqual(facts["building.guarded_parking_present"]["value"], False)
        self.assertEqual(facts["building.elevator_count"]["status"], "unknown")
        self.assertIsNone(facts["building.elevator_count"]["value"])
        self.assertEqual(facts["building.apartments_per_floor"]["status"], "unknown")
        self.assertEqual(facts["building.expected_completion_year"]["value"], 2028)

    def test_missing_fields_are_unknown_and_not_false(self):
        record = self.parse_fixture()[0]
        for key in ("unit.balcony", "building.closed_territory", "building.lift_present", "building.parking_type"):
            self.assertEqual(record["facts"][key]["status"], "unknown", key)
            self.assertIsNone(record["facts"][key]["value"], key)


class RegistryTsvTests(unittest.TestCase):
    def test_normalizes_only_reference_labels_and_preserves_extra_columns(self):
        records = normalize_registry_tsv(
            FIXTURES / "newbuildings.synthetic.tsv",
            rights=DEMO_RIGHTS,
            data_mode="demo",
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["record_type"], "reference_label")
        self.assertEqual(record["data_mode"], "demo")
        self.assertEqual(record["source"]["record_mode"], "reference_label")
        self.assertIsNone(record["normalized"]["price"])
        self.assertEqual(record["project_id"], "SYNTHETIC-PROJECT-001")
        self.assertEqual(record["building_id"], "SYNTHETIC-BUILDING-001")
        self.assertEqual(record["source"]["source_fields"]["columns_in_source_order"][-1], "SYNTHETIC-EXTRA-COLUMN")
        self.assertEqual(record["source"]["source_fields"]["extra_columns"], [
            {"position": 9, "value": "SYNTHETIC-EXTRA-COLUMN"}
        ])
        self.assertEqual(record["source"]["source_rights"]["redistribution"], "not_applicable")
        self.assertEqual(record["source"]["rights_status"], "demo")
        if jsonschema is not None:
            contract = json.loads((HERE.parents[2] / "contracts" / "schemas.json").read_text(encoding="utf-8"))
            source_schema = {
                "$schema": contract["$schema"],
                "$defs": contract["$defs"],
                "$ref": "#/$defs/Source",
            }
            jsonschema.validate(record["source"], source_schema)

    def test_rejects_short_rows_and_credential_urls(self):
        with self.assertRaisesRegex(ValueError, "expected at least 8"):
            normalize_registry_tsv(
                "only\ttwo",
                rights=DEMO_RIGHTS,
                data_mode="demo",
                observed_at=OBSERVED_AT,
            )
        with self.assertRaisesRegex(ValueError, "credential-like"):
            normalize_registry_tsv(
                "Москва\tDEMO\tID\t1\t2099\tадрес\tBID\turl",
                rights=REAL_TSV_RIGHTS,
                data_mode="real",
                observed_at=OBSERVED_AT,
                source_url="https://example.invalid/newbuildings.tsv?key=secret",
            )


class BnmapPriceTests(unittest.TestCase):
    def normalize(self, **overrides):
        options = {
            "source_id": "synthetic_bnmap_test",
            "rights": DEMO_RIGHTS,
            "data_mode": "demo",
            "observed_at": OBSERVED_AT,
        }
        options.update(overrides)
        json_data = options.pop("json_data", FIXTURES / "bnmap_prices.synthetic.json")
        return normalize_bnmap_prices(json_data, **options)

    def test_maps_only_documented_fields_and_uses_exact_derived_ratio(self):
        [record] = self.normalize()
        self.assertEqual(record["offer_id"], "synthetic_bnmap_test:SYNTHETIC-BNMAP-LOT-01")
        self.assertEqual(record["unit_id"], "SYNTHETIC-APT-0512")
        self.assertEqual(record["complex_id"], "SYNTHETIC-BNMAP-COMPLEX-01")
        self.assertEqual(record["source"]["record_mode"], "demo")
        self.assertEqual(record["price"]["amount_rub"], 30000000)
        self.assertEqual(record["area_sqm"], 65.4)
        self.assertEqual(record["facts"]["offer.price_per_sqm_rub"]["value"], {
            "numerator_rub": 30000000,
            "denominator_sqm": "65.4000000000000001",
        })
        self.assertEqual(record["facts"]["offer.price_per_sqm_rub"]["status"], "illustrative")
        self.assertEqual(record["facts"]["unit.area_sqm"]["unit"], "m²")
        self.assertEqual(record["facts"]["unit.area_sqm"]["value"], "65.4000000000000001")
        self.assertEqual(record["facts"]["offer.price_per_sqm_rub"]["provenance"]["inputs"][1]["provenance"]["source_field"], "square")
        self.assertEqual(record["facts"]["building.parking_type"]["value"], "underground")
        self.assertEqual(record["facts"]["building.expected_completion_year"]["value"], 2028)
        self.assertEqual(record["facts"]["building.apartments_per_floor"]["status"], "unknown")
        self.assertEqual(record["normalized"]["advertised_square_price_raw"], "999999.99")
        self.assertEqual(record["source"]["source_fields"]["squarePrice"], 999999.99)

    def test_rejects_bad_status_duplicate_lots_and_missing_rights(self):
        with self.assertRaisesRegex(ValueError, "status must be 'ok'"):
            self.normalize(json_data={"status": "error", "content": []})
        duplicate = {"status": "ok", "content": [
            {"a_lot_hash": "DUPLICATE", "hc_id_hash": "C"},
            {"a_lot_hash": "DUPLICATE", "hc_id_hash": "C"},
        ]}
        with self.assertRaisesRegex(ValueError, "duplicate bnMAP source record ID"):
            self.normalize(json_data=duplicate)
        with self.assertRaisesRegex(ValueError, "rights must provide"):
            self.normalize(rights={})


if __name__ == "__main__":
    unittest.main()
