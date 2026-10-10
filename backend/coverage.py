"""Provider-hidden counts over the loaded catalogue and pinned city tile manifest."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.store import ASSET_ROOT, ROOT, asset_manifest
from backend.registry import DATASET_ID, load_registry as load_building_registry, registry_enabled
from recommendations.criteria import load_registry
from recommendations.engine import eligible_offers


QUALITY_KEYS = ("observed", "derived", "estimated", "illustrative", "render_fallback")
def _quality(**counts: int) -> dict[str, int]:
    return {key: counts.get(key, 0) for key in QUALITY_KEYS}


def _field(key: str, unit: str | None, total: int, quality: dict[str, int],
           stale: int = 0) -> dict[str, Any]:
    populated = sum(quality.values())
    if populated + stale > total:
        raise ValueError("Coverage counts exceed the dataset population")
    return {"key": key, "unit": unit, "populated_count": populated,
            "unknown_count": total - populated - stale, "stale_count": stale,
            "quality_counts": quality}


def _offer_fields(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for criterion in load_registry()["criteria"]:
        key, unit = criterion["key"], criterion.get("unit")
        quality = _quality()
        stale = 0
        for offer in offers:
            fact = (offer.get("facts") or {}).get(key) or {}
            status, value = fact.get("status"), fact.get("value")
            if status == "stale" and value is None:
                stale += 1
            elif value is not None:
                if status not in quality:
                    raise ValueError("Populated fact has an invalid quality status")
                quality[status] += 1
        rows.append(_field(key, unit, len(offers), quality, stale))
    return rows


def _bins(values: list[Decimal]) -> list[dict[str, Any]]:
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [{"lower": float(low), "upper": float(high), "upper_inclusive": True,
                 "count": len(values)}]
    span = high - low
    counts = [0] * 5
    for value in values:
        counts[min(4, int((value - low) * 5 / span))] += 1
    return [{"lower": float(low + span * Decimal(index) / 5),
             "upper": float(low + span * Decimal(index + 1) / 5),
             "upper_inclusive": index == 4, "count": count}
            for index, count in enumerate(counts)]


def _distributions(offers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for key, unit, basis in (("offer.price_total_rub", "RUB", "asking_total"),
                             ("unit.area_sqm", "m²", "total_area")):
        values: list[Decimal] = []
        excluded = 0
        for offer in offers:
            if key == "offer.price_total_rub":
                price = offer.get("price") or {}
                if price.get("basis") == "conditional_total":
                    excluded += 1
                    continue
                value = price.get("amount_rub") if price.get("basis") == "asking_total" else None
            else:
                value = offer.get("area_sqm")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            try:
                numeric = Decimal(str(value))
            except ArithmeticError:
                continue
            if not numeric.is_finite() or numeric <= 0:
                continue
            values.append(numeric)
        total = len(offers)
        result.append({"key": key, "unit": unit, "basis": basis,
                       "population_count": total, "sample_count": len(values),
                       "missing_count": total - len(values) - excluded,
                       "excluded_count": excluded, "bins": _bins(values)})
    return result


def _offer_dataset(dataset_id: str, data_mode: str, offers: list[dict[str, Any]],
                   snapshot_at: str | None, status: str) -> dict[str, Any]:
    return {"dataset_id": dataset_id, "data_mode": data_mode, "status": status,
            "snapshot_at": snapshot_at, "records_total": len(offers),
            "complexes_total": len({offer["complex_id"] for offer in offers}),
            "fields": _offer_fields(offers), "distributions": _distributions(offers)}


def _city_dataset() -> dict[str, Any]:
    pointer = asset_manifest()
    tileset_url = pointer.get("city_tileset_url", "")
    relative = tileset_url.removeprefix("/assets/") if isinstance(tileset_url, str) else ""
    folder = ASSET_ROOT / Path(relative).parent if relative and ".." not in Path(relative).parts else None
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8")) \
        if folder is not None and (folder / "manifest.json").is_file() else {}
    source_manifest_path = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8")) \
        if source_manifest_path.is_file() else {}
    total = int(manifest.get("source_rows", 0))
    measured = int(manifest.get("measured_height_count", 0))
    floor_render = int(manifest.get("assumed_floor_height_count", 0))
    imputed = int(manifest.get("imputed_height_count", 0))
    fallback = total - measured - floor_render - imputed
    if fallback < 0:
        raise ValueError("City height categories exceed the source count")
    floors_source = min(total, int(source_manifest.get("floors_present_rows", 0)))
    fields = [
        _field("city.footprint_geometry", "building", total, _quality(observed=total)),
        _field("city.source_height_m", "m", total, _quality(observed=measured)),
        _field("city.source_num_floors", "floor", total, _quality(observed=floors_source)),
        _field("city.render_height_from_floors_m", "m", total, _quality(estimated=floor_render)),
        _field("city.render_height_imputed_m", "m", total, _quality(estimated=imputed)),
        _field("city.render_height_m", "m", total,
               _quality(observed=measured, estimated=floor_render + imputed,
                        render_fallback=fallback)),
    ]
    captured_at = source_manifest.get("created_at_utc")
    try:
        parsed_capture = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        if parsed_capture.tzinfo is None:
            raise ValueError("City source snapshot lacks a timezone")
    except ValueError:
        captured_at = None
    return {"dataset_id": "city_geometry", "data_mode": "geospatial",
            "status": "available" if total else "not_loaded",
            "snapshot_at": captured_at if total else None,
            "records_total": total, "complexes_total": None,
            "fields": fields, "distributions": []}


def _registry_dataset() -> dict[str, Any]:
    registry = load_building_registry() if registry_enabled() else None
    items = registry["items"] if registry else []
    total = len(items)
    fields = []
    for key, member, quality in (
        ("building.name", "name", "observed"),
        ("building.record_kind", "record_kind", "observed"),
        ("building.address", "address", "observed"),
        ("building.location", "location", "derived"),
        ("building.status_as_reported", "status", "observed"),
        ("building.handover_as_reported", "handover", "observed"),
        ("building.floors_as_reported", "floors", "observed"),
    ):
        populated = sum(item[member] is not None for item in items)
        fields.append(_field(key, None, total, _quality(**{quality: populated})))
    return {"dataset_id": DATASET_ID, "data_mode": "registry",
            "status": "historical_records" if total else "not_loaded",
            "snapshot_at": registry["snapshot_at"] if registry else None,
            "records_total": total, "complexes_total": None,
            "fields": fields, "distributions": []}


def _developer_dataset(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    rows = snapshot["rows"]
    total = len(rows)
    fields = []
    for key, member, quality in (
        ("building.name", "name", "observed"),
        ("building.address", "address", "observed"),
        ("building.project_name", "project_name", "observed"),
        ("building.developer_name", "developer_name", "observed"),
        ("building.location", "location", "derived"),
        ("building.unit_count", "unit_count", "observed"),
    ):
        populated = sum(row[member] is not None for row in rows)
        fields.append(_field(key, None, total, _quality(**{quality: populated})))
    observed_at = max((row["observed_at"] for row in rows), default=None)
    return {"dataset_id": "developer_building_registry", "data_mode": "registry",
            "status": "observed_buildings", "snapshot_at": observed_at,
            "records_total": total, "complexes_total": len(snapshot["groups"]),
            "fields": fields, "distributions": []}


def data_coverage(catalogue: Mapping[str, Any],
                  developer_snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    mode = catalogue["data_mode"]
    eligible = eligible_offers(catalogue["offers"], snapshot_at=catalogue["snapshot_at"], data_mode=mode)
    demo = _offer_dataset("demo_offers", "demo", eligible if mode == "demo" else [],
                          catalogue["snapshot_at"] if mode == "demo" else None,
                          "illustrative" if mode == "demo" else "not_loaded")
    real_status = ("current" if mode == "real" and eligible else
                   "no_current_offers" if mode == "real" and os.environ.get("NOVOSTROY_REAL_STORE") == "postgis" else
                   "no_licensed_feed")
    real = _offer_dataset("real_current_offers", "real", eligible if mode == "real" else [],
                          catalogue["snapshot_at"] if mode == "real" else None,
                          real_status)
    return {"snapshot_id": catalogue["snapshot_id"], "data_mode": mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "criteria_total": len(load_registry()["criteria"]),
            "datasets": [demo, real, _city_dataset(),
                         _developer_dataset(developer_snapshot) if developer_snapshot is not None
                         else _registry_dataset()]}
