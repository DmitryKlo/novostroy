"""Deterministic offer selection with strict filters and honest missing-data handling.

The module has no database or web-framework dependency. The backend supplies a
snapshot clock and plain JSON-compatible mappings; it adds the wire envelope.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
import re
from typing import Any, Mapping, Sequence

from .criteria import load_registry, matches_predicates, validate_predicates


FILTER_KEYS = frozenset({
    "budget_min_rub", "budget_max_rub", "district_ids", "complex_ids", "rooms",
    "area_min_sqm", "area_max_sqm", "price_basis", "limit", "predicates",
})
PRICE_BASES = frozenset({"asking_total", "conditional_total"})
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
STALE_AFTER = timedelta(hours=24)


def _clock(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("snapshot_at and price timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е").strip())


def _clarification(field: str, code: str, message: str,
                   options: list[dict[str, str]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"field": field, "code": code, "message": message}
    if options:
        result["options"] = options
    return result


def _number_millions(value: str) -> int:
    return round(float(value.replace(",", ".")) * 1_000_000)


def parse_query(query: str | None,
                districts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Parse deliberately narrow Russian expressions; ask when intent is uncertain."""
    if not query or not query.strip():
        return {"filters": {}, "clarification": None}
    text = _norm(query)
    filters: dict[str, Any] = {}

    if re.search(r"престижн|качественн.{0,12}район", text):
        return {"filters": {}, "clarification": _clarification(
            "district_ids", "query_unsupported_preference",
            "Престиж и качество района не подтверждены данными. Укажите конкретные районы."
        )}
    if re.search(r"цен[аыу]? за (?:кв(?:адратный)?\.?\s*)?метр|ипотек", text):
        return {"filters": {}, "clarification": _clarification(
            "price_basis", "query_ambiguous_price_basis",
            "Укажите полную цену предложения и условия, которые нужно учитывать."
        )}
    if re.search(r"сокол|\bцентр[еу]?\b|центру|север[еау]?|поближе к центру", text):
        return {"filters": {}, "clarification": _clarification(
            "district_ids", "query_ambiguous_district",
            "Укажите точный район: название может обозначать станцию или широкую часть города."
        )}
    if re.search(r"метро|инфраструктур|поликлиник|близко ко всему", text):
        return {"filters": {}, "clarification": _clarification(
            "infrastructure_categories", "query_ambiguous_infrastructure",
            "Уточните важные категории инфраструктуры и допустимое время пути."
        )}
    if re.search(r"больш[а-я]* семь", text):
        return {"filters": {}, "clarification": _clarification(
            "rooms", "query_ambiguous_rooms", "Сколько комнат вам нужно?"
        )}
    if re.search(r"больш[а-я]* квартир", text):
        return {"filters": {}, "clarification": _clarification(
            "area_min_sqm", "query_ambiguous_area", "Укажите минимальную площадь в м²."
        )}
    if re.search(r"дешев|не дороже среднего|бюджет около", text):
        return {"filters": {}, "clarification": _clarification(
            "budget_max_rub", "query_ambiguous_budget", "Укажите максимальный бюджет в рублях."
        )}

    million = r"(?:млн\.?|миллион(?:ов|а)?)"
    max_match = re.search(rf"\bдо\s+(\d+(?:[.,]\d+)?)\s*{million}", text)
    min_match = re.search(rf"\bот\s+(\d+(?:[.,]\d+)?)\s*{million}", text)
    if max_match:
        filters["budget_max_rub"] = _number_millions(max_match.group(1))
    if min_match:
        filters["budget_min_rub"] = _number_millions(min_match.group(1))
    rub_match = re.search(r"\bдо\s+(\d[\d\s]{5,})\s*(?:руб(?:лей)?|₽)", text)
    if rub_match and "budget_max_rub" not in filters:
        filters["budget_max_rub"] = int(rub_match.group(1).replace(" ", ""))
    numeric_to = re.search(r"\bдо\s+\d+(?:[.,]\d+)?\b", text)
    if numeric_to and not max_match and not rub_match:
        following = text[numeric_to.end():].lstrip()
        if not re.match(r"(?:кв\.?\s*м|м2|м²)\b", following):
            return {"filters": {}, "clarification": _clarification(
                "budget_max_rub", "query_ambiguous_budget", "Укажите сумму и единицу: рубли или миллионы рублей."
            )}

    room_patterns = (
        (0, r"\bстуди[яию]\b"),
        (1, r"\bоднушк|однокомнат|\b1[\s-]*комнат"),
        (2, r"\bдвушк|двухкомнат|\b2[\s-]*комнат"),
        (3, r"\bтрешк|трехкомнат|\b3[\s-]*комнат"),
        (4, r"\bчетырехкомнат|\b4[\s-]*комнат"),
    )
    found_rooms = [rooms for rooms, pattern in room_patterns if re.search(pattern, text)]
    if found_rooms:
        filters["rooms"] = found_rooms

    area_min = re.search(r"\bот\s+(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*м|м2|м²)", text)
    area_max = re.search(r"\bдо\s+(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*м|м2|м²)", text)
    if area_min:
        filters["area_min_sqm"] = float(area_min.group(1).replace(",", "."))
    if area_max:
        filters["area_max_sqm"] = float(area_max.group(1).replace(",", "."))

    matching_districts = []
    for district in districts:
        name = district.get("name")
        district_id = district.get("district_id")
        if not isinstance(name, str) or not isinstance(district_id, str):
            continue
        words = [word for word in _norm(name).split() if len(word) >= 5]
        if words and all(word[:5] in text for word in words):
            matching_districts.append({"district_id": district_id, "name": name})
    if len(matching_districts) > 1:
        return {"filters": {}, "clarification": _clarification(
            "district_ids", "query_ambiguous_district",
            "Найдено несколько районов. Выберите точный район.", matching_districts
        )}
    if len(matching_districts) == 1:
        filters["district_ids"] = [matching_districts[0]["district_id"]]
    elif "район" in text or "районе" in text:
        return {"filters": {}, "clarification": _clarification(
            "district_ids", "query_ambiguous_district", "Укажите точное название района."
        )}

    if not filters and text not in {"квартира", "квартиры", "новостройки", "жк"}:
        return {"filters": {}, "clarification": _clarification(
            "query", "query_unsupported_preference",
            "Не удалось надёжно распознать условие. Уточните бюджет, район, комнаты или площадь."
        )}
    return {"filters": filters, "clarification": None}


def _validated_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(filters or {})
    unknown = set(result).difference(FILTER_KEYS)
    if unknown:
        raise ValueError(f"Unsupported filters: {', '.join(sorted(unknown))}")
    for key in ("budget_min_rub", "budget_max_rub"):
        if key in result and (isinstance(result[key], bool) or not isinstance(result[key], int) or result[key] < 0):
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("area_min_sqm", "area_max_sqm"):
        if key in result and (isinstance(result[key], bool) or not isinstance(result[key], (int, float))
                              or not isfinite(result[key]) or result[key] <= 0):
            raise ValueError(f"{key} must be positive")
    for key in ("district_ids", "complex_ids", "rooms"):
        if key in result and not isinstance(result[key], list):
            raise ValueError(f"{key} must be a list")
    if "district_ids" in result and any(not isinstance(x, str) for x in result["district_ids"]):
        raise ValueError("district_ids must contain strings")
    if "complex_ids" in result and any(not isinstance(x, str) for x in result["complex_ids"]):
        raise ValueError("complex_ids must contain strings")
    if "rooms" in result and any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in result["rooms"]):
        raise ValueError("rooms must contain nonnegative integers")
    if "price_basis" in result and result["price_basis"] not in PRICE_BASES:
        raise ValueError("Unsupported price_basis")
    if "limit" in result and (isinstance(result["limit"], bool) or not isinstance(result["limit"], int)
                              or not 1 <= result["limit"] <= MAX_LIMIT):
        raise ValueError(f"limit must be 1..{MAX_LIMIT}")
    if "predicates" in result:
        result["predicates"] = validate_predicates(result["predicates"])
    return result


def _merge_filters(structured: dict[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(structured)
    for key, value in parsed.items():
        if key not in result:
            result[key] = value
        elif key.endswith("_min_rub") or key == "area_min_sqm":
            result[key] = max(result[key], value)
        elif key.endswith("_max_rub") or key == "area_max_sqm":
            result[key] = min(result[key], value)
        elif key in {"district_ids", "complex_ids", "rooms"}:
            result[key] = sorted(set(result[key]).intersection(value))
        elif result[key] != value:
            raise ValueError(f"Conflicting {key} in text and filters")
    return result


def eligible_offers(offers: Sequence[Mapping[str, Any]], *, snapshot_at: str | datetime,
                    data_mode: str, as_of: str | datetime | None = None) -> list[dict[str, Any]]:
    """Select same snapshot-eligible rows for map, search, and recommendations."""
    if data_mode not in {"demo", "real"}:
        raise ValueError("data_mode must be demo or real")
    # A frozen real snapshot must become stale as wall time advances. Tests may
    # inject as_of; demo rows are illustrative and use their pinned snapshot.
    clock = _clock(snapshot_at) if data_mode == "demo" else (
        _clock(as_of) if as_of is not None else datetime.now(timezone.utc)
    )
    selected = []
    for offer in offers:
        if offer.get("availability") == "unavailable":
            continue
        source = offer.get("source") or {}
        price = offer.get("price") or {}
        if data_mode == "demo":
            if source.get("record_mode") != "demo" or source.get("quality") != "illustrative":
                continue
        else:
            if (source.get("record_mode") not in {"developer_offer", "asking_price"}
                    or source.get("quality") != "dated_asking"
                    or source.get("rights_status") != "cleared"):
                continue
            if not isinstance(price, Mapping) or price.get("amount_rub") is None:
                continue
            try:
                observed = _clock(price["observed_at"])
            except (KeyError, TypeError, ValueError):
                continue
            age = clock - observed
            if age < timedelta(0) or age > STALE_AFTER:
                continue
        selected.append(dict(offer))
    return selected


def filter_offers(offers: Sequence[Mapping[str, Any]],
                  filters: Mapping[str, Any] | None, *, data_mode: str = "demo",
                  as_of: str | datetime | None = None) -> list[dict[str, Any]]:
    """Apply every hard constraint before any ranking or limit."""
    criteria = _validated_filters(filters)
    if data_mode not in {"demo", "real"}:
        raise ValueError("data_mode must be demo or real")
    clock = _clock(as_of) if as_of is not None else datetime.now(timezone.utc)
    registry = load_registry() if criteria.get("predicates") else None
    if criteria.get("budget_min_rub", 0) > criteria.get("budget_max_rub", float("inf")):
        return []
    if criteria.get("area_min_sqm", 0) > criteria.get("area_max_sqm", float("inf")):
        return []
    if "district_ids" in criteria and not criteria["district_ids"]:
        return []
    if "complex_ids" in criteria and not criteria["complex_ids"]:
        return []
    if "rooms" in criteria and not criteria["rooms"]:
        return []
    matches = []
    for offer in offers:
        price = offer.get("price")
        requires_price = any(key in criteria for key in ("budget_min_rub", "budget_max_rub", "price_basis"))
        if requires_price:
            if not isinstance(price, Mapping):
                continue
            amount = price.get("amount_rub")
            if isinstance(amount, bool) or not isinstance(amount, int):
                continue
            if "budget_min_rub" in criteria and amount < criteria["budget_min_rub"]:
                continue
            if "budget_max_rub" in criteria and amount > criteria["budget_max_rub"]:
                continue
            if "price_basis" in criteria and price.get("basis") != criteria["price_basis"]:
                continue
        if "district_ids" in criteria and offer.get("district_id") not in criteria["district_ids"]:
            continue
        if "complex_ids" in criteria and offer.get("complex_id") not in criteria["complex_ids"]:
            continue
        if "rooms" in criteria and offer.get("rooms") not in criteria["rooms"]:
            continue
        area = offer.get("area_sqm")
        if "area_min_sqm" in criteria or "area_max_sqm" in criteria:
            if isinstance(area, bool) or not isinstance(area, (int, float)) or not isfinite(area):
                continue
            if "area_min_sqm" in criteria and area < criteria["area_min_sqm"]:
                continue
            if "area_max_sqm" in criteria and area > criteria["area_max_sqm"]:
                continue
        if not matches_predicates(offer, criteria.get("predicates", ()), data_mode=data_mode,
                                  as_of=clock, registry=registry):
            continue
        matches.append(dict(offer))
    return sorted(matches, key=lambda row: str(row.get("offer_id", "")))


def _coverage(required: Sequence[str], data_mode: str,
              offer: Mapping[str, Any] | None = None) -> dict[str, Any]:
    social = (offer or {}).get("social") or {}
    observed = social.get("observed_categories") or []
    if not isinstance(observed, list):
        observed = []
    has_all = bool(required) and set(required).issubset(observed)
    status = "complete" if has_all and social.get("coverage") == "complete" else (
        "partial" if observed else ("illustrative" if data_mode == "demo" else "unknown")
    )
    return {
        "status": status,
        "observed_categories": sorted(set(str(x) for x in observed)),
        "required_categories": sorted(set(required)),
        "method": social.get("method"),
        "source_version": social.get("source_version"),
    }


def resolve_filters(request: Mapping[str, Any],
                    districts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Resolve text and structured constraints for map, search, and ranking."""
    structured = _validated_filters(request.get("filters"))
    parsed = parse_query(request.get("query"), districts)
    clarification = parsed["clarification"]
    resolvable_codes = {
        "query_ambiguous_district", "query_ambiguous_budget",
        "query_ambiguous_area", "query_ambiguous_rooms",
    }
    if (clarification and clarification["code"] in resolvable_codes
            and clarification["field"] in structured):
        # The explicit filter resolves an ambiguous phrase without guessing its meaning.
        clarification = None
    if clarification:
        return {"filters": structured, "clarification": clarification}
    criteria = _merge_filters(structured, parsed["filters"])
    return {"filters": criteria, "clarification": None}


def _prepare(offers: Sequence[Mapping[str, Any]], request: Mapping[str, Any], *,
             snapshot_at: str | datetime, data_mode: str,
             as_of: str | datetime | None,
             districts: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    resolved = resolve_filters(request, districts)
    criteria, clarification = resolved["filters"], resolved["clarification"]
    if clarification:
        return [], criteria, clarification
    candidates = eligible_offers(offers, snapshot_at=snapshot_at, data_mode=data_mode, as_of=as_of)
    return filter_offers(candidates, criteria, data_mode=data_mode, as_of=as_of), criteria, None


def search(offers: Sequence[Mapping[str, Any]], request: Mapping[str, Any], *,
           snapshot_at: str | datetime, data_mode: str = "demo",
           as_of: str | datetime | None = None,
           districts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Return strict matches; count is before the result limit."""
    matches, criteria, clarification = _prepare(
        offers, request, snapshot_at=snapshot_at, data_mode=data_mode, as_of=as_of, districts=districts
    )
    preferences = request.get("preferences") or {}
    required = preferences.get("infrastructure_categories") or []
    return {
        "items": matches[:criteria.get("limit", DEFAULT_LIMIT)],
        "count": len(matches),
        "applied_filters": criteria,
        "clarification": clarification,
        "coverage": _coverage(required, data_mode),
    }


def recommend(offers: Sequence[Mapping[str, Any]], request: Mapping[str, Any], *,
              snapshot_at: str | datetime, data_mode: str = "demo",
              as_of: str | datetime | None = None,
              districts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Rank eligible offers only with known evidence; explain missing coverage."""
    matches, criteria, clarification = _prepare(
        offers, request, snapshot_at=snapshot_at, data_mode=data_mode, as_of=as_of, districts=districts
    )
    preferences = request.get("preferences") or {}
    required = preferences.get("infrastructure_categories") or []
    ranked = []
    for source_offer in matches:
        offer = deepcopy(source_offer)
        price = offer.get("price") or {}
        amount = price.get("amount_rub")
        max_budget = criteria.get("budget_max_rub")
        affordability = None
        comparable_basis = (price.get("basis") == "asking_total"
                            or criteria.get("price_basis") == "conditional_total")
        if isinstance(amount, int) and max_budget and max_budget > 0 and comparable_basis:
            affordability = max(0.0, min(1.0, 1 - amount / max_budget))
        coverage = _coverage(required, data_mode, offer)
        social = offer.get("social") or {}
        index = social.get("index")
        if required:
            if coverage["status"] == "complete" and isinstance(index, (int, float)) and 0 <= index <= 1:
                # Fixed weights: missing price evidence contributes zero, never a larger social weight.
                score = round(0.6 * (affordability or 0.0) + 0.4 * float(index), 6)
            else:
                score = None
        else:
            score = round(affordability, 6) if affordability is not None else None
        explanation = []
        if "budget_max_rub" in criteria or "budget_min_rub" in criteria:
            explanation.append("Цена соответствует указанному бюджету.")
        if "district_ids" in criteria:
            explanation.append("Район соответствует выбранному фильтру.")
        if "complex_ids" in criteria:
            explanation.append("Жилой комплекс соответствует выбранному фильтру.")
        if "rooms" in criteria:
            explanation.append("Количество комнат соответствует выбранному фильтру.")
        if "area_min_sqm" in criteria or "area_max_sqm" in criteria:
            explanation.append("Площадь соответствует выбранному фильтру.")
        if isinstance(amount, int):
            label = "Цена предложения" if price.get("basis") == "asking_total" else "Условная цена"
            explanation.append(f"{label}: {amount:,} ₽; дата: {price.get('observed_at', 'не указана')}.")
            if price.get("basis") == "conditional_total" and criteria.get("price_basis") != "conditional_total":
                explanation.append("Условная цена не сравнивается с обычной запрашиваемой ценой.")
        if required and score is None:
            explanation.append("Покрытие инфраструктурных данных недостаточно; индекс не рассчитан.")
        if offer.get("availability") in {None, "unknown"}:
            explanation.append("Наличие квартиры не подтверждено источником.")
        if data_mode == "demo":
            explanation.append("Предложение и цена демонстрационные.")
        offer["recommendation"] = {"score": score, "explanation": explanation, "coverage": coverage}
        ranked.append(offer)

    def rank_key(offer: Mapping[str, Any]) -> tuple[float, int, float, str]:
        score = offer["recommendation"]["score"]
        price = offer.get("price") or {}
        amount = price.get("amount_rub")
        basis_rank = 0 if price.get("basis") == "asking_total" else 1
        return (-(score if score is not None else -1),
                basis_rank,
                float(amount) if isinstance(amount, int) else float("inf"),
                str(offer.get("offer_id", "")))

    ranked.sort(key=rank_key)
    return {
        "items": ranked[:criteria.get("limit", DEFAULT_LIMIT)],
        "count": len(ranked),
        "applied_filters": criteria,
        "clarification": clarification,
        "coverage": _coverage(required, data_mode),
    }
