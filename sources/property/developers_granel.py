"""Offline observations from public Granel SSR pages.

This module only reads supplied HTML.  It never requests pages or images, and
it keeps catalogue prices distinct from the more specific unit-page offer.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse


_HOST = "granelle.ru"
_FLAT_PATH = re.compile(r"^/flats/([a-z0-9-]+)/([0-9]+)/?$")
_PROJECT_PATH = re.compile(r"^/objects/([a-z0-9-]+)/?$")
_MONEY = re.compile(r"([0-9][0-9\s\u00a0]*)\s*₽")
_AREA = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*м")
_FLOOR = re.compile(r"этаж\s+(\d+)\s+из\s+(\d+)", re.I)
_BUILDING = re.compile(r"корпус\s+([\w.-]+)", re.I)
_ROOMS = re.compile(r"^(Студия|\d+-комн(?:\.\s*евро|\.)?)", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

# The Moscow location is stated on this official project page. Other catalogue
# projects remain unknown until their own address evidence is supplied.
_CONFIRMED_MOSCOW = {
    "mypriority-mnevniki": "https://granelle.ru/objects/mypriority-mnevniki/",
    "danilovskij-ot-granel": "https://granelle.ru/objects/danilovskij-ot-granel/",
    "triniti": "https://granelle.ru/objects/triniti/",
    "mypriority-paveletskaya": "https://granelle.ru/objects/mypriority-paveletskaya/",
    "mypriority-nizhegorodskaya": "https://granelle.ru/objects/mypriority-nizhegorodskaya/",
}
_CONFIRMED_OUTSIDE = {"mytishi": "project name and official catalogue explicitly say Мытищи"}


class _Element:
    def __init__(self, tag: str, attrs: dict[str, str], parent: _Element | None = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Element | str] = []

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, _Element) else child)
        return " ".join(" ".join(parts).split())

    def descendants(self, tag: str | None = None) -> Iterator[_Element]:
        for child in self.children:
            if isinstance(child, _Element):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.descendants(tag)

    def first_class(self, prefix: str) -> _Element | None:
        return next((item for item in self.descendants() if any(c.startswith(prefix) for c in item.attrs.get("class", "").split())), None)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)
        if tag not in _VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _Element(tag, {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _tree(html: str) -> _Element:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _visible_text(root: _Element) -> str:
    def parts(node: _Element) -> list[str]:
        if node.tag in {"script", "style"}:
            return []
        values: list[str] = []
        for child in node.children:
            values.extend(parts(child) if isinstance(child, _Element) else [child])
        return values
    return " ".join(" ".join(parts(root)).split())


def _official(href: str, page_url: str) -> str | None:
    url = urljoin(page_url, href)
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname != _HOST or parts.username or parts.password:
        return None
    return url


def _api_next(raw_url: str) -> str | None:
    """Upgrade only a source-emitted same-host pagination URL to HTTPS."""
    parts = urlparse(raw_url)
    if parts.scheme not in {"http", "https"} or parts.netloc != _HOST or parts.path != "/api/flats/":
        return None
    if not parts.query or parts.fragment:
        return None
    return parts._replace(scheme="https").geturl()


def _scope(slug: str) -> tuple[str, str | None]:
    if slug in _CONFIRMED_MOSCOW:
        return "in_scope_moscow", _CONFIRMED_MOSCOW[slug]
    if slug in _CONFIRMED_OUTSIDE:
        return "out_of_scope", _CONFIRMED_OUTSIDE[slug]
    return "unknown", None


def _base(record_type: str, page_url: str, project_name: str | None, source_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": record_type,
        "publisher_unit_id": None,
        "publisher_project_id": None,
        "publisher_building_id": None,
        "source_url": page_url,
        "source_updated_at": None,
        "project_name": project_name,
        "address": None,
        "corpus": None,
        "section": None,
        "unit_number": None,
        "floor": None,
        "rooms": None,
        "area_m2": None,
        "price_rub": None,
        "price_basis": None,
        "promo_conditions": None,
        "media": [],
        "rights_status": "unknown",
        "source_fields": source_fields,
    }


def discover_granel_pages(html: str, page_url: str) -> list[dict[str, str]]:
    if html.lstrip().startswith("{"):
        try:
            payload = json.loads(html)
        except ValueError:
            return []
        raw_next = payload.get("next") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and not raw_next and isinstance(payload.get("pagination"), dict):
            raw_next = payload["pagination"].get("next")
        next_url = _api_next(raw_next) if isinstance(raw_next, str) else None
        return [{"url": next_url, "page_kind": "catalog", "source_next_url": raw_next}] if next_url else []
    root = _tree(html)
    found: dict[str, dict[str, str]] = {}
    for link in root.descendants("a"):
        url = _official(link.attrs.get("href", ""), page_url)
        if not url:
            continue
        path = urlparse(url).path
        flat = _FLAT_PATH.fullmatch(path)
        project = _PROJECT_PATH.fullmatch(path)
        if flat:
            ref = {"url": url, "page_kind": "listing", "project_hint": flat.group(1)}
        elif project:
            ref = {"url": url, "page_kind": "project", "project_hint": project.group(1)}
        elif path.rstrip("/") == "/flats":
            ref = {"url": url, "page_kind": "catalog"}
        else:
            continue
        found.setdefault(url, ref)
    for script in root.descendants("script"):
        # Nuxt's public pagination object contains a literal next URL.  It may
        # use HTTP internally; request only its HTTPS form on the same host.
        for match in re.finditer(r'next:"((?:http|https):(?:\\u[0-9a-fA-F]{4}|[^"\\])+)"', script.text()):
            try:
                raw_next = json.loads('"' + match.group(1) + '"')
            except ValueError:
                continue
            next_url = _api_next(raw_next)
            if next_url:
                found.setdefault(next_url, {"url": next_url, "page_kind": "catalog", "source_next_url": raw_next})
    return list(found.values())


def _money(text: str) -> int | None:
    match = _MONEY.search(text)
    return int(re.sub(r"\D", "", match.group(1))) if match else None


def _media(url: str, role: str, source_field: str, caption: str, scope: str = "unit") -> dict[str, str]:
    return {"url": url, "role": role, "caption": caption, "scope": scope,
            "source_field": source_field, "rights_status": "unknown"}


def _rubles(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _api_records(payload: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) > 200:
        return []
    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or item.get("type") != "flat":
            continue
        unit_id, slug = item.get("id"), item.get("project_slug")
        if not isinstance(unit_id, int) or unit_id <= 0 or not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
            continue
        scope, scope_source = _scope(slug)
        row = _base("unit", page_url, item.get("project") if isinstance(item.get("project"), str) else None, {
            "unit_id": str(unit_id), "project_slug": slug,
            "location_scope": scope, "scope_evidence": scope_source,
            "record_granularity": "public_api_catalog_row",
            "source_status_code": item.get("status"),
            "availability_scope": "sale_candidate" if item.get("status") == 1 else "unverified_or_inactive",
            "raw_current_price": str(item.get("current_price")) if item.get("current_price") is not None else None,
            "raw_base_price": str(item.get("price")) if item.get("price") is not None else None,
            "discount_full_payment_percent": item.get("profit_full_payment"),
            "discount_until": item.get("profit_full_payment_until_date"),
            "building_floors": item.get("floor_count"),
            "district": item.get("district"),
            "catalog_total_claimed": payload.get("count") if isinstance(payload.get("count"), int) else None,
        })
        row["publisher_unit_id"] = str(unit_id)
        row["publisher_project_id"] = slug
        row["unit_number"] = str(item["number"]) if item.get("number") is not None else None
        row["corpus"] = str(item["building"]) if item.get("building") is not None else None
        row["section"] = str(item["section"]) if item.get("section") is not None else None
        row["floor"] = item.get("floor") if isinstance(item.get("floor"), int) else None
        row["rooms"] = item.get("rooms") if isinstance(item.get("rooms"), int) else None
        row["area_m2"] = float(item["area"]) if isinstance(item.get("area"), (int, float)) else None
        discounted = item.get("show_price_discounted") is True and isinstance(item.get("profit_full_payment"), int)
        if item.get("status") == 1:
            row["price_rub"] = _rubles(item.get("current_price"))
        row["price_basis"] = "discounted price at 100% payment" if discounted else "public API current_price; conditions unknown"
        if discounted:
            row["promo_conditions"] = f"{item['profit_full_payment']}% discount for 100% payment"
        row["source_fields"]["former_display_price_rub"] = _rubles(item.get("price"))
        for field, role, caption in (
            ("plan", "apartment_plan", "План квартиры SVG"),
            ("plan_png", "apartment_plan", "План квартиры PNG"),
            ("plan_3d", "apartment_plan", "3D планировка"),
            ("floor_plan", "floor_plan", "План этажа"),
        ):
            url = item.get(field)
            if isinstance(url, str) and url.startswith("https://storage.yandexcloud.net/granelleprod/"):
                row["media"].append(_media(url, role, f"public_api.results[].{field}", caption))
        rows.append(row)
    return rows


def _catalog_cards(root: _Element, page_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link in root.descendants("a"):
        url = _official(link.attrs.get("href", ""), page_url)
        if not url:
            continue
        match = _FLAT_PATH.fullmatch(urlparse(url).path)
        if not match:
            continue
        slug, unit_id = match.groups()
        title = link.first_class("title_")
        price = link.first_class("priceLabel_")
        project = link.first_class("project_")
        if not (title and price and project):
            continue
        area_match = _AREA.search(title.text())
        if not area_match:
            continue
        scope, scope_source = _scope(slug)
        row = _base("unit", url, project.text(), {
            "unit_id": unit_id, "project_slug": slug, "location_scope": scope,
            "scope_evidence": scope_source, "observation_page_url": page_url,
            "observed_price_text": price.text(), "record_granularity": "catalog_card",
        })
        row["publisher_unit_id"] = unit_id
        row["publisher_project_id"] = slug
        row["area_m2"] = float(area_match.group(1).replace(",", "."))
        room_match = _ROOMS.search(title.text())
        row["rooms"] = 0 if room_match and room_match.group(1).lower() == "студия" else (int(room_match.group(1).split("-")[0]) if room_match else None)
        row["price_rub"] = _money(price.text())
        row["price_basis"] = "from; payment conditions not shown on catalog card"
        old_price = link.first_class("oldPrice_")
        if old_price:
            row["source_fields"]["former_display_price_rub"] = _money(old_price.text())
        discount = link.first_class("profitTags_")
        row["promo_conditions"] = discount.text() if discount else None
        for crumb in link.descendants("li"):
            text = crumb.text()
            building = _BUILDING.search(text)
            floor = _FLOOR.search(text)
            if building:
                row["corpus"] = building.group(1)
            if floor:
                row["floor"] = int(floor.group(1))
                row["source_fields"]["building_floors"] = int(floor.group(2))
        for image in link.descendants("img"):
            src = image.attrs.get("data-lazy-src") or image.attrs.get("src", "")
            if not src.startswith("https://storage.yandexcloud.net/granelleprod/"):
                continue
            if "/media/properties/property/plan/" in src:
                role, caption = "apartment_plan", "План квартиры в карточке; вариант 2D/3D не указан"
            elif "/media/buildings/floor/plan/" in src:
                role, caption = "floor_plan", "План этажа в карточке"
            else:
                continue
            if not any(item["url"] == src for item in row["media"]):
                row["media"].append(_media(src, role, "catalog_card.img", caption))
        rows.append(row)
    return rows


def _listing(root: _Element, page_url: str) -> list[dict[str, Any]]:
    match = _FLAT_PATH.fullmatch(urlparse(page_url).path)
    if not match:
        return []
    slug, unit_id = match.groups()
    nodes: list[dict[str, Any]] = []
    for script in root.descendants("script"):
        if script.attrs.get("type") == "application/ld+json":
            try:
                value = json.loads(script.text())
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                nodes.extend(item for item in value.get("@graph", []) if isinstance(item, dict))
    apartment = next((item for item in nodes if str(item.get("sku")) == unit_id), None)
    if not apartment:
        return []
    complex_node = next((item for item in nodes if item.get("@type") == "ApartmentComplex"), {})
    floor_plan = next((item for item in nodes if item.get("@type") == "FloorPlan" and item.get("isPlanForApartment", {}).get("@id") == apartment.get("@id")), {})
    scope, scope_source = _scope(slug)
    row = _base("unit", page_url, complex_node.get("name"), {
        "unit_id": unit_id, "project_slug": slug, "location_scope": scope,
        "scope_evidence": scope_source, "record_granularity": "unit_page_jsonld",
        "availability_claim": apartment.get("offers", {}).get("availability"),
    })
    row["publisher_unit_id"] = unit_id
    row["publisher_project_id"] = slug
    row["unit_number"] = str(apartment.get("productID")) if apartment.get("productID") is not None else None
    row["floor"] = int(apartment["floorLevel"]) if str(apartment.get("floorLevel", "")).isdigit() else None
    row["rooms"] = apartment.get("numberOfRooms") if isinstance(apartment.get("numberOfRooms"), int) else None
    area = apartment.get("floorSize", {}).get("value")
    row["area_m2"] = float(area) if isinstance(area, (float, int)) else None
    offer = apartment.get("offers", {})
    if isinstance(offer.get("price"), (int, float)):
        row["price_rub"] = int(offer["price"])
    row["price_basis"] = "from; 100% payment" if "100%" in str(offer.get("description", "")) else "offer terms unknown"
    row["promo_conditions"] = offer.get("description")
    for item in apartment.get("additionalProperty", []):
        if not isinstance(item, dict):
            continue
        field = item.get("propertyID")
        if field == "building_number":
            row["corpus"] = str(item.get("value"))
        elif field == "building_section":
            row["section"] = str(item.get("value"))
        elif field in {"district", "building_floors", "commissioning_period"}:
            row["source_fields"][field] = item.get("value")
    layout = floor_plan.get("layoutImage")
    if isinstance(layout, str) and layout.startswith("https://storage.yandexcloud.net/granelleprod/"):
        row["media"].append(_media(layout, "apartment_plan", "JSON-LD FloorPlan.layoutImage", "Планировка квартиры"))
    return [row]


def _project(root: _Element, page_url: str) -> list[dict[str, Any]]:
    match = _PROJECT_PATH.fullmatch(urlparse(page_url).path)
    if not match:
        return []
    slug = match.group(1)
    scope, scope_source = _scope(slug)
    complex_node: dict[str, Any] = {}
    for script in root.descendants("script"):
        if script.attrs.get("type") != "application/ld+json":
            continue
        try:
            value = json.loads(script.text())
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            candidates = value.get("@graph", [value])
            complex_node = next((item for item in candidates if isinstance(item, dict)
                                 and item.get("@type") == "ApartmentComplex"), complex_node)
    name = complex_node.get("name")
    if not isinstance(name, str) or not name:
        title = next(root.descendants("title"), None)
        title_text = title.text() if title else ""
        named = re.search(r"ЖК\s+«([^»]+)»", title_text)
        name = named.group(1) if named else title_text.split(":", 1)[0].strip() or None
    row = _base("project", page_url, name, {
        "project_slug": slug, "location_scope": scope, "scope_evidence": scope_source,
        "record_granularity": "official_project_page",
    })
    row["publisher_project_id"] = slug
    address = complex_node.get("address")
    if isinstance(address, dict) and isinstance(address.get("streetAddress"), str):
        row["address"] = address["streetAddress"]
        row["source_fields"]["address_components"] = {
            key: address[key] for key in ("streetAddress", "addressLocality", "addressRegion")
            if isinstance(address.get(key), str)
        }
    visible = _visible_text(root)
    count = re.search(r"\b(\d+)\s+квартир(?:ы|а)?\b", visible, re.I)
    if count:
        row["source_fields"]["catalog_total_claimed"] = int(count.group(1))
    for image in root.descendants("img"):
        url = image.attrs.get("src", "")
        caption = image.attrs.get("alt", "").strip()
        if (not caption or caption.lower() in {"image", "изображение"}
                or "/media/projects" not in url
                or not (url.startswith("https://granelle.ru/")
                        or url.startswith("https://storage.yandexcloud.net/granelleprod/"))):
            continue
        if not any(item["url"] == url for item in row["media"]):
            row["media"].append(_media(url, "unknown", "project_page.img.alt/src", caption, "project"))
        if len(row["media"]) >= 8:
            break
    return [row]


def parse_granel_page(html: str, page_url: str, observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    """Return source-backed observations, including scope-unknown audit rows."""
    if not _official(page_url, page_url):
        return []
    if html.lstrip().startswith("{"):
        try:
            payload = json.loads(html)
        except ValueError:
            return []
        rows = _api_records(payload, page_url) if isinstance(payload, dict) else []
    else:
        root = _tree(html)
        path = urlparse(page_url).path
        if _FLAT_PATH.fullmatch(path):
            rows = _listing(root, page_url)
        elif _PROJECT_PATH.fullmatch(path):
            rows = _project(root, page_url)
        else:
            rows = _catalog_cards(root, page_url)
        if path.rstrip("/") == "/flats":
            total_match = re.search(r"Показать\s+еще\s+\d+\s+из\s+(\d+)", _visible_text(root), re.I)
            if total_match:
                for row in rows:
                    # The UI's "из N" is the number remaining after the SSR
                    # cards, as confirmed by the filtered API count 463 = 439+24.
                    row["source_fields"]["catalog_remaining_claimed"] = int(total_match.group(1))
                    row["source_fields"]["catalog_total_claimed"] = int(total_match.group(1)) + len(rows)
    for row in rows:
        row["source_fields"]["observed_at"] = observed_at
        row["source_fields"]["raw_sha256"] = raw_sha256
    return rows
