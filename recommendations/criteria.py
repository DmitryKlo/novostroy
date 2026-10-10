"""Registry-validated, strict typed predicates for the pure offer engine."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "contracts/criteria_registry.json"
MAX_PREDICATES = 24
MAX_IN_VALUES = 64
KNOWN_STATUSES = frozenset({"observed", "derived", "illustrative"})
NULL_STATUSES = frozenset({"unknown", "stale", "conflict", "not_applicable"})


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _number(value: Any) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Numeric criterion value must be a number or decimal string")
    raw = str(value)
    if len(raw) > 80:
        raise ValueError("Numeric criterion value is too long")
    try:
        decimal = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Numeric criterion value is invalid") from exc
    if not decimal.is_finite():
        raise ValueError("Numeric criterion value must be finite")
    if decimal.as_tuple().exponent < -64 or decimal.as_tuple().exponent > 18 or decimal.adjusted() > 18:
        raise ValueError("Numeric criterion value is outside the supported range")
    return Fraction(decimal)


def _ratio(value: Any) -> Fraction:
    if isinstance(value, Mapping):
        numerator = value.get("numerator_rub")
        denominator = value.get("denominator_sqm")
        if isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0:
            raise ValueError("Price/m² numerator must be a nonnegative integer RUB amount")
        if not isinstance(denominator, str):
            raise ValueError("Price/m² denominator must be a decimal string")
        divisor = _number(denominator)
        if divisor <= 0:
            raise ValueError("Price/m² denominator must be positive")
        return Fraction(numerator) / divisor
    return _number(value)


def _validate_scalar(value: Any, kind: str) -> None:
    if kind in {"integer", "decimal", "ratio"}:
        numeric = _number(value)
        if kind == "integer" and numeric.denominator != 1:
            raise ValueError("Integer criterion requires an integer value")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("Boolean criterion requires true or false")
    elif kind in {"string", "enum", "string_array"}:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError("Text criterion requires a nonempty string of at most 256 characters")
    else:
        raise ValueError(f"Unsupported criterion type: {kind}")


def validate_predicates(raw: Any,
                        registry: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Reject unsupported keys, operators and value types before selection."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PREDICATES:
        raise ValueError(f"predicates must be an array of at most {MAX_PREDICATES} entries")
    by_key = {entry["key"]: entry for entry in (registry or load_registry())["criteria"]}
    result = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"key", "op", "value"}:
            raise ValueError("Each predicate needs exactly key, op, and value")
        key, op, value = item["key"], item["op"], item["value"]
        if not isinstance(key, str) or key not in by_key:
            raise ValueError(f"Unknown criterion key: {key}")
        criterion = by_key[key]
        if not criterion["filterable"] or op not in criterion["operators"]:
            raise ValueError(f"Unsupported operator {op} for {key}")
        kind = criterion["value_type"]
        if op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise ValueError(f"in requires 1..{MAX_IN_VALUES} values")
            for element in value:
                _validate_scalar(element, "string" if kind == "string_array" else kind)
        else:
            _validate_scalar(value, "string" if kind == "string_array" else kind)
        if kind == "enum" and criterion.get("enum_values") and "|string" not in criterion["type"]:
            allowed = {entry.casefold() for entry in criterion["enum_values"]}
            supplied = value if op == "in" else [value]
            if any(entry.casefold() not in allowed for entry in supplied):
                raise ValueError(f"Unsupported enum value for {key}")
        result.append({"key": key, "op": op, "value": value})
    return result


def _canonical_fact(offer: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """Expose existing v1 offer fields as canonical v1.1 facts when absent."""
    price = offer.get("price") or {}
    source = offer.get("source") or {}
    rights = source.get("rights_status")
    status = "illustrative" if source.get("record_mode") == "demo" else "observed"
    common = {
        "status": status, "observed_at": price.get("observed_at") or source.get("observed_at"),
        "provenance": {
            "rights_status": rights, "source_id": source.get("source_id") or source.get("name"),
            "source_record_id": source.get("source_record_id") or offer.get("offer_id"),
            "source_updated_at": source.get("source_updated_at"),
        },
    }
    if key == "offer.price_total_rub":
        if (price.get("basis") != "asking_total" or isinstance(price.get("amount_rub"), bool)
                or not isinstance(price.get("amount_rub"), int) or price["amount_rub"] < 0):
            return None
        return {**common, "value": price["amount_rub"], "unit": "RUB", "scope": "offer",
                "provenance": {**common["provenance"], "source_field": "price.amount_rub"}}
    if key == "offer.price_per_sqm_rub":
        if (price.get("basis") != "asking_total" or isinstance(price.get("amount_rub"), bool)
                or not isinstance(price.get("amount_rub"), int) or price["amount_rub"] < 0):
            return None
        area = offer.get("area_sqm")
        # A feed can preserve more decimal digits in the field fact than in the
        # backward-compatible numeric `area_sqm` projection. Keep those digits
        # for the exact ratio, while checking both projections describe the
        # same source value.
        facts = offer.get("facts") or {}
        area_observed_at = source.get("observed_at")
        if isinstance(facts, Mapping) and "unit.area_sqm" in facts:
            area_fact = facts["unit.area_sqm"]
            expected_rights = "demo" if source.get("record_mode") == "demo" else "cleared"
            allowed_statuses = KNOWN_STATUSES if expected_rights == "demo" else {"observed", "derived"}
            if (not isinstance(area_fact, Mapping) or area_fact.get("status") not in allowed_statuses
                    or area_fact.get("unit") != "m²" or area_fact.get("value") is None
                    or not isinstance(area_fact.get("provenance"), Mapping)
                    or area_fact["provenance"].get("rights_status") != expected_rights):
                return None
            exact_area = area_fact["value"]
            try:
                if area is not None and float(_number(exact_area)) != float(area):
                    return None
            except (ValueError, OverflowError, TypeError):
                return None
            area = exact_area
            area_observed_at = area_fact.get("observed_at")
        try:
            divisor = _number(area)
        except ValueError:
            return None
        if divisor <= 0:
            return None
        return {
            **common, "status": "illustrative" if status == "illustrative" else "derived",
            "value": {"numerator_rub": price["amount_rub"], "denominator_sqm": str(area)},
            "unit": "RUB/m2", "scope": "offer",
            "provenance": {**common["provenance"], "method": "exact_ratio",
                           "formula": "price.amount_rub / area_sqm",
                           "inputs": [
                               {"field": "price.amount_rub", "value": price["amount_rub"],
                                "observed_at": price.get("observed_at")},
                               {"field": "area_sqm", "value": area,
                                "observed_at": area_observed_at},
                           ]},
        }
    base = {
        "unit.floor": ("floor", "floor", "unit"),
        "unit.area_sqm": ("area_sqm", "m²", "unit"),
        "unit.rooms": ("rooms", "room", "unit"),
    }
    if key in base:
        field, unit, scope = base[key]
        value = offer.get(field)
        if value is not None:
            return {**common, "value": value, "unit": unit, "scope": scope,
                    "provenance": {**common["provenance"], "source_field": field}}
    return None


def canonical_facts(offer: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project documented v1 offer fields into additive public facts."""
    keys = ("offer.price_total_rub", "offer.price_per_sqm_rub", "unit.area_sqm",
            "unit.rooms", "unit.floor")
    return {key: dict(fact) for key in keys if (fact := _canonical_fact(offer, key)) is not None}


def _fact_for(offer: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    facts = offer.get("facts") or {}
    if isinstance(facts, Mapping) and key in facts:
        supplied = facts[key]
        return supplied if isinstance(supplied, Mapping) else None
    return _canonical_fact(offer, key)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else None


def _fact_value(offer: Mapping[str, Any], criterion: Mapping[str, Any],
                data_mode: str, as_of: datetime) -> Any | None:
    fact = _fact_for(offer, criterion["key"])
    if not fact or fact.get("value") is None:
        return None
    status = fact.get("status")
    if status not in KNOWN_STATUSES or status in NULL_STATUSES:
        return None
    if data_mode == "real" and status not in {"observed", "derived"}:
        return None
    if data_mode == "demo" and status not in {"observed", "derived", "illustrative"}:
        return None
    provenance = fact.get("provenance") or {}
    if not isinstance(provenance, Mapping):
        return None
    allowed_rights = {"cleared"} if data_mode == "real" else {"demo", "cleared"}
    if provenance.get("rights_status") not in allowed_rights:
        return None
    if criterion.get("unit") is not None and fact.get("unit") != criterion["unit"]:
        return None
    if data_mode == "real":
        observed = _timestamp(fact.get("observed_at"))
        if observed is None or observed > as_of:
            return None
    if criterion["coverage_requirement"] == "complete":
        if provenance.get("coverage_status") != "complete" or "sample" in str(fact.get("scope", "")).casefold():
            return None
    elif criterion["coverage_requirement"] == "source_scope":
        if provenance.get("coverage_status") not in {"source_bounded", "complete"}:
            return None
    value = fact["value"]
    # The price criteria refer to asking prices. A producer-supplied fact may
    # not reclassify a conditional price or alter the canonical exact ratio.
    key = criterion["key"]
    if key in {"offer.price_total_rub", "offer.price_per_sqm_rub"}:
        canonical = _canonical_fact(offer, key)
        if canonical is None:
            return None
        try:
            if key == "offer.price_total_rub" and _number(value) != _number(canonical["value"]):
                return None
            if key == "offer.price_per_sqm_rub" and _ratio(value) != _ratio(canonical["value"]):
                return None
        except ValueError:
            return None
    return value


def _matches(value: Any, predicate: Mapping[str, Any], kind: str) -> bool:
    op, target = predicate["op"], predicate["value"]
    if kind in {"integer", "decimal", "ratio"}:
        try:
            actual = _ratio(value) if kind == "ratio" else _number(value)
            thresholds = [_number(v) for v in target] if op == "in" else [_number(target)]
        except ValueError:
            return False
        if kind == "integer" and actual.denominator != 1:
            return False
        return {"min": lambda: actual >= thresholds[0],
                "max": lambda: actual <= thresholds[0],
                "equals": lambda: actual == thresholds[0],
                "in": lambda: actual in thresholds}[op]()
    if kind == "boolean":
        if not isinstance(value, bool):
            return False
        return value in target if op == "in" else value == target
    if kind in {"string", "enum"}:
        if not isinstance(value, str):
            return False
        actual = value.casefold()
        if op == "in":
            return actual in {entry.casefold() for entry in target}
        if op == "contains":
            return target.casefold() in actual
        return actual == target.casefold()
    if kind == "string_array":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return False
        entries = {item.casefold() for item in value}
        if op == "contains":
            return target.casefold() in entries
        return bool(entries.intersection(item.casefold() for item in target))
    return False


def matches_predicates(offer: Mapping[str, Any], predicates: Sequence[Mapping[str, Any]], *,
                       data_mode: str, as_of: datetime | None = None,
                       registry: Mapping[str, Any] | None = None) -> bool:
    if not predicates:
        return True
    definitions = {entry["key"]: entry for entry in (registry or load_registry())["criteria"]}
    clock = as_of or datetime.now(timezone.utc)
    for predicate in predicates:
        criterion = definitions[predicate["key"]]
        value = _fact_value(offer, criterion, data_mode, clock)
        if value is None or not _matches(value, predicate, criterion["value_type"]):
            return False
    return True
