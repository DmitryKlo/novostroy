"""Pure parser for Level's public homepage and its source-linked catalog JSON."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sources.property.developer_level_sample import parse_sample


MEDIA_FIELDS = {
    "plan": ("apartment_plan", "unit"),
    "floor_plan": ("floor_plan", "floor"),
    "plan_3d_visualization": ("visualized_apartment_plan", "unit"),
    "visualization_plan": ("visualized_apartment_plan", "unit"),
    "mini_plan": ("apartment_plan_thumbnail", "unit"),
}


def _media(flat: dict) -> list[dict]:
    result = []
    for field, (role, scope) in MEDIA_FIELDS.items():
        raw = flat.get(field)
        if not isinstance(raw, str) or len(raw) > 2048:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        result.append({"url": raw, "role": role, "scope": scope,
                       "source_field": f"results[].{field}",
                       "rights_status": "unknown", "public_display_allowed": False})
    return result


def _source_next(value: object) -> str | None:
    """Keep only publisher-emitted catalog pagination; normalize its HTTP link."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname != "level.ru"
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
            or parsed.path != "/api/filter/"):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"city", "limit", "offset"}:
        return None
    if query["city"] != ["moscow"] or query["limit"] != ["8"]:
        return None
    try:
        offset = int(query["offset"][0])
    except (ValueError, IndexError):
        return None
    if len(query["offset"]) != 1 or not 0 < offset <= 10000:
        return None
    return urlunsplit(("https", "level.ru", parsed.path, parsed.query, ""))


def _catalog_data(text: str, page_url: str) -> tuple[list[dict], dict]:
    if urlsplit(page_url).path == "/api/filter/":
        payload = json.loads(text)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("Level catalog JSON has no results")
        if len(payload["results"]) > 100:
            raise ValueError("Level catalog page exceeds result cap")
        return payload["results"], payload
    rows, info = parse_sample(text.encode("utf-8"), page_url, "")
    flats = [row["source_fields"] for row in rows]
    return flats, {"count": info["catalog_total_claimed"],
                   "next": info["source_next_url"],
                   "project_count_claimed": info["project_count_claimed"]}


def discover_level_pages(html: str, page_url: str) -> list[dict]:
    if urlsplit(page_url).hostname != "level.ru":
        return []
    _, payload = _catalog_data(html, page_url)
    next_url = _source_next(payload.get("next"))
    return [{"url": next_url, "page_kind": "catalog"}] if next_url else []


def parse_level_page(html: str, page_url: str, observed_at: str,
                     raw_sha256: str) -> list[dict]:
    if urlsplit(page_url).hostname != "level.ru":
        return []
    flats, payload = _catalog_data(html, page_url)
    rows: list[dict] = []
    projects: dict[str, dict] = {}
    corpora: dict[str, dict] = {}
    for flat in flats:
        if not isinstance(flat, dict):
            raise ValueError("Level result is not an object")
        unit_id = flat.get("pk")
        if not isinstance(unit_id, int) or isinstance(unit_id, bool) or unit_id <= 0:
            raise ValueError("Level publisher unit ID missing")
        project_id = flat.get("project_id")
        building_id = flat.get("building_id")
        source_fields = {
            "location_scope": "unknown",
            "catalog_filter_city": "moscow",
            "catalog_total_claimed": payload.get("count"),
            "catalog_count_without_alterplans": payload.get("count_without_alterplans"),
            "source_next_url": payload.get("next"),
            "realty_pk": flat.get("realty_pk"),
            "flat_id": flat.get("flat_id"),
            "metro_city": flat.get("metro_city"),
            "status": flat.get("status"),
            "status_modified": flat.get("status_modified"),
            "sales_start": flat.get("sales_start"),
            "old_price_rub": flat.get("old_price"),
            "discount": flat.get("discount"),
            "show_price": flat.get("show_price"),
            "flat_category": flat.get("flat_category"),
            "is_apartment": flat.get("is_apartment"),
            "source_scope_basis": "publisher_catalog_query_city_moscow_not_verified_municipality",
        }
        rows.append({
            "record_type": "unit", "publisher_unit_id": str(unit_id),
            "publisher_project_id": str(project_id) if project_id is not None else None,
            "publisher_building_id": str(building_id) if building_id is not None else None,
            "publisher_section_id": str(flat["section_id"]) if flat.get("section_id") is not None else None,
            "project_name": flat.get("project"), "building_label": flat.get("building_name"),
            "section_label": flat.get("section_title"), "unit_number": flat.get("number"),
            "floor": flat.get("floor"), "area_m2": flat.get("area"),
            "rooms": flat.get("room"),
            "price_rub": flat.get("price") if flat.get("show_price") is True else None,
            "price_basis": "catalog_displayed_conditions_unverified",
            "media": _media(flat), "source_updated_at": None,
            "source_fields": source_fields,
        })
        if project_id is not None:
            projects[str(project_id)] = {
                "record_type": "project", "publisher_project_id": str(project_id),
                "project_name": flat.get("project"), "project_slug": flat.get("project_short_slug"),
                "source_fields": {"location_scope": "unknown",
                                  "catalog_filter_city": "moscow",
                                  "source_scope_basis": "publisher_catalog_query_not_project_address"},
                "media": [],
            }
        if building_id is not None:
            corpora[str(building_id)] = {
                "record_type": "corpus", "publisher_building_id": str(building_id),
                "publisher_project_id": str(project_id) if project_id is not None else None,
                "building_label": flat.get("building_name"),
                "source_fields": {"location_scope": "unknown",
                                  "source_scope_basis": "publisher_catalog_query_not_building_address"},
                "media": [],
            }
    return rows + list(projects.values()) + list(corpora.values())
