"""Server-controlled public projection of catalogue and criterion responses."""

from __future__ import annotations

import os
from typing import Any, Mapping


# Read once at server startup. Requests cannot opt into source disclosure.
EXPOSE_SOURCES = os.environ.get("NOVOSTROY_EXPOSE_SOURCES", "0") == "1"
PUBLIC_SOURCE_FIELDS = ("quality", "record_mode", "observed_at", "rights_status")
PUBLIC_CRITERION_FIELDS = (
    "key", "label_ru", "group", "type", "value_type", "unit", "operators",
    "enum_values", "availability", "filterable", "coverage_requirement",
)
SAFE_METHODS = frozenset({"exact_ratio", "straight_line", "haversine_straight_line",
                          "routed", "source_assertion", "spatial_join"})
PRIVATE_FIELDS = frozenset({
    "source_id", "source_ids", "source_record_id", "source_fields",
    "source_updated_at", "source_version", "source_url", "source_page_url",
    "source_identity", "source_response_sha256", "raw_source",
    "provenance", "poi_source", "provider", "provider_id", "provider_name",
    "evidence", "license", "licenses",
})


def _fact(value: Mapping[str, Any]) -> dict[str, Any]:
    public = {key: public_payload(value[key]) for key in
              ("value", "unit", "status", "scope", "observed_at") if key in value}
    provenance = value.get("provenance")
    for key in ("coverage_status", "approximate", "method"):
        candidate = value.get(key)
        if candidate is None and isinstance(provenance, Mapping):
            candidate = provenance.get(key)
        if candidate is not None:
            if key == "method" and candidate not in SAFE_METHODS:
                continue
            public[key] = public_payload(candidate)
    return public


def public_payload(value: Any) -> Any:
    """Strip provider identity from every nested public JSON payload by default."""
    if EXPOSE_SOURCES:
        return value
    if isinstance(value, list):
        return [public_payload(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    if "provenance" in value and {"value", "status", "scope"}.issubset(value):
        return _fact(value)
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key in PRIVATE_FIELDS or key.startswith("provider_"):
            continue
        if key == "source":
            if isinstance(child, Mapping):
                result[key] = {field: public_payload(child[field]) for field in PUBLIC_SOURCE_FIELDS if field in child}
            continue
        if key == "facts" and isinstance(child, Mapping):
            result[key] = {fact_key: _fact(fact) if isinstance(fact, Mapping) else None
                           for fact_key, fact in child.items()}
            continue
        if key == "criteria" and isinstance(child, list):
            result[key] = [{field: public_payload(item[field]) for field in PUBLIC_CRITERION_FIELDS
                            if field in item} for item in child if isinstance(item, Mapping)]
            continue
        if key == "attribution":
            if {"kind", "url", "model_quality"}.issubset(value):
                result[key] = child
            continue
        result[key] = public_payload(child)
    return result
