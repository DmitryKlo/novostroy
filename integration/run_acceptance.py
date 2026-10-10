"""Execute the 100 independent acceptance predicates against actual code.

The first 80 cases exercise search/recommendations on a deliberately small
candidate set with boundary prices, missing fields, and two districts. The
remaining 20 exercise the social adapter and the coverage-aware index.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recommendations.engine import recommend, search  # noqa: E402
from recommendations.social_metrics import summarize_infrastructure  # noqa: E402
from sources.social.osm_adapter import normalize_element  # noqa: E402


SNAPSHOT_AT = "2026-10-09T12:00:00Z"
DISTRICTS = [
    {"district_id": "district-test-a", "name": "Тверской", "boundary_version": "test-v1"},
    {"district_id": "district-test-b", "name": "Пресненский", "boundary_version": "test-v1"},
]


def _offer(offer_id: str, amount: int | None, district_id: str | None,
           rooms: int | None, area: float | None, basis: str = "asking_total") -> dict[str, Any]:
    return {
        "offer_id": offer_id, "unit_id": None, "complex_id": "test-complex",
        "district_id": district_id, "rooms": rooms, "area_sqm": area,
        "floor": None,
        "price": None if amount is None else {
            "amount_rub": amount, "basis": basis, "observed_at": SNAPSHOT_AT
        },
        "source": {
            "name": "Acceptance fixture", "quality": "illustrative",
            "record_mode": "demo", "observed_at": SNAPSHOT_AT
        },
        "availability": "unknown", "social": {"coverage": "unknown", "index": None},
    }


OFFERS = [
    _offer("offer-01", 5_000_000, "district-test-a", 1, 35),
    _offer("offer-02", 10_000_000, "district-test-a", 2, 55),
    _offer("offer-03", 15_000_000, "district-test-b", 2, 65),
    _offer("offer-04", 16_000_000, "district-test-b", 3, 75),
    _offer("offer-05", 20_000_000, "district-test-a", 4, 95),
    _offer("offer-06", 10_000_000, "district-test-b", 1, 45, "conditional_total"),
    _offer("offer-07", None, "district-test-a", 2, 55),
    _offer("offer-08", 9_000_000, None, 2, 50),
    _offer("offer-09", 11_000_000, "district-test-b", None, None),
    _offer("offer-10", 17_000_000, "district-test-b", 2, 60),
]


def _oracle_matches(offers: list[Mapping[str, Any]], filters: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Independent direct assertions on the hand-authored candidate rows."""
    matches = []
    for offer in offers:
        price = offer["price"]
        amount = price["amount_rub"] if price else None
        if "budget_min_rub" in filters and (amount is None or amount < filters["budget_min_rub"]):
            continue
        if "budget_max_rub" in filters and (amount is None or amount > filters["budget_max_rub"]):
            continue
        if "price_basis" in filters and (price is None or price["basis"] != filters["price_basis"]):
            continue
        if "district_ids" in filters and offer["district_id"] not in filters["district_ids"]:
            continue
        if "complex_ids" in filters and offer["complex_id"] not in filters["complex_ids"]:
            continue
        if "rooms" in filters and offer["rooms"] not in filters["rooms"]:
            continue
        if "area_min_sqm" in filters and (offer["area_sqm"] is None or offer["area_sqm"] < filters["area_min_sqm"]):
            continue
        if "area_max_sqm" in filters and (offer["area_sqm"] is None or offer["area_sqm"] > filters["area_max_sqm"]):
            continue
        matches.append(offer)
    return matches


def _values(response: Mapping[str, Any], path: str) -> list[Any]:
    if path == "items":
        return [response["items"]]
    if path.startswith("items[*]"):
        keys = [key for key in path.removeprefix("items[*]").split(".") if key]
        output = []
        for item in response["items"]:
            value: Any = item
            for key in keys:
                value = value.get(key) if isinstance(value, Mapping) else None
            output.append(value)
        return output
    value: Any = response
    for key in path.split("."):
        value = value.get(key) if isinstance(value, Mapping) else None
    return [value]


def _check_response_case(case: Mapping[str, Any]) -> None:
    request = case["request"]
    operation = recommend if case["operation"] == "recommendations" else search
    response = operation(OFFERS, request, snapshot_at=SNAPSHOT_AT,
                         data_mode="demo", districts=DISTRICTS)
    expected = case["expected"]
    clarification = expected.get("clarification")
    if isinstance(clarification, Mapping) and clarification.get("required"):
        actual = response["clarification"]
        assert actual is not None, "missing required clarification"
        assert actual["field"] == clarification["field"]
        assert actual["code"] == clarification["code"]
        assert response["items"] == [] and response["count"] == 0
    else:
        assert response["clarification"] is None, "unexpected clarification"
        criteria = dict(request.get("filters") or {})
        if case["case_id"] == "SC-017":
            criteria["budget_max_rub"] = 16_000_000
        oracle = _oracle_matches(OFFERS, criteria)
        assert response["count"] == len(oracle), (response["count"], len(oracle))
        assert len(response["items"]) == min(len(oracle), criteria.get("limit", 20))
        expected_ids = {item["offer_id"] for item in oracle}
        assert {item["offer_id"] for item in response["items"]}.issubset(expected_ids)
    for invariant in expected["expected_invariants"]:
        assert invariant["scope"] == "response"
        predicate = invariant.get("predicate")
        path = invariant["path"]
        values = _values(response, path)
        if predicate == "empty_until_required_clarification_is_resolved":
            assert not response["items"]
        elif predicate == "never_include_item_outside_selected_districts":
            selected = set((request.get("filters") or {}).get("district_ids", []))
            assert all(item["district_id"] in selected for item in response["items"])
        elif predicate == "every_item_satisfies_all_structured_hard_filters":
            criteria = request.get("filters") or {}
            assert len(_oracle_matches(response["items"], criteria)) == len(response["items"])
        elif predicate == "empty_when_no_candidate_satisfies_hard_filters":
            criteria = request.get("filters") or {}
            if not _oracle_matches(OFFERS, criteria):
                assert response["items"] == []
        else:
            op = invariant["op"]
            target = invariant["value"]
            for value in values:
                if op == "eq":
                    assert value == target, (path, value, target)
                elif op == "in":
                    assert value in target, (path, value, target)
                elif op == "gte":
                    assert value is not None and value >= target, (path, value, target)
                elif op == "lte":
                    assert value is not None and value <= target, (path, value, target)
                elif op == "between_inclusive":
                    assert value is not None and target[0] <= value <= target[1], (path, value, target)
                else:
                    raise AssertionError(f"unknown operator: {op}")


def _check_social_case(case: Mapping[str, Any]) -> None:
    predicates = case["expected"]["expected_invariants"]
    assert len(predicates) == 1 and predicates[0]["scope"] == "social_data"
    predicate = predicates[0]["predicate"]
    base = {"type": "node", "id": 42, "lon": 37.6, "lat": 55.7,
            "tags": {"amenity": "clinic"}}
    normalized = normalize_element(base, observed_at=SNAPSHOT_AT)
    unknown = summarize_infrastructure([normalized], ["healthcare"], {})
    incomplete = summarize_infrastructure([normalized], ["healthcare"], {"healthcare": "sample"})
    complete_empty = summarize_infrastructure([], ["healthcare"], {"healthcare": "complete"})
    if predicate == "unknown_not_zero":
        if case["case_id"] == "SC-081":
            assert unknown["categories"]["healthcare"]["count"] is None
        elif case["case_id"] == "SC-090":
            assert normalized.get("travel_time") is None
            assert unknown["index"]["value"] is None
        else:
            raise AssertionError(f"unexpected unknown_not_zero case: {case['case_id']}")
        return
    checks = {
        "missing_is_unknown": lambda: unknown["index"]["coverage"] == "unknown",
        "null_below_declared_coverage_threshold": lambda: incomplete["index"]["value"] is None,
        "zero_only_if_query_coverage_is_complete": lambda: complete_empty["categories"]["healthcare"]["count"] == 0
        and unknown["categories"]["healthcare"]["count"] is None,
        "unknown_not_assumed_public_or_private": lambda: normalized["ownership"] is None,
        "unknown_not_zero_rating": lambda: normalized["quality"] is None,
        "unknown_not_inferred_from_category": lambda: normalize_element(
            {**base, "tags": {"amenity": "school"}}, observed_at=SNAPSHOT_AT
        )["quality"] is None,
        "source_point_is_not_assumed_entrance": lambda: normalized["entrance"] is None,
        "true_for_overpass_center": lambda: normalize_element(
            {"type": "way", "id": 43, "center": {"lon": 37.6, "lat": 55.7}, "tags": {}},
            observed_at=SNAPSHOT_AT
        )["location"]["approximate"] is True,
        "unknown_not_wildcard": lambda: normalized["district_id"] is None,
        "do_not_assign_from_unversioned_geometry": lambda: normalized["district_id"] is None,
        "do_not_conflate_identifier_types": lambda: normalize_element(
            {**base, "tags": {"municipality": "A"}}, observed_at=SNAPSHOT_AT
        )["district_id"] is None,
        "record_source_version_or_leave_unknown": lambda: normalized["district_boundary_version"] is None,
        "unclassified_not_guessed": lambda: normalize_element(
            {**base, "tags": {"amenity": "pharmacy", "shop": "supermarket"}
             if case["case_id"] == "SC-096" else {"amenity": "cafe"}},
            observed_at=SNAPSHOT_AT
        )["category"] is None,
        "sample_not_citywide": lambda: normalized["coverage"] == "sample",
        "do_not_create_rating": lambda: normalized["quality"] is None,
        "no_ml_generated_label": lambda: normalized["ownership"] is None,
        "category_is_not_quality_evidence": lambda: normalized["category"] == "healthcare"
        and normalized["quality"] is None,
    }
    assert predicate in checks, f"unhandled social predicate: {predicate}"
    assert checks[predicate](), f"social predicate failed: {predicate}"


def main() -> None:
    data = json.loads((ROOT / "quality/acceptance_cases.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == 100
    seen = set()
    failures = []
    for case in cases:
        case_id = case["case_id"]
        assert case_id not in seen
        seen.add(case_id)
        try:
            if case["operation"] == "social_data":
                _check_social_case(case)
            else:
                _check_response_case(case)
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            failures.append(f"{case_id}: {exc}")
    assert len(seen) == 100
    if failures:
        raise AssertionError("\n".join(failures))
    print("PASS: 100/100 acceptance cases executed (80 engine, 20 social adapter/index)")


if __name__ == "__main__":
    main()
