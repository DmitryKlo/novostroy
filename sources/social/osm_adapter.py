"""Conservative OSM POI classification and normalization.

The adapter preserves source tags and coordinates. It does not infer service
quality, ownership, operational status, district membership, or entrances.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


CLASSIFICATION_VERSION = "social-osm-2026-10-09-v1"

# Ordered, explicit rules. Unknown or conflicting tags remain unclassified.
_RULES: tuple[tuple[str, str, str], ...] = (
    ("pharmacy", "amenity", "pharmacy"),
    ("childcare", "amenity", "kindergarten"),
    ("education", "amenity", "school"),
    ("education", "amenity", "college"),
    ("education", "amenity", "university"),
    ("healthcare", "amenity", "clinic"),
    ("healthcare", "amenity", "doctors"),
    ("healthcare", "amenity", "hospital"),
    ("healthcare", "amenity", "dentist"),
    ("grocery", "shop", "supermarket"),
    ("grocery", "shop", "convenience"),
    ("green_space", "leisure", "park"),
    ("green_space", "leisure", "playground"),
    ("sports", "leisure", "sports_centre"),
    ("sports", "leisure", "fitness_centre"),
)


def classify_osm_tags(tags: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return a broad category and matched rule; unknown tags yield ``None``."""
    matches = [
        (category, f"{key}={value}")
        for category, key, value in _RULES
        if tags.get(key) == value
    ]
    categories = {category for category, _ in matches}
    if len(categories) != 1:
        return None, None
    category = next(iter(categories))
    rules = [rule for match_category, rule in matches if match_category == category]
    return category, ";".join(sorted(rules))


def _source_point(element: Mapping[str, Any]) -> tuple[float, float, str, bool] | None:
    """Return (lon, lat, role, approximate) only from values in the source response."""
    if element.get("type") == "node" and "lon" in element and "lat" in element:
        return float(element["lon"]), float(element["lat"]), "osm_node", False
    center = element.get("center")
    if isinstance(center, Mapping) and "lon" in center and "lat" in center:
        # Overpass's center for a way/relation is a derived center, not an entrance.
        return float(center["lon"]), float(center["lat"]), "overpass_center", True
    return None


def normalize_element(
    element: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any]:
    """Normalize one Overpass element while keeping unknown facts explicit."""
    osm_type = str(element.get("type", "unknown"))
    osm_id = element.get("id")
    tags = dict(element.get("tags") or {})
    category, reason = classify_osm_tags(tags)
    point = _source_point(element)
    location = None
    if point is not None:
        lon, lat, role, approximate = point
        location = {
            "type": "Point",
            "coordinates": [lon, lat],
            "role": role,
            "approximate": approximate,
        }

    return {
        "source": "openstreetmap",
        "source_kind": "osm",
        "coverage": "sample",
        "source_id": f"{osm_type}/{osm_id}" if osm_id is not None else None,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "osm_version": element.get("version"),
        "osm_timestamp": element.get("timestamp"),
        "observed_at": observed_at,
        "category": category,
        "classification_rule": reason,
        "classification_version": CLASSIFICATION_VERSION,
        "tags_raw": tags,
        "location": location,
        "entrance": None,
        "ownership": None,
        "quality": None,
        "district_id": None,
        "district_boundary_version": None,
    }


def normalize_elements(
    elements: Sequence[Mapping[str, Any]],
    *,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Normalize elements without filtering unclassified or ungeocoded records."""
    return [normalize_element(element, observed_at=observed_at) for element in elements]
