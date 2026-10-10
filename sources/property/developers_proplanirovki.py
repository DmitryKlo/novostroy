"""Private observations from the public Proplanirovki aggregator API.

This is an aggregator source, not a developer feed. Project cards and project
details keep their Proplanirovki provenance; advertised prices and image reuse
rights are not independently verified by this parser.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

try:
    from shapely.geometry import Point, shape
    from shapely.ops import nearest_points
except ImportError:  # The non-geo parser remains usable without the optional OSM stack.
    Point = shape = nearest_points = None


_HOST = "proplanirovki.ru"
_CDN_HOST = "cdn-dataout.trendagent.ru"
_CATALOG_PATH = "/api/catalog"
_PROJECT_PATH = re.compile(r"^/api/catalog/([A-Za-z0-9_-]{1,128})/?$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_ROWS = 4999
_REGION_BOUNDS = (35.0, 54.0, 42.0, 58.0)  # Broad Moscow/Oblast sanity bounds, lon/lat.
_MAX_PROJECT_BUILDING_DISTANCE_KM = 3.0


def _on_host(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.hostname == _HOST
            and parts.port in (None, 443) and not parts.username and not parts.password
            and not parts.fragment)


def _source_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    result = str(value)
    return result if _ID.fullmatch(result) else None


def _text(value: Any, maximum: int = 1024) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:maximum] if value else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _rubles(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    return value if re.fullmatch(r"\d{4}-\d\d-\d\dT[^\s]+", value) else None


def _project_coordinates(value: Any) -> dict[str, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    longitude, latitude = (_number(part) for part in value)
    west, south, east, north = _REGION_BOUNDS
    if (longitude is None or latitude is None or not
            (west < longitude < east and south < latitude < north)):
        return None
    return {"longitude": longitude, "latitude": latitude}


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon_a, lat_a = map(math.radians, a)
    lon_b, lat_b = map(math.radians, b)
    delta_lat, delta_lon = lat_b - lat_a, lon_b - lon_a
    haversine = (math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b)
                 * math.sin(delta_lon / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _source_building_geometry(value: Any, coordinates: dict[str, float] | None
                              ) -> tuple[dict[str, Any] | None, str]:
    """Keep only structurally valid, nearby aggregator polygons as source evidence."""
    if value is None:
        return None, "missing"
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        return None, "unsupported_type"
    rings = value.get("coordinates")
    if not isinstance(rings, list) or not rings or len(rings) > 20:
        return None, "malformed_coordinates"
    west, south, east, north = _REGION_BOUNDS
    vertices = 0
    for ring in rings:
        if not isinstance(ring, list) or not 4 <= len(ring) <= 1000 or ring[0] != ring[-1]:
            return None, "malformed_ring"
        vertices += len(ring)
        if vertices > 2000:
            return None, "too_many_vertices"
        for point in ring:
            if not isinstance(point, list) or len(point) != 2:
                return None, "malformed_point"
            longitude, latitude = (_number(part) for part in point)
            if (longitude is None or latitude is None or not
                    (west < longitude < east and south < latitude < north)):
                return None, "out_of_region_bounds"
    if shape is None or Point is None or nearest_points is None:
        return None, "geometry_validation_unavailable"
    try:
        polygon = shape(value)
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
            return None, "invalid_topology"
        if coordinates is not None:
            center = (coordinates["longitude"], coordinates["latitude"])
            nearest = nearest_points(Point(center), polygon)[1]
            if _distance_km(center, (nearest.x, nearest.y)) > _MAX_PROJECT_BUILDING_DISTANCE_KM:
                return None, "far_from_project_center"
    except (TypeError, ValueError, AttributeError):
        return None, "geometry_validation_error"
    return value, "accepted"


def _scope(address: Any, district: Any, region: Any) -> tuple[str, str]:
    address_text = (_text(address) or "").casefold()
    district_text = (_text(district) or "").casefold()
    region_text = (_text(region) or "").casefold()
    says_moscow = (district_text in {"новая москва", "москва"}
                   or re.match(r"^(?:г\.?\s*)?москва(?:[,\s]|$)", address_text) is not None)
    says_outside = (
        ("моск" in district_text and "область" in district_text)
        or re.search(r"(?:^|[, ])мо(?:$|[, ])", district_text) is not None
        or "калужская область" in district_text
        or re.match(r"^(?:г\.?\s*)?(?!москва\b)[^,]+,\s*(?:московская|калужская) область",
                    address_text) is not None
        or (bool(region_text) and region_text not in {"msk", "moscow", "moscow_region"})
    )
    if says_moscow and says_outside:
        return "unknown", "aggregator address, district, and region conflict"
    if says_moscow:
        return "in_scope_moscow", "aggregator address or district explicitly names Moscow"
    if says_outside:
        return "out_of_scope", "aggregator district, address, or region names an area outside Moscow"
    return "unknown", "aggregator locality does not establish city-versus-region scope"


def _public_page(path: str) -> str:
    return f"https://{_HOST}{path}"


def _catalog_query(page_url: str, next_page: int) -> str:
    parts = urlsplit(page_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(next_page)]
    return urlunsplit(("https", _HOST, _CATALOG_PATH, urlencode(query, doseq=True), ""))


def discover_proplanirovki_pages(body: str, page_url: str) -> list[dict[str, str]]:
    """Follow public catalog pagination and public project API references."""
    if not _on_host(page_url):
        return []
    parts = urlsplit(page_url)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []

    if parts.path.rstrip("/") == _CATALOG_PATH and isinstance(payload.get("items"), list):
        refs: list[dict[str, str]] = []
        for item in payload["items"][:1000]:
            if not isinstance(item, dict):
                continue
            project_id = _source_id(item.get("id") or item.get("slug"))
            if project_id:
                refs.append({"url": _public_page(f"/api/catalog/{project_id}"),
                             "page_kind": "project"})
        page = payload.get("page")
        limit = payload.get("limit")
        total = payload.get("total")
        if (all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in (page, limit, total)) and page * limit < total):
            refs.append({"url": _catalog_query(page_url, page + 1), "page_kind": "catalog"})
        return refs

    return []


def _media(value: Any, role: str, caption: str | None,
           scope: str, source_field: str) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    result: list[dict[str, str]] = []
    for url in values[:50]:
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != _CDN_HOST
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment):
            continue
        result.append({"url": url, "role": role, "caption": caption or "",
                       "scope": scope, "source_field": source_field,
                       "rights_status": "unknown"})
    return result


def _catalog_records(payload: dict[str, Any], page_url: str,
                     observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 1000:
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        project_id = _source_id(item.get("id") or item.get("slug"))
        name = _text(item.get("name"), 256)
        if not project_id or not name:
            continue
        address = _text(item.get("address"), 512)
        district = _text(item.get("district"), 256)
        region = _text(item.get("region"), 64)
        location_scope, scope_basis = _scope(address, district, region)
        price_from = _rubles(item.get("priceFrom"))
        coordinates = _project_coordinates(item.get("coords"))
        media = _media(item.get("cover"), "project_cover", name,
                       "project", "catalog.items[].cover")
        room_prices = item.get("roomsPrices")
        if not isinstance(room_prices, list):
            room_prices = []
        rows.append({
            # This summary is intentionally page-local. The project-detail
            # record later carries the aggregator's asserted project ID while
            # this record retains the builder label from the catalog card.
            "record_type": "project", "source_url": page_url,
            "project_name": name, "address": address,
            "coordinates": coordinates,
            "source_updated_at": _timestamp(item.get("updatedAt")),
            "price_from_rub": price_from,
            "price_per_sqm_from_rub_m2": _rubles(item.get("pricePerSqmFrom")),
            "area_from_m2": _number(item.get("areaFrom")),
            "area_to_m2": _number(item.get("areaTo")),
            "media": media, "rights_status": "unknown",
            "source_fields": {
                "source_project_id": project_id,
                "public_page_url": _public_page(f"/object/{project_id}"),
                "aggregator_builder_label": _text(item.get("builder"), 256),
                "aggregator_region": region, "aggregator_district": district,
                "location_scope": location_scope, "location_scope_basis": scope_basis,
                "address_source_field": "catalog.items[].address",
                "coordinate_basis": "aggregator project center; unverified",
                "coordinate_crs_assumption": "EPSG:4326 inferred from lon/lat order",
                "catalog_apartment_count": item.get("apartments") if isinstance(item.get("apartments"), int) else None,
                "catalog_projects_total_claimed": payload.get("total") if isinstance(payload.get("total"), int) else None,
                "catalog_rooms_prices": [entry for entry in room_prices[:50] if isinstance(entry, dict)],
                "class": _text(item.get("class"), 64),
                "deadline_from": _timestamp(item.get("deadlineFrom")),
                "deadline_to": _timestamp(item.get("deadlineTo")),
                "metro_station": _text(item.get("metroStation"), 128),
                "metro_minutes": item.get("metroMin") if isinstance(item.get("metroMin"), int) else None,
                "finishings": item.get("finishings") if isinstance(item.get("finishings"), list) else [],
                "raw_price_from": item.get("priceFrom"),
                "price_from_basis": "aggregator-displayed minimum listing price; payment conditions not stated",
                "record_granularity": "aggregator_catalog_card",
                "observation_page_url": page_url,
                "observed_at": observed_at, "raw_sha256": raw_sha256,
            },
        })
    return rows


def _project_records(payload: dict[str, Any], page_url: str,
                     observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    project_id = _source_id(payload.get("id"))
    name = _text(payload.get("name"), 256)
    if not project_id or not name:
        return []
    address = _text(payload.get("address"), 512)
    district = _text(payload.get("district"), 256)
    region = _text(payload.get("region"), 64)
    location_scope, scope_basis = _scope(address, district, region)
    source_updated_at = _timestamp(payload.get("updatedAt"))
    coordinates = _project_coordinates(payload.get("coords"))
    buildings = payload.get("buildings") if isinstance(payload.get("buildings"), list) else []
    apartments = payload.get("apartments") if isinstance(payload.get("apartments"), list) else []
    building_by_id: dict[str, dict[str, Any]] = {}
    for building in buildings[:1000]:
        if not isinstance(building, dict):
            continue
        building_id = _source_id(building.get("id"))
        if building_id:
            building_by_id[building_id] = building

    renderer_media = _media(payload.get("renderer"), "project_rendering", name,
                             "project", "project.renderer[]")
    plan_media = _media(payload.get("plan"), "project_plan", name,
                        "project", "project.plan[]")
    overview_media = (renderer_media + plan_media)[:50]
    rows: list[dict[str, Any]] = [{
        "record_type": "project", "publisher_project_id": project_id,
        "project_name": name, "address": address,
        "coordinates": coordinates,
        "source_url": page_url, "source_updated_at": source_updated_at,
        "media": overview_media, "rights_status": "unknown",
        "source_fields": {
            "public_page_url": _public_page(f"/object/{project_id}"),
            "aggregator_region": region, "aggregator_district": district,
            "location_scope": location_scope, "location_scope_basis": scope_basis,
            "address_source_field": "project.address",
            "source_updated_at_basis": "explicit_project" if source_updated_at else None,
            "class": _text(payload.get("class"), 64),
            "catalog_apartment_count": payload.get("apartmentsTotal") if isinstance(payload.get("apartmentsTotal"), int) else None,
            "deadline_from": _timestamp(payload.get("deadlineFrom")),
            "deadline_to": _timestamp(payload.get("deadlineTo")),
            "finishings": payload.get("finishings") if isinstance(payload.get("finishings"), list) else [],
            "payment": payload.get("payment") if isinstance(payload.get("payment"), dict) else {},
            "coords": payload.get("coords") if isinstance(payload.get("coords"), list) else None,
            "coordinate_basis": "aggregator project center; not a verified building footprint",
            "coordinate_crs_assumption": "EPSG:4326 inferred from lon/lat order",
            "aggregator_updated_at": source_updated_at,
            "publicly_claimed_project_description": _text(payload.get("description"), 12000),
            "record_granularity": "aggregator_project_api",
            "observation_page_url": page_url,
            "observed_at": observed_at, "raw_sha256": raw_sha256,
            "catalog_apartment_array_count": len(apartments),
            "catalog_building_array_count": len(building_by_id),
        },
    }]

    for building_id, building in list(building_by_id.items())[:1000]:
        corpus = _text(building.get("name"), 128)
        queue = _text(building.get("queue"), 128)
        building_updated = _timestamp(building.get("updatedAt"))
        source_geometry, geometry_status = _source_building_geometry(building.get("geo"), coordinates)
        rows.append({
            "record_type": "corpus", "publisher_project_id": project_id,
            "publisher_building_id": building_id, "project_name": name,
            "address": address, "corpus": corpus, "structure": queue,
            "source_url": page_url,
            "source_updated_at": building_updated or source_updated_at,
            "source_geometry": source_geometry,
            "media": [], "rights_status": "unknown",
            "source_fields": {
                "public_page_url": _public_page(f"/object/{project_id}"),
                "location_scope": location_scope, "location_scope_basis": scope_basis,
                "address_source_field": "project.address",
                "queue": queue, "deadline": _timestamp(building.get("deadline")),
                "source_geo_present": isinstance(building.get("geo"), dict),
                "geometry_validation_status": geometry_status,
                "geometry_basis": "aggregator_unverified" if source_geometry is not None else None,
                "geometry_crs_assumption": "EPSG:4326 inferred from lon/lat order",
                "source_updated_at_basis": ("explicit_building" if building_updated else
                                            "inherited_project" if source_updated_at else None),
                "record_granularity": "aggregator_project_building_entry",
                "observation_page_url": page_url,
                "observed_at": observed_at, "raw_sha256": raw_sha256,
            },
        })

    unit_budget = max(0, _MAX_ROWS - len(rows))
    for apartment in apartments[:unit_budget]:
        if not isinstance(apartment, dict):
            continue
        unit_id = _source_id(apartment.get("id"))
        if not unit_id:
            continue
        building_id = _source_id(apartment.get("buildingId"))
        building = building_by_id.get(building_id or "", {})
        corpus = _text(building.get("name"), 128)
        floor = apartment.get("floor")
        floors = apartment.get("floors")
        number = _text(apartment.get("number"), 128)
        area = _number(apartment.get("areaTotal"))
        price_rub = _rubles(apartment.get("price"))
        media = _media(apartment.get("plan"), "apartment_plan", number,
                       "unit", "apartments[].plan[]")
        explicit_unit_updated = _timestamp(apartment.get("updatedAt"))
        unit_updated = explicit_unit_updated or source_updated_at
        price_base = _rubles(apartment.get("priceBase"))
        rows.append({
            "record_type": "unit", "publisher_project_id": project_id,
            "publisher_building_id": building_id, "publisher_unit_id": unit_id,
            "project_name": name, "address": address,
            "corpus": corpus, "unit_number": number,
            "floor": floor if isinstance(floor, int) and not isinstance(floor, bool) and floor > 0 else None,
            "rooms": _text(apartment.get("roomLabel"), 64) or apartment.get("rooms"),
            "area_m2": area if area is not None and area > 0 else None,
            "price_rub": price_rub, "price_basis": "aggregator-displayed asking price; payment conditions not stated",
            "promo_conditions": None, "source_url": page_url,
            "source_updated_at": unit_updated, "media": media,
            "rights_status": "unknown",
            "source_fields": {
                "public_page_url": _public_page(f"/apartment/{unit_id}"),
                "unit_api_url": _public_page(f"/api/apartments/{unit_id}"),
                "aggregator_region": region, "aggregator_district": district,
                "location_scope": location_scope, "location_scope_basis": scope_basis,
                "address_source_field": "project.address",
                "source_updated_at_basis": ("explicit_unit" if explicit_unit_updated else
                                            "inherited_project" if source_updated_at else None),
                "room_code": apartment.get("roomCode"), "room_label": _text(apartment.get("roomLabel"), 64),
                "building_floors": floors if isinstance(floors, int) and not isinstance(floors, bool) else None,
                "deadline": _timestamp(apartment.get("deadline")),
                "finishing": _text(apartment.get("finishing"), 128),
                "source_sale_status": _text(apartment.get("saleStatus"), 128),
                "availability_claim": "listed in aggregator project inventory; current availability not independently verified",
                "raw_price": apartment.get("price"), "raw_price_base": apartment.get("priceBase"),
                "price_base_rub": price_base,
                "price_field_used": "apartments[].price",
                "price_basis_evidence": "aggregator inventory amount; payment terms unverified",
                "record_granularity": "aggregator_project_inventory_row",
                "observation_page_url": page_url,
                "observed_at": observed_at, "raw_sha256": raw_sha256,
            },
        })

    if len(apartments) > unit_budget:
        rows[0]["source_fields"]["unit_records_truncated"] = True
        rows[0]["source_fields"]["unit_records_emitted"] = unit_budget
    return rows


def parse_proplanirovki_page(body: str, page_url: str,
                             observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    """Parse catalog summaries or one project's detail JSON response."""
    if not _on_host(page_url):
        return []
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    path = urlsplit(page_url).path.rstrip("/")
    if path == _CATALOG_PATH and isinstance(payload.get("items"), list):
        return _catalog_records(payload, page_url, observed_at, raw_sha256)
    if _PROJECT_PATH.fullmatch(path):
        return _project_records(payload, page_url, observed_at, raw_sha256)
    return []
