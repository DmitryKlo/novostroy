"""Validate shape and independence of the social search acceptance cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "quality/acceptance_cases.json"
ALLOWED_FILTERS = {
    "budget_min_rub",
    "budget_max_rub",
    "district_ids",
    "rooms",
    "area_min_sqm",
    "area_max_sqm",
    "price_basis",
    "limit",
}


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def main() -> None:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases")
    assert isinstance(cases, list) and len(cases) == 100
    assert data.get("case_count") == len(cases)
    ids = [case.get("case_id") for case in cases]
    assert ids == [f"SC-{index:03d}" for index in range(1, 101)]

    for case in cases:
        assert case.get("name") and case.get("group")
        assert case.get("expected", {}).get("expected_invariants")
        assert all(
            key not in {"offer_id", "offer_ids"}
            for node in walk(case)
            for key in node
        ), f"offer ID present in {case['case_id']}"
        if case["operation"] in {"search", "recommendations"}:
            request = case.get("request", {})
            assert "filters" in request and isinstance(request["filters"], dict)
            unknown = set(request["filters"]) - ALLOWED_FILTERS
            assert not unknown, f"unsupported filter fields in {case['case_id']}: {sorted(unknown)}"
        clarification = case["expected"].get("clarification")
        if isinstance(clarification, dict) and clarification.get("required"):
            assert any(
                item.get("predicate") == "empty_until_required_clarification_is_resolved"
                for item in case["expected"]["expected_invariants"]
            ), f"ambiguous case permits results: {case['case_id']}"

    groups = {}
    for case in cases:
        groups[case["group"]] = groups.get(case["group"], 0) + 1
    assert groups == {
        "strict_budget": 20,
        "strict_district": 20,
        "combined_filters": 20,
        "query_ambiguity": 20,
        "missing_data": 20,
    }, groups
    print(f"validated {len(cases)} independent acceptance cases; groups={groups}")


if __name__ == "__main__":
    main()
