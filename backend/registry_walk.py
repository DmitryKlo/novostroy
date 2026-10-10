"""Snapshot-scoped, positive-evidence walking facts for verified registry points.

The first requested walking filter assesses the current small set of confirmed
origins once. Unknown coordinates and failed routes never become zero minutes.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

from backend import nearby
from backend.origin_access import CONNECTOR_POLICY_VERSION


WALK_CATEGORIES = ("metro", "school", "kindergarten", "medical")
MAX_ASSESSED_ORIGINS = 64
_LOCK = threading.RLock()
_CACHE: dict[tuple[Any, ...], dict[str, dict[str, int | None]]] = {}
_SOURCE_SIGNATURE: tuple[Any, ...] | None = None


def _input_signature() -> tuple[Any, ...]:
    paths: tuple[Path, ...] = (
        nearby.POI_PATH, nearby.POI_MANIFEST, nearby.GRAPH_CONFIG,
        nearby.GRAPH_MANIFEST, nearby.OMS_SIDECAR, nearby.OMS_MANIFEST,
        nearby.METRO_SIDECAR, nearby.METRO_MANIFEST,
        nearby.ROADS_PATH, nearby.ROADS_MANIFEST, nearby.WATER_PATH,
        nearby.WATERWAYS_PATH, nearby.SURFACES_MANIFEST,
    )
    return (CONNECTOR_POLICY_VERSION,
            *(tuple((path.stat().st_mtime_ns, path.stat().st_size))
              if path.is_file() else (None, None) for path in paths))


def _routed_minutes(item: dict[str, Any]) -> int | None:
    route = item.get("route")
    if not isinstance(route, dict) or route.get("status") != "routed":
        return None
    value = route.get("estimated_minutes")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or int(value) != value):
        return None
    return int(value)


def _assessment(response: dict[str, Any]) -> dict[str, int | None]:
    result: dict[str, int | None] = {category: None for category in WALK_CATEGORIES}
    layers = response.get("layers")
    if not isinstance(layers, dict):
        raise RuntimeError("Nearby result lacks layers")
    for layer_name in ("metro", "education", "medical"):
        layer = layers.get(layer_name)
        if not isinstance(layer, dict) or not isinstance(layer.get("items"), list):
            raise RuntimeError("Nearby result lacks a selected layer")
        for item in layer["items"]:
            if not isinstance(item, dict):
                raise RuntimeError("Nearby item is invalid")
            category = {"metro_station": "metro", "school": "school",
                        "kindergarten": "kindergarten", "clinic": "medical"}.get(item.get("category"))
            if category is None:
                continue
            minutes = _routed_minutes(item)
            if minutes is not None and (result[category] is None or minutes < result[category]):
                result[category] = minutes
    return result


def walk_minutes_for_registry(registry: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    """Return facts keyed by opaque registry ID, with a singleflight build."""
    global _SOURCE_SIGNATURE
    origins = [item for item in registry["items"]
               if item["registry_id"] in registry["visible_ids"] and item["location"] is not None]
    if not origins:
        return {}
    signature = _input_signature()
    key = (registry["snapshot_id"], signature)
    with _LOCK:
        if _SOURCE_SIGNATURE is not None and signature != _SOURCE_SIGNATURE:
            raise RuntimeError("Nearby source revision changed; restart the API worker")
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        if len(origins) > MAX_ASSESSED_ORIGINS:
            raise RuntimeError("Walking filter needs a precomputed origin batch")
        result: dict[str, dict[str, int | None]] = {}
        dataset_id: str | None = None
        for item in origins:
            lon, lat = item["location"]
            response = nearby.nearby_for_point(lon, lat, origin_id=item["registry_id"],
                                               education="all")
            current_dataset = response.get("dataset_id")
            if not isinstance(current_dataset, str) or not current_dataset:
                raise RuntimeError("Nearby result lacks a dataset revision")
            if dataset_id is not None and dataset_id != current_dataset:
                raise RuntimeError("Nearby results have mixed data revisions")
            dataset_id = current_dataset
            result[item["registry_id"]] = _assessment(response)
        _SOURCE_SIGNATURE = signature
        _CACHE.clear()
        _CACHE[key] = result
        return result
