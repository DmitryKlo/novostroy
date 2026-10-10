"""Boundary and freshness regressions for the pure selection engine."""

from __future__ import annotations

import unittest

from recommendations.engine import (
    eligible_offers, filter_offers, parse_query, recommend, resolve_filters, search,
)
from recommendations.social_metrics import summarize_infrastructure


SNAPSHOT = "2026-10-09T00:00:00Z"


def offer(offer_id: str, price: int | None, district: str | None = "a", *,
          complex_id: str = "complex-a", basis: str = "asking_total",
          observed_at: str = SNAPSHOT, mode: str = "demo") -> dict:
    return {
        "offer_id": offer_id, "unit_id": None, "complex_id": complex_id,
        "district_id": district, "rooms": 2, "area_sqm": 50.0,
        "price": None if price is None else {
            "amount_rub": price, "basis": basis, "observed_at": observed_at
        },
        "source": {
            "record_mode": mode,
            "quality": "illustrative" if mode == "demo" else "dated_asking",
            "observed_at": observed_at,
            "rights_status": "demo" if mode == "demo" else "cleared",
        },
        "availability": None, "social": {"coverage": "unknown", "index": None},
    }


class EngineBoundaryTests(unittest.TestCase):
    def test_inclusive_budget_and_missing_price(self) -> None:
        candidates = [offer("lo", 5_000_000), offer("hi", 15_000_000),
                      offer("outside", 15_000_001), offer("missing", None)]
        matches = filter_offers(candidates, {
            "budget_min_rub": 5_000_000, "budget_max_rub": 15_000_000
        })
        self.assertEqual([item["offer_id"] for item in matches], ["hi", "lo"])

    def test_district_and_complex_are_strict(self) -> None:
        candidates = [offer("a", 10, "a", complex_id="one"),
                      offer("b", 10, "b", complex_id="one"),
                      offer("c", 10, None, complex_id="two")]
        self.assertEqual(
            [item["offer_id"] for item in filter_offers(
                candidates, {"district_ids": ["a"], "complex_ids": ["one"]}
            )], ["a"]
        )
        self.assertEqual(filter_offers(candidates, {"district_ids": []}), [])

    def test_conditional_price_requires_explicit_basis(self) -> None:
        candidates = [offer("asking", 10), offer("conditional", 8, basis="conditional_total")]
        result = filter_offers(candidates, {"budget_max_rub": 10, "price_basis": "asking_total"})
        self.assertEqual([item["offer_id"] for item in result], ["asking"])
        self.assertEqual(candidates[1]["price"]["basis"], "conditional_total")

    def test_real_snapshot_expires_with_wall_clock(self) -> None:
        candidates = [offer("stale", 10, observed_at=SNAPSHOT, mode="developer_offer"),
                      offer("fresh", 10, observed_at="2026-10-10T00:00:00Z", mode="developer_offer"),
                      offer("future", 10, observed_at="2026-10-11T00:00:01Z", mode="developer_offer"),
                      offer("demo", 10)]
        chosen = eligible_offers(candidates, snapshot_at=SNAPSHOT,
                                 data_mode="real", as_of="2026-10-11T00:00:00Z")
        self.assertEqual([item["offer_id"] for item in chosen], ["fresh"])
        self.assertEqual([item["offer_id"] for item in eligible_offers(
            candidates, snapshot_at=SNAPSHOT, data_mode="demo", as_of="2030-01-01T00:00:00Z"
        )], ["demo"])

    def test_real_mode_requires_cleared_rights(self) -> None:
        cleared = offer("cleared", 10, mode="developer_offer")
        restricted = offer("restricted", 10, mode="developer_offer")
        restricted["source"]["rights_status"] = "restricted"
        self.assertEqual([item["offer_id"] for item in eligible_offers(
            [cleared, restricted], snapshot_at=SNAPSHOT, data_mode="real", as_of=SNAPSHOT
        )], ["cleared"])

    def test_search_and_recommend_count_before_limit(self) -> None:
        candidates = [offer(f"offer-{index}", index * 1_000_000) for index in range(1, 5)]
        request = {"filters": {"budget_max_rub": 4_000_000, "limit": 2}}
        for operation in (search, recommend):
            result = operation(candidates, request, snapshot_at=SNAPSHOT)
            self.assertEqual(result["count"], 4)
            self.assertEqual(len(result["items"]), 2)

    def test_area_query_is_not_budget(self) -> None:
        self.assertEqual(parse_query("до 80 м²")["filters"], {"area_max_sqm": 80.0})
        self.assertEqual(parse_query("до 16 млн и до 80 м²")["filters"], {
            "budget_max_rub": 16_000_000, "area_max_sqm": 80.0,
        })

    def test_district_ambiguity_and_exact_district(self) -> None:
        districts = [{"district_id": "presnensky", "name": "Пресненский"}]
        self.assertEqual(parse_query("в Соколе", districts)["clarification"]["code"],
                         "query_ambiguous_district")
        self.assertEqual(parse_query("в Пресненском районе", districts)["filters"],
                         {"district_ids": ["presnensky"]})

    def test_unsupported_preference_still_needs_clarification(self) -> None:
        resolved = resolve_filters({
            "query": "самый престижный район", "filters": {"district_ids": ["presnensky"]}
        })
        self.assertEqual(resolved["clarification"]["code"], "query_unsupported_preference")

    def test_conditional_price_not_ranked_as_asking_price(self) -> None:
        candidates = [offer("asking", 9_000_000),
                      offer("conditional", 5_000_000, basis="conditional_total")]
        result = recommend(candidates, {"filters": {"budget_max_rub": 10_000_000}},
                           snapshot_at=SNAPSHOT)
        self.assertEqual([item["offer_id"] for item in result["items"]],
                         ["asking", "conditional"])
        self.assertIsNone(result["items"][1]["recommendation"]["score"])

    def test_unknown_social_coverage_has_no_score(self) -> None:
        candidate = offer("one", 9_000_000)
        result = recommend([candidate], {
            "filters": {"budget_max_rub": 10_000_000},
            "preferences": {"infrastructure_categories": ["healthcare"]},
        }, snapshot_at=SNAPSHOT)
        recommendation = result["items"][0]["recommendation"]
        self.assertIsNone(recommendation["score"])
        self.assertIn("недостаточно", " ".join(recommendation["explanation"]))
        self.assertIn("не подтверждено", " ".join(recommendation["explanation"]))

    def test_zero_count_needs_complete_coverage(self) -> None:
        unknown = summarize_infrastructure([], ["pharmacy"], {})
        complete = summarize_infrastructure([], ["pharmacy"], {"pharmacy": "complete"})
        self.assertIsNone(unknown["categories"]["pharmacy"]["count"])
        self.assertEqual(complete["categories"]["pharmacy"]["count"], 0)
        self.assertEqual(complete["index"]["value"], 0.0)


if __name__ == "__main__":
    unittest.main()
