"""Read-only catalogue snapshot and strict demo/real separation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = ROOT / "data" / "demo" / "catalog.json"
CITY_PATH = ROOT / "data" / "city" / "overture_moscow_sample_normalized.geojson"
ASSET_ROOT = ROOT / "assets"


def data_mode() -> str:
    value = os.environ.get("NOVOSTROY_DATA_MODE", "demo")
    if value not in {"demo", "real"}:
        raise RuntimeError("NOVOSTROY_DATA_MODE must be demo or real")
    return value


def project_catalogue_facts(catalogue: dict[str, Any]) -> dict[str, Any]:
    """Expose offer facts and retain complex-level scope without inventing joins."""
    from recommendations.criteria import canonical_facts

    complex_facts = {
        item["complex_id"]: item.get("facts", {})
        for item in catalogue["complexes"]
        if isinstance(item.get("facts", {}), dict)
    }
    projected = []
    for offer in catalogue["offers"]:
        facts = canonical_facts(offer)
        facts.update(complex_facts.get(offer.get("complex_id"), {}))
        explicit = offer.get("facts")
        if isinstance(explicit, dict):
            facts.update(explicit)
        projected.append({**offer, "facts": facts})
    return {**catalogue, "offers": projected}


def load_catalogue() -> dict[str, Any]:
    if data_mode() == "demo":
        if not DEMO_PATH.is_file():
            raise RuntimeError("Demo fixture missing; run python -m pipeline.generate_demo")
        payload = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
        if payload.get("data_mode") != "demo" or payload.get("version") != "1":
            raise RuntimeError("Unexpected demo fixture format")
        return project_catalogue_facts(payload)
    if os.environ.get("NOVOSTROY_REAL_STORE") == "postgis":
        from backend.postgis import load_postgis_catalogue
        return project_catalogue_facts(load_postgis_catalogue())
    if os.environ.get("NOVOSTROY_REAL_STORE") not in {None, "empty"}:
        raise RuntimeError("NOVOSTROY_REAL_STORE must be empty or postgis")
    # No licensed live inventory is configured. Never load demo records here.
    return project_catalogue_facts({
        "version": "1",
        "snapshot_id": "real-empty-v1",
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "data_mode": "real",
        "districts": [],
        "complexes": [],
        "offers": [],
    })


@lru_cache(maxsize=4)
def _read_asset_manifest(mtime_ns: int) -> dict[str, int]:
    path = ASSET_ROOT / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def asset_manifest() -> dict[str, int]:
    path = ASSET_ROOT / "manifest.json"
    return _read_asset_manifest(path.stat().st_mtime_ns) if path.is_file() else {"city_buildings": 0, "facade_models": 0}


def counts(catalogue: dict[str, Any]) -> dict[str, int]:
    return {
        "districts": len(catalogue["districts"]),
        "complexes": len(catalogue["complexes"]),
        "offers": len(catalogue["offers"]),
        "city_buildings": asset_manifest()["city_buildings"],
        "facade_models": asset_manifest()["facade_models"] if catalogue["data_mode"] == "demo" else 0,
    }
