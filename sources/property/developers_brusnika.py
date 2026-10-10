"""Offline observations from public Brusnika Moscow SSR pages.

The general /flat/ catalogue currently renders as a client-side shell.  This
parser reports only unit fields present in the supplied HTML; it does not
guess a private API, a project address, or image rights.
"""

from __future__ import annotations

import re
import unicodedata
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from sources.property.developers_granel import _Element, _tree


_HOST = "moskva.brusnika.ru"
_FLAT_PATH = re.compile(r"^/flat/([0-9]+)/?$")
_PROJECT_PATH = re.compile(r"^/projects/([a-z0-9-]+)/?$")
_FLOOR = re.compile(r"Этаж\s+(\d+)\s+из\s+(\d+)", re.I)
_AREA = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*м²", re.I)
_PRICE = re.compile(r"([0-9]{1,3}(?:[\s\u00a0][0-9]{3})+)\s*(?:руб\.?|₽)", re.I)
_NUMBER = re.compile(r"№\s*([0-9]+)\s*[•·]\s*([^\s<]+)")
_SECTION = re.compile(r"Секция\s+([\w.-]+)", re.I)
_BUILDING = re.compile(r"Дом\s+([\w.-]+)", re.I)
_BEDROOMS = re.compile(r"(\d+)\s+спальн", re.I)
_API_PATH = "/api/filter/flats/"
_PROJECT_API_PATH = "/api/projects/"
_PUBLIC_CATALOG_BUNDLE = "https://cdn.brusnika.ru/static/js/flat-list.f3df9b658140.js"
_PROJECT_SCOPES = {
    87: ("in_scope_moscow", "https://moskva.brusnika.ru/projects/dom-a/"),
    11: ("out_of_scope", "https://moskva.brusnika.ru/projects/perviy/"),
    79: ("in_scope_moscow", "https://moskva.brusnika.ru/projects/metronom/"),
    82: ("in_scope_moscow", "https://moskva.brusnika.ru/projects/mons/"),
    92: ("in_scope_moscow", "https://moskva.brusnika.ru/projects/gertzena/"),
}
_PROJECT_SLUG_IDS = {"dom-a": 87, "perviy": 11, "metronom": 79,
                     "mons": 82, "izdanie": 83, "gertzena": 92}
_PROJECT_SCOPE_BASIS = {
    79: "project_meta_description_explicit_moscow",
    82: "project_meta_description_explicit_capital_and_butyrsky_district",
    92: "project_meta_description_explicit_south_moscow",
}
_CAPTURED_PROJECT_DETAIL_IDS = {79, 82, 83, 92}


def _official(href: str, page_url: str) -> str | None:
    url = urljoin(page_url, href)
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname != _HOST or parts.username or parts.password:
        return None
    return url


def _api_next(raw_url: str) -> str | None:
    parts = urlparse(raw_url)
    if parts.scheme != "https" or parts.netloc != _HOST or parts.path != _API_PATH or not parts.query or parts.fragment:
        return None
    return raw_url


def _rubles(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _visible_text(root: _Element) -> str:
    def parts(node: _Element) -> list[str]:
        if node.tag in {"script", "style"}:
            return []
        values: list[str] = []
        for child in node.children:
            values.extend(parts(child) if isinstance(child, _Element) else [child])
        return values
    return " ".join(" ".join(parts(root)).split())


def discover_brusnika_pages(html: str, page_url: str) -> list[dict[str, str]]:
    if html.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(html)
        except ValueError:
            return []
        if page_url == f"https://{_HOST}{_PROJECT_API_PATH}" and isinstance(payload, list):
            refs: dict[str, dict[str, str]] = {}
            for item in payload:
                if not isinstance(item, dict) or item.get("id") not in _CAPTURED_PROJECT_DETAIL_IDS:
                    continue
                url = item.get("url")
                slug = item.get("slug")
                if (isinstance(url, str) and isinstance(slug, str)
                        and _official(url, page_url) == url
                        and _PROJECT_PATH.fullmatch(urlparse(url).path)
                        and _PROJECT_PATH.fullmatch(urlparse(url).path).group(1) == slug):
                    refs[url] = {"url": url, "page_kind": "project", "project_hint": slug}
            return list(refs.values())
        raw_next = payload.get("next") if isinstance(payload, dict) else None
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
            ref = {"url": url, "page_kind": "listing"}
        elif project:
            ref = {"url": url, "page_kind": "project", "project_hint": project.group(1)}
        elif path.rstrip("/") == "/flat":
            ref = {"url": url, "page_kind": "catalog"}
        else:
            continue
        found.setdefault(url, ref)
    if any(script.attrs.get("src") == _PUBLIC_CATALOG_BUNDLE for script in root.descendants("script")):
        # The linked public JS uses api='/api/filter/' and fetch(api+'flats/?').
        api_url = f"https://{_HOST}{_API_PATH}"
        found.setdefault(api_url, {"url": api_url, "page_kind": "catalog",
                                   "discovery_basis": _PUBLIC_CATALOG_BUNDLE})
    return list(found.values())


def _media(root: _Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for image in root.descendants("img"):
        url = image.attrs.get("src", "")
        if not url.startswith("https://cdn.brusnika.ru/"):
            continue
        alt = image.attrs.get("alt", "").strip()
        if alt == "Планировка" and "/media/flat/" in url:
            role, scope = "apartment_plan", "unit"
        elif alt == "Дом «А»" and "/media/complex/preview/" in url:
            role, scope = "unknown", "project"
        elif alt == "Генплан" and "/media/complex/master_plan_image/" in url:
            role, scope = "unknown", "project"
        else:
            continue
        if not any(item["url"] == url for item in items):
            items.append({"url": url, "role": role, "caption": alt,
                          "scope": scope, "source_field": "SSR img.alt/src",
                          "rights_status": "unknown"})
    return items


def _api_records(payload: dict[str, Any], page_url: str, observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) > 200:
        return []
    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        unit_id = item.get("flat_id")
        unit_url = item.get("page_url")
        if not isinstance(unit_id, int) or unit_id <= 0 or not isinstance(unit_url, str):
            continue
        if _official(unit_url, page_url) != unit_url or not _FLAT_PATH.fullmatch(urlparse(unit_url).path) or str(unit_id) != _FLAT_PATH.fullmatch(urlparse(unit_url).path).group(1):
            continue
        project_id = item.get("complex")
        scope, scope_source = _PROJECT_SCOPES.get(project_id, ("unknown", None))
        booked = item.get("is_booked") is True
        marketing = item.get("show_price_marketing") is True
        price_field = "price_marketing" if marketing else "price"
        price = _rubles(item.get(price_field)) if not booked else None
        row: dict[str, Any] = {
            "record_type": "unit", "source_url": unit_url,
            "publisher_unit_id": str(unit_id),
            "publisher_project_id": str(project_id) if isinstance(project_id, int) else None,
            "publisher_building_id": str(item["building"]) if isinstance(item.get("building"), int) else None,
            "source_updated_at": None,
            "project_name": item.get("complex_name") if isinstance(item.get("complex_name"), str) else None,
            "address": None,
            "corpus": str(item["building_number"]) if item.get("building_number") is not None else None,
            "section": str(item["section_number"]) if item.get("section_number") is not None else None,
            "unit_number": None,
            "floor": item.get("floor") if isinstance(item.get("floor"), int) else None,
            "rooms": item.get("rooms") if isinstance(item.get("rooms"), int) else None,
            "area_m2": float(item["square"]) if isinstance(item.get("square"), (int, float)) else None,
            "price_rub": price,
            "price_basis": "marketing price; applicability flags in source_fields" if marketing else "source base asking price",
            "promo_conditions": "source marks marketing price; exact eligibility not stated" if marketing else None,
            "media": [], "rights_status": "unknown",
            "source_fields": {
                "unit_id": str(unit_id), "api_pk": item.get("pk"),
                "observation_page_url": page_url, "project_id": project_id,
                "location_scope": scope, "scope_evidence": scope_source,
                "scope_evidence_basis": _PROJECT_SCOPE_BASIS.get(project_id),
                "availability_scope": "booked" if booked else "sale_candidate",
                "is_booked": booked, "price_field_used": price_field,
                "raw_price": str(item.get("price")) if item.get("price") is not None else None,
                "raw_price_marketing": str(item.get("price_marketing")) if item.get("price_marketing") is not None else None,
                "marketing_price_full_price": item.get("marketing_price_full_price"),
                "marketing_price_mortgage": item.get("marketing_price_mortgage"),
                "marketing_price_exchange": item.get("marketing_price_exchange"),
                "promo_date": item.get("promo_date"),
                "building_floors": item.get("building_floors_count"),
                "delivery_period": item.get("delivery_title"),
                "observed_at": observed_at, "raw_sha256": raw_sha256,
                "record_granularity": "public_api_catalog_row",
                "catalog_total_claimed": payload.get("count") if isinstance(payload.get("count"), int) else None,
            },
        }
        for field, role, caption in (
            ("main_flat_image", "apartment_plan", "План квартиры в каталоге"),
            ("floor_image_file", "floor_plan", "Изображение этажа в каталоге"),
        ):
            url = item.get(field)
            if isinstance(url, str) and url.startswith("https://cdn.brusnika.ru/"):
                row["media"].append({"url": url, "role": role, "caption": caption,
                                     "scope": "unit", "source_field": f"public_api.results[].{field}",
                                     "rights_status": "unknown"})
        rows.append(row)
    return rows


def _project_api_records(payload: list[Any], page_url: str, observed_at: str,
                         raw_sha256: str) -> list[dict[str, Any]]:
    """Keep publisher project IDs separate from marketing map markers."""
    if page_url != f"https://{_HOST}{_PROJECT_API_PATH}" or len(payload) > 30:
        return []
    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        project_id, slug, url, name = (item.get("id"), item.get("slug"),
                                       item.get("url"), item.get("name"))
        if (not isinstance(project_id, int) or project_id <= 0 or project_id in seen_ids
                or not isinstance(slug, str) or not isinstance(url, str)
                or not isinstance(name, str) or not name.strip()
                or _official(url, page_url) != url
                or not _PROJECT_PATH.fullmatch(urlparse(url).path)
                or _PROJECT_PATH.fullmatch(urlparse(url).path).group(1) != slug):
            continue
        seen_ids.add(project_id)
        scope, scope_source = _PROJECT_SCOPES.get(project_id, ("unknown", None))
        row: dict[str, Any] = {
            "record_type": "project", "source_url": url,
            "publisher_project_id": str(project_id),
            "source_updated_at": None, "project_name": name.strip(),
            "address": None, "corpus": None, "section": None,
            "unit_number": None, "floor": None, "rooms": None,
            "area_m2": None, "price_rub": None, "price_basis": None,
            "promo_conditions": None, "media": [], "rights_status": "unknown",
            "source_fields": {
                "project_id": project_id, "project_slug": slug,
                "commercial_complex_id": item.get("commercial_complex_id"),
                "observation_page_url": page_url,
                "location_scope": scope, "scope_evidence": scope_source,
                "scope_evidence_basis": _PROJECT_SCOPE_BASIS.get(project_id),
                "marketing_project_marker": {
                    "longitude": item.get("longitude"), "latitude": item.get("latitude"),
                    "coordinate_basis": "publisher_project_map_marker_not_building_location",
                },
                "api_project_flat_count_claimed": item.get("count_flat"),
                "record_granularity": "public_api_project_row",
                "observed_at": observed_at, "raw_sha256": raw_sha256,
            },
        }
        preview = item.get("preview")
        if isinstance(preview, str) and preview.startswith("https://cdn.brusnika.ru/"):
            row["media"].append({"url": preview, "role": "unknown",
                                 "caption": "Превью карточки проекта", "scope": "project",
                                 "source_field": "public_api.projects[].preview",
                                 "rights_status": "unknown"})
        slides = item.get("card_slides")
        if isinstance(slides, list):
            for slide in slides[:4]:
                image = slide.get("original") if isinstance(slide, dict) else None
                if (isinstance(image, str) and image.startswith("https://cdn.brusnika.ru/")
                        and not any(media["url"] == image for media in row["media"])):
                    row["media"].append({"url": image, "role": "unknown",
                                         "caption": "Слайд карточки проекта", "scope": "project",
                                         "source_field": "public_api.projects[].card_slides[].original",
                                         "rights_status": "unknown"})
        rows.append(row)
    return rows


def _project(root: _Element, page_url: str, observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    match = _PROJECT_PATH.fullmatch(urlparse(page_url).path)
    if not match:
        return []
    slug = match.group(1)
    project_id = _PROJECT_SLUG_IDS.get(slug)
    scope, scope_source = _PROJECT_SCOPES.get(project_id, ("unknown", None))
    title = next(root.descendants("title"), None)
    name = re.split(r"\s+[–-]\s+", title.text(), maxsplit=1)[0].strip() if title else None
    row: dict[str, Any] = {
        "record_type": "project", "source_url": page_url,
        "publisher_project_id": str(project_id) if project_id is not None else None,
        "source_updated_at": None, "project_name": name,
        "address": None, "corpus": None, "section": None,
        "unit_number": None, "floor": None, "rooms": None,
        "area_m2": None, "price_rub": None, "price_basis": None,
        "promo_conditions": None, "media": [], "rights_status": "unknown",
        "source_fields": {
            "project_slug": slug, "location_scope": scope,
            "scope_evidence": scope_source,
            "scope_evidence_basis": _PROJECT_SCOPE_BASIS.get(project_id),
            "observed_at": observed_at, "raw_sha256": raw_sha256,
            "record_granularity": "official_project_page",
        },
    }
    count = re.search(r"\b(\d+)\s+квартир(?:ы|а)?\b", _visible_text(root), re.I)
    if count:
        row["source_fields"]["catalog_total_claimed"] = int(count.group(1))
    for image in root.descendants("img"):
        url = image.attrs.get("src", "")
        caption = image.attrs.get("alt", "").strip()
        if (not caption or caption.lower() in {"image", "изображение"}
                or not url.startswith("https://cdn.brusnika.ru/")
                or "/media/complex/" not in url):
            continue
        if not any(item["url"] == url for item in row["media"]):
            row["media"].append({"url": url, "role": "unknown", "caption": caption,
                                 "scope": "project", "source_field": "project_page.img.alt/src",
                                 "rights_status": "unknown"})
        if len(row["media"]) >= 8:
            break
    for video in root.descendants("video"):
        poster = video.attrs.get("poster", "")
        if (poster.startswith("https://cdn.brusnika.ru/media/complex/")
                and not any(item["url"] == poster for item in row["media"])):
            row["media"].append({"url": poster, "role": "unknown",
                                 "caption": "Заставка видео проекта", "scope": "project",
                                 "source_field": "project_page.video.poster",
                                 "rights_status": "unknown"})
        if len(row["media"]) >= 8:
            break
    return [row]


def parse_brusnika_page(html: str, page_url: str, observed_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    if not _official(page_url, page_url):
        return []
    if html.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(html)
        except ValueError:
            return []
        if isinstance(payload, list):
            return _project_api_records(payload, page_url, observed_at, raw_sha256)
        return _api_records(payload, page_url, observed_at, raw_sha256) if isinstance(payload, dict) else []
    if _PROJECT_PATH.fullmatch(urlparse(page_url).path):
        return _project(_tree(html), page_url, observed_at, raw_sha256)
    match = _FLAT_PATH.fullmatch(urlparse(page_url).path)
    if not match:
        return []
    unit_id = match.group(1)
    root = _tree(html)
    text = _visible_text(root)
    floor = _FLOOR.search(text)
    area = _AREA.search(text)
    number = _NUMBER.search(text)
    price_node = next((node for node in root.descendants() if any(c.startswith("mainPriceWrapper_") for c in node.attrs.get("class", "").split())), None)
    price = _PRICE.search(price_node.text()) if price_node else None
    if not (floor and area and number and price):
        return []
    project_name = None
    title = next(root.descendants("title"), None)
    if title:
        project_match = re.search(r"в\s+(Доме\s+«[^»]+»)", title.text())
        if project_match:
            project_name = project_match.group(1).replace("Доме ", "Дом ", 1)
    project_image = next((image for image in root.descendants("img") if image.attrs.get("alt", "").startswith("Дом «")), None)
    if project_image:
        project_name = project_image.attrs["alt"]
    section = _SECTION.search(text)
    building = _BUILDING.search(text)
    bedrooms = _BEDROOMS.search(text)
    delivery = re.search(r"Срок сдачи\s+([1-4]\s+квартал\s+20\d\d)", text)
    project_id: int | None = None
    building_id: int | None = None
    for link in root.descendants("a"):
        href = link.attrs.get("href", "")
        if urlparse(urljoin(page_url, href)).path.rstrip("/") != "/flat":
            continue
        query = parse_qs(urlparse(urljoin(page_url, href)).query)
        if query.get("complex", [""])[0].isdigit() and query.get("building", [""])[0].isdigit():
            project_id = int(query["complex"][0])
            building_id = int(query["building"][0])
            break
    scope, scope_source = _PROJECT_SCOPES.get(project_id, ("unknown", None))
    row: dict[str, Any] = {
        "record_type": "unit", "source_url": page_url,
        "publisher_unit_id": unit_id,
        "publisher_project_id": str(project_id) if project_id is not None else None,
        "publisher_building_id": str(building_id) if building_id is not None else None,
        "source_updated_at": None, "project_name": project_name,
        "address": None, "corpus": building.group(1) if building else None,
        "section": section.group(1) if section else None,
        "unit_number": number.group(1), "floor": int(floor.group(1)),
        "rooms": None, "area_m2": float(area.group(1).replace(",", ".")),
        "price_rub": int(re.sub(r"\D", "", price.group(1))),
        "price_basis": "displayed asking price; payment conditions not stated on unit page",
        "promo_conditions": None, "media": _media(root),
        "rights_status": "unknown",
        "source_fields": {
            "unit_id": unit_id,
            "article": "".join(char for char in number.group(2) if unicodedata.category(char) != "Cf"),
            "building_floors": int(floor.group(2)),
            "bedrooms": int(bedrooms.group(1)) if bedrooms else None,
            "delivery_period": delivery.group(1) if delivery else None,
            "location_scope": scope,
            "scope_evidence": scope_source,
            "scope_evidence_basis": _PROJECT_SCOPE_BASIS.get(project_id),
            "location_scope_reason": "official project establishes city only; street address remains unknown",
            "availability_claim": "unit page displayed with booking action; current sale status not independently confirmed",
            "observed_at": observed_at, "raw_sha256": raw_sha256,
            "record_granularity": "unit_page_ssr",
        },
    }
    return [row]
