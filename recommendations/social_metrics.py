"""Evidence-aware social index with explicit coverage and route limits.

Counts refer only to a declared query area/category. An OSM point or a
footprint center cannot be treated as a routed public entrance.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def summarize_infrastructure(
    pois: Sequence[Mapping[str, Any]],
    required_categories: Sequence[str],
    coverage_by_category: Mapping[str, str],
    *,
    travel_minutes_by_source_id: Mapping[str, float] | None = None,
    time_threshold_minutes: float = 15.0,
) -> dict[str, Any]:
    """Return null index unless every category and required route is covered.

    Coverage must be explicitly `complete` for the chosen query area. A sample
    never produces a citywide count or score. Each category has fixed equal
    weight, so missing categories cannot increase the weight of known ones.
    """
    if time_threshold_minutes <= 0:
        raise ValueError("time_threshold_minutes must be positive")
    categories = tuple(dict.fromkeys(required_categories))
    details: dict[str, Any] = {}
    complete = bool(categories)
    for category in categories:
        coverage = coverage_by_category.get(category, "unknown")
        observed = [poi for poi in pois if poi.get("category") == category]
        if coverage != "complete":
            complete = False
        details[category] = {
            "coverage": coverage,
            "count": len(observed) if coverage == "complete" else None,
            "observed_count": len(observed),
            "within_minutes": None,
        }
    routes = travel_minutes_by_source_id or {}
    index: float | None = None
    if complete:
        category_presence = []
        for category in categories:
            observed = [poi for poi in pois if poi.get("category") == category]
            missing_route = any(
                poi.get("source_id") not in routes
                or poi.get("entrance") is None
                for poi in observed
            )
            if missing_route:
                complete = False
                break
            nearby = sum(
                1 for poi in observed
                if routes[poi["source_id"]] <= time_threshold_minutes
            )
            details[category]["within_minutes"] = nearby
            category_presence.append(1.0 if nearby else 0.0)
        if complete:
            index = sum(category_presence) / len(categories)
    return {
        "index": {
            "value": index,
            "coverage": "complete" if complete else (
                "partial" if any(v["coverage"] == "complete" for v in details.values()) else "unknown"
            ),
            "declared_coverage_threshold": 1.0,
            "time_threshold_minutes": time_threshold_minutes,
            "method": "equal_category_presence_with_verified_route",
        },
        "categories": details,
    }
