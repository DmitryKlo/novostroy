"""Small, local-only adapters for property source files.

The parsers do not fetch remote resources. Callers must provide source-rights
context explicitly and must only pass feeds they are allowed to process.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any, Mapping
from urllib.parse import parse_qsl, urlsplit


SCHEMA_VERSION = "property-intermediate/0.1"
RIGHTS_FIELDS = ("access_basis", "local_storage", "public_display", "redistribution")
TSV_POSITIONS = {
    "region": 0,
    "project_name": 1,
    "project_id": 2,
    "queue": 3,
    "handover_raw": 4,
    "address": 5,
    "building_id": 6,
    "project_url": 7,
    "building_label": 8,
}
_SAFE_SCHEMES = {"http", "https"}
_CREDENTIAL_QUERY_KEYS = {"token", "pbi", "key", "api_key", "apikey", "password", "secret", "auth"}
_AREA_UNITS = {"кв. м", "кв.м", "м²", "m²", "sq. m", "sqm"}
_PRICE_AREA_UNITS = {"кв. м", "кв.м", "м²", "m²", "sq. m", "sqm"}
_FACT_STATUS_FOR_NULL = {"unknown", "stale", "conflict", "not_applicable"}
_PROPERTY_FACTS: dict[str, tuple[str, str]] = {
    "offer.price_total_rub": ("RUB", "offer"),
    "offer.price_per_sqm_rub": ("RUB/m2", "offer"),
    "offer.purchase_methods": ("enum", "offer"),
    "unit.floor": ("floor", "unit"),
    "building.parking_type": ("enum", "building_or_complex"),
    "unit.area_sqm": ("m²", "unit"),
    "unit.renovation": ("source vocabulary", "unit_or_building"),
    "unit.rooms": ("room", "unit"),
    "unit.balcony": ("source vocabulary", "unit"),
    "unit.bathroom_count": ("bathroom", "unit"),
    "unit.bathroom_type": ("enum", "unit"),
    "building.ceiling_height_m": ("m", "building"),
    "unit.window_view": ("source vocabulary", "unit"),
    "building.elevator_count": ("elevator", "building"),
    "building.lift_present": ("boolean", "building"),
    "building.freight_elevator_count": ("elevator", "building"),
    "building.closed_territory": ("boolean", "building_or_complex"),
    "building.guarded_parking_present": ("boolean", "building_or_complex"),
    "complex.playground_onsite": ("boolean", "complex_or_building"),
    "building.cctv": ("boolean", "building_or_complex"),
    "building.concierge": ("boolean", "building"),
    "building.security": ("boolean", "building_or_complex"),
    "building.apartments_per_floor": ("apartment/floor", "building_section"),
    "complex.internet_providers": ("provider name", "complex_or_building"),
    "unit.stove_type": ("enum", "unit_or_building"),
    "building.construction_start_year": ("year", "building"),
    "building.expected_completion_year": ("year", "building"),
    "building.energy_class": ("class", "building"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_timestamp(value: str | None) -> str:
    if value is None:
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _validate_rights(rights: Mapping[str, Any], *, mode: str) -> dict[str, str]:
    if not isinstance(rights, Mapping):
        raise ValueError("rights must be supplied explicitly as a mapping")
    result: dict[str, str] = {}
    missing = [field for field in RIGHTS_FIELDS if not isinstance(rights.get(field), str) or not rights[field].strip()]
    if missing:
        raise ValueError(f"rights must provide non-empty text for: {', '.join(missing)}")
    for field in RIGHTS_FIELDS:
        result[field] = rights[field].strip()
    if mode == "demo" and result["access_basis"] != "synthetic_fixture":
        raise ValueError("demo data_mode requires rights.access_basis='synthetic_fixture'")
    if mode == "real" and result["access_basis"] == "synthetic_fixture":
        raise ValueError("synthetic fixtures must use data_mode='demo'")
    rights_status = rights.get("rights_status", "demo" if mode == "demo" else "unknown")
    if rights_status not in {"demo", "cleared", "restricted", "unknown"}:
        raise ValueError("rights.rights_status must be demo, cleared, restricted, or unknown")
    if mode == "demo" and rights_status != "demo":
        raise ValueError("demo data_mode requires rights.rights_status='demo'")
    if mode == "real" and rights_status == "demo":
        raise ValueError("real data_mode cannot use demo rights status")
    result["rights_status"] = rights_status
    return result


def _safe_source_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in _SAFE_SCHEMES or not parsed.netloc:
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("source_url must not contain credentials")
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
    if query_keys & _CREDENTIAL_QUERY_KEYS:
        raise ValueError("source_url must not contain credential-like query parameters")
    return value.strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _element_value(element: ET.Element) -> Any:
    children = list(element)
    attrs = {f"@{key}": value for key, value in element.attrib.items()}
    text = "".join(element.itertext()).strip() if not children else (element.text or "").strip()
    if not children:
        if attrs:
            if text:
                attrs["#text"] = text
            return attrs
        return text
    result: dict[str, Any] = dict(attrs)
    if text:
        result["#text"] = text
    for child in children:
        key = child.tag
        value = _element_value(child)
        if key not in result:
            result[key] = value
        elif isinstance(result[key], list):
            result[key].append(value)
        else:
            result[key] = [result[key], value]
    return result


def _direct_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name.lower()]


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in element.iter() if node is not element and _local_name(node.tag) == name.lower()]


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def _first_text(element: ET.Element, *names: str) -> str | None:
    for name in names:
        direct = _direct_children(element, name)
        for candidate in direct:
            value = _text(candidate)
            if value:
                return value
    for name in names:
        for candidate in _descendants(element, name):
            value = _text(candidate)
            if value:
                return value
    return None


def _nested_text(element: ET.Element, parent_name: str, child_name: str) -> str | None:
    for parent in _direct_children(element, parent_name):
        for child in _direct_children(parent, child_name):
            value = _text(child)
            if value:
                return value
    return None


def _explicit_unit_id(element: ET.Element) -> str | None:
    # These names explicitly identify a unit in the source. `internal-id` is
    # intentionally excluded because Yandex defines it as an offer ID.
    return _first_text(element, "unit-id", "apartment-id", "flat-id")


def _decimal_text(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(value.strip().replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f")


def _offer_price(offer: ET.Element) -> tuple[dict[str, str | None] | None, str | None]:
    price_elements = _direct_children(offer, "price")
    if not price_elements:
        return None, None
    price = price_elements[0]
    raw_amount = _nested_text(price, "price", "value") or _first_text(price, "value")
    amount = _decimal_text(raw_amount)
    if amount is None:
        return None, "price amount is missing or not numeric"
    currency = _first_text(price, "currency")
    raw_unit = _first_text(price, "unit")
    if raw_unit is None:
        basis = "total"
    elif raw_unit.strip().casefold() in _PRICE_AREA_UNITS:
        basis = "per_sqm"
    else:
        basis = "unknown"
    return {
        "amount": amount,
        "currency": currency.upper() if currency else None,
        "basis": basis,
        "source_unit": raw_unit,
    }, ("unrecognized price unit" if basis == "unknown" else None)


def _offer_record(
    offer: ET.Element,
    *,
    source_id: str,
    source_url: str | None,
    source_updated_at: str | None,
    observed_at: str,
    data_mode: str,
    source_rights: dict[str, str],
) -> dict[str, Any]:
    offer_id = next((value for key, value in offer.attrib.items() if _local_name(key) == "internal-id"), None)
    if not offer_id:
        raise ValueError("every YRL offer must have a non-empty internal-id")

    raw_area = _nested_text(offer, "area", "value")
    area_unit = _nested_text(offer, "area", "unit")
    area_value = _decimal_text(raw_area)
    area_sqm = area_value if area_value is not None and (not area_unit or area_unit.strip().casefold() in _AREA_UNITS) else None
    price, price_warning = _offer_price(offer)
    warnings: list[str] = []
    if raw_area and area_value is None:
        warnings.append("area value is not numeric")
    if raw_area and area_value is not None and area_sqm is None:
        warnings.append("unrecognized area unit; area_sqm left unknown")
    if price_warning:
        warnings.append(price_warning)

    project_id = _first_text(offer, "newbuilding-id", "project-id")
    if not project_id:
        raise ValueError(f"offer {offer_id!r} has no explicit newbuilding-id/project-id; cannot create complex_id")
    building_id = _first_text(offer, "building-id")
    rooms_raw = _first_text(offer, "rooms")
    rooms_number = _integer_value(rooms_raw)
    floor_raw = _first_text(offer, "floor")
    floor_number = _integer_value(floor_raw)
    area_number = None
    if area_sqm is not None:
        try:
            candidate_area = float(area_sqm)
        except (OverflowError, ValueError):
            warnings.append("area value is outside the canonical numeric range; area_sqm left unknown")
        else:
            if math.isfinite(candidate_area) and candidate_area > 0:
                area_number = candidate_area
            else:
                warnings.append("area value is outside the canonical numeric range; area_sqm left unknown")
    price_contract = _contract_price(price, observed_at)
    source_updated_at_value, source_date_warning = _date_time_or_none(source_updated_at)
    if source_date_warning:
        warnings.append(source_date_warning)
    source_quality = "illustrative" if data_mode == "demo" else ("dated_asking" if price_contract else "unknown")
    source_name = "Synthetic developer YRL fixture" if data_mode == "demo" else "Authorized developer YRL feed"
    facts = _unknown_facts(
        observed_at=observed_at,
        source_id=source_id,
        source_record_id=offer_id,
        source_updated_at=source_updated_at_value,
        rights_status=source_rights["rights_status"],
    )
    if area_number is not None:
        _observed_fact(
            facts, "unit.area_sqm", area_sqm, data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field="area/value", source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"],
        )
    if floor_number is not None:
        _observed_fact(
            facts, "unit.floor", floor_number, data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field="floor", source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"],
        )
    if rooms_number is not None:
        _observed_fact(
            facts, "unit.rooms", rooms_number, data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field="rooms", source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"],
        )
    for fact_key, tag in (
        ("unit.renovation", "renovation"),
        ("unit.balcony", "balcony"),
        ("unit.window_view", "window-view"),
    ):
        _observed_fact(
            facts, fact_key, _first_text(offer, tag), data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field=tag, source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"],
        )

    bathroom_value = _first_text(offer, "bathroom-unit")
    if bathroom_value:
        bathroom_count = _integer_value(bathroom_value)
        if bathroom_count is not None:
            _observed_fact(
                facts, "unit.bathroom_count", bathroom_count, data_mode=data_mode,
                observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
                source_field="bathroom-unit", source_updated_at=source_updated_at_value,
                rights_status=source_rights["rights_status"],
            )
        else:
            bathroom_type = {
                "совмещенный": "combined",
                "раздельный": "separate",
            }.get(bathroom_value.strip().casefold())
            _observed_fact(
                facts, "unit.bathroom_type", bathroom_type, data_mode=data_mode,
                observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
                source_field="bathroom-unit", source_updated_at=source_updated_at_value,
                rights_status=source_rights["rights_status"],
            )

    ceiling_height = _decimal_text(_first_text(offer, "ceiling-height"))
    if ceiling_height is not None:
        try:
            ceiling_value = float(ceiling_height)
        except (OverflowError, ValueError):
            ceiling_value = None
        if ceiling_value is not None and math.isfinite(ceiling_value) and ceiling_value > 0:
            _observed_fact(
                facts, "building.ceiling_height_m", ceiling_value, data_mode=data_mode,
                observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
                source_field="ceiling-height", source_updated_at=source_updated_at_value,
                rights_status=source_rights["rights_status"], scope="building",
            )

    for fact_key, tag in (
        ("building.lift_present", "lift"),
        ("building.closed_territory", "guarded-building"),
        ("building.guarded_parking_present", "parking"),
    ):
        _observed_fact(
            facts, fact_key, _boolean_value(_first_text(offer, tag)), data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field=tag, source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"],
            scope="building_or_complex" if fact_key != "building.lift_present" else "building",
        )

    if price_contract is not None:
        _observed_fact(
            facts, "offer.price_total_rub", price_contract["amount_rub"], data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field="price/value", source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"], scope="offer",
        )
        if area_value is not None:
            try:
                area_decimal = Decimal(area_value)
            except InvalidOperation:
                area_decimal = Decimal(0)
            if area_decimal.is_finite() and area_decimal > 0:
                price_inputs = [
                    {"key": "offer.price_total_rub", **facts["offer.price_total_rub"]},
                    {
                        "key": "unit.area_sqm",
                        "value": area_value,
                        "unit": "m²",
                        "status": "illustrative" if data_mode == "demo" else "observed",
                        "scope": "unit",
                        "observed_at": observed_at,
                        "provenance": _fact_provenance(
                            source_id=source_id, source_record_id=offer_id,
                            source_field="area/value", source_updated_at=source_updated_at_value,
                            rights_status=source_rights["rights_status"],
                        ),
                    },
                ]
                facts["offer.price_per_sqm_rub"] = _fact(
                    key="offer.price_per_sqm_rub",
                    value={
                        "numerator_rub": price_contract["amount_rub"],
                        "denominator_sqm": format(area_decimal.normalize(), "f"),
                    },
                    status="illustrative" if data_mode == "demo" else "derived",
                    observed_at=observed_at,
                    source_id=source_id,
                    source_record_id=offer_id,
                    source_field=None,
                    source_updated_at=source_updated_at_value,
                    rights_status=source_rights["rights_status"],
                    method="exact_ratio",
                    inputs=price_inputs,
                    formula="offer.price_total_rub / unit.area_sqm",
                    scope="offer",
                )

    built_year = _integer_value(_first_text(offer, "built-year"))
    if built_year is not None:
        _observed_fact(
            facts, "building.expected_completion_year", built_year, data_mode=data_mode,
            observed_at=observed_at, source_id=source_id, source_record_id=offer_id,
            source_field="built-year", source_updated_at=source_updated_at_value,
            rights_status=source_rights["rights_status"], scope="building",
        )
    normalized = {
        "project_name": _first_text(offer, "newbuilding-name", "project-name", "residential-complex"),
        "developer": _nested_text(offer, "sales-agent", "organization") or _first_text(offer, "developer"),
        "address": _nested_text(offer, "location", "address") or _first_text(offer, "address"),
        "locality": _nested_text(offer, "location", "locality-name"),
        "district": _nested_text(offer, "location", "sub-locality-name") or _nested_text(offer, "location", "district"),
        "rooms": _first_text(offer, "rooms"),
        "studio": _first_text(offer, "studio"),
        "floor": _first_text(offer, "floor"),
        "area_sqm": area_sqm,
        "price": price,
        "handover_raw": _first_text(offer, "newbuilding-completion-date", "ready-quarter", "completion-date"),
        "new_build": _first_text(offer, "new-flat"),
        "deal_status": _first_text(offer, "deal-status"),
        "category": _first_text(offer, "category"),
    }
    source_fields = _element_value(offer)
    if not isinstance(source_fields, dict):
        source_fields = {"#value": source_fields}
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "offer",
        "data_mode": data_mode,
        "offer_id": offer_id,
        "unit_id": _explicit_unit_id(offer),
        "building_id": building_id,
        "complex_id": project_id,
        "section_id": _first_text(offer, "section-id"),
        "floor_id": _first_text(offer, "floor-id"),
        "district_id": _first_text(offer, "district-id"),
        "rooms": rooms_number,
        "area_sqm": area_number,
        "floor": floor_number,
        "price": price_contract,
        "availability": None,
        "facts": facts,
        "source": {
            "name": source_name,
            "quality": source_quality,
            "record_mode": "demo" if data_mode == "demo" else "developer_offer",
            "observed_at": observed_at,
            "source_updated_at": source_updated_at_value,
            "rights_status": source_rights["rights_status"],
            "attribution": None,
            "source_id": source_id,
            "source_record_id": offer_id,
            "url": source_url,
            "source_fields": source_fields,
            "source_rights": dict(source_rights),
            "warnings": warnings,
        },
        "normalized": normalized,
    }


def _integer_value(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        number = Decimal(value.strip().replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _boolean_value(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"да", "true", "1", "+", "yes"}:
        return True
    if normalized in {"нет", "false", "0", "-", "no"}:
        return False
    return None


def _fact_provenance(
    *,
    source_id: str,
    source_record_id: str,
    source_field: str | None,
    source_updated_at: str | None,
    rights_status: str,
    method: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    formula: str | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "source_field": source_field,
        "source_updated_at": source_updated_at,
        "rights_status": rights_status,
    }
    if method is not None:
        provenance["method"] = method
    if inputs is not None:
        provenance["inputs"] = inputs
    if formula is not None:
        provenance["formula"] = formula
    return provenance


def _fact(
    *,
    key: str,
    value: Any,
    status: str,
    observed_at: str,
    source_id: str,
    source_record_id: str,
    source_field: str | None,
    source_updated_at: str | None,
    rights_status: str,
    method: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    formula: str | None = None,
    scope: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    default_unit, default_scope = _PROPERTY_FACTS[key]
    if status not in {"observed", "derived", "estimated", "illustrative", "unknown", "stale", "conflict", "not_applicable"}:
        raise ValueError(f"unsupported fact status: {status}")
    if status in _FACT_STATUS_FOR_NULL and value is not None:
        raise ValueError(f"fact status {status!r} requires a null value")
    if value is None and status not in _FACT_STATUS_FOR_NULL:
        raise ValueError(f"fact status {status!r} requires a non-null value")
    return {
        "value": value,
        "unit": unit or default_unit,
        "status": status,
        "scope": scope or default_scope,
        "observed_at": observed_at,
        "provenance": _fact_provenance(
            source_id=source_id,
            source_record_id=source_record_id,
            source_field=source_field,
            source_updated_at=source_updated_at,
            rights_status=rights_status,
            method=method,
            inputs=inputs,
            formula=formula,
        ),
    }


def _unknown_facts(
    *,
    observed_at: str,
    source_id: str,
    source_record_id: str,
    source_updated_at: str | None,
    rights_status: str,
) -> dict[str, dict[str, Any]]:
    return {
        key: _fact(
            key=key,
            value=None,
            status="unknown",
            observed_at=observed_at,
            source_id=source_id,
            source_record_id=source_record_id,
            source_field=None,
            source_updated_at=source_updated_at,
            rights_status=rights_status,
        )
        for key in _PROPERTY_FACTS
    }


def _observed_fact(
    facts: dict[str, dict[str, Any]],
    key: str,
    value: Any,
    *,
    data_mode: str,
    observed_at: str,
    source_id: str,
    source_record_id: str,
    source_field: str,
    source_updated_at: str | None,
    rights_status: str,
    scope: str | None = None,
) -> None:
    if value is None:
        return
    facts[key] = _fact(
        key=key,
        value=value,
        status="illustrative" if data_mode == "demo" else "observed",
        observed_at=observed_at,
        source_id=source_id,
        source_record_id=source_record_id,
        source_field=source_field,
        source_updated_at=source_updated_at,
        rights_status=rights_status,
        scope=scope,
    )


def _date_time_or_none(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "source generation date is not an ISO-8601 date-time"
    if parsed.tzinfo is None:
        return None, "source generation date has no timezone"
    return parsed.isoformat(timespec="seconds"), None


def _contract_price(
    normalized_price: dict[str, str | None] | None,
    observed_at: str,
) -> dict[str, Any] | None:
    if not normalized_price or normalized_price["basis"] != "total":
        return None
    if normalized_price["currency"] not in {"RUB", "RUR"}:
        return None
    try:
        amount = Decimal(normalized_price["amount"] or "")
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        return None
    return {
        "amount_rub": int(amount),
        "basis": "asking_total",
        "observed_at": observed_at,
    }


def parse_developer_yrl(
    xml_data: bytes | str | Path,
    *,
    source_id: str,
    rights: Mapping[str, Any],
    data_mode: str,
    source_url: str | None = None,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Parse an authorized local developer YRL/XML feed into offer records.

    `rights` is mandatory. Use `data_mode="demo"` for synthetic fixtures and
    `data_mode="real"` only for feeds authorized for processing.
    No network access or secret handling is performed by this function.
    """
    if data_mode not in {"real", "demo"}:
        raise ValueError("data_mode must be 'real' or 'demo'")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be non-empty")
    rights_value = _validate_rights(rights, mode=data_mode)
    if data_mode == "real" and observed_at is None:
        raise ValueError("real-mode YRL requires the feed capture observed_at timestamp")
    safe_url = _safe_source_url(source_url)
    observed = _validate_timestamp(observed_at)

    if isinstance(xml_data, Path):
        raw = xml_data.read_bytes()
    elif isinstance(xml_data, bytes):
        raw = xml_data
    elif isinstance(xml_data, str) and xml_data.lstrip().startswith("<"):
        raw = xml_data.encode("utf-8")
    elif isinstance(xml_data, str):
        raw = Path(xml_data).read_bytes()
    else:
        raise TypeError("xml_data must be XML bytes/text or a local Path")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DOCTYPE and ENTITY declarations are not accepted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"invalid YRL/XML: {exc}") from exc
    if _local_name(root.tag) != "realty-feed":
        raise ValueError("expected a YRL <realty-feed> root")
    generation_date = _first_text(root, "generation-date")
    offers = [node for node in root.iter() if _local_name(node.tag) == "offer"]
    ids = [next((value for key, value in node.attrib.items() if _local_name(key) == "internal-id"), None) for node in offers]
    non_empty_ids = [value for value in ids if value]
    if len(non_empty_ids) != len(set(non_empty_ids)):
        raise ValueError("offer internal-id values must be unique within the feed")
    return [
        _offer_record(
            offer,
            source_id=source_id.strip(),
            source_url=safe_url,
            source_updated_at=generation_date,
            observed_at=observed,
            data_mode=data_mode,
            source_rights=rights_value,
        )
        for offer in offers
    ]


def _json_payload(data: bytes | str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(data, Mapping):
        return data
    if isinstance(data, Path):
        raw: Any = json.loads(data.read_text(encoding="utf-8"))
    elif isinstance(data, bytes):
        raw = json.loads(data.decode("utf-8-sig"))
    elif isinstance(data, str):
        stripped = data.lstrip("\ufeff \t\r\n")
        raw = json.loads(stripped if stripped.startswith("{") else Path(data).read_text(encoding="utf-8"))
    else:
        raise TypeError("bnMAP data must be JSON bytes/text, a local Path, or a mapping")
    if not isinstance(raw, Mapping):
        raise ValueError("bnMAP API response must be a JSON object")
    return raw


def _bnmap_parking_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().casefold().replace("ё", "е").split())
    if normalized in {"подземная", "подземный", "подземное", "under", "underground"}:
        return "underground"
    if normalized in {"наземная", "наземный", "наземное", "открытая", "открытый", "surface"}:
        return "surface"
    if normalized in {"нет", "отсутствует", "без паркинга", "none", "no parking"}:
        return "none"
    return None


def _bnmap_date_year(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = datetime.strptime(candidate, "%Y-%m-%d").date()
        except ValueError:
            return None
        return parsed_date.year
    return parsed.year


def normalize_bnmap_prices(
    json_data: bytes | str | Path | Mapping[str, Any],
    *,
    source_id: str,
    rights: Mapping[str, Any],
    data_mode: str,
    source_url: str | None = None,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize local bnMAP GET Prices JSON; never fetches or handles a token.

    The operator supplies a local response and explicit rights manifest. `price`
    is retained as asking/price-list evidence, while source `squarePrice` stays
    in lossless source_fields; canonical price-per-square-metre is derived only
    from total RUB price divided by positive area.
    """
    if data_mode not in {"real", "demo"}:
        raise ValueError("data_mode must be 'real' or 'demo'")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be non-empty")
    rights_value = _validate_rights(rights, mode=data_mode)
    if data_mode == "real" and observed_at is None:
        raise ValueError("real-mode bnMAP input requires the response capture observed_at timestamp")
    safe_url = _safe_source_url(source_url)
    observed = _validate_timestamp(observed_at)
    payload = _json_payload(json_data)
    if payload.get("status") != "ok":
        raise ValueError("bnMAP API response status must be 'ok'")
    rows = payload.get("content")
    if not isinstance(rows, list):
        raise ValueError("bnMAP API response content must be an array")

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"bnMAP row {row_number} must be an object")
        source_record_id_value = row.get("a_lot_hash") or row.get("id")
        project_id = row.get("hc_id_hash")
        if source_record_id_value is None or not str(source_record_id_value).strip():
            raise ValueError(f"bnMAP row {row_number} needs a_lot_hash or id")
        if project_id is None or not str(project_id).strip():
            raise ValueError(f"bnMAP row {row_number} needs hc_id_hash to identify its complex")
        source_record_id = str(source_record_id_value).strip()
        if source_record_id in seen_ids:
            raise ValueError(f"duplicate bnMAP source record ID in response: {source_record_id}")
        seen_ids.add(source_record_id)

        raw_amount = _decimal_text(str(row.get("price"))) if row.get("price") is not None else None
        raw_area = _decimal_text(str(row.get("square"))) if row.get("square") is not None else None
        raw_rooms = str(row["rooms"]) if row.get("rooms") is not None else None
        amount_int: int | None = None
        if raw_amount is not None:
            try:
                amount_decimal = Decimal(raw_amount)
            except InvalidOperation:
                amount_decimal = Decimal(-1)
            if amount_decimal.is_finite() and amount_decimal >= 0 and amount_decimal == amount_decimal.to_integral_value():
                amount_int = int(amount_decimal)
        area_float: float | None = None
        if raw_area is not None:
            try:
                area_decimal = Decimal(raw_area)
                candidate_area = float(area_decimal)
            except (InvalidOperation, OverflowError, ValueError):
                area_decimal = Decimal(0)
                candidate_area = math.nan
            if area_decimal.is_finite() and area_decimal > 0 and math.isfinite(candidate_area):
                area_float = candidate_area
        floor_number = _integer_value(str(row.get("floor"))) if row.get("floor") is not None else None
        rooms_number = _integer_value(raw_rooms)
        price = None
        if amount_int is not None:
            price = {"amount_rub": amount_int, "basis": "asking_total", "observed_at": observed}

        offer_id = f"{source_id.strip()}:{source_record_id}"
        row_url = _safe_source_url(str(row.get("source_url"))) if row.get("source_url") else safe_url
        source_updated_at = None
        warnings: list[str] = []
        facts = _unknown_facts(
            observed_at=observed,
            source_id=source_id.strip(),
            source_record_id=source_record_id,
            source_updated_at=source_updated_at,
            rights_status=rights_value["rights_status"],
        )
        if amount_int is not None:
            _observed_fact(
                facts, "offer.price_total_rub", amount_int, data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field="price", source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope="offer",
            )
        if area_float is not None and raw_area is not None:
            _observed_fact(
                facts, "unit.area_sqm", raw_area, data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field="square", source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope="unit",
            )
        if floor_number is not None:
            _observed_fact(
                facts, "unit.floor", floor_number, data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field="floor", source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope="unit",
            )
        if rooms_number is not None:
            _observed_fact(
                facts, "unit.rooms", rooms_number, data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field="rooms", source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope="unit",
            )

        renovation = row.get("decoration_desc") or row.get("decoration") or row.get("interior")
        if isinstance(renovation, str) and renovation.strip():
            source_field = "decoration_desc" if row.get("decoration_desc") else ("decoration" if row.get("decoration") else "interior")
            source_scope = "unit" if source_field in {"decoration_desc", "decoration"} else "building"
            _observed_fact(
                facts, "unit.renovation", renovation.strip(), data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field=source_field, source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope=source_scope,
            )

        parking_type = _bnmap_parking_type(row.get("parking_type"))
        _observed_fact(
            facts, "building.parking_type", parking_type, data_mode=data_mode,
            observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
            source_field="parking_type", source_updated_at=source_updated_at,
            rights_status=rights_value["rights_status"], scope="complex_or_building",
        )
        completion_year = _bnmap_date_year(row.get("dsc"))
        if completion_year is not None:
            _observed_fact(
                facts, "building.expected_completion_year", completion_year, data_mode=data_mode,
                observed_at=observed, source_id=source_id.strip(), source_record_id=source_record_id,
                source_field="dsc", source_updated_at=source_updated_at,
                rights_status=rights_value["rights_status"], scope="building",
            )

        if amount_int is not None and raw_area is not None:
            try:
                area_decimal = Decimal(raw_area)
            except InvalidOperation:
                area_decimal = Decimal(0)
            if area_decimal.is_finite() and area_decimal > 0:
                facts["offer.price_per_sqm_rub"] = _fact(
                    key="offer.price_per_sqm_rub",
                    value={"numerator_rub": amount_int, "denominator_sqm": format(area_decimal.normalize(), "f")},
                    status="illustrative" if data_mode == "demo" else "derived",
                    observed_at=observed,
                    source_id=source_id.strip(),
                    source_record_id=source_record_id,
                    source_field=None,
                    source_updated_at=source_updated_at,
                    rights_status=rights_value["rights_status"],
                    method="exact_ratio",
                    inputs=[
                        {"key": "offer.price_total_rub", **facts["offer.price_total_rub"]},
                        {
                            "key": "unit.area_sqm",
                            "value": raw_area,
                            "unit": "m²",
                            "status": "illustrative" if data_mode == "demo" else "observed",
                            "scope": "unit",
                            "observed_at": observed,
                            "provenance": _fact_provenance(
                                source_id=source_id.strip(), source_record_id=source_record_id,
                                source_field="square", source_updated_at=source_updated_at,
                                rights_status=rights_value["rights_status"],
                            ),
                        },
                    ],
                    formula="offer.price_total_rub / unit.area_sqm",
                    scope="offer",
                )

        records.append({
            "schema_version": SCHEMA_VERSION,
            "record_type": "offer",
            "data_mode": data_mode,
            "offer_id": offer_id,
            "unit_id": str(row["numApartment"]) if row.get("numApartment") is not None else None,
            "building_id": str(row["b_id_hash"]) if row.get("b_id_hash") is not None else None,
            "complex_id": str(project_id).strip(),
            "section_id": str(row["section"]) if row.get("section") is not None else None,
            "floor_id": None,
            "district_id": None,
            "rooms": rooms_number,
            "area_sqm": area_float,
            "floor": floor_number,
            "price": price,
            "availability": None,
            "facts": facts,
            "source": {
                "name": "Synthetic bnMAP fixture" if data_mode == "demo" else "Authorized bnMAP GET Prices response",
                "quality": "illustrative" if data_mode == "demo" else ("dated_asking" if price else "unknown"),
                "record_mode": "demo" if data_mode == "demo" else "asking_price",
                "observed_at": observed,
                "source_updated_at": source_updated_at,
                "rights_status": rights_value["rights_status"],
                "attribution": None,
                "source_id": source_id.strip(),
                "source_record_id": source_record_id,
                "url": row_url,
                "source_fields": dict(row),
                "source_rights": dict(rights_value),
                "warnings": warnings,
            },
            "normalized": {
                "project_name": row.get("hc_name"),
                "developer": row.get("developer"),
                "address": row.get("building"),
                "district": row.get("district"),
                "rooms_raw": raw_rooms,
                "price_list_price": _decimal_text(str(row.get("price"))) if row.get("price") is not None else None,
                "advertised_square_price_raw": _decimal_text(str(row.get("squarePrice"))) if row.get("squarePrice") is not None else None,
                "parking_type_raw": row.get("parking_type"),
                "source_collection_time_raw": row.get("create_time"),
            },
        })
    return records


def _read_tsv_text(data: bytes | str | Path | IO[str]) -> str:
    if isinstance(data, Path):
        return data.read_text(encoding="utf-8-sig")
    if hasattr(data, "read"):
        value = data.read()
        if not isinstance(value, str):
            raise TypeError("TSV stream must be text mode")
        return value
    if isinstance(data, bytes):
        return data.decode("utf-8-sig")
    if isinstance(data, str):
        return data.lstrip("\ufeff")
    raise TypeError("TSV data must be UTF-8 bytes/text, a local Path, or a text stream")


def normalize_registry_tsv(
    tsv_data: bytes | str | Path | IO[str],
    *,
    rights: Mapping[str, Any],
    data_mode: str,
    observed_at: str | None = None,
    source_url: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize Yandex's public, headerless new-build TSV as labels only.

    This does not produce apartment offers or prices. Positional field names
    are inferred from the sample and all source columns are preserved.
    """
    if data_mode not in {"demo", "real"}:
        raise ValueError("data_mode must be 'demo' or 'real'")
    rights_value = _validate_rights(rights, mode=data_mode)
    if data_mode == "real" and observed_at is None:
        raise ValueError("real-mode registry input requires the sample capture observed_at timestamp")
    source_url = source_url or ("https://realty.yandex.ru/newbuildings.tsv" if data_mode == "real" else None)
    safe_url = _safe_source_url(source_url)
    observed = _validate_timestamp(observed_at)
    text = _read_tsv_text(tsv_data)
    reader = csv.reader(text.splitlines(), delimiter="\t")
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(reader, start=1):
        if not row or all(not value.strip() for value in row):
            continue
        if len(row) < 8:
            raise ValueError(f"TSV row {row_number} has {len(row)} columns; expected at least 8")
        fields = {name: row[position] if len(row) > position else None for name, position in TSV_POSITIONS.items()}
        project_id = fields["project_id"] or None
        building_id = fields["building_id"] or None
        if not project_id:
            raise ValueError(f"TSV row {row_number} has no project ID in column 3")
        source_record_id = f"{project_id}:{building_id or row_number}"
        records.append({
            "schema_version": SCHEMA_VERSION,
            "record_type": "reference_label",
            "data_mode": data_mode,
            "source": {
                "name": "Synthetic TSV fixture" if data_mode == "demo" else "Yandex new-build reference labels",
                "quality": "illustrative" if data_mode == "demo" else "unknown",
                "record_mode": "reference_label",
                "observed_at": observed,
                "source_updated_at": None,
                "rights_status": rights_value["rights_status"],
                "attribution": None,
                "source_id": "synthetic_tsv_fixture" if data_mode == "demo" else "yandex_newbuildings_tsv",
                "source_record_id": source_record_id,
                "url": safe_url,
                "source_fields": {
                    "columns_in_source_order": list(row),
                    "inferred_positions": {name: position for name, position in TSV_POSITIONS.items()},
                    "extra_columns": [
                        {"position": index, "value": value}
                        for index, value in enumerate(row)
                        if index >= len(TSV_POSITIONS)
                    ],
                },
                "source_rights": dict(rights_value),
            },
            "project_id": project_id,
            "building_id": building_id,
            "normalized": {
                "region": fields["region"],
                "project_name": fields["project_name"],
                "queue": fields["queue"],
                "handover_raw": fields["handover_raw"],
                "address": fields["address"],
                "project_url": fields["project_url"],
                "building_label": fields["building_label"],
                "price": None,
            },
            "quality": {"warnings": ["reference labels only; no offer, price, or availability claim"]},
        })
    return records


def _load_rights(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("rights JSON must contain an object")
    return value


def _emit_jsonl(records: list[dict[str, Any]]) -> None:
    for record in records:
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    developer = subparsers.add_parser("developer-xml", help="parse an authorized local developer YRL/XML file")
    developer.add_argument("input", help="local XML path")
    developer.add_argument("--source-id", required=True)
    developer.add_argument("--rights-json", required=True, help="local JSON rights context; contains no API credentials")
    developer.add_argument("--data-mode", choices=("real", "demo"), required=True)
    developer.add_argument("--source-url", help="public, credential-free publisher URL")
    developer.add_argument("--observed-at", help="ISO-8601 timestamp with timezone")
    bnmap = subparsers.add_parser("bnmap-prices", help="normalize a local bnMAP GET Prices JSON response")
    bnmap.add_argument("input", help="local JSON path; adapter never calls bnMAP")
    bnmap.add_argument("--source-id", required=True)
    bnmap.add_argument("--rights-json", required=True, help="local JSON rights context; contains no API credentials")
    bnmap.add_argument("--data-mode", choices=("real", "demo"), required=True)
    bnmap.add_argument("--source-url", help="credential-free source URL")
    bnmap.add_argument("--observed-at", help="ISO-8601 timestamp with timezone")
    tsv = subparsers.add_parser("yandex-tsv", help="normalize a local Yandex new-build TSV as reference labels")
    tsv.add_argument("input", help="local TSV path; adapter does not fetch it")
    tsv.add_argument("--rights-json", required=True, help="local JSON rights context")
    tsv.add_argument("--data-mode", choices=("real", "demo"), required=True)
    tsv.add_argument("--source-url")
    tsv.add_argument("--observed-at", help="ISO-8601 timestamp with timezone")
    args = parser.parse_args(argv)
    try:
        rights = _load_rights(args.rights_json)
        if args.command == "developer-xml":
            records = parse_developer_yrl(
                Path(args.input),
                source_id=args.source_id,
                rights=rights,
                data_mode=args.data_mode,
                source_url=args.source_url,
                observed_at=args.observed_at,
            )
        elif args.command == "bnmap-prices":
            records = normalize_bnmap_prices(
                Path(args.input),
                source_id=args.source_id,
                rights=rights,
                data_mode=args.data_mode,
                source_url=args.source_url,
                observed_at=args.observed_at,
            )
        else:
            records = normalize_registry_tsv(
                Path(args.input),
                rights=rights,
                data_mode=args.data_mode,
                observed_at=args.observed_at,
                source_url=args.source_url,
            )
        _emit_jsonl(records)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
