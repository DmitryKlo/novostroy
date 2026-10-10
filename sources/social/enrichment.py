"""Bounded, opt-in batch facts from POI geometry and configured routers.

No network connector is created here. Callers must pass a configured router
and a positive request budget to make route calls. POI samples are never
treated as complete coverage; available values retain an explicit sample
scope. Output facts follow the shared ``facts[key]`` provenance contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .accessibility import AccessPoint, poi_access_point, straight_line_distance_m
from .taxonomy import TAXONOMY_VERSION, classify_criteria_tags, source_access_kind


class PedestrianRouter(Protocol):
    def route(
        self,
        origin: AccessPoint,
        destination: AccessPoint,
        *,
        allow_approximate: bool = False,
    ) -> Mapping[str, Any]: ...


class TransitRouter(Protocol):
    def route_transit(
        self,
        origin: AccessPoint,
        destination: AccessPoint,
        *,
        allowed_modes: Sequence[str],
        require_direct: bool,
    ) -> Mapping[str, Any]: ...


WALK_CATEGORIES: tuple[str, ...] = (
    "clinic",
    "kindergarten",
    "school",
    "park",
    "pharmacy",
    "supermarket",
    "bus_stop",
    "tram_stop",
    "metro_entrance",
)
STRAIGHT_DISTANCE_CATEGORIES: tuple[str, ...] = (
    "police_station",
    "ambulance_station",
    "fire_station",
)
_WALK_FIELD = {
    "clinic": "walk_time_clinic_s",
    "kindergarten": "walk_time_kindergarten_s",
    "school": "walk_time_school_s",
    "park": "walk_time_park_s",
    "pharmacy": "walk_time_pharmacy_s",
    "supermarket": "walk_time_supermarket_s",
    "bus_stop": "walk_time_bus_stop_s",
    "tram_stop": "walk_time_tram_stop_s",
    "metro_entrance": "walk_time_metro_s",
}
_DISTANCE_FIELD = {
    "police_station": "distance_police_straight_line_m",
    "ambulance_station": "distance_ambulance_station_straight_line_m",
    "fire_station": "distance_fire_station_straight_line_m",
}
_ALLOWED_FACT_STATUSES = {
    "observed",
    "derived",
    "estimated",
    "illustrative",
    "unknown",
    "stale",
    "conflict",
    "not_applicable",
}
_ALLOWED_COVERAGE = {"complete", "partial", "unknown"}


def _source_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("source_id")
    if value:
        return str(value)
    if record.get("osm_type") is not None and record.get("osm_id") is not None:
        return f"{record['osm_type']}/{record['osm_id']}"
    return None


def _tags(record: Mapping[str, Any]) -> Mapping[str, Any]:
    tags = record.get("tags_raw")
    return tags if isinstance(tags, Mapping) else {}


def _poi_categories(record: Mapping[str, Any]) -> tuple[str, ...]:
    return classify_criteria_tags(_tags(record))


def _poi_access(record: Mapping[str, Any]) -> AccessPoint | None:
    """Subway entrances are explicit source access points; others stay labeled."""
    point = poi_access_point(record)
    if point is None:
        return None
    if source_access_kind(_tags(record)) == "entrance_known" and point.kind != "entrance_known":
        return AccessPoint(point.coordinates, "entrance_known", point.source_id)
    return point


def _origin_access(origin: Mapping[str, Any]) -> AccessPoint:
    point = origin.get("access_point")
    if isinstance(point, AccessPoint):
        return point
    if isinstance(point, Mapping):
        coordinates = point.get("coordinates")
        kind = point.get("kind")
        source_id = point.get("source_id")
        if coordinates is not None and isinstance(kind, str):
            return AccessPoint(tuple(coordinates), kind, source_id)
    coordinates = origin.get("coordinates")
    kind = origin.get("access_kind")
    if coordinates is None or kind is None:
        raise ValueError("each origin needs access_point or coordinates and access_kind")
    return AccessPoint(tuple(coordinates), kind, origin.get("source_id"))


def _bounded_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return number


def _fact(
    value: Any,
    *,
    unit: str | None,
    status: str,
    scope: str,
    observed_at: str | None,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if status not in _ALLOWED_FACT_STATUSES:
        raise ValueError(f"unsupported fact status: {status}")
    if status in {"unknown", "stale", "conflict"}:
        value = None
    fact: dict[str, Any] = {
        "value": value,
        "unit": unit,
        "status": status,
        "scope": scope,
        "observed_at": observed_at,
        "provenance": dict(provenance),
    }
    return fact


def _unknown_fact(
    *,
    unit: str | None,
    scope: str,
    source_version: str | None,
    reason: str,
    method: str,
) -> dict[str, Any]:
    return _fact(
        None,
        unit=unit,
        status="unknown",
        scope=scope,
        observed_at=None,
        provenance={
            "source_id": None,
            "source_record_id": None,
            "source_field": None,
            "source_updated_at": None,
            "rights_status": None,
            "method": method,
            "source_version": source_version,
            "coverage_status": "unknown",
            "reason": reason,
        },
    )


def _poi_provenance(
    poi: Mapping[str, Any],
    *,
    source_version: str | None,
    method: str,
    coverage_status: str,
    origin: AccessPoint | None = None,
    target: AccessPoint | None = None,
    source_field: str = "tags_raw",
) -> dict[str, Any]:
    return {
        "source_id": poi.get("source", "openstreetmap"),
        "source_record_id": _source_id(poi),
        "source_field": source_field,
        "source_updated_at": poi.get("osm_timestamp"),
        "rights_status": "ODbL-1.0; attribution required",
        "method": method,
        "source_version": source_version,
        "coverage_status": coverage_status,
        "origin_access_kind": origin.kind if origin else None,
        "target_access_kind": target.kind if target else None,
        "approximate": bool(
            origin is not None
            and target is not None
            and (origin.kind != "entrance_known" or target.kind != "entrance_known")
        ),
    }


def _field_coverage(
    category: str,
    *,
    category_coverage: Mapping[str, str],
    source_sample: bool,
    all_candidates_routed: bool,
) -> str:
    source_status = category_coverage.get(category, "unknown")
    if source_status not in _ALLOWED_COVERAGE:
        source_status = "unknown"
    if source_sample or source_status != "complete":
        return "unknown" if source_status == "unknown" else "partial"
    return "complete" if all_candidates_routed else "partial"


def _sorted_candidates(
    origin: AccessPoint,
    records: Sequence[Mapping[str, Any]],
    category: str,
) -> list[tuple[float, Mapping[str, Any], AccessPoint]]:
    candidates: list[tuple[float, Mapping[str, Any], AccessPoint]] = []
    for record in records:
        if category not in _poi_categories(record):
            continue
        point = _poi_access(record)
        if point is None:
            continue
        candidates.append((straight_line_distance_m(origin, point), record, point))
    return sorted(candidates, key=lambda row: (row[0], _source_id(row[1]) or ""))


def _validate_transit_result(result: Mapping[str, Any]) -> tuple[float, list[str]] | None:
    """Accept a direct bus/tram route only when the connector proves it."""
    if result.get("direct") is not True or result.get("direct_checked") is not True:
        return None
    transfer_count = result.get("transfer_count")
    if isinstance(transfer_count, bool) or transfer_count != 0:
        return None
    modes = result.get("modes")
    if not isinstance(modes, Sequence) or isinstance(modes, (str, bytes)):
        return None
    normalized_modes = [str(mode) for mode in modes]
    if not any(mode in {"bus", "tram"} for mode in normalized_modes):
        return None
    try:
        duration = _bounded_number(result.get("duration_s"), name="duration_s")
    except ValueError:
        return None
    return duration, normalized_modes


def _clinic_metadata_fact(
    metadata: Mapping[str, Any] | None,
    key: str,
    *,
    expected_unit: str | None,
    scope: str,
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return _unknown_fact(
            unit=expected_unit,
            scope=scope,
            source_version=None,
            reason="No authoritative clinic record was supplied; OSM tags do not establish this property.",
            method="official_registry_or_provider_record_required",
        )
    item = metadata.get(key)
    if not isinstance(item, Mapping):
        return _unknown_fact(
            unit=expected_unit,
            scope=scope,
            source_version=None,
            reason="No authoritative clinic record was supplied; OSM tags do not establish this property.",
            method="official_registry_or_provider_record_required",
        )
    status = item.get("status")
    value = item.get("value")
    provenance = item.get("provenance")
    if status not in _ALLOWED_FACT_STATUSES or not isinstance(provenance, Mapping):
        return _unknown_fact(
            unit=expected_unit,
            scope=scope,
            source_version=None,
            reason="Clinic metadata is missing required status or provenance.",
            method="official_registry_or_provider_record_required",
        )
    if status in {"unknown", "stale", "conflict"}:
        value = None
    return _fact(
        value,
        unit=item.get("unit", expected_unit),
        status=status,
        scope=scope,
        observed_at=item.get("observed_at"),
        provenance=provenance,
    )


def enrich_social_batch(
    origins: Sequence[Mapping[str, Any]],
    pois: Sequence[Mapping[str, Any]],
    *,
    pedestrian_router: PedestrianRouter | None = None,
    transit_router: TransitRouter | None = None,
    poi_source_version: str | None = None,
    poi_coverage: str = "unknown",
    category_coverage: Mapping[str, str] | None = None,
    clinic_metadata_by_source_id: Mapping[str, Mapping[str, Any]] | None = None,
    max_pedestrian_requests: int = 0,
    max_transit_requests: int = 0,
    candidate_limit_per_category: int = 5,
    allow_approximate_routes: bool = False,
) -> list[dict[str, Any]]:
    """Return bounded per-origin ``facts`` without inventing completeness.

    The POI source and router are independent inputs. With no router, or a
    zero request budget, no route call is made. Only explicit pedestrian
    router durations populate ``walk_time_*_s``. Straight-line distance is
    available exclusively under separate ``*_straight_line_m`` keys.

    ``poi_coverage`` uses ``complete|partial|unknown``; a sample normally uses
    ``unknown``. Category completeness must be asserted by the caller from a
    versioned source covering the stated geographic scope. This function
    never promotes sample coverage to complete.
    """
    if poi_coverage not in _ALLOWED_COVERAGE | {"sample"}:
        raise ValueError("poi_coverage must be complete, partial, unknown, or sample")
    if (
        isinstance(max_pedestrian_requests, bool)
        or not isinstance(max_pedestrian_requests, int)
        or max_pedestrian_requests < 0
    ):
        raise ValueError("max_pedestrian_requests must be a non-negative integer")
    if (
        isinstance(max_transit_requests, bool)
        or not isinstance(max_transit_requests, int)
        or max_transit_requests < 0
    ):
        raise ValueError("max_transit_requests must be a non-negative integer")
    if (
        isinstance(candidate_limit_per_category, bool)
        or not isinstance(candidate_limit_per_category, int)
        or candidate_limit_per_category < 1
    ):
        raise ValueError("candidate_limit_per_category must be a positive integer")

    records = list(pois)
    category_coverage = category_coverage or {}
    clinic_metadata_by_source_id = clinic_metadata_by_source_id or {}
    source_sample = poi_coverage != "complete"
    pedestrian_calls = 0
    transit_calls = 0
    outputs: list[dict[str, Any]] = []

    for origin_record in origins:
        origin = _origin_access(origin_record)
        complex_id = str(origin_record.get("complex_id") or origin_record.get("id") or "")
        facts: dict[str, dict[str, Any]] = {}
        routed_candidates: dict[str, list[tuple[float, Mapping[str, Any], AccessPoint, Mapping[str, Any]]]] = {}

        # Pedestrian route fields. Route only the bounded nearest-by-air
        # candidate set; any incomplete source or route sweep remains partial.
        for category in WALK_CATEGORIES:
            candidates = _sorted_candidates(origin, records, category)
            selected = candidates[:candidate_limit_per_category]
            results: list[tuple[float, Mapping[str, Any], AccessPoint, Mapping[str, Any]]] = []
            all_candidates_routed = bool(candidates) and len(selected) == len(candidates)
            for _, poi, destination in selected:
                if pedestrian_router is None or pedestrian_calls >= max_pedestrian_requests:
                    all_candidates_routed = False
                    continue
                if (
                    not allow_approximate_routes
                    and (origin.kind != "entrance_known" or destination.kind != "entrance_known")
                ):
                    all_candidates_routed = False
                    continue
                pedestrian_calls += 1
                try:
                    route = pedestrian_router.route(
                        origin,
                        destination,
                        allow_approximate=allow_approximate_routes,
                    )
                    if route.get("method") not in {"routed", "valhalla_pedestrian"}:
                        all_candidates_routed = False
                        continue
                    duration = _bounded_number(route.get("duration_s"), name="duration_s")
                except (RuntimeError, ValueError, TypeError):
                    all_candidates_routed = False
                    continue
                results.append((duration, poi, destination, route))
            if candidates and len(selected) < len(candidates):
                all_candidates_routed = False
            routed_candidates[category] = results
            coverage_status = _field_coverage(
                category,
                category_coverage=category_coverage,
                source_sample=source_sample,
                all_candidates_routed=all_candidates_routed,
            )
            if results:
                duration, poi, destination, route = min(
                    results,
                    key=lambda row: (row[0], _source_id(row[1]) or ""),
                )
                provenance = _poi_provenance(
                    poi,
                    source_version=poi_source_version,
                    method=str(route.get("provider") or route.get("method") or "pedestrian_router"),
                    coverage_status=coverage_status,
                    origin=origin,
                    target=destination,
                    source_field="tags_raw",
                )
                provenance.update(
                    {
                        "router_source_id": route.get("provider"),
                        "router_source_version": route.get("source_version"),
                        "router_rights_status": route.get("rights_status"),
                        "route_retention_gate": route.get(
                            "retention_policy",
                            "do not persist route output until provider-specific rights are confirmed",
                        ),
                        "route_observed_at": route.get("observed_at"),
                        "route_distance_m": route.get("distance_m"),
                        "rights_status": (
                            "POI: ODbL-1.0; route: "
                            + str(route.get("rights_status") or "not supplied; persistence rights unconfirmed")
                        ),
                    }
                )
                facts[_WALK_FIELD[category]] = _fact(
                    round(duration, 3),
                    unit="s",
                    status="derived",
                    scope=f"nearest_routed_{category}_within_candidate_scope",
                    observed_at=route.get("observed_at"),
                    provenance=provenance,
                )
            else:
                reason = (
                    "No source POI was available in this sample. This does not prove absence."
                    if not candidates
                    else "No eligible routed result; a router, request budget, known entrance, or valid route is missing."
                )
                facts[_WALK_FIELD[category]] = _unknown_fact(
                    unit="s",
                    scope=f"nearest_{category}_in_source_scope",
                    source_version=poi_source_version,
                    reason=reason,
                    method="pedestrian_router_required",
                )

        # Nearest metro name comes only from an entrance-to-station relation or
        # other authoritative join included by a source adapter.
        metro_routes = routed_candidates.get("metro_entrance", [])
        nearest_metro = min(metro_routes, key=lambda row: (row[0], _source_id(row[1]) or "")) if metro_routes else None
        if (
            nearest_metro
            and isinstance(nearest_metro[1].get("metro_station_name"), str)
            and nearest_metro[1].get("metro_station_source_id")
            and nearest_metro[1].get("metro_station_link_source_id")
        ):
            duration, poi, target, route = nearest_metro
            station_provenance = _poi_provenance(
                poi,
                source_version=poi_source_version,
                method="pedestrian_route_plus_explicit_station_relation",
                coverage_status=facts["walk_time_metro_s"]["provenance"]["coverage_status"],
                origin=origin,
                target=target,
                source_field="metro_station_relation.name",
            )
            station_provenance.update(
                {
                    "source_record_id": poi["metro_station_link_source_id"],
                    "station_source_id": "openstreetmap",
                    "station_source_record_id": poi["metro_station_source_id"],
                    "station_name_source_field": "tags_raw.name",
                    "entrance_source_record_id": _source_id(poi),
                }
            )
            facts["nearest_metro_name"] = _fact(
                poi["metro_station_name"],
                unit=None,
                status="derived",
                scope="station_linked_to_nearest_routed_metro_entrance_in_candidate_scope",
                observed_at=route.get("observed_at"),
                provenance=station_provenance,
            )
        else:
            facts["nearest_metro_name"] = _unknown_fact(
                unit=None,
                scope="nearest_metro_station_by_pedestrian_route",
                source_version=poi_source_version,
                reason="No routed metro entrance with an explicit linked station name was available.",
                method="station_relation_and_pedestrian_router_required",
            )

        # Separate straight-line distances. Values can describe only the
        # nearest observed source-sample feature and never claim walkability.
        for category, field_key in _DISTANCE_FIELD.items():
            candidates = _sorted_candidates(origin, records, category)
            if candidates:
                distance, poi, target = candidates[0]
                facts[field_key] = _fact(
                    round(distance, 3),
                    unit="m",
                    status="derived",
                    scope=f"nearest_observed_{category}_in_source_sample",
                    observed_at=poi.get("observed_at"),
                    provenance=_poi_provenance(
                        poi,
                        source_version=poi_source_version,
                        method="haversine_straight_line",
                        coverage_status="unknown" if source_sample else "partial",
                        origin=origin,
                        target=target,
                        source_field="location.coordinates",
                    ),
                )
            else:
                facts[field_key] = _unknown_fact(
                    unit="m",
                    scope=f"nearest_{category}_in_source_scope",
                    source_version=poi_source_version,
                    reason="No source POI was available in this sample. This does not prove absence.",
                    method="haversine_straight_line; complete POI source required for citywide nearest",
                )

        # Public transport to a metro station is an independent optional
        # connector. Proximity to a bus/tram stop never stands in for a route.
        transit_results: list[tuple[float, list[str], Mapping[str, Any], AccessPoint, Mapping[str, Any]]] = []
        metro_candidates = _sorted_candidates(origin, records, "metro_entrance")
        for _, poi, destination in metro_candidates[:candidate_limit_per_category]:
            if transit_router is None or transit_calls >= max_transit_requests:
                continue
            if not allow_approximate_routes and (
                origin.kind != "entrance_known" or destination.kind != "entrance_known"
            ):
                continue
            transit_calls += 1
            try:
                route = transit_router.route_transit(
                    origin,
                    destination,
                    allowed_modes=("pedestrian", "bus", "tram"),
                    require_direct=True,
                )
            except (RuntimeError, ValueError, TypeError):
                continue
            validated = _validate_transit_result(route)
            if validated is not None:
                duration, modes = validated
                transit_results.append((duration, modes, poi, destination, route))

        if transit_results:
            duration, modes, poi, target, route = min(
                transit_results,
                key=lambda row: (row[0], _source_id(row[2]) or ""),
            )
            transit_provenance = _poi_provenance(
                poi,
                source_version=poi_source_version,
                method="explicit_direct_public_transport_route",
                coverage_status="unknown" if source_sample else "partial",
                origin=origin,
                target=target,
                source_field="tags_raw",
            )
            transit_provenance.update(
                {
                    "router_source_id": route.get("provider"),
                    "router_source_version": route.get("source_version"),
                    "router_rights_status": route.get("rights_status"),
                    "route_retention_gate": route.get(
                        "retention_policy",
                        "do not persist route output until provider-specific rights are confirmed",
                    ),
                    "route_observed_at": route.get("observed_at"),
                    "transfer_count": 0,
                    "rights_status": (
                        "POI: ODbL-1.0; route: "
                        + str(route.get("rights_status") or "not supplied; persistence rights unconfirmed")
                    ),
                }
            )
            facts["metro_transit_bus_tram_time_s"] = _fact(
                round(duration, 3),
                unit="s",
                status="derived",
                scope="best_explicit_direct_bus_or_tram_route_to_metro_entrance_in_candidate_scope",
                observed_at=route.get("observed_at"),
                provenance=transit_provenance,
            )
            facts["metro_transit_modes"] = _fact(
                modes,
                unit=None,
                status="derived",
                scope="modes_in_explicit_direct_route_to_metro_entrance",
                observed_at=route.get("observed_at"),
                provenance=transit_provenance,
            )
            facts["metro_direct_route"] = _fact(
                True,
                unit=None,
                status="derived",
                scope="provider-confirmed-direct-route-with-zero-transfers-and-bus-or-tram-leg",
                observed_at=route.get("observed_at"),
                provenance=transit_provenance,
            )
        else:
            reason = (
                "No transit connector/direct route result was supplied. Stop proximity is not a route."
                if transit_router is None or max_transit_requests == 0
                else "No provider-confirmed direct route with a bus or tram leg and zero transfers was returned."
            )
            facts["metro_transit_bus_tram_time_s"] = _unknown_fact(
                unit="s",
                scope="direct_bus_or_tram_route_to_metro_station",
                source_version=poi_source_version,
                reason=reason,
                method="explicit_public_transport_router_required",
            )
            facts["metro_transit_modes"] = _unknown_fact(
                unit=None,
                scope="modes_on_direct_route_to_metro_station",
                source_version=poi_source_version,
                reason=reason,
                method="explicit_public_transport_router_required",
            )
            facts["metro_direct_route"] = _unknown_fact(
                unit=None,
                scope="direct_bus_or_tram_route_to_metro_station",
                source_version=poi_source_version,
                reason=reason,
                method="provider-confirmed-direct-route-required",
            )

        # Clinic descriptors are independent registry facts. They are attached
        # to the fastest routed clinic when available, else the nearest observed
        # sample clinic by straight line, always with sample scope.
        clinic_routes = routed_candidates.get("clinic", [])
        clinic_candidates = _sorted_candidates(origin, records, "clinic")
        clinic_poi: Mapping[str, Any] | None = None
        clinic_scope = "nearest_clinic_in_source_sample"
        if clinic_routes:
            clinic_poi = min(clinic_routes, key=lambda row: (row[0], _source_id(row[1]) or ""))[1]
            clinic_scope = "nearest_routed_clinic_within_candidate_scope"
        elif clinic_candidates:
            clinic_poi = clinic_candidates[0][1]
            clinic_scope = "nearest_observed_clinic_in_source_sample_by_straight_line"
        clinic_id = _source_id(clinic_poi) if clinic_poi else None
        metadata = clinic_metadata_by_source_id.get(clinic_id, {}) if clinic_id else {}
        facts["nearest_clinic_ownership_type"] = _clinic_metadata_fact(
            metadata, "ownership_type", expected_unit=None, scope=clinic_scope
        )
        facts["nearest_clinic_oms_participation"] = _clinic_metadata_fact(
            metadata, "oms_participation", expected_unit=None, scope=clinic_scope
        )
        facts["nearest_clinic_has_paid_services"] = _clinic_metadata_fact(
            metadata, "has_paid_services", expected_unit=None, scope=clinic_scope
        )
        facts["nearest_clinic_dms_networks"] = _clinic_metadata_fact(
            metadata, "dms_networks", expected_unit=None, scope=clinic_scope
        )

        # Every stable criterion key exists, so consumers can distinguish an
        # absent result from a missing field in the producer contract.
        required_keys = {
            "walk_time_clinic_s",
            "walk_time_kindergarten_s",
            "walk_time_school_s",
            "walk_time_park_s",
            "walk_time_pharmacy_s",
            "walk_time_supermarket_s",
            "walk_time_bus_stop_s",
            "walk_time_tram_stop_s",
            "walk_time_metro_s",
            "nearest_metro_name",
            "metro_transit_bus_tram_time_s",
            "metro_transit_modes",
            "metro_direct_route",
            "distance_police_straight_line_m",
            "distance_ambulance_station_straight_line_m",
            "distance_fire_station_straight_line_m",
            "nearest_clinic_ownership_type",
            "nearest_clinic_oms_participation",
            "nearest_clinic_has_paid_services",
            "nearest_clinic_dms_networks",
        }
        missing = required_keys - facts.keys()
        if missing:
            raise AssertionError(f"social facts missing required fields: {sorted(missing)}")
        outputs.append(
            {
                "complex_id": complex_id,
                "facts": facts,
                "coverage": {
                    "status": "unknown" if source_sample else "partial",
                    "poi_source_version": poi_source_version,
                    "poi_coverage": poi_coverage,
                    "taxonomy_version": TAXONOMY_VERSION,
                    "pedestrian_route_calls": pedestrian_calls,
                    "transit_route_calls": transit_calls,
                    "accessibility_index": None,
                },
            }
        )
    return outputs
