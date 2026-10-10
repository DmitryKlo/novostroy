"""Rights-gated CIAN listing observations for private review, never a live offer feed.

The public-observation input contains a small set of already reviewed factual
page observations. Saved HTML and automated capture require a separate access
manifest. Nothing here changes the public catalogue or downloads media bytes.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import socket
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_ROOT = ROOT / "data/property/cian/review-v1"
SCHEMA_VERSION = "cian-review/1"
PUBLIC_OBSERVATION_SCHEMA = "cian-public-page-observation-v1"
AUTHORIZED_EXPORT_SCHEMA = "cian-authorized-export-v1"
LISTING_URL_RE = re.compile(r"https://www\.cian\.ru/sale/flat/([0-9]{6,12})/?\Z")
MAX_INPUT_BYTES = 5_000_000
MAX_HTML_BYTES = 2_000_000
MAX_ROWS = 200
MAX_FETCH_URLS = 20
MAX_TOTAL_FETCH_SECONDS = 60
MIN_FETCH_DELAY_SECONDS = 2.0


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_bounded(path: Path, maximum: int = MAX_INPUT_BYTES) -> bytes:
    if path.stat().st_size > maximum:
        raise ValueError("CIAN input exceeds size limit")
    return path.read_bytes()


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} needs a source capture timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} needs a timezone")
    if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError(f"{field} cannot be a future capture")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _listing_identity(url: Any, listing_id: Any = None) -> tuple[str, str]:
    if not isinstance(url, str):
        raise ValueError("CIAN listing needs its source URL")
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("CIAN listing URL must not carry query, fragment, or credentials")
    match = LISTING_URL_RE.fullmatch(url)
    if match is None or (listing_id is not None and str(listing_id) != match.group(1)):
        raise ValueError("CIAN listing ID and canonical URL disagree")
    return match.group(1), f"https://www.cian.ru/sale/flat/{match.group(1)}/"


def _optional_text(value: Any, *, maximum: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        return None
    return text


def _source_updated(value: Any, observed_at: str) -> str | None:
    if value is None:
        return None
    updated = _timestamp(value, "source_updated_at")
    if updated > observed_at:
        raise ValueError("source_updated_at cannot follow observed_at")
    return updated


def _integer(value: Any, *, minimum: int = 0, maximum: int = 10**12) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.replace("\u00a0", "").replace(" ", "")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and value != number:
        return None
    return number if minimum <= number <= maximum else None


def _area(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except (ValueError, TypeError, OverflowError):
        return None
    return number if 0 < number < 100_000 and number == number and number != float("inf") else None


def _market(row: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    evidence: list[str] = []
    new_build = row.get("new_build_label") is True
    if new_build:
        evidence.append("source_newbuild_label")
    deal = (_optional_text(row.get("deal_type_text")) or "").casefold()
    advertiser = (_optional_text(row.get("advertiser_category_text")) or "").casefold()
    details = row.get("advertiser_category_evidence")
    if isinstance(details, list):
        advertiser += " " + " ".join(str(value).casefold() for value in details[:5])
    if "переуступ" in deal:
        evidence.append("source_assignment_deal")
        return ("newbuild" if new_build else "unknown", "assignment", evidence)
    if ("долев" in deal or "214-фз" in deal) and "застройщик" in advertiser:
        evidence.extend(("source_ddu_deal", "source_developer_advertiser"))
        return ("newbuild" if new_build else "unknown", "developer_primary", evidence)
    if row.get("market_kind") == "resale" and row.get("market_channel") == "resale":
        evidence.append("explicit_source_resale")
        return "resale", "resale", evidence
    return ("newbuild" if new_build else "unknown", "unknown", evidence)


def _price(row: Mapping[str, Any], observed_at: str) -> tuple[str, dict[str, Any] | None,
                                                               int | None, list[str]]:
    notes: list[str] = []
    displayed = row.get("displayed_price_basis")
    if not isinstance(displayed, Mapping):
        return "unknown", None, _integer(row.get("price_per_m2_rub")), ["price_basis_unverified"]
    raw_total = _integer(displayed.get("amount_rub"), minimum=1)
    mirrored = _integer(row.get("price_rub"), minimum=1)
    if raw_total is not None and mirrored is not None and raw_total != mirrored:
        return "unknown", None, None, ["price_fields_conflict"]
    amount = raw_total or mirrored
    per_sqm = _integer(displayed.get("price_per_m2_rub"))
    if per_sqm is None:
        per_sqm = _integer(row.get("price_per_m2_rub"))
    if displayed.get("price_marked_as_from") is True:
        return "project_from", None, per_sqm, ["project_from_not_unit_asking"]
    conditions = (displayed.get("unit_specific_discount_or_financing_condition_visible"),
                  displayed.get("unit_specific_discount_or_credit_condition_visible"))
    if True in conditions:
        return "conditional", None, per_sqm, ["unit_price_condition_visible"]
    if amount is None or displayed.get("price_marked_as_from") is not False or False not in conditions:
        return "unknown", None, per_sqm, ["price_basis_unverified"]
    if row.get("page_kind") != "unit_listing":
        return "unknown", None, per_sqm, ["project_price_not_unit_offer"]
    return "unit_asking", {"amount_rub": amount, "basis": "asking_total",
                            "observed_at": observed_at}, per_sqm, notes


def _private_price_basis(row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep bounded source price assertions even when no unit asking is eligible."""
    displayed = row.get("displayed_price_basis")
    if not isinstance(displayed, Mapping):
        displayed = {}
    total = _integer(displayed.get("amount_rub"), minimum=1)
    per_sqm = _integer(displayed.get("price_per_m2_rub"), minimum=1)
    source_total = _integer(row.get("price_rub"), minimum=1)
    source_per_sqm = _integer(row.get("price_per_m2_rub"), minimum=1)
    return {
        "displayed_total_rub": total,
        "source_total_rub": source_total,
        "displayed_per_sqm_rub": per_sqm,
        "source_per_sqm_rub": source_per_sqm,
        "currency": "RUB" if any(value is not None for value in (
            total, per_sqm, source_total, source_per_sqm)) else None,
        "unit": "listing_total" if total is not None or source_total is not None else (
            "per_square_metre_only" if per_sqm is not None or source_per_sqm is not None
            else "unknown"),
        "price_marked_as_from": displayed.get("price_marked_as_from")
        if isinstance(displayed.get("price_marked_as_from"), bool) else None,
        "unit_specific_discount_or_financing_condition_visible":
            displayed.get("unit_specific_discount_or_financing_condition_visible")
            if isinstance(displayed.get("unit_specific_discount_or_financing_condition_visible"), bool)
            else None,
        "unit_specific_discount_or_credit_condition_visible":
            displayed.get("unit_specific_discount_or_credit_condition_visible")
            if isinstance(displayed.get("unit_specific_discount_or_credit_condition_visible"), bool)
            else None,
    }


def _media(row: Mapping[str, Any], source_url: str, observed_at: str,
           rights_status: str) -> list[dict[str, Any]]:
    from sources.property.cian_media import parse_cian_media

    return parse_cian_media({"page_kind": row.get("page_kind"),
                             "listing_id": row.get("listing_id"),
                             "media_items": row.get("media_items", [])},
                            source_url=source_url, observed_at=observed_at,
                            rights_status=rights_status)


def _normalize_listing(row: Mapping[str, Any], *, payload_kind: str,
                       observed_at: str, input_sha256: str,
                       rights_status: str) -> dict[str, Any]:
    listing_id, source_url = _listing_identity(row.get("source_url"), row.get("listing_id"))
    market_kind, market_channel, market_evidence = _market(row)
    price_kind, price, stated_per_sqm, price_notes = _price(row, observed_at)
    complex_label = _optional_text(row.get("complex_name"))
    reasons = ["no_explicit_complex_id", "no_explicit_unit_id",
               "availability_unverified", *price_notes]
    if rights_status != "cleared":
        reasons.append("rights_not_cleared")
    if market_channel == "unknown":
        reasons.append("market_channel_unverified")
    media = _media(row, source_url, observed_at, rights_status)
    if not media:
        reasons.append("media_url_or_rights_unverified")
    labels = row.get("visible_media_labels")
    if labels is None:
        labels = []
    if not isinstance(labels, list):
        raise ValueError("visible_media_labels must be a list")
    return {
        "schema_version": SCHEMA_VERSION,
        "payload_kind": payload_kind,
        "record_type": "listing_observation", "data_mode": "real",
        "listing_id": listing_id, "source_url": source_url,
        "observed_at": observed_at,
        "source_updated_at": _source_updated(row.get("source_updated_at"), observed_at),
        "market_kind": market_kind, "market_channel": market_channel,
        "market_evidence": market_evidence,
        "complex_label": complex_label, "complex_id": None, "unit_id": None,
        "location": None,
        "price_kind": price_kind, "price": price,
        "stated_price_per_sqm_rub": stated_per_sqm,
        "rooms": _integer(row.get("rooms"), maximum=30),
        "area_sqm": _area(row.get("area_m2")),
        "living_area_sqm": _area(row.get("living_area_m2")),
        "kitchen_area_sqm": _area(row.get("kitchen_area_m2")),
        "floor": _integer(row.get("floor"), maximum=500),
        "building_floors": _integer(row.get("building_floors"), maximum=500),
        "handover_year": _integer(row.get("handover_year"), minimum=1900, maximum=2100),
        "availability": "unknown",
        "media": media, "media_file_count": 0,
        "media_labels_observed": [label for value in labels[:20]
                                  if (label := _optional_text(value, maximum=80))],
        "rights_status": rights_status,
        "review_reasons": sorted(set(reasons)),
        "source_capture_sha256": input_sha256,
        "source_fields": {
            **{key: row.get(key) for key in (
                "new_build_label", "deal_type_text", "advertiser_category_text") if key in row},
            "displayed_price_basis": _private_price_basis(row),
        },
    }


def parse_public_observations(raw: bytes | Path | Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Normalize factual, already captured page observations for private review."""
    if isinstance(raw, Path):
        body = _read_bounded(raw)
        payload = json.loads(body)
    elif isinstance(raw, bytes):
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("CIAN input exceeds size limit")
        body = raw
        payload = json.loads(body)
    elif isinstance(raw, Mapping):
        payload = dict(raw)
        body = _json_bytes(payload)
    else:
        raise TypeError("CIAN input must be JSON bytes, Path, or object")
    if not isinstance(payload, dict) or payload.get("schema_version") != PUBLIC_OBSERVATION_SCHEMA:
        raise ValueError("Unsupported public observation schema")
    observed_at = _timestamp(payload.get("observed_at"), "observed_at")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not 1 <= len(observations) <= MAX_ROWS:
        raise ValueError("Public observations must be a bounded nonempty list")
    input_sha = _sha(body)
    rows: list[dict[str, Any]] = []
    skipped = Counter()
    seen_ids: set[str] = set()
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("Public observation item must be an object")
        if item.get("page_kind") != "unit_listing":
            skipped[str(item.get("page_kind", "unknown"))] += 1
            continue
        record = _normalize_listing(item, payload_kind="public_observation",
                                    observed_at=observed_at, input_sha256=input_sha,
                                    rights_status="unknown")
        if record["listing_id"] in seen_ids:
            raise ValueError("Duplicate listing ID in one capture")
        seen_ids.add(record["listing_id"])
        rows.append(record)
    return rows, dict(skipped)


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []
        self.canonical_urls: list[str] = []
        self.metadata: dict[str, list[str]] = {}
        self.jsonld_scripts: list[str] = []
        self._jsonld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonical_urls.append(values.get("href", ""))
        if tag == "meta" and values.get("content"):
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"product:price:amount", "product:price:currency"}:
                self.metadata.setdefault(key, []).append(values["content"])
        if tag == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._jsonld_parts = []
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._jsonld_parts is not None:
            script = "".join(self._jsonld_parts)
            if len(script) <= 200_000 and len(self.jsonld_scripts) < 10:
                self.jsonld_scripts.append(script)
            self._jsonld_parts = None
        if tag in {"script", "style", "template", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if self._jsonld_parts is not None:
            self._jsonld_parts.append(data)
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


def _rub_total(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return _integer(value, minimum=1)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}(?:\.00?)?", value):
        return _integer(value.split(".", 1)[0], minimum=1)
    return None


def _linked_listing_url(value: Any, listing_url: str) -> bool:
    try:
        return _listing_identity(value)[1] == listing_url
    except ValueError:
        return False


def _explicit_html_asking(parser: _VisibleHTML, listing_url: str) -> int | None:
    """Read only a RUB Offer explicitly bound to this listing, or its canonical metadata."""
    jsonld_amounts: set[int] = set()
    for script in parser.jsonld_scripts:
        try:
            payload = json.loads(script)
        except json.JSONDecodeError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                nodes.extend(node["@graph"][:200])
            if not isinstance(node, dict):
                continue
            linked = _linked_listing_url(node.get("url"), listing_url)
            offers = node.get("offers") if linked else None
            offer_nodes = offers if isinstance(offers, list) else [offers]
            if linked and str(node.get("@type", "")).casefold() == "offer":
                offer_nodes.append(node)
            for offer in offer_nodes:
                if not isinstance(offer, dict):
                    continue
                if (not linked and not _linked_listing_url(offer.get("url"), listing_url)):
                    continue
                if (offer.get("priceCurrency") != "RUB" or "lowPrice" in offer
                        or "highPrice" in offer or "priceSpecification" in offer):
                    continue
                amount = _rub_total(offer.get("price"))
                if amount is not None:
                    jsonld_amounts.add(amount)
    meta_amount: int | None = None
    if parser.canonical_urls and all(_linked_listing_url(url, listing_url)
                                     for url in parser.canonical_urls):
        meta_amounts = parser.metadata.get("product:price:amount", [])
        currencies = parser.metadata.get("product:price:currency", [])
        if len(meta_amounts) == len(currencies) == 1 and currencies[0] == "RUB":
            meta_amount = _rub_total(meta_amounts[0])
    amounts = jsonld_amounts | ({meta_amount} if meta_amount is not None else set())
    if len(amounts) != 1:
        return None
    # A unit-level "from" or financing qualifier contradicts an ordinary
    # asking-total interpretation. Unrelated project promotions are not used.
    visible = " ".join(parser.parts).casefold()
    if re.search(r"\bцена\s+(?:квартиры\s+)?от\b|\bцена\s+при\s+(?:ипотеке|скидке)", visible):
        return None
    if not jsonld_amounts and re.search(r"\bцена\s+за\s+(?:м[²2]|кв\.?\s*м)", visible):
        if not re.search(r"\b(?:цена|стоимость)\s+квартиры\b", visible):
            return None
    return next(iter(amounts))


def _adjacent_html_field(parts: list[str], label: str) -> str | None:
    for index, part in enumerate(parts[:-1]):
        if part.casefold().rstrip(":") == label.casefold():
            return _optional_text(parts[index + 1], maximum=100)
    return None


def parse_authorized_html(path: Path, *, source_url: str, observed_at: str,
                          rights: Mapping[str, Any]) -> dict[str, Any]:
    """Conservative fallback for a rights-supplied saved page; no hidden keys."""
    _validate_access_manifest(rights, require_automated=False)
    body = _read_bounded(path, MAX_HTML_BYTES)
    listing_id, source_url = _listing_identity(source_url)
    parser = _VisibleHTML()
    parser.feed(body.decode("utf-8", errors="replace"))
    visible = " | ".join(parser.parts)
    if any(token in visible.casefold() for token in ("captcha", "подтвердите, что вы не робот")):
        raise ValueError("Access challenge is not a listing capture")
    rooms_match = re.search(r"\b([0-9]{1,2})\s*[- ]?комн", visible, re.IGNORECASE)
    area_match = re.search(r"Общая площадь\s*[|:]?\s*([0-9]+(?:[.,][0-9]+)?)\s*м[²2]", visible, re.IGNORECASE)
    floor_match = re.search(r"Этаж\s*[|:]?\s*([0-9]{1,3})\s*из\s*([0-9]{1,3})", visible, re.IGNORECASE)
    amount = _explicit_html_asking(parser, source_url)
    deal = _adjacent_html_field(parser.parts, "Условия сделки")
    advertiser = (_adjacent_html_field(parser.parts, "Категория продавца")
                  or _adjacent_html_field(parser.parts, "Тип продавца")
                  or _adjacent_html_field(parser.parts, "Продавец"))
    row = {"page_kind": "unit_listing", "listing_id": listing_id,
           "source_url": source_url,
           "new_build_label": _adjacent_html_field(parser.parts, "Тип жилья") == "Новостройка",
           "deal_type_text": deal, "advertiser_category_text": advertiser,
           "displayed_price_basis": ({"amount_rub": amount,
                                      "price_marked_as_from": False,
                                      "unit_specific_discount_or_financing_condition_visible": False}
                                     if amount is not None else None),
           "rooms": rooms_match.group(1) if rooms_match else None,
           "area_m2": area_match.group(1) if area_match else None,
           "floor": floor_match.group(1) if floor_match else None,
           "building_floors": floor_match.group(2) if floor_match else None}
    checked_at = _timestamp(observed_at, "observed_at")
    rights_status = str(rights.get("rights_status", "restricted"))
    record = _normalize_listing(row, payload_kind="authorized_html",
                                observed_at=checked_at, input_sha256=_sha(body),
                                rights_status=rights_status)
    from sources.property.cian_media import parse_cian_media
    record["media"] = (parse_cian_media(body.decode("utf-8", errors="replace"),
                                        source_url=source_url, observed_at=checked_at,
                                        rights_status=rights_status)
                       if rights_status == "cleared" else [])
    if record["media"]:
        record["review_reasons"] = [reason for reason in record["review_reasons"]
                                    if reason != "media_url_or_rights_unverified"]
    return record


def _validate_access_manifest(rights: Mapping[str, Any], *, require_automated: bool) -> None:
    if (not isinstance(rights, Mapping) or rights.get("source") != "cian"
            or rights.get("local_processing_authorized") is not True
            or rights.get("permitted_storage") is not True
            or not _optional_text(rights.get("agreement_reference"))):
        raise ValueError("CIAN access manifest lacks explicit processing/storage basis")
    if rights.get("rights_status") not in {"cleared", "restricted", "unknown"}:
        raise ValueError("CIAN rights_status must be explicit")
    if require_automated and rights.get("automated_access_authorized") is not True:
        raise ValueError("CIAN automated access is not authorized by this manifest")
    expires = rights.get("expires_at")
    if require_automated:
        if not isinstance(expires, str):
            raise ValueError("CIAN automated access authorization needs an expiry")
        try:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("CIAN automated access expiry must be ISO-8601") from exc
        if expiry.tzinfo is None or expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("CIAN automated access authorization is expired")


def _fingerprint(row: Mapping[str, Any]) -> str:
    facts = {key: value for key, value in row.items()
             if key not in {"review_reasons", "source_capture_sha256"}}
    return _sha(_json_bytes(facts))


def _atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _report(rows: list[dict[str, Any]], skipped: Mapping[str, int], duplicate_count: int) -> str:
    captures = Counter(row["payload_kind"] for row in rows)
    kinds = Counter(row["market_kind"] for row in rows)
    markets = Counter(row["market_channel"] for row in rows)
    prices = Counter(row["price_kind"] for row in rows)
    reasons = Counter(reason for row in rows for reason in row["review_reasons"])
    lines = ["# CIAN private observation import", "",
             f"New listing observations: {len(rows)}", f"Already imported: {duplicate_count}",
             f"Non-unit pages skipped: {sum(skipped.values())}",
             "Live automated fetches in this import: 0", "Media files downloaded: 0", "",
             "These are dated source observations, not verified current inventory or public offers.",
             "No project or unit identity was inferred from a name, URL proximity, or price range.", "",
             "## Source capture kinds", ""]
    lines.extend(f"- {key}: {value}" for key, value in sorted(captures.items()))
    lines.extend(("", "## Skipped non-unit page kinds", ""))
    lines.extend(f"- {key}: {value}" for key, value in sorted(skipped.items()))
    lines.extend(("", "## Market kinds", ""))
    lines.extend(f"- {key}: {value}" for key, value in sorted(kinds.items()))
    lines.extend(("", "## Market channels", ""))
    lines.extend(f"- {key}: {value}" for key, value in sorted(markets.items()))
    lines.extend(("", "## Price kinds", ""))
    lines.extend(f"- {key}: {value}" for key, value in sorted(prices.items()))
    lines.extend(("", "## Review reasons", ""))
    lines.extend(f"- {key}: {value}" for key, value in sorted(reasons.items()))
    return "\n".join(lines) + "\n"


def write_review_run(rows: list[dict[str, Any]], *, input_sha256: str,
                     output_root: Path = DEFAULT_REVIEW_ROOT,
                     skipped: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Append an immutable, deduplicated review run and update a tiny index."""
    if not rows:
        raise ValueError("No listing observations to import")
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = output_root / "index.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = output_root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {
            "schema_version": "cian-review-index/1", "runs": [], "observations": []}
        if index.get("schema_version") != "cian-review-index/1":
            raise ValueError("Unsupported CIAN review index")
        existing = {(entry["listing_id"], entry["observed_at"], entry["fingerprint"])
                    for entry in index["observations"]}
        same_capture = {(entry["listing_id"], entry["observed_at"]): entry["fingerprint"]
                        for entry in index["observations"]}
        fresh: list[dict[str, Any]] = []
        fingerprints: list[str] = []
        duplicates = 0
        for row in rows:
            fingerprint = _fingerprint(row)
            key = (row["listing_id"], row["observed_at"], fingerprint)
            if key in existing:
                duplicates += 1
                continue
            if same_capture.get(key[:2]) not in {None, fingerprint}:
                row = {**row, "review_reasons": sorted(set(row["review_reasons"]
                                                            + ["same_capture_conflict"]))}
            existing.add(key)
            same_capture[key[:2]] = fingerprint
            fresh.append(row)
            fingerprints.append(fingerprint)
        if not fresh:
            return {"status": "already_imported", "new_records": 0,
                    "duplicate_records": duplicates, "runs_total": len(index["runs"])}
        body = b"".join(_json_bytes(row) for row in fresh)
        run_id = "cian-review-" + _sha(body)[:16]
        runs_root = output_root / "runs"
        runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = runs_root / run_id
        if run_dir.exists():
            raise ValueError("CIAN review run path already exists outside index")
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}-", dir=runs_root))
        os.chmod(temporary_dir, 0o700)
        _atomic_write(temporary_dir / "observations.jsonl", body)
        manifest = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "input_sha256": input_sha256, "observations_sha256": _sha(body),
            "records_total": len(fresh), "duplicate_records": duplicates,
            "non_unit_pages_skipped": dict(skipped or {}),
            "capture_kinds": dict(Counter(row["payload_kind"] for row in fresh)),
            "market_kinds": dict(Counter(row["market_kind"] for row in fresh)),
            "market_channels": dict(Counter(row["market_channel"] for row in fresh)),
            "price_kinds": dict(Counter(row["price_kind"] for row in fresh)),
            "rights_statuses": dict(Counter(row["rights_status"] for row in fresh)),
            "media_descriptors": sum(len(row["media"]) for row in fresh),
            "media_statuses": dict(Counter(media["fetch_status"] for row in fresh
                                           for media in row["media"])),
            "review_reasons": dict(Counter(reason for row in fresh
                                           for reason in row["review_reasons"])),
            "media_files_downloaded": 0, "live_fetches": 0,
            "public_offer_imported": False,
        }
        _atomic_write(temporary_dir / "manifest.json", _json_bytes(manifest))
        csv_path = temporary_dir / "observations.csv"
        with csv_path.open("x", newline="", encoding="utf-8") as stream:
            os.chmod(csv_path, 0o600)
            columns = [
                "listing_id", "source_url", "observed_at", "complex_label",
                "market_kind", "market_channel", "price_kind", "asking_total_rub",
                "rooms", "area_sqm", "floor", "building_floors", "rights_status",
                "media_descriptors", "media_files_downloaded", "review_reasons"]
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in fresh:
                values = {key: row.get(key) for key in columns}
                values["asking_total_rub"] = (row["price"] or {}).get("amount_rub")
                values["media_descriptors"] = len(row["media"])
                values["media_files_downloaded"] = row["media_file_count"]
                values["review_reasons"] = ",".join(row["review_reasons"])
                writer.writerow(values)
        _atomic_write(temporary_dir / "report.md", _report(fresh, skipped or {}, duplicates).encode("utf-8"))
        os.replace(temporary_dir, run_dir)
        index["runs"].append(run_id)
        index["observations"].extend({
            "listing_id": row["listing_id"], "observed_at": row["observed_at"],
            "fingerprint": fingerprint, "run_id": run_id,
        } for row, fingerprint in zip(fresh, fingerprints))
        _atomic_write(index_path, _json_bytes(index))
        return {"status": "written", "run_id": run_id, "new_records": len(fresh),
                "duplicate_records": duplicates, "runs_total": len(index["runs"]),
                "path": str(run_dir)}


class _SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname != "www.cian.ru"
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ValueError("CIAN redirect left the authorized host")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def collect_authorized_pages(urls: list[str], *, rights: Mapping[str, Any],
                             output_root: Path, timeout_seconds: float = 5.0,
                             delay_seconds: float = MIN_FETCH_DELAY_SECONDS) -> dict[str, Any]:
    """Explicit-URL capture only when a separate access agreement is supplied."""
    _validate_access_manifest(rights, require_automated=True)
    if (not 1 <= len(urls) <= MAX_FETCH_URLS or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 10):
        raise ValueError("CIAN capture exceeds request/time bounds")
    if (not math.isfinite(delay_seconds)
            or not MIN_FETCH_DELAY_SECONDS <= delay_seconds <= 10):
        raise ValueError("CIAN capture delay must be finite and bounded")
    identities = [_listing_identity(url) for url in urls]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate CIAN listing URL")
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    opener = build_opener(_SameHostRedirect())
    checkpoint_path = output_root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {
        "schema_version": "cian-capture-checkpoint/1", "pages": {}}
    if checkpoint.get("schema_version") != "cian-capture-checkpoint/1":
        raise ValueError("Unsupported CIAN capture checkpoint")
    started = time.monotonic()
    captured = 0
    for index, (listing_id, url) in enumerate(identities):
        if listing_id in checkpoint["pages"]:
            continue
        waiting = delay_seconds if index else 0
        if time.monotonic() - started + waiting + timeout_seconds > MAX_TOTAL_FETCH_SECONDS:
            break
        if index:
            time.sleep(delay_seconds)
        request = Request(url, headers={"User-Agent": "NovostroyAuthorizedPropertyImport/1.0",
                                        "Accept": "text/html"})
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                if response.status != 200 or "text/html" not in response.headers.get("Content-Type", ""):
                    raise ValueError("CIAN did not return a listing HTML document")
                body = response.read(MAX_HTML_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
            checkpoint["pages"][listing_id] = {"status": "access_unavailable",
                                                 "captured_at": datetime.now(timezone.utc).isoformat()}
            _atomic_write(checkpoint_path, _json_bytes(checkpoint))
            break  # No retries or anti-bot/captcha handling.
        if len(body) > MAX_HTML_BYTES or b"captcha" in body.lower():
            checkpoint["pages"][listing_id] = {"status": "challenge_or_oversize",
                                                 "captured_at": datetime.now(timezone.utc).isoformat()}
            _atomic_write(checkpoint_path, _json_bytes(checkpoint))
            break
        page_path = output_root / f"listing_{listing_id}_{_sha(body)[:16]}.html"
        _atomic_write(page_path, body)
        checkpoint["pages"][listing_id] = {"status": "captured", "sha256": _sha(body),
                                            "file": page_path.name,
                                            "captured_at": datetime.now(timezone.utc).isoformat()}
        _atomic_write(checkpoint_path, _json_bytes(checkpoint))
        captured += 1
    return {"captured": captured, "checkpoint": str(checkpoint_path),
            "attempted": len(checkpoint["pages"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    observed = commands.add_parser("import-observations", help="Import captured factual page observations")
    observed.add_argument("input", type=Path)
    observed.add_argument("--output-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    html = commands.add_parser("import-html", help="Review a separately authorized saved listing HTML")
    html.add_argument("input", type=Path)
    html.add_argument("--source-url", required=True)
    html.add_argument("--observed-at", required=True)
    html.add_argument("--rights-manifest", required=True, type=Path)
    html.add_argument("--output-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    collect = commands.add_parser("collect-authorized", help="Bounded, explicitly permissioned listing capture")
    collect.add_argument("urls", type=Path, help="File of explicit CIAN listing URLs")
    collect.add_argument("--rights-manifest", required=True, type=Path)
    collect.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "import-observations":
        rows, skipped = parse_public_observations(args.input)
        result = write_review_run(rows, input_sha256=_sha(_read_bounded(args.input)),
                                  output_root=args.output_root, skipped=skipped)
    elif args.command == "import-html":
        rights = json.loads(_read_bounded(args.rights_manifest))
        row = parse_authorized_html(args.input, source_url=args.source_url,
                                    observed_at=args.observed_at, rights=rights)
        result = write_review_run([row], input_sha256=_sha(_read_bounded(args.input)),
                                  output_root=args.output_root)
    else:
        rights = json.loads(_read_bounded(args.rights_manifest))
        urls = [line.strip() for line in args.urls.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
        result = collect_authorized_pages(urls, rights=rights, output_root=args.output_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
