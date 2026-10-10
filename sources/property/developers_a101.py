"""Offline parser and URL discovery for public A101 listing pages.

This module contains no network, retry, rate-limit, cookie, or media-download
code. The shared developer harvester owns those concerns and supplies the exact
page URL, capture timestamp, and raw response hash.
"""

from __future__ import annotations

import json
import math
import re
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, parse_qsl, urljoin, urlsplit, urlunsplit


OFFICIAL_HOSTS = {"a101.ru", "www.a101.ru", "cdn.a101.ru"}
PAGE_HOSTS = {"a101.ru", "www.a101.ru"}
WRAPPER_TAGS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}
LISTING_PATH = re.compile(r"^/kvartiry/([0-9]+)/?$", re.IGNORECASE)
CATALOG_PATH = re.compile(r"^/kvartiry/?$", re.IGNORECASE)
PROJECT_PATH = re.compile(r"^/projects(?:/([^/]+))?/?$", re.IGNORECASE)
API_FLATS_PATH = re.compile(r"^/api/flats/?$", re.IGNORECASE)

_MEDIA_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("big_layout", "apartment_plan", "unit"),
    ("big_layout_png", "apartment_plan", "unit"),
    ("big_layout_jpg", "apartment_plan", "unit"),
    ("furniture_layout", "furnished_apartment_plan", "unit"),
    ("furniture_layout_png", "furnished_apartment_plan", "unit"),
    ("furniture_layout_jpg", "furnished_apartment_plan", "unit"),
    ("design_layout", "design_plan", "unit"),
    ("design_layout_png", "design_plan", "unit"),
    ("floor_plan", "floor_plan", "floor"),
    ("floor_plan_png", "floor_plan", "floor"),
    ("mini_floor_plan", "mini_floor_plan", "floor"),
    ("mini_floor_plan_png", "mini_floor_plan", "floor"),
    ("genplan_image", "project_genplan", "project"),
    ("genplan_of_house_image", "building_genplan", "building"),
)


class _HtmlParts(HTMLParser):
    """Collect only JSON Nuxt state, anchors, and visible page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nuxt_parts: list[str] = []
        self.hrefs: list[str] = []
        self.visible_parts: list[str] = []
        self._in_nuxt = False
        self._ignore_text = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and isinstance(values.get("href"), str):
            self.hrefs.append(values["href"] or "")
        if tag == "script":
            self._in_nuxt = (
                values.get("id") == "__NUXT_DATA__"
                and values.get("type") == "application/json"
            )
            if not self._in_nuxt:
                self._ignore_text += 1
        elif tag in {"style", "noscript"}:
            self._ignore_text += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_nuxt = False
            if self._ignore_text:
                self._ignore_text -= 1
        elif tag in {"style", "noscript"} and self._ignore_text:
            self._ignore_text -= 1

    def handle_data(self, data: str) -> None:
        if self._in_nuxt:
            self.nuxt_parts.append(data)
        elif not self._ignore_text:
            self.visible_parts.append(data)

    @property
    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.visible_parts)).strip()


def _parse_html(text: str) -> _HtmlParts:
    parser = _HtmlParts()
    parser.feed(text)
    return parser


def _nuxt_nodes(parts: _HtmlParts) -> list[Any] | None:
    if not parts.nuxt_parts:
        return None
    try:
        value = json.loads("".join(parts.nuxt_parts))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def _ref(nodes: list[Any], value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(nodes):
        return nodes[value]
    return value


def _unwrap(nodes: list[Any], value: Any) -> Any:
    current = _ref(nodes, value)
    for _ in range(8):
        if (isinstance(current, list) and len(current) == 2
                and current[0] in WRAPPER_TAGS):
            current = _ref(nodes, current[1])
        else:
            return current
    return current


def _field(nodes: list[Any], record: Mapping[str, Any], key: str) -> Any:
    return _unwrap(nodes, record.get(key))


def _root_data(nodes: list[Any]) -> dict[str, Any] | None:
    if len(nodes) < 2 or not isinstance(nodes[1], dict):
        return None
    wrapper = _unwrap(nodes, nodes[1].get("data"))
    if isinstance(wrapper, dict):
        return wrapper
    return None


def _is_record_candidate(value: Any, nodes: list[Any] | None = None) -> bool:
    if not isinstance(value, Mapping):
        return False

    def get(key: str) -> Any:
        return _field(nodes, value, key) if nodes is not None else value.get(key)

    lot_id = get("id")
    building = get("building")
    area = get("area")
    project = get("project_commercial") or get("project") or get("project_slug")
    room = get("room_name") or get("room")
    price = get("actual_price") or get("price") or get("base_price")
    return (
        isinstance(lot_id, (str, int)) and not isinstance(lot_id, bool)
        and bool(str(lot_id).strip())
        and isinstance(building, str) and bool(building.strip())
        and isinstance(area, (int, float)) and not isinstance(area, bool)
        and math.isfinite(area) and area > 0
        and isinstance(project, str) and bool(project.strip())
        and room is not None and price is not None
    )


def _flat_items_from_nuxt(nodes: list[Any]) -> tuple[list[Mapping[str, Any]], int | None, str | None]:
    data = _root_data(nodes)
    if data is None:
        return [], None, None
    response_ref = data.get("getFlatsPageData")
    if response_ref is None:
        response_ref = next((value for key, value in data.items()
                             if key.startswith("getFlatsPageData_")), None)
    response = _unwrap(nodes, response_ref)
    if not isinstance(response, Mapping):
        return [], None, None
    properties = _unwrap(nodes, response.get("propertiesRes"))
    if not isinstance(properties, Mapping):
        return [], None, None
    results_res = _unwrap(nodes, properties.get("resultsRes"))
    if not isinstance(results_res, Mapping):
        return [], None, None
    raw_results = _unwrap(nodes, results_res.get("results"))
    if not isinstance(raw_results, list):
        return [], None, None
    results = [_unwrap(nodes, item) for item in raw_results]
    offers = [item for item in results if _is_record_candidate(item, nodes)]
    count = _unwrap(nodes, results_res.get("count"))
    next_url = _unwrap(nodes, results_res.get("next"))
    return offers, count if isinstance(count, int) and not isinstance(count, bool) else None, next_url if isinstance(next_url, str) else None


def _listing_item_from_nuxt(nodes: list[Any], page_url: str) -> Mapping[str, Any] | None:
    parsed = urlsplit(page_url)
    match = LISTING_PATH.fullmatch(parsed.path)
    if not match:
        return None
    data = _root_data(nodes)
    if data is None:
        return None
    key = f"getLotPageData_{match.group(1)}"
    response = _unwrap(nodes, data.get(key))
    if not isinstance(response, Mapping):
        return None
    lot = _unwrap(nodes, response.get("pageDataRes"))
    return lot if _is_record_candidate(lot, nodes) else None


def _project_item_from_nuxt(nodes: list[Any], page_url: str) -> Mapping[str, Any] | None:
    _, slug = _page_kind(page_url)
    if not slug:
        return None
    data = _root_data(nodes)
    if data is None:
        return None
    key = f"getProjectPageData_{slug}"
    response = _unwrap(nodes, data.get(key))
    if not isinstance(response, Mapping):
        return None
    if not isinstance(response, Mapping):
        return None
    item = _unwrap(nodes, response.get("pageStartRes"))
    if not isinstance(item, Mapping):
        return None
    return item


def _walk_records(nodes: list[Any], value: Any, *, max_nodes: int = 100_000) -> Iterable[Mapping[str, Any]]:
    """Find offer-shaped records in project SSR state, deduplicating references."""
    stack = [value]
    seen: set[int] = set()
    visited = 0
    while stack and visited < max_nodes:
        raw = stack.pop()
        current = _unwrap(nodes, raw)
        if not isinstance(current, (dict, list)):
            continue
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        visited += 1
        if isinstance(current, dict):
            if _is_record_candidate(current, nodes):
                yield current
            stack.extend(current.values())
        else:
            stack.extend(current)


def _official_url(value: str, base_url: str | None = None) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(urljoin(base_url, value) if base_url else value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
    if (parsed.scheme != "https" or host not in OFFICIAL_HOSTS
            or parsed.username or parsed.password or port not in (None, 443)
            or query_keys & {"token", "key", "api_key", "apikey", "password", "secret", "auth"}):
        return None
    if host == "cdn.a101.ru":
        return None
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _page_kind(url: str) -> tuple[str | None, str | None]:
    parsed = urlsplit(url)
    if API_FLATS_PATH.fullmatch(parsed.path):
        project = parse_qs(parsed.query).get("project", [None])[0]
        return "catalog", project
    if CATALOG_PATH.fullmatch(parsed.path):
        project = parse_qs(parsed.query).get("project", [None])[0]
        return "catalog", project
    if LISTING_PATH.fullmatch(parsed.path):
        return "listing", None
    match = PROJECT_PATH.fullmatch(parsed.path)
    if match:
        return "project", match.group(1)
    return None, None


def _page_ref(value: str, base_url: str, *, permit_listing: bool = False) -> dict[str, Any] | None:
    url = _official_url(value, base_url)
    if url is None:
        return None
    kind, project_hint = _page_kind(url)
    if kind is None or (kind == "listing" and not permit_listing):
        return None
    ref: dict[str, Any] = {"url": url, "page_kind": kind}
    if project_hint:
        ref["project_hint"] = project_hint
    return ref


def _reference_key(ref: Mapping[str, Any]) -> tuple[str, str]:
    parsed = urlsplit(str(ref.get("url", "")))
    path = parsed.path
    if path.startswith("/projects/") and path != "/projects/":
        path = path.rstrip("/")
    return path, parsed.query


def _next_urls(raw: str, page_url: str, parts: _HtmlParts | None = None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    payload: Any = None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    next_value: Any = None
    if isinstance(payload, Mapping):
        next_value = payload.get("next")
    elif parts is not None:
        nodes = _nuxt_nodes(parts)
        if nodes is not None:
            _, _, next_value = _flat_items_from_nuxt(nodes)
    if isinstance(next_value, str) and next_value:
        ref = _page_ref(next_value, page_url)
        if ref is not None:
            refs.append(ref)
    return refs


def discover_a101_pages(html: str, page_url: str) -> list[dict[str, Any]]:
    """Discover only source-linked project/catalog/next pages; never guesses IDs."""
    current = _official_url(page_url)
    if current is None:
        return []
    parsed_url = urlsplit(current)
    kind, _ = _page_kind(current)
    if kind is None:
        return []
    is_project_index = parsed_url.path.rstrip("/") == "/projects"

    parts = _parse_html(html)
    refs = _next_urls(html, current, parts)
    seen = {_reference_key(item) for item in refs}
    for href in parts.hrefs:
        ref = _page_ref(href, current, permit_listing=False)
        if (ref is None or ref["url"] == current
                or _reference_key(ref) in seen):
            continue
        linked_kind = ref["page_kind"]
        # Catalog cards already contain complete offer rows, so do not enqueue
        # one detail request per apartment. Project links are useful for explicit
        # location and project evidence; pagination comes from the SSR next URL.
        if kind == "catalog" and linked_kind != "project":
            continue
        if kind == "listing":
            continue
        if kind == "project" and (not is_project_index or linked_kind != "project"):
            # Project pages are identity/location evidence; their navigation
            # often links many filtered catalog variants. Unit pagination is
            # followed only from the page's source-linked SSR `next` field.
            continue
        seen.add(_reference_key(ref))
        refs.append(ref)
    return refs


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value) if float(value).is_integer() else float(value)


def _safe_asset(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
    if (parsed.scheme != "https" or host not in OFFICIAL_HOSTS
            or parsed.username or parsed.password or port not in (None, 443)
            or query_keys & {"token", "key", "api_key", "apikey", "password", "secret", "auth"}):
        return None
    return value


def _explicit_location(nodes: list[Any] | None, offer: Mapping[str, Any]) -> tuple[str | None, str | None, dict[str, Any], str]:
    def get(key: str) -> Any:
        return _field(nodes, offer, key) if nodes is not None else offer.get(key)

    address = next((get(key) for key in ("address", "project_address", "building_address")
                    if isinstance(get(key), str) and get(key).strip()), None)
    city = next((get(key) for key in ("city", "project_city", "locality", "settlement", "region")
                 if isinstance(get(key), str) and get(key).strip()), None)
    evidence = {key: get(key) for key in ("city", "project_city", "locality", "settlement", "region",
                                           "address", "project_address", "building_address")
                if isinstance(get(key), (str, int, float)) and str(get(key)).strip()}
    combined = " ".join(str(value).casefold().replace("ё", "е") for value in (city, address) if value)
    if re.search(r"(?:^|[\s,])(?:г\.?\s*)?москва(?:$|[\s,])", combined):
        scope = "in_scope_moscow"
    elif city:
        scope = "out_of_scope"
    else:
        scope = "unknown"
    return address, city, evidence, scope


def _city_label(city: str | None, address: str | None) -> str | None:
    if isinstance(city, str) and city.strip():
        return city.strip()
    combined = (address or "").casefold().replace("ё", "е")
    if re.search(r"(?:^|[\s,])(?:г\.?\s*)?москва(?:$|[\s,])", combined):
        return "Москва"
    return None


def _project_coordinates(value: Any) -> dict[str, float] | None:
    if not isinstance(value, str):
        return None
    parts = re.split(r"[\s,;]+", value.strip())
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = (float(part) for part in parts)
    except ValueError:
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _project_record_from_nuxt(
    nodes: list[Any], page_url: str, raw_sha256: str,
) -> dict[str, Any] | None:
    item = _project_item_from_nuxt(nodes, page_url)
    if item is None:
        return None

    def get(key: str) -> Any:
        return _field(nodes, item, key)

    slug = get("slug")
    page_id = get("id")
    if not isinstance(slug, str) or not slug.strip():
        _, url_slug = _page_kind(page_url)
        slug = url_slug
    if not isinstance(slug, str) or not slug.strip():
        return None
    name = next((get(key) for key in ("commercial_short_name", "commercial_name", "name")
                 if isinstance(get(key), str) and get(key).strip()), None)
    address, explicit_city, location_evidence, location_scope = _explicit_location(nodes, item)
    city = _city_label(explicit_city, address)
    coords_raw = get("coords")
    coords = _project_coordinates(coords_raw)
    source_fields: dict[str, Any] = {
        "location_scope": location_scope,
        "city_source_value": explicit_city or city,
        "location_evidence": location_evidence,
        "address_scope": "project" if address else None,
        "address_source_field": "pageStartRes.address" if address else None,
        "a101_project_page_id": str(page_id) if page_id is not None else None,
        "a101_project_slug": slug,
        "project_coordinates_scope": "project" if coords else None,
        "project_coordinates_source_field": "pageStartRes.coords" if coords else None,
        "project_coordinates_raw": coords_raw if isinstance(coords_raw, str) else None,
        "source_updated_at_status": "not provided in captured page data",
        "raw_sha256": raw_sha256,
    }
    return {
        "record_type": "project",
        "source_url": page_url,
        "source_updated_at": None,
        "project_name": name,
        "city": city,
        "address": address,
        "corpus": None,
        "section": None,
        "unit_number": None,
        "floor": None,
        "rooms": None,
        "area_m2": None,
        "price_rub": None,
        "price_basis": "unknown",
        "promo_conditions": None,
        "publisher_project_id": slug,
        "publisher_building_id": None,
        "publisher_unit_id": None,
        "coordinates": coords,
        "media": [],
        "rights_status": "unknown",
        "source_fields": source_fields,
    }


def _visible_cash_context(text: str, amount: int | float | None) -> bool:
    if not text or amount is None:
        return False
    digits = str(int(amount)) if float(amount).is_integer() else str(amount)
    spaced = f"{int(amount):,}".replace(",", " ") if float(amount).is_integer() else digits
    normalized = re.sub(r"\s+", " ", text.replace("\xa0", " "))
    for needle in (spaced, digits):
        start = 0
        while True:
            index = normalized.find(needle, start)
            if index < 0:
                break
            prefix = normalized[max(0, index - 90):index].casefold()
            if re.search(r"оплата\s*100\s*%", prefix):
                return True
            start = index + 1
    return False


def _media(nodes: list[Any] | None, offer: Mapping[str, Any]) -> list[dict[str, str]]:
    def get(key: str) -> Any:
        return _field(nodes, offer, key) if nodes is not None else offer.get(key)

    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field_name, role, scope in _MEDIA_FIELDS:
        url = _safe_asset(get(field_name))
        if url is None or (url, role) in seen:
            continue
        seen.add((url, role))
        output.append({"url": url, "role": role, "scope": scope,
                       "source_field": field_name})
    slides = get("visualization_slides")
    if isinstance(slides, list):
        for slide in slides[:20]:
            slide_obj = _unwrap(nodes, slide) if nodes is not None else slide
            if not isinstance(slide_obj, Mapping):
                continue
            for key in ("image", "image_url", "src", "url"):
                value = _field(nodes, slide_obj, key) if nodes is not None else slide_obj.get(key)
                url = _safe_asset(value)
                if url is not None and (url, "visualization_render") not in seen:
                    seen.add((url, "visualization_render"))
                    output.append({"url": url, "role": "visualization_render",
                                   "scope": "unit", "source_field": "visualization_slides"})
                    break
    return output


def _record_from_offer(
    offer: Mapping[str, Any],
    *,
    nodes: list[Any] | None,
    source_url: str,
    visible_text: str,
    catalog_count: int | None,
    allow_visible_cash_context: bool = False,
) -> dict[str, Any] | None:
    def get(key: str) -> Any:
        return _field(nodes, offer, key) if nodes is not None else offer.get(key)

    if not _is_record_candidate(offer, nodes):
        return None
    raw_id = get("id")
    project_name = get("project_commercial_short_name") or get("project_commercial") or get("project")
    area = _num(get("area"))
    actual = _num(get("actual_price"))
    source_price = _num(get("price"))
    base = _num(get("base_price"))
    price_rub = actual if actual is not None else source_price if source_price is not None else base
    address, city, location_evidence, location_scope = _explicit_location(nodes, offer)
    source_status = get("status")
    cash_basis = allow_visible_cash_context and _visible_cash_context(visible_text, actual)
    sale_value = _num(get("sale_value"))
    raw_prices = {
        key: _num(get(key))
        for key in ("actual_price", "price", "base_price", "special_base_price", "special_price")
        if _num(get(key)) is not None
    }
    promo: dict[str, Any] = {}
    if source_price is not None and actual is not None and source_price != actual:
        promo["source_price_rub"] = source_price
    if cash_basis:
        promo["displayed_selector"] = "Оплата 100%"

    project_group = get("project_group")
    project_group_name = None
    project_group_id = None
    if isinstance(project_group, Mapping):
        project_group_name = _field(nodes, project_group, "name") if nodes is not None else project_group.get("name")
        project_group_id = _field(nodes, project_group, "id") if nodes is not None else project_group.get("id")
    fields: dict[str, Any] = {
        "location_scope": location_scope,
        "city_source_value": city,
        "location_evidence": location_evidence,
        "a101_listing_id": str(raw_id),
        "a101_offer_code": get("mindbox_offer_id"),
        "a101_project_slug": get("project_slug"),
        "a101_project_group_id": str(project_group_id) if project_group_id is not None else None,
        "a101_project_group_name": project_group_name,
        "a101_building_number": get("building_number"),
        "a101_section_id": get("section_id"),
        "a101_project_id": str(get("project_id")) if get("project_id") is not None else None,
        "a101_building_id": str(get("building_id")) if get("building_id") is not None else None,
        "a101_room_enum": get("room"),
        "a101_room_source_label": get("room_name"),
        "a101_flatsplan_type": get("flatsplan_type"),
        "a101_price_fields_rub": raw_prices,
        "a101_sale_value": sale_value,
        "a101_status_code": source_status,
        "a101_building_realized": get("building_realized"),
        "availability_assertion": {
            "catalog_or_offer_record_present": True,
            "source_status_code": source_status,
            "availability_conclusion": "unknown",
        },
    }
    if catalog_count is not None:
        fields["catalog_total_claimed"] = catalog_count
    if project_group_name is not None:
        fields["project_group_name"] = project_group_name

    return {
        "record_type": "unit",
        "source_url": source_url,
        "source_updated_at": None,
        "project_name": project_name if isinstance(project_name, str) else None,
        "address": address,
        "corpus": get("building") if isinstance(get("building"), str) else None,
        "unit_number": get("number"),
        "floor": get("floor"),
        "rooms": get("room_name") if isinstance(get("room_name"), str) else None,
        "area_m2": area,
        "price_rub": price_rub,
        "price_basis": "cash_100_percent" if cash_basis else "unknown",
        "promo_conditions": promo or None,
        "media": _media(nodes, offer),
        "rights_status": "unknown",
        "publisher_project_id": (str(get("project_slug")) if isinstance(get("project_slug"), str)
                                 and get("project_slug").strip()
                                 else str(get("project_id")) if get("project_id") is not None else None),
        "publisher_building_id": str(get("building_id")) if get("building_id") is not None else None,
        "publisher_unit_id": str(raw_id),
        "offer_id": get("mindbox_offer_id"),
        "source_fields": fields,
    }


def _api_payload(raw: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def parse_a101_page(html: str, page_url: str, observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    """Parse A101 listing/project/catalog HTML or a source-linked flats JSON page."""
    safe_source = _official_url(page_url)
    if safe_source is None:
        return []
    payload = _api_payload(html)
    if payload is not None:
        raw_items = payload.get("results")
        if not isinstance(raw_items, list):
            return []
        count = payload.get("count") if isinstance(payload.get("count"), int) else None
        page_type = _page_kind(safe_source)[0]
        if page_type != "catalog":
            return []
        rows = []
        for item in raw_items:
            if isinstance(item, Mapping):
                row = _record_from_offer(item, nodes=None, source_url=safe_source,
                                          visible_text="", catalog_count=count)
                if row is not None:
                    rows.append(row)
        return _dedupe_rows(rows)

    parts = _parse_html(html)
    nodes = _nuxt_nodes(parts)
    if nodes is None:
        return []
    page_type = _page_kind(safe_source)[0]
    offers: list[Mapping[str, Any]] = []
    catalog_count: int | None = None
    if page_type == "listing":
        item = _listing_item_from_nuxt(nodes, safe_source)
        if item is not None:
            offers = [item]
    else:
        offers, catalog_count, _ = _flat_items_from_nuxt(nodes)
        if not offers and page_type == "project":
            data = _root_data(nodes)
            if data is not None:
                offers = list(_walk_records(nodes, data))
    rows = []
    for offer in offers:
        row = _record_from_offer(offer, nodes=nodes, source_url=safe_source,
                                 visible_text=parts.visible_text,
                                 catalog_count=catalog_count,
                                 allow_visible_cash_context=(page_type == "listing" and len(offers) == 1))
        if row is not None:
            rows.append(row)
    if page_type == "project":
        project_row = _project_record_from_nuxt(nodes, safe_source, raw_sha256)
        if project_row is not None:
            rows.append(project_row)
    return _dedupe_rows(rows)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output = []
    for row in rows:
        key = str(row.get("publisher_unit_id") or row.get("offer_id") or row.get("source_url"))
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output
