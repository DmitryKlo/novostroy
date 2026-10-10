"""Stable, deliberately narrow OSM categories for social criteria.

This taxonomy is separate from the historic broad categories in
``osm_adapter`` so existing normalized sample hashes and consumers stay
stable. A source tag can map to more than one category (for example, a shared
bus/tram platform). It never infers service quality, ownership, insurance, or
operating status.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TAXONOMY_VERSION = "social-criteria-osm-2026-10-09-v1"

# Category order is stable and used when emitting facts.
SOCIAL_CATEGORIES: tuple[str, ...] = (
    "clinic",
    "kindergarten",
    "school",
    "park",
    "pharmacy",
    "supermarket",
    "bus_stop",
    "tram_stop",
    "metro_station",
    "metro_entrance",
    "police_station",
    "ambulance_station",
    "fire_station",
)


def _is(tags: Mapping[str, Any], key: str, value: str) -> bool:
    return tags.get(key) == value


def classify_criteria_tags(tags: Mapping[str, Any]) -> tuple[str, ...]:
    """Return all narrow categories directly supported by the source tags."""
    categories: list[str] = []

    if _is(tags, "amenity", "clinic") or _is(tags, "healthcare", "clinic"):
        categories.append("clinic")
    if _is(tags, "amenity", "kindergarten"):
        categories.append("kindergarten")
    if _is(tags, "amenity", "school"):
        categories.append("school")
    if _is(tags, "leisure", "park"):
        categories.append("park")
    if _is(tags, "amenity", "pharmacy"):
        categories.append("pharmacy")
    if _is(tags, "shop", "supermarket"):
        categories.append("supermarket")

    # A shared platform can serve more than one mode; retain both facts.
    if _is(tags, "highway", "bus_stop") or (
        _is(tags, "public_transport", "platform") and _is(tags, "bus", "yes")
    ) or (
        _is(tags, "public_transport", "stop_position") and _is(tags, "bus", "yes")
    ):
        categories.append("bus_stop")
    if _is(tags, "railway", "tram_stop") or (
        _is(tags, "public_transport", "platform") and _is(tags, "tram", "yes")
    ) or (
        _is(tags, "public_transport", "stop_position") and _is(tags, "tram", "yes")
    ):
        categories.append("tram_stop")

    if (
        _is(tags, "railway", "station") and _is(tags, "station", "subway")
    ) or (
        _is(tags, "public_transport", "station") and _is(tags, "subway", "yes")
    ):
        categories.append("metro_station")
    if _is(tags, "railway", "subway_entrance"):
        categories.append("metro_entrance")

    if _is(tags, "amenity", "police"):
        categories.append("police_station")
    if _is(tags, "emergency", "ambulance_station"):
        categories.append("ambulance_station")
    if _is(tags, "amenity", "fire_station"):
        categories.append("fire_station")

    return tuple(category for category in SOCIAL_CATEGORIES if category in categories)


def source_access_kind(tags: Mapping[str, Any]) -> str | None:
    """Return entrance semantics only for explicitly mapped access features."""
    if _is(tags, "railway", "subway_entrance") or _is(tags, "entrance", "yes"):
        return "entrance_known"
    return None
