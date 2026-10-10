"""Independent v1.1 predicate semantics and evidence-boundary regressions."""

from __future__ import annotations

from copy import deepcopy
import unittest

from contracts.build_registry import build_registry
from recommendations.criteria import canonical_facts, load_registry, validate_predicates
from recommendations.engine import filter_offers, search
from recommendations.test_engine import SNAPSHOT, offer


def fact(value, *, unit="boolean", status="observed", scope="building",
         rights="demo", coverage=None, observed_at=SNAPSHOT):
    return {
        "value": value, "unit": unit, "status": status, "scope": scope,
        "observed_at": observed_at,
        "provenance": {"rights_status": rights, "coverage_status": coverage,
                       "source_id": "test", "source_record_id": "1"},
    }


class CriteriaTests(unittest.TestCase):
    def test_registry_has_unique_typed_keys_and_labels(self) -> None:
        registry = load_registry()
        self.assertEqual(registry, build_registry(), "Regenerate contracts/criteria_registry.json after matrix changes")
        keys = [entry["key"] for entry in registry["criteria"]]
        self.assertEqual(len(keys), 55)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(entry["label_ru"] and entry["value_type"] for entry in registry["criteria"]))

    def test_unknown_key_operator_and_value_are_rejected(self) -> None:
        bad = [
            {"key": "not.a.criterion", "op": "equals", "value": 1},
            {"key": "unit.floor", "op": "contains", "value": "1"},
            {"key": "unit.floor", "op": "min", "value": True},
            {"key": "unit.floor", "op": "in", "value": []},
            {"key": "unit.floor", "op": "in", "value": list(range(65))},
            {"key": "unit.floor", "op": "min", "value": "1e999999999"},
            {"key": "building.closed_territory", "op": "equals", "value": "false"},
            {"key": "building.parking_type", "op": "equals", "value": "unverified_rooftop"},
            {"key": "air.district_cleanliness_rank", "op": "min", "value": 2},
        ]
        for predicate in bad:
            with self.subTest(predicate=predicate), self.assertRaises(ValueError):
                validate_predicates([predicate])

    def test_numeric_bounds_are_inclusive_and_missing_fails(self) -> None:
        first = offer("first", 10_000_000)
        first["floor"] = 7
        second = offer("second", 10_000_000)
        second["floor"] = 8
        missing = offer("missing", 10_000_000)
        filters = {"predicates": [
            {"key": "unit.floor", "op": "min", "value": 7},
            {"key": "unit.floor", "op": "max", "value": 7},
        ]}
        self.assertEqual([row["offer_id"] for row in filter_offers([first, second, missing], filters)],
                         ["first"])

    def test_nonfinite_area_never_satisfies_strict_bounds(self) -> None:
        invalid = offer("invalid", 10)
        invalid["area_sqm"] = float("nan")
        self.assertEqual(filter_offers([invalid], {"area_min_sqm": 50}), [])
        for threshold in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                filter_offers([offer("valid", 10)], {"area_max_sqm": threshold})

    def test_price_per_sqm_exact_ratio_and_asking_only(self) -> None:
        chosen = offer("chosen", 10_000_001)
        chosen["area_sqm"] = 3.0
        conditional = offer("conditional", 10_000_001, basis="conditional_total")
        conditional["area_sqm"] = 3.0
        lower = {"predicates": [{"key": "offer.price_per_sqm_rub", "op": "min",
                                 "value": "3333333.6666666666666666"}]}
        upper = {"predicates": [{"key": "offer.price_per_sqm_rub", "op": "max",
                                 "value": "3333333.6666666666666666"}]}
        self.assertEqual([row["offer_id"] for row in filter_offers([chosen, conditional], lower)],
                         ["chosen"])
        self.assertEqual(filter_offers([chosen, conditional], upper), [])
        exact = offer("exact", 10_000_000)
        exact["area_sqm"] = 40.0
        self.assertEqual(filter_offers([exact], {"predicates": [
            {"key": "offer.price_per_sqm_rub", "op": "min", "value": 250000},
            {"key": "offer.price_per_sqm_rub", "op": "max", "value": 250000},
        ]})[0]["offer_id"], "exact")

    def test_supplied_price_ratio_must_match_canonical_inputs(self) -> None:
        row = offer("row", 10_000_000)
        row["facts"] = {"offer.price_per_sqm_rub": fact(
            {"numerator_rub": 9_000_000, "denominator_sqm": "50"},
            unit="RUB/m2", status="derived", scope="offer")}
        self.assertEqual(filter_offers([row], {"predicates": [
            {"key": "offer.price_per_sqm_rub", "op": "max", "value": 300000,
             }
        ]}), [])

    def test_exact_area_fact_preserves_digits_lost_in_legacy_float(self) -> None:
        row = offer("precise", 10_000_000)
        row["area_sqm"] = 40.0
        exact_area = "40.000000000000000001"
        row["facts"] = {
            "unit.area_sqm": fact(exact_area, unit="m²", status="illustrative", scope="unit"),
            "offer.price_per_sqm_rub": fact(
                {"numerator_rub": 10_000_000, "denominator_sqm": exact_area},
                unit="RUB/m2", status="illustrative", scope="offer"),
        }
        self.assertEqual(filter_offers([row], {"predicates": [
            {"key": "offer.price_per_sqm_rub", "op": "min", "value": 250000}]}), [])
        self.assertEqual(len(filter_offers([row], {"predicates": [
            {"key": "offer.price_per_sqm_rub", "op": "max", "value": 250000}]})), 1)

    def test_unknown_stale_conflict_estimated_and_missing_fail(self) -> None:
        predicate = {"predicates": [{"key": "building.closed_territory", "op": "equals", "value": False}]}
        good = offer("good", 10)
        good["facts"] = {"building.closed_territory": fact(False)}
        rows = [good]
        for status in ("unknown", "stale", "conflict", "not_applicable", "estimated"):
            row = offer(status, 10)
            row["facts"] = {"building.closed_territory": fact(
                None if status != "estimated" else False, status=status)}
            rows.append(row)
        rows.append(offer("missing", 10))
        self.assertEqual([row["offer_id"] for row in filter_offers(rows, predicate)], ["good"])

    def test_real_facts_require_cleared_rights_and_valid_observation_time(self) -> None:
        request = {"filters": {"predicates": [
            {"key": "building.lift_present", "op": "equals", "value": True}]}}
        row = offer("real", 10, mode="developer_offer")
        row["facts"] = {"building.lift_present": fact(True, rights="cleared")}
        self.assertEqual(search([row], request, snapshot_at=SNAPSHOT,
                                data_mode="real", as_of=SNAPSHOT)["count"], 1)
        for mutate in (
            lambda x: x["facts"]["building.lift_present"]["provenance"].update(rights_status="unknown"),
            lambda x: x["facts"]["building.lift_present"].update(status="illustrative"),
            lambda x: x["facts"]["building.lift_present"].update(observed_at="2026-10-10T00:00:00Z"),
        ):
            changed = deepcopy(row)
            mutate(changed)
            self.assertEqual(search([changed], request, snapshot_at=SNAPSHOT,
                                    data_mode="real", as_of=SNAPSHOT)["count"], 0)

    def test_sample_nearest_cannot_pass_complete_coverage(self) -> None:
        row = offer("sample", 10)
        predicate = {"predicates": [{"key": "walk_time_metro_s", "op": "max", "value": 600}]}
        row["facts"] = {"walk_time_metro_s": fact(
            300, unit="s", status="illustrative", scope="nearest_in_sample", coverage="partial")}
        self.assertEqual(filter_offers([row], predicate), [])
        row["facts"]["walk_time_metro_s"]["provenance"]["coverage_status"] = "complete"
        self.assertEqual(filter_offers([row], predicate), [])
        row["facts"]["walk_time_metro_s"]["scope"] = "complex"
        self.assertEqual(len(filter_offers([row], predicate)), 1)

    def test_environment_proxy_requires_declared_bounded_coverage_and_unit(self) -> None:
        row = offer("road", 10)
        predicate = {"predicates": [{"key": "noise_proxy.major_road_distance_m",
                                     "op": "max", "value": 500}]}
        row["facts"] = {"noise_proxy.major_road_distance_m": fact(
            500, unit="m", status="illustrative", scope="building_or_property_point")}
        self.assertEqual(filter_offers([row], predicate), [])
        row["facts"]["noise_proxy.major_road_distance_m"]["provenance"]["coverage_status"] = "source_bounded"
        self.assertEqual(len(filter_offers([row], predicate)), 1)
        row["facts"]["noise_proxy.major_road_distance_m"]["unit"] = "km"
        self.assertEqual(filter_offers([row], predicate), [])

    def test_canonical_projection_is_additive_and_exact(self) -> None:
        row = offer("row", 12_000_000)
        row["area_sqm"] = 48.0
        projected = canonical_facts(row)
        self.assertEqual(projected["offer.price_per_sqm_rub"]["value"],
                         {"numerator_rub": 12_000_000, "denominator_sqm": "48.0"})
        self.assertEqual(projected["unit.rooms"]["value"], 2)
        self.assertNotIn("unit.floor", projected)


if __name__ == "__main__":
    unittest.main()
