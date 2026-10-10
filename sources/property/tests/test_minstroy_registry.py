import unittest

from sources.property.minstroy_registry import (
    find_moscow_housing_mentions,
    is_explicit_residential_name,
    normalize_registry_rows,
)


class MinstroyRegistryTests(unittest.TestCase):
    def test_normalizes_synthetic_residential_row_and_marks_missing_fields_unknown(self):
        synthetic_source_row = {
            "№ п/п": "17",
            "Номер из ЕРУЛ": "TEST-EGRUL-17",
            "Наименование объекта": "«Многоквартирные жилые дома для временного персонала, г. Сочи»",
            "Наименование застройщика": "СИНТЕТИЧЕСКАЯ ДЕМО-ОРГАНИЗАЦИЯ",
            "Дата выдачи разрешения": "27.04.2015",
            "Номер выданного разрешения": "TEST-PERMIT-17",
        }
        records = normalize_registry_rows(
            [synthetic_source_row],
            dataset_kind="construction_permit",
            observed_at="2026-10-09T10:15:09Z",
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["record_type"], "registry_event")
        self.assertEqual(record["data_mode"], "real")
        self.assertEqual(record["source"]["record_mode"], "registry_status")
        self.assertEqual(record["source"]["rights_status"], "cleared")
        self.assertEqual(record["source"]["source_record_id"], "7707780887-reestrstroit:17")
        self.assertEqual(record["source"]["source_fields"], synthetic_source_row)
        self.assertNotIn("offer", record)
        self.assertNotIn("offer_id", record)
        self.assertEqual(record["registry_event"]["event_date"], "2015-04-27")
        self.assertEqual(record["project_reference"]["name"], synthetic_source_row["Наименование объекта"])
        self.assertIsNone(record["project_reference"]["address"])
        self.assertIsNone(record["project_reference"]["area_sqm"])
        self.assertEqual(record["project_reference"]["field_status"]["completion_date"], "unknown")
        self.assertEqual(record["facts"]["registry.permit_issue_date"]["status"], "observed")
        self.assertEqual(record["facts"]["registry.permit_issue_date"]["value"], "2015-04-27")
        self.assertEqual(record["facts"]["project.address"]["status"], "unknown")
        self.assertIsNone(record["facts"]["project.address"]["value"])

    def test_residential_filter_excludes_incidental_and_similar_words(self):
        self.assertTrue(is_explicit_residential_name("«Многоквартирные жилые дома, корпус 1»"))
        self.assertTrue(is_explicit_residential_name("Жилой квартал в селе Веселое"))
        self.assertFalse(
            is_explicit_residential_name(
                "Газопровод для реализации строительства жилого микрорайона"
            )
        )
        self.assertFalse(is_explicit_residential_name("МНПП Рязань-Москва, участок Жилево"))

    def test_incidental_housing_description_is_not_in_registry_layer(self):
        synthetic_rows = [
            {
                "№ п/п": "1",
                "Наименование объекта": "Газопровод для строительства жилого микрорайона",
                "Наименование заявителя": "Демо",
                "Дата выданного разрешения": "01.01.2020",
                "Номер выданного разрешения": "TEST-1",
                "Номер из ЕРУЛ": "",
            },
            {
                "№ п/п": "2",
                "Наименование объекта": "Жилой дом, объект DEMO",
                "Наименование заявителя": "Демо",
                "Дата выданного разрешения": "01.01.2020",
                "Номер выданного разрешения": "TEST-2",
                "Номер из ЕРУЛ": "",
            },
        ]
        records = normalize_registry_rows(
            synthetic_rows,
            dataset_kind="commissioning_permit",
            observed_at="2026-10-09T10:15:09+00:00",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"]["source_record_id"], "7707780887-puttingintooperation:2")

    def test_capture_timestamp_requires_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            normalize_registry_rows(
                [],
                dataset_kind="construction_permit",
                observed_at="2026-10-09T10:15:09",
            )

    def test_moscow_housing_word_match_in_pipeline_is_audit_only(self):
        synthetic_pipeline_row = {
            "№ п/п": "2003",
            "Номер из ЕРУЛ": "",
            "Наименование объекта": (
                "Реконструкция участка газопровода г. Москва для реализации "
                "строительства жилого микрорайона Южное Бутово"
            ),
            "Наименование застройщика": "СИНТЕТИЧЕСКАЯ ДЕМО-ОРГАНИЗАЦИЯ",
            "Дата выдачи разрешения": "03.08.2018",
            "Номер выданного разрешения": "TEST-PIPELINE-2003",
        }
        mentions = find_moscow_housing_mentions(
            [synthetic_pipeline_row],
            dataset_kind="construction_permit",
            observed_at="2026-10-09T10:15:09Z",
        )
        self.assertEqual(len(mentions), 1)
        self.assertFalse(mentions[0]["is_explicit_residential_title"])
        self.assertEqual(
            mentions[0]["classification"],
            "exclude_non_residential_object_with_incidental_housing_reference",
        )
        self.assertNotIn("offer_id", mentions[0])


if __name__ == "__main__":
    unittest.main()
