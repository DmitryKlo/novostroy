"""Source-reference and price/display gates for one-page Level SSR sample."""

import json
from unittest import TestCase

from sources.property.developer_level_sample import parse_sample


def fixture(*, show_price: bool = True) -> bytes:
    values = [None] * 20
    values[3] = {"projects-moscow": 4}
    values[4] = {"flatsResultsRes": 5, "resultsRes": 6}
    values[5] = {"count": 7, "next": 8, "results": 9}
    values[6] = {"count": 10}
    values[7] = 300
    values[8] = "http://example.org/api/?offset=1"
    values[9] = [11]
    values[10] = 4
    values[11] = {"pk": 12, "project_id": 13, "building_id": 14,
                  "show_price": 15, "price": 16, "plan": 17,
                  "status_modified": 18}
    values[12] = 42
    values[13] = 9
    values[14] = 8
    values[15] = show_price
    values[16] = 12345678
    values[17] = "https://cdn.example.org/plan.svg"
    values[18] = "2026-09-01"
    return ('<script type="application/json" id="__NUXT_DATA__">' +
            json.dumps(values) + '</script>').encode()


class LevelSampleTests(TestCase):
    def test_devalue_total_is_resolved_and_price_requires_show_flag(self) -> None:
        rows, info = parse_sample(fixture(), "https://example.org/", "2026-10-10T00:00:00Z")
        self.assertEqual(info["catalog_total_claimed"], 300)
        self.assertEqual(info["sample_rows"], 1)
        self.assertEqual(rows[0]["publisher_unit_id"], "42")
        self.assertEqual(rows[0]["price_rub"], 12345678)
        self.assertEqual(rows[0]["media"][0]["role"], "apartment_plan")
        self.assertFalse(rows[0]["media"][0]["public_display_allowed"])
        hidden, _ = parse_sample(fixture(show_price=False), "https://example.org/",
                                 "2026-10-10T00:00:00Z")
        self.assertIsNone(hidden[0]["price_rub"])

