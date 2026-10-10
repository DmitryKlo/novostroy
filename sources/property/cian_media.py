"""Classify explicitly labelled CIAN media references from authorised input.

This is a local metadata parser, not a page collector or image downloader. The
caller supplies one already-associated URL and its own labels per media item.
Unbound page-wide labels never become image evidence, and every result remains
private regardless of the reported rights state.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Collection, Literal, Mapping, TypedDict
from urllib.parse import parse_qsl, urlsplit


ROLES = frozenset({"apartment_plan", "floor_plan", "facade_image", "photo", "unknown"})
VISUAL_KINDS = frozenset({"render", "photo", "unknown"})
RIGHTS_STATUSES = frozenset({"cleared", "restricted", "unknown", "demo"})
Role = Literal["apartment_plan", "floor_plan", "facade_image", "photo", "unknown"]
VisualKind = Literal["render", "photo", "unknown"]
RightsStatus = Literal["cleared", "restricted", "unknown", "demo"]
_SECRET_QUERY_KEYS = frozenset({"token", "api_key", "apikey", "key", "password", "secret", "auth"})
_FLOOR_PLAN = re.compile(r"(?:\bплан\s+этажа\b|\bпоэтажн\w*\s+план\b|\bfloor\s+plan\b)", re.I)
_APARTMENT_PLAN = re.compile(
    r"(?:\bпланировк\w*\s+квартир\w*\b|\bплан\s+квартир\w*\b|\bapartment\s+(?:floor\s+)?plan\b|\bunit\s+layout\b)", re.I
)
_BARE_LAYOUT = re.compile(r"^\s*планировка\s*$", re.I)
_FACADE = re.compile(r"(?:\bфасад\w*\b|\bfacade\b|\bfaçade\b)", re.I)
_RENDER = re.compile(r"(?:\bрендер\w*\b|\bвизуализац\w*\b|\brender\w*\b|\bcgi\b)", re.I)
_PHOTO = re.compile(r"(?:\bфото\w*\b|\bфотограф\w*\b|\bснимок\b|\bphoto\w*\b)", re.I)


class MediaCandidate(TypedDict):
    media_ref_id: str
    role: Role
    visual_kind: VisualKind
    source_url: str
    listing_url: str
    listing_id: str | None
    corpus_id: str | None
    source_field: str | None
    caption: str | None
    section_label: str | None
    type_label: str | None
    evidence_label: str | None
    classification_basis: str
    observed_at: str
    publisher_updated_at: str | None
    publisher: str | None
    source_rights_status: RightsStatus
    rights_status: RightsStatus
    approved_media_host: bool
    fetch_status: str
    public_display_allowed: bool


def _timestamp(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _short_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")
    result = value.strip()
    if not result:
        return None
    if len(result) > 256:
        raise ValueError(f"{label} exceeds 256 characters")
    return result


def _public_hostname(host: str) -> bool:
    labels = host.split(".")
    if (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host)
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in labels)
            or not re.fullmatch(r"[a-z]{2,63}", labels[-1])):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _safe_https_url(value: Any, *, label: str, cian_page: bool = False) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{label} must be a bounded absolute HTTPS URL")
    original = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in original):
        raise ValueError(f"{label} contains control characters")
    parsed = urlsplit(original)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an absolute credential-free HTTPS URL")
    if parsed.port not in {None, 443}:
        raise ValueError(f"{label} must use the standard HTTPS port")
    if any(key.casefold() in _SECRET_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
        raise ValueError(f"{label} contains a credential-like query parameter")
    if not _public_hostname(host):
        raise ValueError(f"{label} must not target a local host")
    if cian_page and host != "cian.ru" and not host.endswith(".cian.ru"):
        raise ValueError("listing_url must be on cian.ru")
    return original, host


def _classify(item: Mapping[str, Any], page_kind: str | None) -> tuple[str, str, str, str | None]:
    labels = [(field, _short_text(item.get(field), label=field))
              for field in ("caption", "section_label", "type_label")]
    source_field = _short_text(item.get("source_field"), label="source_field")
    specific: set[str] = set()
    evidence: list[tuple[str, str]] = []
    for field, label in labels:
        if label is None:
            continue
        apartment_spans = [match.span() for match in _APARTMENT_PLAN.finditer(label)]
        floor_spans = [match.span() for match in _FLOOR_PLAN.finditer(label)]
        # "Apartment floor plan" names a unit plan. Its embedded "floor plan"
        # is not independent evidence of a whole-storey plan.
        independent_floor = any(not any(start <= floor_start and floor_end <= end
                                        for start, end in apartment_spans)
                                for floor_start, floor_end in floor_spans)
        if independent_floor:
            specific.add("floor_plan")
            evidence.append((field, label))
        if apartment_spans or (page_kind == "unit_listing" and _BARE_LAYOUT.fullmatch(label)):
            specific.add("apartment_plan")
            evidence.append((field, label))
        if _FACADE.search(label):
            specific.add("facade_image")
            evidence.append((field, label))
    # LayoutPhoto is documented for the publisher XML import, not an inferred
    # key in CIAN's public pages. A rights-cleared caller may supply this exact
    # source path when reading an authorised feed.
    if source_field is not None and "LayoutPhoto" in source_field.split("/"):
        specific.add("apartment_plan")
        evidence.append(("source_field", source_field))
    if len(specific) > 1:
        return "unknown", "unknown", "conflicting_labels", None
    if specific:
        role = next(iter(specific))
        basis, label = evidence[0]
    else:
        photo_evidence = next(((field, label) for field, label in labels
                               if label is not None and _PHOTO.search(label)), None)
        if photo_evidence is None and source_field is not None and "Photos" in source_field.split("/"):
            photo_evidence = ("source_field", source_field)
        if photo_evidence is None:
            return "unknown", "unknown", "none", None
        role = "photo"
        basis, label = photo_evidence
    if role in {"apartment_plan", "floor_plan"}:
        return role, "unknown", basis, label
    text = " ".join(label for _, label in labels if label)
    render = bool(_RENDER.search(text))
    photo = bool(_PHOTO.search(text))
    visual_kind = "unknown" if render == photo else ("render" if render else "photo")
    return role, visual_kind, basis, label


class _ExplicitImageParser(HTMLParser):
    """Read only an image tag's own URL and alt/title, never nearby page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "img":
            return
        values = {key.casefold(): value for key, value in attrs if value is not None}
        url = values.get("src")
        if url:
            index = len(self.items)
            self.items.append({"url": url, "caption": values.get("alt", ""),
                               "type_label": values.get("title", ""),
                               "source_field": f"html:img[{index}]"})


def parse_cian_media(
    payload: Mapping[str, Any] | str,
    *,
    source_url: str,
    observed_at: str,
    rights_status: str,
    allowed_media_hosts: Collection[str] = (),
) -> list[MediaCandidate]:
    """Return private media metadata from explicit item-level evidence.

    Mapping input must contain a collector-normalised ``media_items`` list;
    arbitrary nested source keys and page-wide labels are deliberately ignored.
    HTML input is limited to rights-cleared local fragments with an ``img`` tag's
    own ``src`` and ``alt``/``title``. No URL is fetched or published here.
    ``allowed_media_hosts`` is an exact-host approval list for a *future*,
    separate downloader; even approved hosts are not requested by this parser.
    """
    if rights_status not in RIGHTS_STATUSES:
        raise ValueError("rights_status must be cleared, restricted, unknown, or demo")
    listing_url, _ = _safe_https_url(source_url, label="source_url", cian_page=True)
    checked_at = _timestamp(observed_at, label="observed_at")
    approved_hosts = {host.casefold().rstrip(".") for host in allowed_media_hosts}
    if any(not _public_hostname(host) for host in approved_hosts):
        raise ValueError("allowed_media_hosts must contain exact public hostnames")
    if isinstance(payload, str):
        if rights_status not in {"cleared", "demo"}:
            raise ValueError("HTML parsing requires explicit cleared rights or a synthetic fixture")
        parser = _ExplicitImageParser()
        parser.feed(payload)
        media_items: Any = parser.items
        page_kind = None
        listing_id = corpus_id = None
    elif isinstance(payload, Mapping):
        media_items = payload.get("media_items", [])
        page_kind = _short_text(payload.get("page_kind"), label="page_kind")
        listing_id = _short_text(payload.get("listing_id"), label="listing_id")
        corpus_id = _short_text(payload.get("corpus_id"), label="corpus_id")
    else:
        raise ValueError("payload must be a media mapping or local HTML text")
    if not isinstance(media_items, list) or len(media_items) > 1000:
        raise ValueError("media_items must be a list of at most 1000 entries")
    output: list[MediaCandidate] = []
    for index, item in enumerate(media_items):
        if not isinstance(item, Mapping):
            raise ValueError(f"media_items[{index}] must be an object")
        image_url, media_host = _safe_https_url(item.get("url"), label=f"media_items[{index}].url")
        item_rights = item.get("rights_status", "demo" if rights_status == "demo" else "unknown")
        if item_rights not in RIGHTS_STATUSES or (rights_status != "cleared" and item_rights == "cleared"):
            raise ValueError(f"media_items[{index}].rights_status is unsupported by source rights")
        role, visual_kind, basis, evidence_label = _classify(item, page_kind)
        source_field = _short_text(item.get("source_field"), label="source_field")
        if source_field is None:
            role, visual_kind, basis, evidence_label = "unknown", "unknown", "missing_source_field", None
        item_listing_id = _short_text(item.get("listing_id"), label="listing_id") or listing_id
        item_corpus_id = _short_text(item.get("corpus_id"), label="corpus_id") or corpus_id
        publisher_updated = item.get("publisher_updated_at")
        updated_at = (_timestamp(publisher_updated, label="publisher_updated_at")
                      if publisher_updated is not None else None)
        reference = hashlib.sha256(
            f"{listing_url}\0{image_url}\0{source_field or ''}".encode("utf-8")
        ).hexdigest()[:24]
        output.append({
            "media_ref_id": f"cian_media_{reference}",
            "role": role, "visual_kind": visual_kind,
            "source_url": image_url, "listing_url": listing_url,
            "listing_id": item_listing_id, "corpus_id": item_corpus_id,
            "source_field": source_field,
            "caption": _short_text(item.get("caption"), label="caption"),
            "section_label": _short_text(item.get("section_label"), label="section_label"),
            "type_label": _short_text(item.get("type_label"), label="type_label"),
            "evidence_label": evidence_label, "classification_basis": basis,
            "observed_at": checked_at, "publisher_updated_at": updated_at,
            "publisher": _short_text(item.get("publisher"), label="publisher"),
            "source_rights_status": rights_status, "rights_status": item_rights,
            "approved_media_host": media_host in approved_hosts,
            "fetch_status": "not_requested", "public_display_allowed": False,
        })
    return output
