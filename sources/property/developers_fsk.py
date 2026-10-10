"""Offline parsers for publicly rendered FSK project and apartment pages.

The module performs no network requests. Callers own HTTP policy, caching,
snapshot hashes, pacing, and stop conditions. Only facts bound to visible page
elements or explicit JSON-LD fields are emitted; absent values remain ``None``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_FSK_HOSTS = {"fsk.ru", "www.fsk.ru"}
_MEDIA_HOSTS = {"fsk.ru", "www.fsk.ru", "cdn.fsk.ru"}
_UNIT_PATH_RE = re.compile(r"^/([a-z0-9-]+)/flats/(\d+)/?$", re.I)
_CATALOG_API_PATH = "/api/v3/flats"
_CATALOG_UI_PAGE_SIZE = 20
_CATALOG_PATHS = {"/flats", "/flats/list", "/flats/projects"}
_AREA_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[,.]\d{1,2})?)\s*(?:м²|м2|кв\.\s*м)", re.I)
_ROOM_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-‑–—]?\s*комнат", re.I)
_UNIT_RE = re.compile(r"(?:№|номер\s*)\s*(\d{1,8})", re.I)
_FLOOR_RE = re.compile(r"(?<!\d)(\d{1,3})\s*/\s*(\d{1,3})(?!\d)")
_CONDITIONAL_PRICE_RE = re.compile(
    r"(?:при\s+100\s*%\s*оплат\w*|"
    r"при\s+(?:условии|оформлении|оплате|покупке)\b.{0,80}"
    r"(?:ипотек|рассрочк|наличн|100\s*%|субсид|кредит|аккредитив)|"
    r"(?:ипотек|рассрочк|наличн|100\s*%|субсид|кредит|аккредитив)"
    r".{0,50}\bпри\s+(?:условии|оформлении|оплате|покупке)|"
    r"(?:только\s+при|цена\s+(?:при|для))\b.{0,80}"
    r"(?:ипотек|рассрочк|наличн|100\s*%|субсид|кредит|аккредитив))",
    re.I,
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(
            part
            for child in self.children
            for part in ([child] if isinstance(child, str) else [child.text()])
            if part
        )

    def descendants(self, tag: str | None = None) -> Iterable[_Node]:
        for child in self.children:
            if isinstance(child, _Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.descendants(tag)

    def by_class(self, class_name: str) -> list[_Node]:
        return [
            node
            for node in self.descendants()
            if class_name in node.attrs.get("class", "").split()
        ]

    def first_class(self, class_name: str) -> _Node | None:
        return next(iter(self.by_class(class_name)), None)


class _TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag.lower() not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        cursor: _Node | None = self.current
        while cursor is not None and cursor is not self.root:
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent

    def handle_data(self, data: str) -> None:
        if data:
            self.current.children.append(data)


def _parse_tree(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _safe_url(value: str, base_url: str, allowed_hosts: set[str]) -> str | None:
    candidate = urljoin(base_url, value.strip())
    parts = urlsplit(candidate)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or host not in allowed_hosts
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        return None
    # Preserve a public query if the source itself supplied it, but discard a
    # fragment which is client-side navigation rather than a distinct page.
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _page_url(page_url: str) -> str:
    safe = _safe_url(page_url, page_url, _FSK_HOSTS)
    if safe is None:
        raise ValueError("page_url must be an HTTPS URL on fsk.ru")
    return safe


def _classify_path(url: str) -> str | None:
    path = urlsplit(url).path.rstrip("/") or "/"
    if path == _CATALOG_API_PATH:
        return "catalog"
    if path in _CATALOG_PATHS:
        return "catalog"
    if _UNIT_PATH_RE.fullmatch(path + "/"):
        return "listing"
    # A project page is linked by the publisher under a single slug segment.
    if re.fullmatch(r"/[a-z0-9-]+", path, re.I):
        return "project"
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _number(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d[\d\s\u00a0]*(?:[,.]\d+)?", text)
    if not match:
        return None
    normalized = re.sub(r"[\s\u00a0]", "", match.group(0)).replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _money(text: str | None) -> int | None:
    if not text:
        return None
    if "за м" in text.lower() or re.search(r"\bот\s+\d", text.lower()):
        return None
    digits = re.sub(r"\D", "", text)
    if not digits or "₽" not in text and "руб" not in text.lower():
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _jsonld_documents(root: _Node) -> list[Any]:
    documents: list[Any] = []
    for node in root.descendants("script"):
        if node.attrs.get("type", "").lower() != "application/ld+json":
            continue
        raw = node.text().strip()
        if not raw:
            continue
        try:
            documents.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return documents


def _walk_jsonld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_jsonld(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_jsonld(child)


def _jsonld_entities(documents: list[Any]) -> list[dict[str, Any]]:
    return [item for document in documents for item in _walk_jsonld(document)]


def _price_basis(price_text: str | None, promo_texts: list[str],
                 price_marked_from: bool = False) -> str:
    if price_marked_from:
        return "project_from"
    if not price_text or _money(price_text) is None:
        return "unknown"
    if any(_CONDITIONAL_PRICE_RE.search(text) for text in [price_text, *promo_texts]):
        return "conditional_total"
    return "asking_total"


def _media(url: str | None, role: str, scope: str, source_field: str,
           label: str | None = None, evidence: str | None = None,
           visual_kind: str = "unknown") -> dict[str, Any] | None:
    if not url:
        return None
    return {
        "url": url,
        "role": role,
        "scope": scope,
        "visual_kind": visual_kind,
        "source_field": source_field,
        "label": label,
        "evidence": evidence,
        "rights_status": "unknown",
    }


def _api_catalog_url(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "https" and (parts.hostname or "").lower() in _FSK_HOSTS
            and parts.path == _CATALOG_API_PATH)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and parsed < float("inf") else None


def _text_value(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = _clean(str(value))
        return text or None
    return None


def _api_unit_record(item: Any, index: int, source_url: str, observed_at: str,
                     raw_sha256: str, catalog_meta: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    external_id = _text_value(item.get("externalId"))
    if not external_id or not re.fullmatch(r"\d{1,20}", external_id):
        return None
    project = item.get("project") if isinstance(item.get("project"), dict) else {}
    complex_data = item.get("complex") if isinstance(item.get("complex"), dict) else {}
    corpus = item.get("corpus") if isinstance(item.get("corpus"), dict) else {}
    section = item.get("section") if isinstance(item.get("section"), dict) else {}
    project_name = _text_value(project.get("title")) or _text_value(complex_data.get("title"))
    corpus_number = _text_value(corpus.get("number"))
    section_number = _text_value(section.get("number"))
    unit_number = _text_value(item.get("number"))
    try:
        unit_number_value = int(unit_number) if unit_number and re.fullmatch(r"\d{1,8}", unit_number) else None
    except ValueError:
        unit_number_value = None
    floor_number = _positive_int(item.get("floorNumber"))
    if floor_number is None:
        floor = item.get("floor") if isinstance(item.get("floor"), dict) else {}
        floor_number = _positive_int(floor.get("number"))
    rooms_value = item.get("rooms")
    rooms = rooms_value if isinstance(rooms_value, int) and not isinstance(rooms_value, bool) \
        and rooms_value >= 0 else None
    area_m2 = _finite_number(item.get("areaTotal"))
    price_rub = _positive_int(item.get("price"))
    labels = item.get("labels") if isinstance(item.get("labels"), list) else []
    financials = item.get("financials") if isinstance(item.get("financials"), list) else []
    promo_texts = [text for value in labels
                   if isinstance(value, dict)
                   for text in [_text_value(value.get("title")),
                                _text_value(value.get("description"))] if text]
    promo_texts.extend(text for value in financials if isinstance(value, dict)
                       for text in [_text_value(value.get("title"))] if text)
    conditions = [text for text in promo_texts if _CONDITIONAL_PRICE_RE.search(text)]
    if item.get("priceOnRequest") is True or item.get("prePrice") is True:
        price_basis = "unknown"
    elif price_rub:
        price_basis = _price_basis(f"{price_rub} ₽", promo_texts)
    else:
        price_basis = "unknown"
    media: list[dict[str, Any]] = []
    for field_name in ("plan", "planFurniture"):
        raw_url = item.get(field_name)
        if not isinstance(raw_url, str):
            continue
        asset_url = _safe_url(raw_url, source_url, _MEDIA_HOSTS)
        asset = _media(asset_url, "apartment_plan", "unit", f"json:items[{index}].{field_name}",
                       field_name, "Explicit FSK apartment-plan response field")
        if asset:
            media.append(asset)
    project_id = _text_value(project.get("_id"))
    building_id = _text_value(corpus.get("_id"))
    source_fields = {
        "api_external_id": external_id,
        "api_item_id": _text_value(item.get("_id")),
        "api_project_slug": _text_value(project.get("slug")),
        "api_project_id": project_id,
        "api_building_id": building_id,
        "api_previous_price_rub": _positive_int(item.get("priceWoDiscount")),
        "api_pre_discount_price_rub": _positive_int(item.get("preDiscountPrice")),
        "api_discount_percent": _finite_number(item.get("discount")),
        "api_price_on_request": item.get("priceOnRequest") if isinstance(item.get("priceOnRequest"), bool) else None,
        "api_pre_price_flag": item.get("prePrice") if isinstance(item.get("prePrice"), bool) else None,
        "api_status_code": item.get("status") if isinstance(item.get("status"), int)
        and not isinstance(item.get("status"), bool) else None,
        "api_financial_labels": promo_texts,
        "api_corpus_id": building_id,
        "api_section_floors_count": _positive_int(section.get("floorsCount")),
        "api_delivery_date": _text_value(corpus.get("dateDelivery")),
        **catalog_meta,
    }
    return {
        "record_type": "unit",
        "source_url": source_url,
        "observed_at": observed_at,
        "source_updated_at": None,
        "raw_sha256": raw_sha256,
        "project_name": project_name,
        "address": None,
        "corpus": corpus_number,
        "section": section_number,
        "unit_number": unit_number_value,
        "floor": floor_number,
        "rooms": rooms,
        "area_m2": area_m2,
        "price_rub": price_rub,
        "price_basis": price_basis,
        "promo_conditions": conditions or None,
        "publisher_project_id": project_id,
        "publisher_building_id": building_id,
        "publisher_unit_id": external_id,
        "media": media,
        "rights_status": "unknown",
        "source_fields": source_fields,
    }


def _parse_fsk_catalog_response(payload: Any, source_url: str, observed_at: str,
                                raw_sha256: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    total = _positive_int(payload.get("total"))
    has_next = payload.get("hasNextPage")
    request_query = dict(parse_qsl(urlsplit(source_url).query, keep_blank_values=True))
    try:
        offset = max(0, int(request_query.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, int(request_query.get("limit", payload.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    # This total comes from the structured response itself; unlike the UI
    # button, it is the catalog count claimed by FSK's API.
    catalog_meta = {
        "catalog_total_claimed": total,
        "catalog_offset": offset,
        "catalog_page_limit": limit,
        "catalog_page_has_next": has_next if isinstance(has_next, bool) else None,
    }
    return [record for index, item in enumerate(payload["items"])
            if (record := _api_unit_record(item, index, source_url, observed_at,
                                           raw_sha256, catalog_meta)) is not None]


def _next_fsk_catalog_ref(payload: Any, source_url: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("hasNextPage") is not True:
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    parts = urlsplit(source_url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query = dict(query_items)
    try:
        offset = max(0, int(query.get("offset", "0")))
    except (TypeError, ValueError):
        return None
    next_offset = offset + len(items)
    total = _positive_int(payload.get("total"))
    if next_offset <= offset or (total is not None and next_offset >= total):
        return None
    # The linked UI uses an 18-item first request when its two banner slots
    # are present, then requests 20 items for each subsequent page.
    updated = [(key, value) for key, value in query_items if key not in {"offset", "limit"}]
    updated.extend((("offset", str(next_offset)), ("limit", str(_CATALOG_UI_PAGE_SIZE))))
    next_url = urlunsplit(("https", parts.hostname or "fsk.ru", parts.path,
                           urlencode(updated), ""))
    return {"url": next_url, "page_kind": "catalog",
            "discovery_source": "fsk_frontend_offset_pagination"}


def _canonical_url(root: _Node, fallback: str) -> str:
    for node in root.descendants("link"):
        if "canonical" in node.attrs.get("rel", "").lower().split():
            target = _safe_url(node.attrs.get("href", ""), fallback, _FSK_HOSTS)
            if target:
                return target
    return fallback


def _ancestor_classes(node: _Node) -> set[str]:
    classes: set[str] = set()
    parent = node.parent
    while parent:
        classes.update(parent.attrs.get("class", "").split())
        parent = parent.parent
    return classes


def _project_link(root: _Node, current_url: str) -> tuple[str | None, str | None]:
    current_path = urlsplit(current_url).path
    candidates: list[tuple[int, str, str]] = []
    for node in root.descendants("a"):
        href = node.attrs.get("href", "")
        target = _safe_url(href, current_url, _FSK_HOSTS)
        if not target or target == current_url:
            continue
        path = urlsplit(target).path.rstrip("/")
        if re.fullmatch(r"/[a-z0-9-]+", path, re.I) and path != current_path.rstrip("/"):
            context = _ancestor_classes(node)
            if not context.intersection({"complex-navbar", "flat-project-container",
                                         "v8-breadcrumbs"}):
                continue
            label = _clean(node.text())
            if label and label.lower() not in {"главная", "каталог", "квартиры", "фск"}:
                score = 1
                classes = set(node.attrs.get("class", "").split())
                if "breadcrumb-link" in classes:
                    score += 3
                if label.lower() in {"о проекте", "подробнее", "перейти", "назад"}:
                    score -= 2
                candidates.append((score, target, label))
    if not candidates:
        return None, None
    _, target, label = max(candidates, key=lambda item: item[0])
    return target, label


def discover_fsk_pages(html: str, page_url: str) -> list[dict[str, Any]]:
    """Return linked pages and frontend-confirmed catalog continuations."""
    current = _page_url(page_url)
    if _api_catalog_url(current):
        try:
            payload = json.loads(html)
        except (json.JSONDecodeError, TypeError):
            return []
        next_ref = _next_fsk_catalog_ref(payload, current)
        return [next_ref] if next_ref else []
    root = _parse_tree(html)
    results: dict[str, dict[str, Any]] = {}
    for link in root.descendants("a"):
        raw_href = link.attrs.get("href", "")
        target = _safe_url(raw_href, current, _FSK_HOSTS)
        if not target or target == current:
            continue
        kind = _classify_path(target)
        if kind is None:
            continue
        # Only take apartment pages from actual unit cards. The site repeats
        # global navigation, recommendations, and footer links on every page.
        if kind == "listing" and not (
            "card" in link.attrs.get("class", "").split()
            or link.attrs.get("data-testid", "").startswith("flats-link-detail-")
        ):
            continue
        if kind == "listing" and _classify_path(current) == "listing":
            # Unit pages contain recommended/cross-sell cards. They are not
            # pagination or part of the currently selected catalog snapshot.
            continue
        # A next catalog page is included only when the source provides a
        # visible pagination link, never by synthesizing page/offset URLs.
        if kind == "catalog":
            ancestors = _ancestor_classes(link)
            if ancestors.intersection({"menu-container", "footer", "footer-nav"}):
                continue
            label = _clean(link.text()).lower()
            if not (link.attrs.get("rel", "").lower() == "next"
                    or any(word in label for word in ("следующ", "показать ещё", "показать еще"))):
                continue
        if kind == "project":
            # Project links must come from the unit/project context. Global
            # menus and footer lists would otherwise turn discovery into a
            # site-wide crawl unrelated to the current page.
            context_classes = {"complex-navbar", "flat-project-container", "v8-breadcrumbs",
                               "project-card", "project-details"}
            if not context_classes.intersection(_ancestor_classes(link)):
                continue
        # The /flats/projects route is a separate project list, not a page of
        # market inventory records.
        if kind == "catalog" and urlsplit(target).path.rstrip("/") == "/flats/projects":
            continue
        project_hint = None
        address_hint = None
        anchor_text = _clean(link.text()) or None
        if kind == "listing":
            project_node = link.first_class("flat-project-text")
            project_hint = _clean(project_node.text()) if project_node else None
        elif kind == "project":
            project_hint = anchor_text
        results[target] = {
            "url": target,
            "page_kind": kind,
            "project_hint": project_hint,
            "address_hint": address_hint,
        }
    current_parts = urlsplit(current)
    if (current_parts.path.rstrip("/") in {"/flats", "/flats/list"}
            and not current_parts.query):
        visible_unit_count = sum(
            1 for node in root.descendants("a")
            if "card" in node.attrs.get("class", "").split()
            and _UNIT_PATH_RE.fullmatch(urlsplit(
                _safe_url(node.attrs.get("href", ""), current, _FSK_HOSTS) or "").path)
        )
        has_more = any(
            re.search(r"Показать\s+ещ[её]\s+\d+\s+из\s+\d+", _clean(node.text()), re.I)
            for node in root.descendants("button")
        )
        if visible_unit_count > 0 and has_more:
            api_query = urlencode({
                "city": 1,
                "offset": 0,
                "limit": visible_unit_count,
                "sort": "price",
                "order": 1,
            })
            api_url = urlunsplit(("https", current_parts.hostname or "fsk.ru",
                                  _CATALOG_API_PATH, api_query, ""))
            results[api_url] = {
                "url": api_url,
                "page_kind": "catalog",
                "project_hint": None,
                "address_hint": None,
                "discovery_source": "fsk_frontend_catalog_request",
            }
    return list(results.values())


def _card_record(card: _Node, source_url: str, observed_at: str,
                 raw_sha256: str, entities: list[dict[str, Any]]) -> dict[str, Any] | None:
    href = _safe_url(card.attrs.get("href", ""), source_url, _FSK_HOSTS)
    if not href:
        return None
    match = _UNIT_PATH_RE.fullmatch(urlsplit(href).path)
    if not match:
        return None
    project_node = card.first_class("flat-project-text")
    title_node = card.first_class("card-flat")
    price_node = card.first_class("price-text")
    old_node = card.first_class("old-price")
    corpus = None
    corpus_max = None
    for node in card.by_class("card-info-params"):
        for child in node.descendants("span"):
            text = _clean(child.text())
            corpus_match = re.search(r"(\d+)\s*(?:корпус|корп\.)", text, re.I)
            if corpus_match:
                corpus = corpus_match.group(1)
                break
        if corpus:
            break
    floor_text = _clean((card.first_class("card-floor-number") or _Node("", {})).text()) or None
    floor_match = _FLOOR_RE.search(floor_text or "")
    title_text = _clean(title_node.text()) if title_node else ""
    rooms: int | None = None
    if "студия" in title_text.lower():
        rooms = 0
    else:
        room_match = _ROOM_RE.search(title_text)
        if room_match:
            rooms = int(room_match.group(1))
    area_match = _AREA_RE.search(title_text)
    area_m2 = _number(area_match.group(1)) if area_match else None
    price_text = _clean(price_node.text()) if price_node else None
    old_price_text = _clean(old_node.text()) if old_node else None
    promos = [_clean(node.text()) for node in card.by_class("card-promo-item")]
    basis = _price_basis(price_text, promos,
                         bool(price_text and re.search(r"\bот\s+\d", price_text, re.I)))
    conditions = [text for text in promos if _CONDITIONAL_PRICE_RE.search(text)]
    images: list[dict[str, Any]] = []
    image_node = card.first_class("card-image")
    if image_node:
        for idx, image in enumerate(image_node.descendants("img")):
            source = image.attrs.get("data-src") or image.attrs.get("src")
            safe = _safe_url(source, source_url, _MEDIA_HOSTS) if source else None
            item = _media(safe, "unknown", "unit", f"html:card-image.img[{idx}]",
                          image.attrs.get("alt") or None,
                          "Image is bound to this individual unit card; role is not stated")
            if item:
                images.append(item)
    source_fields = {
        "catalog_card_href": card.attrs.get("href"),
        "card_title_text": title_text or None,
        "visible_price_text": price_text,
        "visible_previous_price_text": old_price_text,
        "visible_promo_texts": promos,
        "visible_floor_text": floor_text,
        "card_test_id": card.attrs.get("data-testid"),
    }
    jsonld = [entity for entity in entities if entity.get("@type") in {"Product", "Apartment"}
              and entity.get("url") == href]
    if jsonld:
        source_fields["jsonld"] = [{
            "@type": entity.get("@type"),
            "name": entity.get("name"),
            "offers": entity.get("offers"),
            "identifier": entity.get("identifier"),
        } for entity in jsonld]
    return {
        "record_type": "unit",
        "source_url": source_url,
        "observed_at": observed_at,
        "source_updated_at": None,
        "raw_sha256": raw_sha256,
        "project_name": _clean(project_node.text()) if project_node else None,
        "address": None,
        "corpus": corpus,
        "section": None,
        "unit_number": None,
        "floor": int(floor_match.group(1)) if floor_match else None,
        "rooms": rooms,
        "area_m2": area_m2,
        "price_rub": _money(price_text),
        "price_basis": basis,
        "promo_conditions": conditions or None,
        "publisher_project_id": None,
        "publisher_building_id": None,
        "publisher_unit_id": match.group(2),
        "media": images,
        "rights_status": "unknown",
        "source_fields": source_fields,
    }


def _detail_unit(root: _Node, source_url: str, observed_at: str,
                 raw_sha256: str, entities: list[dict[str, Any]]) -> dict[str, Any] | None:
    page_match = _UNIT_PATH_RE.fullmatch(urlsplit(source_url).path)
    if not page_match:
        return None
    heading = next((node for node in root.descendants("h1") if node.text().strip()), None)
    title_text = _clean(heading.text()) if heading else ""
    corpus_text_node = root.first_class("flat-corpus-section-floor")
    corpus_text = _clean(corpus_text_node.text()) if corpus_text_node else ""
    rooms: int | None = 0 if "студия" in title_text.lower() else None
    room_match = _ROOM_RE.search(title_text)
    if room_match:
        rooms = int(room_match.group(1))
    area_match = _AREA_RE.search(title_text)
    unit_match = _UNIT_RE.search(title_text)
    corpus_match = re.search(r"(\d+)\s+корпус", corpus_text, re.I)
    section_match = re.search(r"(\d+)\s+секц", corpus_text, re.I)
    floor_match = re.search(r"(\d+)\s+этаж", corpus_text, re.I)
    project_url, project_name = _project_link(root, source_url)
    project_entity = next((entity for entity in entities
                           if entity.get("@type") in {"Apartment", "Product"}), {})
    if not project_name:
        description = str(project_entity.get("description") or "")
        name_match = re.search(r"в ЖК\s+(.+?)\s+от застройщика", description, re.I)
        project_name = name_match.group(1) if name_match else None
    price_node = root.first_class("price-value")
    old_node = root.first_class("price-line-th")
    price_text = _clean(price_node.text()) if price_node else None
    old_price_text = _clean(old_node.text()) if old_node else None
    promo_nodes = root.by_class("flat-promo-item")
    promos = [_clean(node.text()) for node in promo_nodes if _clean(node.text())]
    basis = _price_basis(price_text, promos,
                         bool(price_text and re.search(r"\bот\s+\d", price_text, re.I)))
    conditions = [text for text in promos if _CONDITIONAL_PRICE_RE.search(text)]
    media: list[dict[str, Any]] = []
    for box in root.by_class("flat-plan-image"):
        classes = set(box.attrs.get("class", "").split())
        role: str | None = None
        scope = "unit"
        visual_kind = "unknown"
        label = None
        if "-plan" in classes:
            role, label = "apartment_plan", "Планировка"
        elif "-floor" in classes:
            role, label = "floor_plan", "На этаже"
            scope = "floor"
        elif "-sizes" in classes:
            role, label = "dimensioned_apartment_plan", "С размерами"
        elif "-windowview" in classes:
            role, label = "view", "Вид из окна"
        elif "-plan3d" in classes:
            role, label, visual_kind = "apartment_plan", "3D-план квартиры", "3d"
        if not role:
            continue
        image = next((node for node in box.descendants("img")
                      if node.attrs.get("data-src") or node.attrs.get("src")), None)
        if not image:
            continue
        image_url = _safe_url(image.attrs.get("data-src") or image.attrs.get("src", ""),
                              source_url, _MEDIA_HOSTS)
        asset = _media(image_url, role, scope, f"html:.flat-plan-image.{next(iter(classes - {'flat-plan-image'}), '')}.img[data-src]",
                       label, f"Explicit FSK plan viewer class {sorted(classes)}", visual_kind)
        if asset:
            media.append(asset)
    offer = project_entity.get("offers") if isinstance(project_entity, dict) else None
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    source_fields: dict[str, Any] = {
        "visible_heading": title_text or None,
        "visible_corpus_section_floor": corpus_text or None,
        "visible_price_text": price_text,
        "visible_previous_price_text": old_price_text,
        "visible_promo_texts": promos,
        "project_page_url": project_url,
    }
    if isinstance(offer, dict):
        source_fields["jsonld_offer"] = {
            "price": offer.get("price"),
            "priceCurrency": offer.get("priceCurrency"),
            "availability": offer.get("availability"),
        }
    price_rub = _money(price_text)
    if price_rub is None and isinstance(offer, dict) and offer.get("priceCurrency") == "RUB":
        try:
            structured_price = float(offer.get("price"))
            if structured_price > 0:
                price_rub = round(structured_price)
        except (TypeError, ValueError):
            pass
    address = None
    geo = None
    if project_entity:
        entity_address = project_entity.get("address")
        if isinstance(entity_address, dict) and entity_address.get("@type") == "PostalAddress":
            locality = entity_address.get("addressLocality")
            street = entity_address.get("streetAddress")
            if locality and street:
                address = f"{locality}, {street}"
        entity_geo = project_entity.get("geo")
        if isinstance(entity_geo, dict) and entity_geo.get("@type") == "GeoCoordinates":
            lat, lon = entity_geo.get("latitude"), entity_geo.get("longitude")
            try:
                lat, lon = float(lat), float(lon)
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    geo = {"latitude": lat, "longitude": lon}
            except (TypeError, ValueError):
                pass
    source_fields["address_source"] = "JSON-LD Product/Apartment PostalAddress" if address else None
    source_fields["geo_source"] = "JSON-LD Product/Apartment GeoCoordinates" if geo else None
    return {
        "record_type": "unit",
        "source_url": source_url,
        "observed_at": observed_at,
        "source_updated_at": next((entity.get("dateModified") for entity in entities
                                    if entity.get("dateModified")), None),
        "raw_sha256": raw_sha256,
        "project_name": project_name,
        "address": address,
        "corpus": corpus_match.group(1) if corpus_match else None,
        "section": section_match.group(1) if section_match else None,
        "unit_number": int(unit_match.group(1)) if unit_match else None,
        "floor": int(floor_match.group(1)) if floor_match else None,
        "rooms": rooms,
        "area_m2": _number(area_match.group(1)) if area_match else None,
        "price_rub": price_rub,
        "price_basis": basis,
        "promo_conditions": conditions or None,
        "publisher_project_id": None,
        "publisher_building_id": None,
        "publisher_unit_id": page_match.group(2),
        "media": media,
        "coordinates": geo,
        "rights_status": "unknown",
        "source_fields": source_fields,
    }


def _project_record(root: _Node, source_url: str, observed_at: str,
                    raw_sha256: str, entities: list[dict[str, Any]]) -> dict[str, Any] | None:
    path = urlsplit(source_url).path.rstrip("/")
    if not re.fullmatch(r"/[a-z0-9-]+", path, re.I):
        return None
    heading = next((node for node in root.descendants("h1") if _clean(node.text())), None)
    project_name = _clean(heading.text()) if heading else None
    if not project_name:
        project_name = next((str(entity.get("name")) for entity in entities
                             if entity.get("@type") in {"ApartmentComplex", "Place", "Product"}
                             and entity.get("name")), None)
    project_entity = next((entity for entity in entities
                           if entity.get("@type") in {"ApartmentComplex", "Place"}), {})
    address = None
    source_scope = "unknown"
    address_basis = None
    entity_address = project_entity.get("address") if isinstance(project_entity, dict) else None
    if isinstance(entity_address, dict) and entity_address.get("@type") == "PostalAddress":
        locality = entity_address.get("addressLocality")
        street = entity_address.get("streetAddress")
        if locality and street:
            address = f"{locality}, {street}"
            # "Москва и МО" is a mixed catalog label. The project street
            # address itself must explicitly start with the city name.
            if re.match(r"^(?:г(?:ород)?\.?\s*)?Москва(?:\s*,|$)", str(street), re.I):
                source_scope = "in_scope_moscow"
                address_basis = "project_jsonld_street_address_explicit_moscow"
    if not address:
        # Only extract a value when the page labels it as the project address.
        for node in root.descendants():
            label = _clean(node.text())
            if re.fullmatch(r"(?:Адрес|Адрес проекта|Адрес ЖК)\s*: ?.+", label, re.I):
                address = re.sub(r"^(?:Адрес|Адрес проекта|Адрес ЖК)\s*:\s*", "", label,
                                 flags=re.I)
                break
    media: list[dict[str, Any]] = []
    # Page-level image roles require an explicit project-gallery caption bound
    # to the nearest image container. This keeps generic hero renders unknown.
    for index, image in enumerate(root.descendants("img")):
        label = _clean(image.attrs.get("alt", "") or image.attrs.get("title", ""))
        caption = label
        parent = image.parent
        for _ in range(6):
            if parent is None:
                break
            text = _clean(parent.text())
            lowered_text = text.lower()
            one_image = sum(1 for child in parent.descendants("img")) == 1
            if (one_image and "архитектур" in lowered_text
                    and ("реальное фото" in lowered_text or "фото" in lowered_text
                         or "рендер" in lowered_text or "визуализац" in lowered_text)):
                caption = text[:240]
                break
            parent = parent.parent
        if not caption:
            continue
        role = "unknown"
        visual_kind = "unknown"
        lowered = caption.lower()
        if "архитектур" in lowered and ("фото" in lowered or "реальное" in lowered):
            role, visual_kind = "facade_image", "photo"
        elif "архитектур" in lowered and ("рендер" in lowered or "визуализац" in lowered):
            role, visual_kind = "facade_image", "render"
        elif "генплан" in lowered or "генеральный план" in lowered:
            role = "site_plan"
        source = image.attrs.get("data-src") or image.attrs.get("src")
        image_url = _safe_url(source, source_url, _MEDIA_HOSTS) if source else None
        if role == "unknown":
            continue
        item = _media(image_url, role, "project", f"html:img[{index}].alt/title or gallery caption",
                      caption, "Page-level project gallery caption bound to image", visual_kind)
        if item:
            media.append(item)
    geo = None
    entity_geo = project_entity.get("geo") if isinstance(project_entity, dict) else None
    if isinstance(entity_geo, dict) and entity_geo.get("@type") == "GeoCoordinates":
        try:
            lat, lon = float(entity_geo.get("latitude")), float(entity_geo.get("longitude"))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                geo = {"latitude": lat, "longitude": lon}
        except (TypeError, ValueError):
            pass
    if not project_name and not address and not media and not geo:
        return None
    project_ids = {node.attrs["_id"] for node in root.descendants("div")
                   if "project" in node.attrs.get("class", "").split()
                   and re.fullmatch(r"[a-fA-F0-9]{24}", node.attrs.get("_id", ""))}
    project_id = next(iter(project_ids)) if len(project_ids) == 1 else None
    return {
        "record_type": "project",
        "source_url": source_url,
        "observed_at": observed_at,
        "source_updated_at": next((entity.get("dateModified") for entity in entities
                                    if entity.get("dateModified")), None),
        "raw_sha256": raw_sha256,
        "project_name": project_name,
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
        "publisher_project_id": project_id,
        "publisher_building_id": None,
        "publisher_unit_id": None,
        "coordinates": geo,
        "media": media,
        "rights_status": "unknown",
        "source_fields": {"page_path": path, "jsonld_entity_types": sorted({
            str(entity.get("@type")) for entity in entities if entity.get("@type")}),
            "publisher_project_id_basis": "html:div.project[_id]" if project_id else None,
            "location_scope": source_scope, "location_scope_basis": address_basis},
    }


def parse_fsk_page(html: str, page_url: str, observed_at: str,
                   raw_sha256: str) -> list[dict[str, Any]]:
    """Parse one locally captured public FSK page; this function is offline."""
    current = _page_url(page_url)
    if _api_catalog_url(current):
        try:
            payload = json.loads(html)
        except (json.JSONDecodeError, TypeError):
            return []
        return _parse_fsk_catalog_response(payload, current, observed_at, raw_sha256)
    root = _parse_tree(html)
    canonical = _canonical_url(root, current)
    documents = _jsonld_documents(root)
    entities = _jsonld_entities(documents)
    records: list[dict[str, Any]] = []
    page_kind = _classify_path(canonical)
    if page_kind == "listing":
        detail = _detail_unit(root, canonical, observed_at, raw_sha256, entities)
        if detail:
            records.append(detail)
    elif page_kind == "catalog":
        for node in root.descendants("a"):
            if "card" not in node.attrs.get("class", "").split():
                continue
            record = _card_record(node, canonical, observed_at, raw_sha256, entities)
            if record:
                records.append(record)
        visible_more = None
        for node in root.descendants("button"):
            text = _clean(node.text())
            match = re.search(r"Показать\s+ещ[её]\s+(\d+)\s+из\s+(\d+)", text, re.I)
            if match:
                visible_more = {
                    "button_label": text,
                    "next_chunk_size": int(match.group(1)),
                    # The linked UI template computes this as total minus
                    # already-rendered records, so it is a remaining count,
                    # not the full catalog total.
                    "remaining_count": int(match.group(2)),
                }
                break
        for record in records:
            record["source_fields"]["catalog_visible_unit_card_count"] = len(records)
            if visible_more:
                record["source_fields"]["catalog_more_button"] = visible_more
    elif page_kind == "project":
        project = _project_record(root, canonical, observed_at, raw_sha256, entities)
        if project:
            records.append(project)
    return records
