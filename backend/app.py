"""FastAPI v1 surface shared by the web and native clients."""

from __future__ import annotations

import os
import json
import re
import hashlib
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.middleware.gzip import GZipMiddleware

from analytics.store import AnalyticsStore
from backend.public import EXPOSE_SOURCES, public_payload
from backend.coverage import data_coverage
from backend import (air_quality_index, developer_experience, developer_map,
                     developer_models, developer_overlay, developer_units)
from backend.generation import pin_generation, reset_generation, resolve_generation
from backend.map_point_groups import GroupEvidenceError, load_group_evidence
from backend.models import (CANDIDATE_ID_RE, MODEL_FILE_RE, PUBLIC_ROOT_RE,
                            REASONS, load_model_run, public_model_filenames,
                            select_candidates)
from backend.nearby import nearby_for_point, nearby_in_viewport, park_walk_for_point
from backend.point_labels import load_point_labels
from backend.registry import load_registry, registry_enabled, select_records
from backend.registry_walk import walk_minutes_for_registry
from backend.store import ASSET_ROOT, ROOT, asset_manifest, counts, data_mode, load_catalogue


LOGGER = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Filters(StrictModel):
    budget_min_rub: int | None = Field(default=None, ge=0)
    budget_max_rub: int | None = Field(default=None, ge=0)
    district_ids: list[str] | None = None
    complex_ids: list[str] | None = None
    rooms: list[int] | None = None
    area_min_sqm: float | None = Field(default=None, gt=0)
    area_max_sqm: float | None = Field(default=None, gt=0)
    price_basis: Literal["asking_total", "conditional_total"] | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    predicates: list[dict[str, Any]] | None = Field(default=None, max_length=24)

    @model_validator(mode="after")
    def check_ranges(self) -> "Filters":
        if self.budget_min_rub is not None and self.budget_max_rub is not None and self.budget_min_rub > self.budget_max_rub:
            raise ValueError("budget_min_rub exceeds budget_max_rub")
        if self.area_min_sqm is not None and self.area_max_sqm is not None and self.area_min_sqm > self.area_max_sqm:
            raise ValueError("area_min_sqm exceeds area_max_sqm")
        if self.district_ids is not None and len(set(self.district_ids)) != len(self.district_ids):
            raise ValueError("district_ids must be unique")
        if self.complex_ids is not None and len(set(self.complex_ids)) != len(self.complex_ids):
            raise ValueError("complex_ids must be unique")
        if self.rooms is not None and (len(set(self.rooms)) != len(self.rooms) or any(value < 0 for value in self.rooms)):
            raise ValueError("rooms must be unique non-negative integers")
        return self


class Preferences(StrictModel):
    infrastructure_categories: list[str] | None = None


class QueryRequest(StrictModel):
    query: str | None = Field(default=None, max_length=1000)
    filters: Filters = Field(default_factory=Filters)
    preferences: Preferences = Field(default_factory=Preferences)


class CompareRequest(StrictModel):
    offer_ids: list[str] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def distinct(self) -> "CompareRequest":
        if len(set(self.offer_ids)) != len(self.offer_ids):
            raise ValueError("offer_ids must be distinct")
        return self


class EventRequest(StrictModel):
    event_type: Literal["search", "offer_view", "compare", "recommendation_view", "map_view"]
    session_id: str | None = Field(default=None, min_length=32, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


app = FastAPI(title="Novostroy API", version="1.2.0")
PUBLIC_GROUND_ROOTS = frozenset({"public-ground-v2", "public-ground-v3"})
REGION_TILESET_ROOTS = frozenset({"public-citywide-v7", "public-citywide-v8",
                                 "public-citywide-v9", "public-citywide-v10",
                                 "public-citywide-v11", "public-citywide-v12"}) | PUBLIC_GROUND_ROOTS


class RenderAssetFiles(StaticFiles):
    """Serve render content, not internal source manifests and ID indexes."""

    async def get_response(self, path: str, scope: Any) -> Any:
        relative = PurePosixPath(path)
        name = relative.name
        model_root = bool(relative.parts and PUBLIC_ROOT_RE.fullmatch(relative.parts[0]))
        if model_root:
            if len(relative.parts) != 2 or MODEL_FILE_RE.fullmatch(name) is None:
                raise HTTPException(status_code=404)
        region_tileset = re.fullmatch(r"region_-?[0-9]+_-?[0-9]+\.tileset\.json", name) is not None
        if not model_root and name != "tileset.json" and not name.endswith(".glb") and not region_tileset:
            raise HTTPException(status_code=404)
        if name == "tileset.json" and relative.parts[0] != "facades" and len(relative.parts) != 2:
            raise HTTPException(status_code=404)
        if region_tileset and (len(relative.parts) != 2 or
                               relative.parts[0] not in REGION_TILESET_ROOTS):
            raise HTTPException(status_code=404)
        roots = {"facades"}  # Only synthetic, application-owned facade models.
        for key in ("ground_tileset_url", "city_tileset_url", "roads_tileset_url",
                    "parks_tileset_url", "water_tileset_url"):
            url = asset_manifest().get(key)
            if isinstance(url, str) and url.startswith("/assets/"):
                root = PurePosixPath(url.removeprefix("/assets/")).parts[0]
                if self._sanitized_public_root(root):
                    roots.add(root)
        model_pointer = asset_manifest().get("project_models_root")
        if isinstance(model_pointer, str) and self._sanitized_public_root(model_pointer):
            roots.add(model_pointer)
        for root in asset_manifest().get("previous_public_roots", []):
            if isinstance(root, str) and self._sanitized_public_root(root):
                roots.add(root)
        if not relative.parts or relative.parts[0] not in roots:
            raise HTTPException(status_code=404)
        if model_root:
            try:
                if name not in public_model_filenames(relative.parts[0]):
                    raise HTTPException(status_code=404)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=404) from exc
        response = await super().get_response(path, scope)
        if relative.parts[0] != "facades" and (re.fullmatch(r"public-[a-z0-9-]+-v[0-9]+", relative.parts[0])
                                                   or PUBLIC_ROOT_RE.fullmatch(relative.parts[0])):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response

    @staticmethod
    def _sanitized_public_root(root: str) -> bool:
        if PUBLIC_ROOT_RE.fullmatch(root):
            try:
                public_model_filenames(root)
                return True
            except (OSError, ValueError, json.JSONDecodeError):
                return False
        if root.startswith("public-ground-") and root not in PUBLIC_GROUND_ROOTS:
            return False
        if not re.fullmatch(r"public-[a-z0-9-]+-v[0-9]+", root):
            return False
        manifest_path = ASSET_ROOT / root / "manifest.json"
        return (manifest_path.is_file() and
                json.loads(manifest_path.read_text(encoding="utf-8")).get("public_redacted_source_ids") is True)


class GenerationPinMiddleware:
    """Keep source, visibility and model references in one API request generation."""

    def __init__(self, wrapped_app: Any) -> None:
        self.wrapped_app = wrapped_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            await self.wrapped_app(scope, receive, send)
            return
        try:
            generation = resolve_generation()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, json.JSONDecodeError):
            response = JSONResponse({"detail": {"code": "generation_unavailable",
                                                "message": "Проверенный снимок данных временно недоступен"}},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        try:
            developer_snapshot = developer_map.resolve_active()
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, json.JSONDecodeError):
            response = JSONResponse({"detail": {"code": "developer_map_unavailable",
                                                "message": "Проверенный снимок домов временно недоступен"}},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        if developer_snapshot is not None and data_mode() != "real":
            response = JSONResponse({"detail": {"code": "developer_map_mode_mismatch",
                                                "message": "Проверенный снимок домов временно недоступен"}},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        try:
            experience = (developer_experience.resolve_active(developer_snapshot)
                          if developer_snapshot is not None else None)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, json.JSONDecodeError):
            response = JSONResponse({"detail": {"code": "developer_experience_unavailable",
                                                "message": "Проверенный снимок квартир временно недоступен"}},
                                    status_code=503, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        token = pin_generation(generation)
        developer_token = developer_map.pin(developer_snapshot)
        experience_token = developer_experience.pin(experience)
        try:
            await self.wrapped_app(scope, receive, send)
        finally:
            developer_experience.reset(experience_token)
            developer_map.reset(developer_token)
            reset_generation(token)


class RangeSafeGZipMiddleware:
    """Compress ordinary responses while preserving asset byte ranges and HEAD."""

    def __init__(self, wrapped_app: Any) -> None:
        self.wrapped_app = wrapped_app
        self.gzip_app = GZipMiddleware(wrapped_app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (scope["type"] != "http" or scope.get("method") == "HEAD"
                or scope.get("path", "").startswith("/v1/models/candidates")):
            await self.wrapped_app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        if b"range" in headers or b"if-range" in headers:
            await self.wrapped_app(scope, receive, send)
        else:
            await self.gzip_app(scope, receive, send)


class PrivateApiCacheMiddleware:
    """Private API stays no-store; sanitized model reviews may revalidate."""

    def __init__(self, wrapped_app: Any) -> None:
        self.wrapped_app = wrapped_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            await self.wrapped_app(scope, receive, send)
            return
        async def no_store_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"cache-control", b"etag", b"last-modified"}]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        validated_path = (scope.get("path", "").startswith("/v1/models/candidates")
                          or scope.get("path") in {
                              "/v1/map/points", "/v1/map/nearby",
                              "/v1/map/nearby/viewport"})
        if validated_path and not EXPOSE_SOURCES:
            async def model_send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    if (message.get("status") not in {200, 304}
                            or headers.get(b"cache-control") not in {
                                b"public, max-age=0, must-revalidate",
                                b"private, max-age=0, must-revalidate"}
                            or b"etag" not in headers):
                        await no_store_send(message)
                        return
                await send(message)

            await self.wrapped_app(scope, receive, model_send)
            return

        await self.wrapped_app(scope, receive, no_store_send)


app.add_middleware(RangeSafeGZipMiddleware)
app.add_middleware(PrivateApiCacheMiddleware)
app.add_middleware(GenerationPinMiddleware)
ASSET_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/assets", RenderAssetFiles(directory=ASSET_ROOT), name="assets")


def error(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0]
    return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request", "message": str(first["msg"])}})


def validate_district_ids(filters: dict[str, Any], catalogue: dict[str, Any]) -> None:
    valid = {item["district_id"] for item in catalogue["districts"]}
    unknown = set(filters.get("district_ids") or []) - valid
    if unknown:
        raise error("unknown_district", f"Unknown district IDs: {', '.join(sorted(unknown))}", 422)
    valid_complexes = {item["complex_id"] for item in catalogue["complexes"]}
    unknown_complexes = set(filters.get("complex_ids") or []) - valid_complexes
    if unknown_complexes:
        raise error("unknown_complex", f"Unknown complex IDs: {', '.join(sorted(unknown_complexes))}", 422)


def engine() -> Any:
    try:
        from recommendations import engine as recommendation_engine
    except ImportError as exc:
        raise error("engine_unavailable", "Recommendation engine is not installed", 503) from exc
    return recommendation_engine


def query_engine(kind: str, request: QueryRequest, catalogue: dict[str, Any]) -> dict[str, Any]:
    body = request.model_dump(exclude_none=True)
    validate_district_ids(body.get("filters", {}), catalogue)
    method = getattr(engine(), kind)
    try:
        result = method(
            catalogue["offers"], body,
            snapshot_at=catalogue["snapshot_at"],
            data_mode=catalogue["data_mode"],
            districts=catalogue["districts"],
        )
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    return public_payload({"snapshot_id": catalogue["snapshot_id"], "data_mode": catalogue["data_mode"], **result})


@app.get("/v1/health")
def health() -> dict[str, Any]:
    catalogue = load_catalogue()
    developer_snapshot = active_developer_map()
    try:
        experience = (developer_experience.current(developer_snapshot)
                      if developer_snapshot is not None else None)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise error("unit_menu_unavailable", "Проверенный список квартир временно недоступен", 503) from exc
    registry_is_enabled = developer_snapshot is not None or registry_enabled()
    registry_snapshot_id = (developer_snapshot["manifest"]["snapshot_id"] if developer_snapshot is not None
                            else load_registry()["snapshot_id"] if registry_is_enabled else None)
    current_counts = counts(catalogue)
    if catalogue["data_mode"] == "demo":
        status = "demo"
    else:
        current_counts["offers"] = len(engine().eligible_offers(catalogue["offers"], snapshot_at=catalogue["snapshot_at"], data_mode="real"))
        status = ("developer_buildings_ready" if developer_snapshot is not None else
                  "ready" if current_counts["offers"] else
                  "no_current_offers" if os.environ.get("NOVOSTROY_REAL_STORE") == "postgis"
                  else "no_licensed_feed")
    return public_payload({
        "api_version": "1",
        "snapshot_id": catalogue["snapshot_id"],
        "snapshot_at": catalogue["snapshot_at"],
        "data_mode": catalogue["data_mode"],
        "status": status,
        "counts": current_counts,
        "registry_enabled": registry_is_enabled,
        "registry_snapshot_id": registry_snapshot_id,
        "catalog_view": ("developer_buildings" if developer_snapshot is not None else
                         "building_registry" if registry_is_enabled else catalogue["data_mode"]),
        "map_run_id": developer_snapshot["manifest"]["run_id"] if developer_snapshot is not None else None,
        "unit_run_id": experience[0]["manifest"]["run_id"] if experience is not None else None,
        "enrichment_run_id": (experience[0]["enrichment"]["manifest"]["run_id"]
                              if experience is not None and
                              experience[0].get("enrichment") is not None else None),
        "overlay_run_id": _overlay_run_id(experience[0] if experience else None),
    })


@app.get("/v1/districts")
def districts() -> dict[str, Any]:
    catalogue = load_catalogue()
    return public_payload({"snapshot_id": catalogue["snapshot_id"], "data_mode": catalogue["data_mode"], "items": catalogue["districts"], "count": len(catalogue["districts"])})


@app.get("/v1/criteria")
def criteria() -> dict[str, Any]:
    from recommendations.criteria import load_registry

    catalogue = load_catalogue()
    registry = load_registry()
    return public_payload({"snapshot_id": catalogue["snapshot_id"], "data_mode": catalogue["data_mode"],
                           "version": registry["version"], "criteria": registry["criteria"]})


@app.get("/v1/data-coverage")
def data_coverage_view() -> dict[str, Any]:
    return public_payload(data_coverage(load_catalogue(), active_developer_map()))


def active_registry() -> dict[str, Any]:
    if not registry_enabled():
        raise error("not_found", "Building registry is not loaded", 404)
    return load_registry()


def active_developer_map() -> dict[str, Any] | None:
    try:
        snapshot = developer_map.current()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise error("developer_map_unavailable", "Проверенный снимок домов временно недоступен", 503) from exc
    if snapshot is not None and data_mode() != "real":
        raise error("developer_map_mode_mismatch", "Проверенный снимок домов временно недоступен", 503)
    return snapshot


def _developer_filters(**filters: Any) -> None:
    unsupported = [key for key, value in filters.items() if value is not None]
    if unsupported:
        raise error("invalid_request", "Для этих домов фильтр пока недоступен: " +
                    ", ".join(sorted(unsupported)), 422)


def _legacy_unit_filter_guard(**filters: Any) -> None:
    if any(value is not None for value in filters.values()):
        raise error("invalid_request", "Фильтры квартир недоступны для этого набора", 422)


def _developer_as_of(snapshot: dict[str, Any]) -> str:
    observed = [row["observed_at"] for row in snapshot["rows"]]
    return (max(observed, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
            if observed else snapshot["manifest"]["observed_at"])


def _overlay_run_id(menu: dict[str, Any] | None) -> str | None:
    geometry = menu.get("overlay") if menu else None
    return geometry["manifest"]["run_id"] if geometry else None


def _developer_list(
    snapshot: dict[str, Any], query: str | None, *, district: str | None = None,
    okrug_id: str | None = None, max_noise_index: int | None = None,
    max_air_index: int | None = None,
    **raw_unit_filters: Any
) -> tuple[list[dict[str, Any]], dict[str, int], str | None, dict[str, Any],
           dict[str, Any] | None, dict[str, Any] | None]:
    """Use one observed apartment for every hard constraint across all views."""
    try:
        filters = developer_units.normalize_unit_filters(**raw_unit_filters)
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    try:
        experience = developer_experience.current(snapshot)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise error("unit_menu_unavailable", "Проверенный список квартир временно недоступен", 503) from exc
    if district is not None:
        if not isinstance(district, str) or not 0 < len(district.strip()) <= 160:
            raise error("invalid_request", "Неверный район", 422)
        district = district.strip()
    if okrug_id is not None:
        if not isinstance(okrug_id, str) or not 0 < len(okrug_id.strip()) <= 100:
            raise error("invalid_request", "Неверный округ", 422)
        okrug_id = okrug_id.strip()
    if max_noise_index is not None and (type(max_noise_index) is not int or
                                        not 0 <= max_noise_index <= 100):
        raise error("invalid_request", "Индекс шума должен быть от 0 до 100", 422)
    if max_air_index is not None and (type(max_air_index) is not int or
                                      not 0 <= max_air_index <= 100):
        raise error("invalid_request", "Индекс загрязнения должен быть от 0 до 100", 422)
    overlay = experience[0].get("enrichment") if experience is not None else None
    if overlay is None and any(value is not None for value in
                               (district, okrug_id, max_noise_index, max_air_index)):
        raise error("developer_enrichment_unavailable",
                    "Фильтры округа, района и шума ещё подготавливаются", 503)
    selected = developer_map.selected(snapshot, query)
    if overlay is not None:
        enriched = overlay["buildings"]
        if query and query.strip():
            selected_ids = {row["registry_id"] for row in selected}
            needle = query.strip().casefold()
            selected.extend(row for row in snapshot["rows"]
                            if row["registry_id"] not in selected_ids and
                            needle in str(enriched[row["registry_id"]].get(
                                "developer_display_name") or "").casefold())
        selected = [row for row in selected if
                    enriched[row["registry_id"]]["visibility_eligible"] and
                    (district is None or district.casefold() in {
                        str(enriched[row["registry_id"]].get("district_id") or "").casefold(),
                        str(enriched[row["registry_id"]].get("district_name") or "").casefold()}) and
                    (okrug_id is None or enriched[row["registry_id"]].get("okrug_id") == okrug_id) and
                    (max_air_index is None or _air_index_matches(
                        enriched[row["registry_id"]], max_air_index)) and
                    (max_noise_index is None or (
                        enriched[row["registry_id"]].get("noise_index") is not None and
                        enriched[row["registry_id"]]["noise_index"] <= max_noise_index))]
    if experience is None:
        if filters:
            raise error("unit_menu_unavailable", "Фильтры квартир ещё подготавливаются", 503)
        counts = {row["registry_id"]: row["unit_count"] for row in selected}
        menu_run_id = None
    else:
        menu = experience[0]
        menu_run_id = menu["manifest"]["run_id"]
        key = (snapshot["manifest"]["run_id"], menu_run_id,
               overlay["manifest"]["run_id"] if overlay else None,
               query, district, okrug_id, max_noise_index, max_air_index,
               tuple(sorted(filters.items())))
        cache = menu.setdefault("selection_cache", {})
        counts = cache.get(key)
        if counts is None:
            counts = developer_units.matching_unit_counts(
                menu, {row["registry_id"] for row in selected}, filters)
            if len(cache) >= 128:
                cache.clear()
            cache[key] = counts
        if filters:
            selected = [row for row in selected if counts[row["registry_id"]] > 0]
    applied = {key: value for key, value in (("query", query),
               ("district", district), ("okrug_id", okrug_id),
               ("max_noise_index", max_noise_index),
               ("max_air_index", max_air_index), *filters.items())
               if value is not None}
    return selected, counts, menu_run_id, applied, overlay, experience[0] if experience else None


def _public_air_index(model: dict[str, Any] | None) -> dict[str, Any]:
    """Expose only the dated internal score, never source IDs or private hashes."""
    computed = air_quality_index.compute_air_quality_index(model)
    return {key: computed[key] for key in (
        "status", "value", "scale_id", "higher_is_worse", "component_scores",
        "dominant_pollutants", "official_index", "measurement",
        "measured_district_average", "valid_at", "retrieved_at", "stale_after",
        "spatial_scope", "grid_cell_center", "cell_resolution_km_approx", "reason")}


def _air_index_matches(building: dict[str, Any], maximum: int) -> bool:
    index = air_quality_index.compute_air_quality_index(building.get("air_model"))
    return (index["status"] == "modeled_forecast" and
            type(index["value"]) is int and index["value"] <= maximum)


def _public_air_quality(building: dict[str, Any] | None) -> dict[str, Any]:
    """Label one dated CAMS grid sample without calling it a district measurement."""
    model = building.get("air_model") if building is not None else None
    index = _public_air_index(model)
    result = {"status": "unavailable", "label": None, "value": None,
              "unit": None, "observed_at": None, "scope": "district",
              "pm2_5_ug_m3": None, "pm10_ug_m3": None, "no2_ug_m3": None,
              "valid_at": None, "retrieved_at": None,
              "spatial_scope": None, "grid_cell_center": None,
              "cell_resolution_km_approx": None, "stale": None,
              "measurement": False, "measured_district_average": False,
              "data_credit": None, "reason": "no_district_model_sample",
              "internal_index": index}
    if model is None:
        return result
    try:
        valid = datetime.fromisoformat(model["forecast_at"].replace("Z", "+00:00"))
        retrieved = datetime.fromisoformat(model["retrieved_at"].replace("Z", "+00:00"))
        if valid.tzinfo is None or retrieved.tzinfo is None:
            return result
        stale = datetime.now(timezone.utc) > valid + timedelta(hours=24)
    except (KeyError, TypeError, ValueError):
        return result
    return {**result, "status": "modeled_forecast", "label": "Модельная оценка",
            "unit": "µg/m³", "scope": "sampled_grid_cell_for_district",
            "pm2_5_ug_m3": model.get("pm2_5_ug_m3"),
            "pm10_ug_m3": model.get("pm10_ug_m3"),
            "no2_ug_m3": model.get("no2_ug_m3"),
            "valid_at": model["forecast_at"],
            "retrieved_at": model["retrieved_at"],
            "spatial_scope": "sampled_grid_cell_for_district",
            "grid_cell_center": model.get("returned_grid_center_lon_lat"),
            "cell_resolution_km_approx": 11,
            "stale": stale,
            "data_credit": "Open-Meteo / CAMS Europe, CC BY 4.0",
            "reason": None}


def _developer_public_environment(row: dict[str, Any],
                                  overlay: dict[str, Any] | None) -> dict[str, Any]:
    if overlay is None:
        return {"enrichment_run_id": None,
                "developer_display_name": None, "developer_display_basis": None,
                "developer_legal_name": None,
                "okrug_id": None, "okrug_name": None, "district_id": None,
                "district_name": None, "noise_index": None,
                "air_quality": _public_air_quality(None), "media": []}
    value = overlay["buildings"][row["registry_id"]]
    return {"enrichment_run_id": overlay["manifest"]["run_id"],
            "developer_display_name": value.get("developer_display_name"),
            "developer_display_basis": value.get("developer_display_basis"),
            "developer_legal_name": value.get("developer_legal_name"),
            "okrug_id": value.get("okrug_id"), "okrug_name": value.get("okrug_name"),
            "district_id": value.get("district_id"),
            "district_name": value.get("district_name"),
            "noise_index": value.get("noise_index"),
            "air_quality": _public_air_quality(value),
            "media": _public_developer_media(overlay["media_by_registry"].get(
                row["registry_id"], []))}


def _developer_public_structure(row: dict[str, Any],
                                menu: dict[str, Any] | None) -> dict[str, Any]:
    """Keep source building totals distinct from the highest offered floor."""
    building = menu["buildings"].get(row["registry_id"]) if menu else None
    sections: list[dict[str, Any]] = []
    if building is not None:
        units = menu["units_by_building"].get(row["registry_id"], [])
        for label in sorted(building.get("source_section_total_floors") or {}):
            observed_max = max((unit["floor_number"] for unit in units
                                if unit["section_label"] == label and
                                unit["floor_number"] is not None), default=0)
            total, basis = developer_units.section_total_floors(
                building, label, observed_max)
            if total is not None:
                sections.append({"section_label": label, "total_floors": total,
                                 "total_floors_basis": basis})
    return {"total_floors": building.get("total_floors") if building else None,
            "total_floors_basis": building.get("total_floors_basis") if building else None,
            "max_observed_unit_floor": (building.get("max_observed_unit_floor")
                                        if building else None),
            "section_floor_counts": sections}


def _public_developer_media(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {"unit_plan": "Планировка квартиры",
              "floor_plan": "План этажа",
              "project_gallery": "Визуализация ЖК",
              "project_gallery_single_corpus": "Визуализация ЖК"}
    return [{"media_id": row["media_id"], "scope": row["scope"],
             "role": row["role"],
             "label": ("Фото ЖК" if row["scope"] == "project_gallery_single_corpus"
                       and row["role"] == "facade_photo" else labels[row["scope"]]),
             "observed_at": row["observed_at"],
             "asset_url": f"/v1/developer-media/{row['media_id']}",
             "availability": "prepared" if row.get("prepared_sha256") else "on_demand"}
            for row in rows]


def _public_unit_enrichment(menu: dict[str, Any], unit: dict[str, Any]
                            ) -> dict[str, Any]:
    overlay = menu.get("enrichment")
    if overlay is None:
        return {"media": [], "observed_attributes": None}
    details = overlay["units"][unit["unit_id"]]
    attributes = {"observed_at": details["observed_at"],
                  "record_updated_at": details.get("source_updated_at"),
                  "finishing_label": details.get("finishing_label"),
                  "delivery_deadline": details.get("delivery_deadline_raw"),
                  "source_sale_status": details.get("source_sale_status"),
                  "previous_price_rub": details.get("source_previous_price_rub"),
                  "discount_percent": details.get("source_discount_percent"),
                  "availability_confirmed_current": False,
                  "payment_terms_confirmed_unconditional": False}
    return {"media": _public_developer_media(
                overlay["media_by_unit"].get(unit["unit_id"], [])),
            "observed_attributes": attributes}


def _developer_environment_facets(overlay: dict[str, Any] | None) -> dict[str, Any]:
    if overlay is None:
        return {"okrugs": [], "districts": [], "noise_known_count": 0,
                "air_index_known_count": 0}
    visible = [row for row in overlay["buildings"].values()
               if row["visibility_eligible"]]
    def options(id_key: str, name_key: str) -> list[dict[str, Any]]:
        values = Counter((row[id_key], row[name_key]) for row in visible
                         if row.get(id_key) and row.get(name_key))
        return [{"id": identity, "name": name, "count": count}
                for (identity, name), count in sorted(values.items())]

    return {"okrugs": options("okrug_id", "okrug_name"),
            "districts": options("district_id", "district_name"),
            "noise_known_count": sum(row.get("noise_index") is not None for row in visible),
            "air_index_known_count": sum(
                air_quality_index.compute_air_quality_index(row.get("air_model"))["status"] ==
                "modeled_forecast" for row in visible)}


def _selected_registry(registry: dict[str, Any], **filters: Any) -> list[dict[str, Any]]:
    """Use identical eligibility for list, registry map, and map points."""
    walk_keys = ("walk_metro_max_min", "walk_school_max_min",
                 "walk_kindergarten_max_min", "walk_medical_max_min")
    has_walk_filter = any(filters.get(key) is not None for key in walk_keys)
    try:
        checked = select_records(registry, walking_minutes={} if has_walk_filter else None,
                                 **filters)
        if not has_walk_filter:
            return checked
        facts = walk_minutes_for_registry(registry)
        return select_records(registry, walking_minutes=facts, **filters)
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    except (OSError, RuntimeError) as exc:
        raise error("walking_filter_unavailable", "Проверенные пешие маршруты пока недоступны", 503) from exc


def active_model_run(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_model_run(registry)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise error("model_run_unavailable", "Проверенная оценка моделей пока недоступна", 503) from exc


def model_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run[key] for key in ("dataset_id", "registry_snapshot_id",
                                      "model_run_id", "checked_at")}


def model_response(request: Request, payload: dict[str, Any], *,
                   cache_scope: Literal["public", "private"] = "public",
                   weak_validator: bool = False) -> Response:
    """Conditional response only for an already redacted DTO."""
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if EXPOSE_SOURCES:
        return Response(content, media_type="application/json",
                        headers={"Cache-Control": "no-store"})
    etag = ('W/' if weak_validator else '') + '"' + hashlib.sha256(content).hexdigest() + '"'
    headers = {"Cache-Control": f"{cache_scope}, max-age=0, must-revalidate", "ETag": etag}
    validators = [value.strip() for value in request.headers.get("if-none-match", "").split(",")]
    if etag in validators or "*" in validators:
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="application/json", headers=headers)


@app.get("/v1/models/candidates/summary")
def model_candidates_summary(request: Request) -> Response:
    run = active_model_run()
    reason_counts = Counter(code for row in run["items"] for code in row["reason_codes"])
    group_counts = Counter(row["source_group"] for row in run["items"])
    ready_depictions = Counter(row["depiction"] for row in run["items"]
                               if row["decision"] == "ready")
    return model_response(request, {
        **model_snapshot(run), "run_status": run["run_status"], "counts": run["counts"],
        "ready_depictions": dict(sorted(ready_depictions.items())),
        "facets": {
            "reason_codes": [{"value": code, "count": count}
                             for code, count in sorted(reason_counts.items())],
            "source_groups": [{"value": group, "count": count}
                              for group, count in sorted(group_counts.items())],
        },
    })


@app.get("/v1/models/candidates")
def model_candidates_list(
    request: Request,
    query: str | None = Query(default=None, max_length=1000),
    decision: str | None = Query(default=None, max_length=32),
    reason_code: str | None = Query(default=None, max_length=64),
    source_group: str | None = Query(default=None, max_length=64),
    registry_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=48, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Response:
    run = active_model_run()
    try:
        selected = select_candidates(run, query=query, decision=decision,
                                     reason_code=reason_code, source_group=source_group,
                                     registry_id=registry_id)
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    return model_response(request, {
        **model_snapshot(run), "dataset_total": run["counts"]["scanned"],
        "records_total": len(selected), "limit": limit, "offset": offset,
        "items": selected[offset:offset + limit],
    })


@app.get("/v1/models/candidates/{candidate_id}/geometry")
def model_candidates_geometry(candidate_id: str, request: Request) -> Response:
    if CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise error("not_found", "Кандидат модели не найден", 404)
    run = active_model_run()
    geometry = run["geometries"].get(candidate_id)
    if geometry is None:
        raise error("not_found", "Проверенная геометрия недоступна", 404)
    return model_response(request, {**model_snapshot(run), "candidate_id": candidate_id,
                                    **geometry})


@app.get("/v1/models/candidates/{candidate_id}")
def model_candidates_detail(candidate_id: str, request: Request) -> Response:
    if CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise error("not_found", "Кандидат модели не найден", 404)
    run = active_model_run()
    item = run["by_id"].get(candidate_id)
    if item is None:
        raise error("not_found", "Кандидат модели не найден", 404)
    geometry = run["geometries"].get(candidate_id)
    return model_response(request, {**model_snapshot(run), "item": {
        **item, "model_tileset": geometry["model_tileset"] if geometry else None,
        "attempts": run["attempts_by_id"][candidate_id],
    }})


def _map_point_id(point_type: str, identity: str) -> str:
    digest = hashlib.sha256(f"{point_type}\0{identity}".encode("utf-8")).hexdigest()
    return "mappt_" + digest[:24]


def _mapped_point_name(item: dict[str, Any]) -> str:
    name = item["name"]
    if name.startswith("Кандидат модели "):
        return "Строящийся объект на карте"
    return name


def _confirmed_map_origin(point_id: str, registry: dict[str, Any],
                          run: dict[str, Any]) -> tuple[float, float]:
    """Resolve only IDs emitted by the current point map, with no coordinate override."""
    if re.fullmatch(r"mappt_[0-9a-f]{24}", point_id) is None:
        raise error("invalid_point_id", "Неверный идентификатор точки", 422)
    for item in registry["items"]:
        if item["registry_id"] not in registry["visible_ids"]:
            continue
        if _map_point_id("registry", item["registry_id"]) == point_id:
            if item["location"] is None:
                raise error("origin_unavailable", "Подтверждённая точка объекта отсутствует", 422)
            return tuple(item["location"])
    for item in run["items"]:
        if item["entity_type"] != "mapped_construction" or not item["ready_to_render"]:
            continue
        if _map_point_id("mapped_construction", item["candidate_id"]) == point_id:
            center = run["geometries"][item["candidate_id"]]["center"]
            if center is None:
                raise error("origin_unavailable", "Подтверждённая точка объекта отсутствует", 422)
            return tuple(center)
    raise error("point_not_found", "Точка отсутствует в текущем наборе карты", 404)


@app.get("/v1/map/points")
def map_points(
    request: Request,
    query: str | None = Query(default=None, max_length=1000),
    status: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None, max_length=80),
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    handover_year: int | None = None,
    handover_quarter: int | None = None,
    floors_min: int | None = None,
    floors_max: int | None = None,
    walk_metro_max_min: int | None = None,
    walk_school_max_min: int | None = None,
    walk_kindergarten_max_min: int | None = None,
    walk_medical_max_min: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
) -> Response:
    """All source-backed point records, preserving distinct project identities."""
    developer_snapshot = active_developer_map()
    if developer_snapshot is not None:
        _developer_filters(status=status, kind=kind,
                           handover_year=handover_year, handover_quarter=handover_quarter,
                           floors_min=floors_min, floors_max=floors_max,
                           walk_metro_max_min=walk_metro_max_min,
                           walk_school_max_min=walk_school_max_min,
                           walk_kindergarten_max_min=walk_kindergarten_max_min,
                           walk_medical_max_min=walk_medical_max_min)
        selected, unit_counts, unit_run_id, applied, overlay, menu = _developer_list(
            developer_snapshot, query, district=district, okrug_id=okrug_id,
            max_noise_index=max_noise_index, max_air_index=max_air_index,
            price_min_rub=price_min_rub,
            price_max_rub=price_max_rub,
            price_per_m2_min_rub=price_per_m2_min_rub,
            price_per_m2_max_rub=price_per_m2_max_rub,
            rooms=rooms, area_min_m2=area_min_m2, area_max_m2=area_max_m2,
            unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
        rows = [row for row in selected if row["map_eligible"]]
        items = [{**developer_map.public_point(row),
                  **_developer_public_environment(row, overlay),
                  **_developer_public_structure(row, menu),
                  "matching_units_count": unit_counts[row["registry_id"]]} for row in rows]
        manifest = developer_snapshot["manifest"]
        point_ids = {row["point_id"] for row in rows}
        return model_response(request, {
            "dataset_id": "developer_map_points",
            "registry_snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
            "unit_run_id": unit_run_id,
            "enrichment_run_id": overlay["manifest"]["run_id"] if overlay else None,
            "overlay_run_id": _overlay_run_id(menu),
            "snapshot_at": _developer_as_of(developer_snapshot)[:10],
            "checked_at": _developer_as_of(developer_snapshot),
            "dataset_total": manifest["buildings_total"],
            "counts": {"points": len(items),
                       "registry_points": len(items),
                       "registry_points_total": manifest["map_points_total"],
                       "mapped_construction_points": 0,
                       "excluded_mapped_by_registry_filters": 0,
                       "developer_building_points": len(items),
                       "developer_building_points_total": manifest["map_points_total"],
                       "unlocated_buildings": manifest["buildings_total"] - manifest["map_points_total"],
                       "coordinate_positions": len({tuple(row["location"]) for row in rows})},
            "applied_filters": applied,
            "group_evidence": {"schema_version": 1, "visibility": "public",
                               "registry_snapshot_id": manifest["snapshot_id"],
                               "map_run_id": manifest["run_id"],
                               "groups": developer_map.public_groups(developer_snapshot, point_ids)},
            "items": items,
        }, cache_scope="private", weak_validator=True)
    _legacy_unit_filter_guard(
        okrug_id=okrug_id, max_noise_index=max_noise_index,
        max_air_index=max_air_index,
        price_min_rub=price_min_rub, price_max_rub=price_max_rub,
        price_per_m2_min_rub=price_per_m2_min_rub,
        price_per_m2_max_rub=price_per_m2_max_rub, rooms=rooms,
        area_min_m2=area_min_m2, area_max_m2=area_max_m2,
        unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
    registry = active_registry()
    run = active_model_run(registry)
    if run["registry_snapshot_id"] != registry["source_snapshot_id"]:
        raise error("snapshot_mismatch", "Снимки реестра и моделей не совпадают", 503)
    try:
        mapped_labels = load_point_labels(run)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise error("point_labels_unavailable", "Проверенные подписи точек недоступны", 503) from exc
    all_registry = [item for item in registry["items"]
                    if item["registry_id"] in registry["visible_ids"] and item["location"] is not None]
    selected_registry = [item for item in
                         _selected_registry(registry, query=query, status=status, kind=kind,
                                            district=district, handover_year=handover_year,
                                            handover_quarter=handover_quarter,
                                            floors_min=floors_min, floors_max=floors_max,
                                            walk_metro_max_min=walk_metro_max_min,
                                            walk_school_max_min=walk_school_max_min,
                                            walk_kindergarten_max_min=walk_kindergarten_max_min,
                                            walk_medical_max_min=walk_medical_max_min)
                         if item["location"] is not None]
    all_mapped = [item for item in run["items"]
                  if item["entity_type"] == "mapped_construction"
                  and item["ready_to_render"]
                  and run["geometries"][item["candidate_id"]]["center"] is not None]
    needle = query.strip().casefold() if query else None
    matching_mapped = [item for item in all_mapped
                       if not needle or any(needle in value.casefold() for value in (
                           _mapped_point_name(item), item["candidate_id"],
                           mapped_labels[item["candidate_id"]]["address"] or "",
                           mapped_labels[item["candidate_id"]]["complex_label"] or ""))]
    registry_filters = any(value is not None for value in
                           (status, kind, district, handover_year, handover_quarter, floors_min, floors_max,
                            walk_metro_max_min, walk_school_max_min, walk_kindergarten_max_min,
                            walk_medical_max_min))
    selected_mapped = [] if registry_filters else matching_mapped
    items = [{
        "point_id": _map_point_id("registry", item["registry_id"]),
        "point_type": "registry", "registry_id": item["registry_id"],
        "candidate_id": None, "name": item["name"], "location": item["location"],
        "latest_known_date": item["latest_known_date"], "geometry_as_of": None,
        "address": item["address"],
        "address_basis": "registry_source" if item["address"] else None,
        "complex_label": item["name"] if re.search(r"(?:\bЖК\b|\bЖилой\s+комплекс\b)", item["name"], re.I) else None,
        "complex_label_basis": "registry_source_name" if re.search(r"(?:\bЖК\b|\bЖилой\s+комплекс\b)", item["name"], re.I) else None,
        "known_catalog_registry_id": item["registry_id"],
    } for item in selected_registry]
    items.extend({
        "point_id": _map_point_id("mapped_construction", item["candidate_id"]),
        "point_type": "mapped_construction", "registry_id": None,
        "candidate_id": item["candidate_id"], "name": _mapped_point_name(item),
        "location": run["geometries"][item["candidate_id"]]["center"],
        "latest_known_date": None, "geometry_as_of": item["geometry_as_of"],
        **mapped_labels[item["candidate_id"]],
        "known_catalog_registry_id": None,
    } for item in selected_mapped)
    if len({item["point_id"] for item in items}) != len(items):
        raise error("invalid_point_set", "Повтор идентичности точки", 503)
    full_point_ids = {_map_point_id("registry", item["registry_id"]) for item in all_registry}
    full_point_ids.update(_map_point_id("mapped_construction", item["candidate_id"])
                          for item in all_mapped)
    try:
        group_evidence = load_group_evidence(run["registry_snapshot_id"], run["model_run_id"],
                                             full_point_ids)
    except (GroupEvidenceError, OSError, json.JSONDecodeError) as exc:
        # Optional membership evidence must not take down the individual points.
        # Its private diagnostics stay in the server log, never in the response.
        LOGGER.warning("map_point_groups_unavailable: %s", type(exc).__name__)
        group_evidence = {"schema_version": 1, "visibility": "public",
                          "model_run_id": run["model_run_id"], "groups": []}
    selected_point_ids = {item["point_id"] for item in items}
    group_evidence = {**group_evidence,
                      "registry_snapshot_id": registry["snapshot_id"],
                      "groups": [group for group in group_evidence["groups"]
                                 if set(group["point_ids"]) <= selected_point_ids]}
    coordinates = {tuple(item["location"]) for item in items}
    return model_response(request, {
        "dataset_id": "map_points", "registry_snapshot_id": registry["snapshot_id"],
        "model_run_id": run["model_run_id"], "snapshot_at": registry["snapshot_at"],
        "checked_at": run["checked_at"],
        "registry_rights_status": "user_supplied_internal_use",
        "mapped_attribution": "© OpenStreetMap contributors; ODbL 1.0",
        "group_evidence": group_evidence,
        "dataset_total": len(all_registry) + len(all_mapped),
        "registry_source_total": registry["visibility"]["source_total"],
        "registry_archived_total": registry["visibility"]["archived_total"],
        "counts": {"points": len(items), "registry_points": len(selected_registry),
                   "registry_points_total": len(all_registry),
                   "mapped_construction_points": len(selected_mapped),
                   "coordinate_positions": len(coordinates),
                   "excluded_mapped_by_registry_filters": len(matching_mapped) if registry_filters else 0},
        "applied_filters": {key: value for key, value in
                            (("query", query), ("status", status), ("kind", kind), ("district", district),
                             ("handover_year", handover_year), ("handover_quarter", handover_quarter),
                             ("floors_min", floors_min), ("floors_max", floors_max),
                             ("walk_metro_max_min", walk_metro_max_min),
                             ("walk_school_max_min", walk_school_max_min),
                             ("walk_kindergarten_max_min", walk_kindergarten_max_min),
                             ("walk_medical_max_min", walk_medical_max_min)) if value is not None},
        "items": items,
    }, cache_scope="private", weak_validator=True)


def _nearby_map_context() -> tuple[dict[str, Any], dict[str, Any]]:
    registry = active_registry()
    run = active_model_run(registry)
    if run["registry_snapshot_id"] != registry["source_snapshot_id"]:
        raise error("snapshot_mismatch", "Снимки реестра и моделей не совпадают", 503)
    try:
        load_point_labels(run)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise error("point_labels_unavailable", "Проверенные подписи точек недоступны", 503) from exc
    return registry, run


@app.get("/v1/map/nearby")
def map_nearby(
    request: Request,
    point_id: str = Query(..., min_length=30, max_length=30, pattern=r"^mappt_[0-9a-f]{24}$"),
    education: Literal["all", "school", "kindergarten"] = "all",
) -> Response:
    """Local walking evidence for one current, source-backed map point."""
    developer_snapshot = active_developer_map()
    if developer_snapshot is not None:
        row = developer_snapshot["by_point"].get(point_id)
        if row is None:
            raise error("point_not_found", "Точка отсутствует в текущем наборе карты", 404)
        try:
            nearby = nearby_for_point(*row["location"], origin_id=point_id, education=education)
        except ValueError as exc:
            raise error("invalid_request", str(exc), 422) from exc
        except (OSError, RuntimeError) as exc:
            raise error("nearby_unavailable", "Локальные маршруты пока недоступны", 503) from exc
        manifest = developer_snapshot["manifest"]
        return model_response(request, {
            **nearby, "registry_snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
        }, cache_scope="private", weak_validator=True)
    registry, run = _nearby_map_context()
    lon, lat = _confirmed_map_origin(point_id, registry, run)
    try:
        nearby = nearby_for_point(lon, lat, origin_id=point_id, education=education)
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    except (OSError, RuntimeError) as exc:
        raise error("nearby_unavailable", "Локальные маршруты пока недоступны", 503) from exc
    return model_response(request, {
        **nearby, "registry_snapshot_id": registry["snapshot_id"],
        "model_run_id": run["model_run_id"],
    }, cache_scope="private", weak_validator=True)


@app.get("/v1/map/nearby/viewport")
def map_nearby_viewport(
    request: Request,
    bbox: str = Query(..., max_length=100),
    layers: str = Query(default="metro,education,medical", max_length=64),
    education: Literal["all", "school", "kindergarten"] = "all",
    limit: int = Query(default=150, ge=1, le=150),
) -> Response:
    """Capped visible POI slice; this route does not calculate walking paths."""
    developer_snapshot = active_developer_map()
    if developer_snapshot is None:
        registry, run = _nearby_map_context()
    try:
        bbox_values = tuple(float(part.strip()) for part in bbox.split(","))
        selected_layers = tuple(part.strip() for part in layers.split(","))
        nearby = nearby_in_viewport(bbox_values, layers=selected_layers,
                                    education=education, limit=limit)
    except (ValueError, TypeError) as exc:
        raise error("invalid_request", str(exc), 422) from exc
    except (OSError, RuntimeError) as exc:
        raise error("nearby_unavailable", "Локальные объекты пока недоступны", 503) from exc
    if developer_snapshot is not None:
        manifest = developer_snapshot["manifest"]
        return model_response(request, {
            **nearby, "registry_snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
        }, cache_scope="private", weak_validator=True)
    return model_response(request, {
        **nearby, "registry_snapshot_id": registry["snapshot_id"],
        "model_run_id": run["model_run_id"],
    }, cache_scope="private", weak_validator=True)


@app.get("/v1/registry/buildings")
def registry_buildings(
    query: str | None = Query(default=None, max_length=1000),
    status: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None, max_length=80),
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    handover_year: int | None = None,
    handover_quarter: int | None = None,
    floors_min: int | None = None,
    floors_max: int | None = None,
    walk_metro_max_min: int | None = None,
    walk_school_max_min: int | None = None,
    walk_kindergarten_max_min: int | None = None,
    walk_medical_max_min: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
    limit: int = Query(default=48, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    developer_snapshot = active_developer_map()
    if developer_snapshot is not None:
        _developer_filters(status=status, kind=kind,
                           handover_year=handover_year, handover_quarter=handover_quarter,
                           floors_min=floors_min, floors_max=floors_max,
                           walk_metro_max_min=walk_metro_max_min,
                           walk_school_max_min=walk_school_max_min,
                           walk_kindergarten_max_min=walk_kindergarten_max_min,
                           walk_medical_max_min=walk_medical_max_min)
        selected, unit_counts, unit_run_id, applied, overlay, menu = _developer_list(
            developer_snapshot, query, district=district, okrug_id=okrug_id,
            max_noise_index=max_noise_index, max_air_index=max_air_index,
            price_min_rub=price_min_rub,
            price_max_rub=price_max_rub,
            price_per_m2_min_rub=price_per_m2_min_rub,
            price_per_m2_max_rub=price_per_m2_max_rub,
            rooms=rooms, area_min_m2=area_min_m2, area_max_m2=area_max_m2,
            unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
        manifest = developer_snapshot["manifest"]
        return public_payload({
            "dataset_id": "developer_building_registry", "snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
            "unit_run_id": unit_run_id,
            "enrichment_run_id": overlay["manifest"]["run_id"] if overlay else None,
            "overlay_run_id": _overlay_run_id(menu),
            "snapshot_at": _developer_as_of(developer_snapshot)[:10],
            "dataset_total": manifest["buildings_total"],
            "source_total": manifest["buildings_total"], "archived_total": 0,
            "records_total": len(selected), "limit": limit, "offset": offset,
            "facets": {"developers": [{"value": value, "count": count} for value, count in
                                       sorted(Counter(row["developer_name"] for row in developer_snapshot["rows"]
                                                      if row["developer_name"]).items())],
                       "map_eligible_count": manifest["map_points_total"],
                       "unlocated_count": manifest["buildings_total"] - manifest["map_points_total"],
                       **_developer_environment_facets(overlay)},
            "applied_filters": applied,
            "items": [{**developer_map.public_building(row),
                       **_developer_public_environment(row, overlay),
                       **_developer_public_structure(row, menu),
                       "matching_units_count": unit_counts[row["registry_id"]]}
                      for row in selected[offset:offset + limit]],
        })
    _legacy_unit_filter_guard(
        okrug_id=okrug_id, max_noise_index=max_noise_index,
        max_air_index=max_air_index,
        price_min_rub=price_min_rub, price_max_rub=price_max_rub,
        price_per_m2_min_rub=price_per_m2_min_rub,
        price_per_m2_max_rub=price_per_m2_max_rub, rooms=rooms,
        area_min_m2=area_min_m2, area_max_m2=area_max_m2,
        unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
    registry = active_registry()
    items = _selected_registry(registry, query=query, status=status, kind=kind, district=district,
                               handover_year=handover_year, handover_quarter=handover_quarter,
                               floors_min=floors_min, floors_max=floors_max,
                               walk_metro_max_min=walk_metro_max_min,
                               walk_school_max_min=walk_school_max_min,
                               walk_kindergarten_max_min=walk_kindergarten_max_min,
                               walk_medical_max_min=walk_medical_max_min)
    all_items = [item for item in registry["items"]
                 if item["registry_id"] in registry["visible_ids"]]
    return public_payload({
        "dataset_id": registry["dataset_id"], "snapshot_id": registry["snapshot_id"],
        "snapshot_at": registry["snapshot_at"], "dataset_total": len(all_items),
        "source_total": registry["visibility"]["source_total"],
        "archived_total": registry["visibility"]["archived_total"],
        "records_total": len(items), "limit": limit, "offset": offset,
        "facets": {
            "statuses": [{"value": value, "count": count} for value, count in
                         sorted(Counter(item["status"] for item in all_items if item["status"]).items())],
            "kinds": [{"value": value, "count": count} for value, count in
                      sorted(Counter(item["record_kind"] for item in all_items).items())],
            "handover_years": [{"value": str(value), "count": count} for value, count in
                               sorted(Counter(item["handover_year"] for item in all_items
                                              if item["handover_year"] is not None).items())],
            "numeric_floors_count": sum(type(item["floors"]) is int for item in all_items),
            "districts": [{"value": value, "count": count} for value, count in
                          sorted(Counter(registry["district_names"][item["registry_id"]]
                                         for item in all_items if item["registry_id"] in registry["district_names"]).items())],
            "district_coverage_count": sum(item["registry_id"] in registry["district_names"]
                                           for item in all_items),
            "district_source_release": registry["district_basis"]["release"],
            "walking_origin_count": sum(item["location"] is not None for item in all_items),
        },
        "items": items[offset:offset + limit],
    })


@app.get("/v1/registry/buildings/{registry_id}")
def registry_building_detail(registry_id: str) -> dict[str, Any]:
    developer_snapshot = active_developer_map()
    if developer_snapshot is not None:
        row = developer_snapshot["by_registry"].get(registry_id)
        if row is None:
            raise error("not_found", "Building record not found", 404)
        try:
            experience = developer_experience.current(developer_snapshot)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError,
                json.JSONDecodeError) as exc:
            raise error("developer_enrichment_unavailable",
                        "Проверенный снимок домов временно недоступен", 503) from exc
        overlay = experience[0].get("enrichment") if experience is not None else None
        if overlay is not None and not overlay["buildings"][registry_id]["visibility_eligible"]:
            raise error("not_found", "Building record not found", 404)
        return public_payload({**developer_map.public_building(row),
                               **_developer_public_environment(row, overlay),
                               **_developer_public_structure(
                                   row, experience[0] if experience else None),
                               "overlay_run_id": _overlay_run_id(
                                   experience[0] if experience else None)})
    registry = active_registry()
    item = next((item for item in registry["items"] if item["registry_id"] == registry_id
                 and registry_id in registry["visible_ids"]), None)
    if item is None:
        raise error("not_found", "Building record not found", 404)
    return public_payload(item)


def active_unit_menu() -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one apartment/model commit against this request's pinned map."""
    map_snapshot = active_developer_map()
    if map_snapshot is None:
        raise error("not_found", "Меню квартир недоступно для этого набора", 404)
    try:
        experience = developer_experience.current(map_snapshot)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise error("unit_menu_unavailable", "Проверенный список квартир временно недоступен", 503) from exc
    if experience is None:
        raise error("unit_menu_unavailable", "Список квартир ещё подготавливается", 503)
    return map_snapshot, experience[0]


def _unit_identity(map_snapshot: dict[str, Any], menu: dict[str, Any],
                   registry_id: str) -> dict[str, Any]:
    if registry_id not in menu["buildings"]:
        raise error("not_found", "Дом отсутствует в текущем наборе", 404)
    manifest = map_snapshot["manifest"]
    return {"registry_snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
            "unit_run_id": menu["manifest"]["run_id"],
            "enrichment_run_id": (menu["enrichment"]["manifest"]["run_id"]
                                  if menu.get("enrichment") else None),
            "overlay_run_id": _overlay_run_id(menu),
            "registry_id": registry_id}


@app.get("/v1/developer-buildings/{registry_id}/park-walk")
def developer_building_park_walk(registry_id: str) -> dict[str, Any]:
    """Walking route to a mapped park boundary approach, never its centroid."""
    map_snapshot, menu = active_unit_menu()
    identity = _unit_identity(map_snapshot, menu, registry_id)
    _menu_environment_filters(menu, registry_id, {})
    building = map_snapshot["by_registry"][registry_id]
    if not building["map_eligible"] or building["location"] is None:
        payload = public_payload({**identity, "status": "unavailable", "park": None,
                               "distance_m": None, "estimated_minutes": None,
                               "approximate": True, "endpoint_basis": None,
                               "boundary_gap_m": None, "access_verified": False,
                               "origin_access": None, "dataset_id": None,
                               "osm_snapshot_at": None,
                               "park_candidate_limit": 0, "route_attempts": 0,
                               "attribution": "© OpenStreetMap contributors"})
        payload["attribution"] = "© OpenStreetMap contributors"
        return payload
    try:
        route = park_walk_for_point(*building["location"],
                                    origin_id=building["point_id"])
    except (OSError, RuntimeError, ValueError) as exc:
        raise error("park_walk_unavailable", "Маршрут до парка временно недоступен", 503) from exc
    payload = public_payload({**identity, **route})
    payload["attribution"] = "© OpenStreetMap contributors"
    return payload


def _menu_unit_filters(values: dict[str, Any]) -> dict[str, int | float]:
    try:
        return developer_units.normalize_unit_filters(**{
            key: values.get(key) for key in developer_units.UNIT_FILTER_NAMES
        })
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc


def _menu_environment_filters(menu: dict[str, Any], registry_id: str,
                              values: dict[str, Any]) -> dict[str, Any]:
    district = values.get("district")
    okrug_id = values.get("okrug_id")
    max_noise_index = values.get("max_noise_index")
    max_air_index = values.get("max_air_index")
    if district is not None and (not isinstance(district, str) or
                                 not 0 < len(district.strip()) <= 160):
        raise error("invalid_request", "Неверный район", 422)
    if okrug_id is not None and (not isinstance(okrug_id, str) or
                                 not 0 < len(okrug_id.strip()) <= 100):
        raise error("invalid_request", "Неверный округ", 422)
    if max_noise_index is not None and (type(max_noise_index) is not int or
                                        not 0 <= max_noise_index <= 100):
        raise error("invalid_request", "Индекс шума должен быть от 0 до 100", 422)
    if max_air_index is not None and (type(max_air_index) is not int or
                                      not 0 <= max_air_index <= 100):
        raise error("invalid_request", "Индекс загрязнения должен быть от 0 до 100", 422)
    district = district.strip() if district is not None else None
    okrug_id = okrug_id.strip() if okrug_id is not None else None
    overlay = menu.get("enrichment")
    if overlay is None:
        if any(value is not None for value in
               (district, okrug_id, max_noise_index, max_air_index)):
            raise error("developer_enrichment_unavailable",
                        "Фильтры округа, района и шума ещё подготавливаются", 503)
        return {}
    row = overlay["buildings"][registry_id]
    if not row["visibility_eligible"]:
        raise error("not_found", "Дом отсутствует в текущем наборе", 404)
    if (district is not None and district.casefold() not in {
            str(row.get("district_id") or "").casefold(),
            str(row.get("district_name") or "").casefold()} or
            okrug_id is not None and row.get("okrug_id") != okrug_id or
            max_air_index is not None and not _air_index_matches(row, max_air_index) or
            max_noise_index is not None and
            (row.get("noise_index") is None or row["noise_index"] > max_noise_index)):
        raise error("not_found", "Дом не подходит под выбранные условия", 404)
    return {key: value for key, value in (("district", district),
            ("okrug_id", okrug_id), ("max_noise_index", max_noise_index),
            ("max_air_index", max_air_index))
            if value is not None}


@app.get("/v1/developer-buildings/{registry_id}/floors")
def developer_building_floors(
    registry_id: str,
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
) -> dict[str, Any]:
    map_snapshot, menu = active_unit_menu()
    identity = _unit_identity(map_snapshot, menu, registry_id)
    environment_filters = _menu_environment_filters(menu, registry_id, locals())
    map_run_id = identity["map_run_id"]
    units = menu["units_by_building"][registry_id]
    filters = _menu_unit_filters(locals())
    matched = developer_units.filter_units(units, filters)
    matched_ids = {row["unit_id"] for row in matched}
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for floor in menu["floors"].values():
        if floor["registry_id"] != registry_id:
            continue
        section = floor["section_label"]
        prepared = [developer_units.public_plan(plan)
                    for plan in menu["plans_by_floor"].get(floor["floor_id"], [])]
        plan = next((value for value in prepared if value is not None), None)
        matching_count = sum(unit_id in matched_ids for unit_id in floor["unit_ids"])
        if filters and matching_count == 0:
            continue
        groups.setdefault(section, []).append({
            "floor_id": floor["floor_id"], "floor_number": floor["floor_number"],
            "unit_count": matching_count,
            "source_unit_count": len(floor["unit_ids"]),
            "matching_unit_count": matching_count,
            "geometry_status": "source_plan_image" if plan else "none",
            "plan": plan,
        })
    building = menu["buildings"][registry_id]
    sections = []
    for label, floors in sorted(groups.items(), key=lambda pair: pair[0] or ""):
        observed_max = max(item["floor_number"] for item in floors)
        section_total, section_basis = developer_units.section_total_floors(
            building, label, observed_max)
        sections.append({"section_id": developer_units.section_id(map_run_id, registry_id, label),
                         "section_label": label, "total_floors": section_total,
                         "total_floors_basis": section_basis,
                         "floors": sorted(floors, key=lambda item: item["floor_number"])})
    return public_payload({**identity, "observed_at": max(
                               (row["observed_at"] for row in units),
                               key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))),
                           "total_floors": building["total_floors"],
                           "total_floors_basis": building.get("total_floors_basis"),
                           "max_observed_unit_floor": building["max_observed_unit_floor"],
                           "sections": sections,
                           "unassigned_unit_count": sum(row["floor_number"] is None for row in matched),
                           "source_unassigned_unit_count": sum(row["floor_number"] is None
                                                               for row in units),
                           "matching_unassigned_unit_count": sum(row["floor_number"] is None
                                                                 for row in matched),
                           "matching_units_total": len(matched),
                           "applied_filters": {**filters, **environment_filters}})


@app.get("/v1/developer-buildings/{registry_id}/floors/{floor_id}")
def developer_building_floor(
    registry_id: str,
    floor_id: str,
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
) -> dict[str, Any]:
    map_snapshot, menu = active_unit_menu()
    identity = _unit_identity(map_snapshot, menu, registry_id)
    environment_filters = _menu_environment_filters(menu, registry_id, locals())
    floor = menu["floors"].get(floor_id)
    if floor is None or floor["registry_id"] != registry_id:
        raise error("not_found", "Этаж отсутствует у выбранного дома", 404)
    filters = _menu_unit_filters(locals())
    matching_count = len(developer_units.filter_units(
        [menu["units"][unit_id] for unit_id in floor["unit_ids"]], filters))
    if filters and matching_count == 0:
        raise error("not_found", "На этаже нет квартир по выбранным условиям", 404)
    prepared = [developer_units.public_plan(plan)
                for plan in menu["plans_by_floor"].get(floor_id, [])]
    plan = next((value for value in prepared if value is not None), None)
    plan_regions = []
    if plan is not None:
        for source_plan in menu["plans_by_floor"].get(floor_id, []):
            if source_plan.get("asset_sha256") == plan["asset_sha256"]:
                plan_regions = developer_units.public_plan_regions(source_plan)
                break
    return public_payload({**identity, "floor_id": floor_id,
                           "section_id": developer_units.section_id(
                               identity["map_run_id"], registry_id, floor["section_label"]),
                           "section_label": floor["section_label"],
                           "floor_number": floor["floor_number"],
                           "unit_count": matching_count,
                           "source_unit_count": len(floor["unit_ids"]),
                           "matching_unit_count": matching_count,
                           "applied_filters": {**filters, **environment_filters},
                           "geometry_status": "source_plan_image" if plan else "none",
                           "plan": plan, "plan_regions": plan_regions if not filters else [],
                           "media": _public_developer_media(
                               menu["enrichment"]["media_by_floor"].get(floor_id, []))
                               if menu.get("enrichment") else [],
                           "hotspots": []})


@app.get("/v1/developer-buildings/{registry_id}/units")
def developer_building_units(
    registry_id: str,
    section_id: str | None = Query(default=None, max_length=32),
    floor_id: str | None = Query(default=None, max_length=30),
    floor_known: bool | None = None,
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
    limit: int = Query(default=48, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    map_snapshot, menu = active_unit_menu()
    identity = _unit_identity(map_snapshot, menu, registry_id)
    environment_filters = _menu_environment_filters(menu, registry_id, locals())
    if floor_id is not None and floor_known is False:
        raise error("invalid_request", "Фильтры этажа противоречат друг другу", 422)
    floor = menu["floors"].get(floor_id) if floor_id is not None else None
    if floor_id is not None and (floor is None or floor["registry_id"] != registry_id):
        raise error("not_found", "Этаж отсутствует у выбранного дома", 404)
    rows = menu["units_by_building"][registry_id]
    if section_id is not None:
        valid_sections = {developer_units.section_id(identity["map_run_id"], registry_id,
                                                     row["section_label"])
                          for row in rows if row["section_label"] is not None}
        if section_id not in valid_sections:
            raise error("not_found", "Секция отсутствует у выбранного дома", 404)
        rows = [row for row in rows if developer_units.section_id(
            identity["map_run_id"], registry_id, row["section_label"]) == section_id]
    if floor_id is not None:
        rows = [row for row in rows if row["unit_id"] in floor["unit_ids"]]
    if floor_known is not None:
        rows = [row for row in rows if (row["floor_number"] is not None) == floor_known]
    unit_filters = _menu_unit_filters(locals())
    rows = developer_units.filter_units(rows, unit_filters)
    rows = sorted(rows, key=lambda row: (row["floor_number"] is None,
                                        row["floor_number"] if row["floor_number"] is not None else 0,
                                        row["section_label"] or "", row["unit_number"] or "",
                                        row["unit_id"]))
    return public_payload({**identity, "total": len(rows), "limit": limit, "offset": offset,
                           "applied_filters": {**unit_filters, **environment_filters},
                           "items": [developer_units.public_unit(
                               row, identity["map_run_id"],
                               menu["plans_by_unit"].get(row["unit_id"], [])) |
                               _public_unit_enrichment(menu, row)
                               for row in rows[offset:offset + limit]]})


@app.get("/v1/developer-buildings/{registry_id}/units/{unit_id}")
def developer_building_unit(
    registry_id: str,
    unit_id: str,
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
) -> dict[str, Any]:
    map_snapshot, menu = active_unit_menu()
    identity = _unit_identity(map_snapshot, menu, registry_id)
    environment_filters = _menu_environment_filters(menu, registry_id, locals())
    row = menu["units"].get(unit_id)
    if row is None or row["registry_id"] != registry_id:
        raise error("not_found", "Квартира отсутствует у выбранного дома", 404)
    filters = _menu_unit_filters(locals())
    if filters and not developer_units.filter_units([row], filters):
        raise error("not_found", "Квартира не подходит под выбранные условия", 404)
    plans = menu["plans_by_unit"].get(unit_id, [])
    return public_payload({**identity,
                           **developer_units.public_unit(row, identity["map_run_id"], plans),
                           **_public_unit_enrichment(menu, row),
                           "applied_filters": {**filters, **environment_filters},
                           "plans": [value for plan in plans
                                     if (value := developer_units.public_plan(plan)) is not None]})


@app.get("/v1/developer-unit-assets/{sha256}")
def developer_unit_asset(sha256: str) -> Response:
    _, menu = active_unit_menu()
    try:
        content, mime = developer_units.asset_bytes(menu, sha256)
    except FileNotFoundError as exc:
        raise error("not_found", "Подготовленный план отсутствует", 404) from exc
    except (OSError, ValueError) as exc:
        raise error("unit_asset_unavailable", "Подготовленный план временно недоступен", 503) from exc
    return Response(content, media_type=mime,
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'; sandbox"})


@app.get("/v1/developer-media/{media_id}")
def developer_media_asset(media_id: str) -> Response:
    """Materialize one exact, snapshot-listed plan or project image as PNG."""
    map_snapshot, menu = active_unit_menu()
    del map_snapshot
    overlay = menu.get("enrichment")
    if overlay is None:
        raise error("not_found", "Изображение отсутствует в текущем наборе", 404)
    row = overlay["media"].get(media_id)
    if row is None:
        raise error("not_found", "Изображение отсутствует в текущем наборе", 404)
    _menu_environment_filters(menu, row["registry_id"], {})
    if row.get("prepared_source") == "menu_asset":
        try:
            content, mime = developer_units.asset_bytes(menu, row["prepared_sha256"])
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise error("developer_media_unavailable",
                        "Изображение временно недоступно", 503) from exc
        if (mime != "image/png" or
                len(content) != row.get("prepared_bytes")):
            raise error("developer_media_unavailable",
                        "Изображение временно недоступно", 503)
        media_type = mime
    else:
        try:
            from backend.developer_media_cache import MediaCacheError, get_or_prepare
        except ImportError as exc:
            raise error("developer_media_unavailable",
                        "Изображение временно недоступно", 503) from exc
        try:
            prepared = get_or_prepare(
                row, cache_root=ROOT / "data/runtime/developer_media/cache")
            if (prepared is None or prepared.path is None or
                    prepared.mime_type not in {"image/png", "image/jpeg"} or
                    prepared.sha256 is None):
                raise MediaCacheError("prepared_media_missing")
            content = prepared.path.read_bytes()
            if (len(content) != prepared.byte_length or
                    hashlib.sha256(content).hexdigest() != prepared.sha256):
                raise MediaCacheError("prepared_media_hash_mismatch")
            media_type = prepared.mime_type
        except MediaCacheError as exc:
            LOGGER.warning("Developer media unavailable: %s", exc.code)
            raise error("developer_media_unavailable",
                        "Изображение временно недоступно", 503) from exc
        except OSError as exc:
            raise error("developer_media_unavailable",
                        "Изображение временно недоступно", 503) from exc
    return Response(content, media_type=media_type,
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'; sandbox"})


def active_selected_models() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    map_snapshot = active_developer_map()
    if map_snapshot is None:
        raise error("not_found", "Модели домов недоступны для этого набора", 404)
    try:
        experience = developer_experience.current(map_snapshot)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise error("developer_models_unavailable", "Реконструкция временно недоступна", 503) from exc
    if experience is None:
        raise error("developer_models_unavailable", "Реконструкция ещё подготавливается", 503)
    return map_snapshot, experience[0], experience[1]


@app.get("/v1/developer-buildings/{registry_id}/model")
def developer_building_model(registry_id: str) -> dict[str, Any]:
    map_snapshot, menu, model_run = active_selected_models()
    if registry_id not in map_snapshot["by_registry"]:
        raise error("not_found", "Дом отсутствует в текущем наборе", 404)
    _menu_environment_filters(menu, registry_id, {})
    return public_payload({**developer_models.public_model(
        map_snapshot, registry_id, model_run, menu["manifest"]["run_id"]),
        "enrichment_run_id": (menu["enrichment"]["manifest"]["run_id"]
                              if menu.get("enrichment") else None),
        "overlay_run_id": _overlay_run_id(menu)})


@app.get("/v1/developer-model-assets/{sha256}")
def developer_model_asset(sha256: str) -> Response:
    _, _, model_run = active_selected_models()
    if model_run is None:
        raise error("not_found", "Реконструкция ещё не подготовлена", 404)
    try:
        content = developer_models.asset_bytes(model_run, sha256)
    except FileNotFoundError as exc:
        raise error("not_found", "Модель отсутствует в текущем наборе", 404) from exc
    except (OSError, ValueError) as exc:
        raise error("developer_model_asset_unavailable", "Реконструкция временно недоступна", 503) from exc
    return Response(content, media_type="model/gltf-binary",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'; sandbox"})


def background_tilesets(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """Source credited geometry shared by offer and registry maps."""
    tilesets: list[dict[str, Any]] = []
    layer_manifest = asset_manifest()
    ground_url = layer_manifest.get("ground_tileset_url")
    if ground_url is not None:
        if ground_url not in {f"/assets/{root}/tileset.json" for root in PUBLIC_GROUND_ROOTS}:
            raise RuntimeError("Invalid public ground tileset pointer")
        tilesets.append({"id": "overture-moscow-building-ground", "url": ground_url,
                         "kind": "ground_buildings", "model_quality": "cartographic_surface",
                         "label": "Контуры зданий",
                         "attribution": "© OpenStreetMap contributors; ODbL 1.0; Overture Maps"})
    if counts(catalogue)["city_buildings"]:
        city_url = layer_manifest.get("city_tileset_url")
        if isinstance(city_url, str) and city_url.startswith("/assets/"):
            demo_city = layer_manifest.get("city_source_mode") == "synthetic_demo"
            tilesets.append({"id": "overture-moscow-city", "url": city_url,
                             "kind": "city_volumes", "model_quality": "synthetic_massing",
                             "label": "Синтетические здания" if demo_city else "Объёмы зданий",
                             "attribution": "Синтетический демо-набор" if demo_city else
                                            "© OpenStreetMap contributors; ODbL 1.0; Overture Maps"})
    for kind, pointer, tile_id, quality, label in (
        ("roads", "roads_tileset_url", "moscow-road-lines", "cartographic_ribbon", "Дороги"),
        ("parks", "parks_tileset_url", "moscow-parks", "cartographic_surface", "Парки и сады"),
        ("water", "water_tileset_url", "moscow-open-water", "cartographic_surface", "Вода"),
    ):
        url = layer_manifest.get(pointer)
        if isinstance(url, str) and url.startswith("/assets/"):
            tilesets.append({"id": tile_id, "url": url, "kind": kind,
                             "model_quality": quality, "label": label,
                             "attribution": "© OpenStreetMap contributors"})
    return tilesets


@app.get("/v1/registry/map")
def registry_map(
    query: str | None = Query(default=None, max_length=1000),
    status: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None, max_length=80),
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    handover_year: int | None = None,
    handover_quarter: int | None = None,
    floors_min: int | None = None,
    floors_max: int | None = None,
    walk_metro_max_min: int | None = None,
    walk_school_max_min: int | None = None,
    walk_kindergarten_max_min: int | None = None,
    walk_medical_max_min: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
) -> dict[str, Any]:
    developer_snapshot = active_developer_map()
    if developer_snapshot is not None:
        _developer_filters(status=status, kind=kind,
                           handover_year=handover_year, handover_quarter=handover_quarter,
                           floors_min=floors_min, floors_max=floors_max,
                           walk_metro_max_min=walk_metro_max_min,
                           walk_school_max_min=walk_school_max_min,
                           walk_kindergarten_max_min=walk_kindergarten_max_min,
                           walk_medical_max_min=walk_medical_max_min)
        selected, unit_counts, unit_run_id, applied, overlay, menu = _developer_list(
            developer_snapshot, query, district=district, okrug_id=okrug_id,
            max_noise_index=max_noise_index, max_air_index=max_air_index,
            price_min_rub=price_min_rub,
            price_max_rub=price_max_rub,
            price_per_m2_min_rub=price_per_m2_min_rub,
            price_per_m2_max_rub=price_per_m2_max_rub,
            rooms=rooms, area_min_m2=area_min_m2, area_max_m2=area_max_m2,
            unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
        mapped = [row for row in selected if row["map_eligible"]]
        manifest = developer_snapshot["manifest"]
        tilesets = background_tilesets(load_catalogue())
        geometry = menu.get("overlay") if menu else None
        if isinstance(geometry, dict) and geometry.get("city_patch") is not None:
            patch_url = (f"/v1/developer-overlay/{geometry['manifest']['run_id']}"
                         "/city-patch/tileset.json")
            tilesets = [{**tile, "url": patch_url} if tile.get("kind") == "city_volumes"
                        else tile for tile in tilesets]
        return public_payload({
            "dataset_id": "developer_building_registry", "snapshot_id": manifest["snapshot_id"],
            "map_run_id": manifest["run_id"],
            "unit_run_id": unit_run_id,
            "enrichment_run_id": overlay["manifest"]["run_id"] if overlay else None,
            "overlay_run_id": _overlay_run_id(menu),
            "snapshot_at": _developer_as_of(developer_snapshot)[:10],
            "center": [37.620, 55.750], "tilesets": tilesets,
            "items": [{**developer_map.public_building(row),
                       **_developer_public_environment(row, overlay),
                       **_developer_public_structure(row, menu),
                       "matching_units_count": unit_counts[row["registry_id"]]}
                      for row in mapped],
            "counts": {"records": len(selected), "geocoded": len(mapped), "offers": 0,
                       "source_records": manifest["buildings_total"], "archived_records": 0,
                       "city_buildings": asset_manifest().get("city_buildings", 0), "facade_models": 0},
            "applied_filters": applied,
        })
    _legacy_unit_filter_guard(
        okrug_id=okrug_id, max_noise_index=max_noise_index,
        max_air_index=max_air_index,
        price_min_rub=price_min_rub, price_max_rub=price_max_rub,
        price_per_m2_min_rub=price_per_m2_min_rub,
        price_per_m2_max_rub=price_per_m2_max_rub, rooms=rooms,
        area_min_m2=area_min_m2, area_max_m2=area_max_m2,
        unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
    registry = active_registry()
    selected = _selected_registry(registry, query=query, status=status, kind=kind, district=district,
                                  handover_year=handover_year, handover_quarter=handover_quarter,
                                  floors_min=floors_min, floors_max=floors_max,
                                  walk_metro_max_min=walk_metro_max_min,
                                  walk_school_max_min=walk_school_max_min,
                                  walk_kindergarten_max_min=walk_kindergarten_max_min,
                                  walk_medical_max_min=walk_medical_max_min)
    marked = [item for item in selected if item["location"] is not None]
    return public_payload({
        "dataset_id": registry["dataset_id"], "snapshot_id": registry["snapshot_id"],
        "snapshot_at": registry["snapshot_at"], "center": [37.620, 55.750],
        "tilesets": background_tilesets(load_catalogue()), "items": marked,
        "counts": {"records": len(selected), "geocoded": len(marked), "offers": 0,
                   "source_records": registry["visibility"]["source_total"],
                   "archived_records": registry["visibility"]["archived_total"],
                   "city_buildings": asset_manifest().get("city_buildings", 0), "facade_models": 0},
        "applied_filters": {key: value for key, value in
                            (("query", query), ("status", status), ("kind", kind), ("district", district),
                             ("handover_year", handover_year), ("handover_quarter", handover_quarter),
                             ("floors_min", floors_min), ("floors_max", floors_max),
                             ("walk_metro_max_min", walk_metro_max_min),
                             ("walk_school_max_min", walk_school_max_min),
                             ("walk_kindergarten_max_min", walk_kindergarten_max_min),
                             ("walk_medical_max_min", walk_medical_max_min)) if value is not None},
    })


@app.get("/v1/complexes/{complex_id}")
def complex_detail(complex_id: str) -> dict[str, Any]:
    catalogue = load_catalogue()
    item = next((item for item in catalogue["complexes"] if item["complex_id"] == complex_id), None)
    if item is None:
        raise error("not_found", "Complex not found", 404)
    return public_payload(item)


@app.get("/v1/offers/{offer_id}")
def offer_detail(offer_id: str) -> dict[str, Any]:
    catalogue = load_catalogue()
    eligible = engine().eligible_offers(catalogue["offers"], snapshot_at=catalogue["snapshot_at"], data_mode=catalogue["data_mode"])
    item = next((item for item in eligible if item["offer_id"] == offer_id), None)
    if item is None:
        raise error("not_found", "Offer not found", 404)
    return public_payload(item)


@app.post("/v1/search")
def search(request: QueryRequest) -> dict[str, Any]:
    return query_engine("search", request, load_catalogue())


@app.post("/v1/recommendations")
def recommendations(request: QueryRequest) -> dict[str, Any]:
    return query_engine("recommend", request, load_catalogue())


@app.post("/v1/compare")
def compare(request: CompareRequest) -> dict[str, Any]:
    catalogue = load_catalogue()
    eligible = engine().eligible_offers(catalogue["offers"], snapshot_at=catalogue["snapshot_at"], data_mode=catalogue["data_mode"])
    by_id = {item["offer_id"]: item for item in eligible}
    missing = [offer_id for offer_id in request.offer_ids if offer_id not in by_id]
    if missing:
        raise error("not_found", f"Offer IDs not found: {', '.join(missing)}", 404)
    return public_payload({
        "snapshot_id": catalogue["snapshot_id"],
        "data_mode": catalogue["data_mode"],
        "items": [by_id[offer_id] for offer_id in request.offer_ids],
        "fields": ["price", "area_sqm", "rooms", "floor", "district_id", "source", "availability"],
    })


@app.post("/v1/events")
def events(request: EventRequest) -> dict[str, bool]:
    AnalyticsStore(ROOT / "data/runtime/analytics.sqlite3").record_event(request.event_type, request.session_id)
    return {"accepted": True}


@app.get("/v1/metrics")
def metrics() -> dict[str, Any]:
    return AnalyticsStore(ROOT / "data/runtime/analytics.sqlite3").metrics()


@app.get("/v1/map")
def map_view(
    request: Request,
    query: str | None = Query(default=None, max_length=1000),
    budget_min_rub: int | None = None,
    budget_max_rub: int | None = None,
    district_ids: list[str] | None = Query(default=None),
    complex_ids: list[str] | None = Query(default=None),
    rooms: list[int] | None = Query(default=None),
    area_min_sqm: float | None = None,
    area_max_sqm: float | None = None,
    price_basis: Literal["asking_total", "conditional_total"] | None = None,
    predicates: str | None = Query(default=None, max_length=20000),
) -> dict[str, Any]:
    unsupported = set(request.query_params) - {"query", "budget_min_rub", "budget_max_rub", "district_ids", "complex_ids", "rooms", "area_min_sqm", "area_max_sqm", "price_basis", "predicates"}
    if unsupported:
        raise error("invalid_request", f"Unsupported map filters: {', '.join(sorted(unsupported))}", 422)
    parsed_predicates = None
    if predicates is not None:
        try:
            parsed_predicates = json.loads(predicates)
        except json.JSONDecodeError as exc:
            raise error("invalid_request", "predicates must be a JSON array", 422) from exc
    catalogue = load_catalogue()
    try:
        filters = Filters(
            budget_min_rub=budget_min_rub, budget_max_rub=budget_max_rub,
            district_ids=district_ids, complex_ids=complex_ids, rooms=rooms,
            area_min_sqm=area_min_sqm, area_max_sqm=area_max_sqm,
            price_basis=price_basis, predicates=parsed_predicates,
        ).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise error("invalid_request", str(exc.errors()[0]["msg"]), 422) from exc
    validate_district_ids(filters, catalogue)
    recommendation_engine = engine()
    try:
        resolved = recommendation_engine.resolve_filters({"query": query, "filters": filters}, catalogue["districts"])
    except ValueError as exc:
        raise error("invalid_request", str(exc), 422) from exc
    filters = resolved["filters"]
    clarification = resolved["clarification"]
    if clarification:
        return public_payload({
            "snapshot_id": catalogue["snapshot_id"], "data_mode": catalogue["data_mode"],
            "center": [37.620, 55.750], "tilesets": background_tilesets(catalogue), "complexes": [],
            "counts": {"districts": 0, "complexes": 0, "offers": 0,
                       "city_buildings": counts(catalogue)["city_buildings"], "facade_models": 0},
            "applied_filters": filters, "clarification": clarification,
        })
    eligible = recommendation_engine.eligible_offers(catalogue["offers"], snapshot_at=catalogue["snapshot_at"], data_mode=catalogue["data_mode"])
    matched = recommendation_engine.filter_offers(eligible, filters, data_mode=catalogue["data_mode"])
    by_complex = Counter(item["complex_id"] for item in matched)
    asking_min: dict[str, int] = {}
    for item in matched:
        price = item.get("price")
        if price and price["basis"] == "asking_total":
            cid = item["complex_id"]
            asking_min[cid] = min(asking_min.get(cid, price["amount_rub"]), price["amount_rub"])
    complexes = [
        {**item, "offer_count": by_complex[item["complex_id"]], "min_price_rub": asking_min.get(item["complex_id"])}
        for item in catalogue["complexes"] if by_complex[item["complex_id"]]
    ]
    tilesets = background_tilesets(catalogue)
    if catalogue["data_mode"] == "demo":
        for item in complexes:
            if item["facade_tileset_url"]:
                tilesets.append({"id": item["complex_id"], "url": item["facade_tileset_url"], "kind": "facade", "model_quality": "synthetic_facade", "label": f"Синтетическая модель {item['name']}", "attribution": "Generated synthetic geometry and texture"})
    return public_payload({
        "snapshot_id": catalogue["snapshot_id"],
        "data_mode": catalogue["data_mode"],
        "center": [37.620, 55.750],
        "tilesets": tilesets,
        "complexes": complexes,
        "counts": {
            "districts": len({item["district_id"] for item in complexes if item["district_id"]}),
            "complexes": len(complexes), "offers": len(matched),
            "city_buildings": counts(catalogue)["city_buildings"],
            "facade_models": sum(tile["kind"] == "facade" for tile in tilesets),
        },
        "applied_filters": filters,
        "clarification": None,
    })


def _active_developer_overlay() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    map_snapshot, menu = active_unit_menu()
    geometry = menu.get("overlay")
    if geometry is None:
        raise error("developer_overlay_unavailable",
                    "Проверенная геометрия домов ещё подготавливается", 503)
    return map_snapshot, menu, geometry


@app.get("/v1/developer-overlay/viewport")
def developer_overlay_viewport(
    request: Request,
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    query: str | None = Query(default=None, max_length=1000),
    district: str | None = None,
    okrug_id: str | None = None,
    max_noise_index: int | None = None,
    max_air_index: int | None = None,
    price_min_rub: int | None = None,
    price_max_rub: int | None = None,
    price_per_m2_min_rub: int | None = None,
    price_per_m2_max_rub: int | None = None,
    rooms: int | None = None,
    area_min_m2: float | None = None,
    area_max_m2: float | None = None,
    unit_floor_min: int | None = None,
    unit_floor_max: int | None = None,
    limit: int = Query(default=1200, ge=1, le=1500),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict[str, Any]:
    """Return filter-eligible IDs for a bounded, batch-tiled city viewport."""
    if west >= east or south >= north or east - west > 10 or north - south > 10:
        raise error("invalid_request", "Неверные границы области карты", 422)
    allowed = {"west", "south", "east", "north", "query", "district", "okrug_id",
               "max_noise_index", "max_air_index", "price_min_rub", "price_max_rub",
               "price_per_m2_min_rub", "price_per_m2_max_rub", "rooms", "area_min_m2",
               "area_max_m2", "unit_floor_min", "unit_floor_max", "limit", "offset"}
    unknown = set(request.query_params) - allowed
    if unknown:
        raise error("invalid_request", "Неподдерживаемые фильтры карты", 422)
    map_snapshot, menu, geometry = _active_developer_overlay()
    selected, _, _, applied, _, matched_menu = _developer_list(
        map_snapshot, query, district=district, okrug_id=okrug_id,
        max_noise_index=max_noise_index, max_air_index=max_air_index,
        price_min_rub=price_min_rub, price_max_rub=price_max_rub,
        price_per_m2_min_rub=price_per_m2_min_rub,
        price_per_m2_max_rub=price_per_m2_max_rub, rooms=rooms,
        area_min_m2=area_min_m2, area_max_m2=area_max_m2,
        unit_floor_min=unit_floor_min, unit_floor_max=unit_floor_max)
    if matched_menu is None or _overlay_run_id(matched_menu) != geometry["manifest"]["run_id"]:
        raise error("snapshot_mismatch", "Снимки домов и геометрии не совпадают", 503)
    def intersects(bounds: list[float]) -> bool:
        return bounds[0] <= east and bounds[2] >= west and bounds[1] <= north and bounds[3] >= south
    eligible = sorted(row["registry_id"] for row in selected if row["map_eligible"] and
                      intersects(geometry["features"][row["registry_id"]]["bounds_lonlat"]))
    overlay_id = geometry["manifest"]["run_id"]
    return public_payload({
        "dataset_id": "developer_source_geometry_overlay",
        "registry_snapshot_id": map_snapshot["manifest"]["snapshot_id"],
        "map_run_id": map_snapshot["manifest"]["run_id"],
        "unit_run_id": menu["manifest"]["run_id"],
        "enrichment_run_id": menu["enrichment"]["manifest"]["run_id"],
        "overlay_run_id": overlay_id,
        "tileset_url": f"/v1/developer-overlay/{overlay_id}/tileset.json",
        "city_patch_url": (f"/v1/developer-overlay/{overlay_id}/city-patch/tileset.json"
                           if geometry.get("city_patch") is not None else None),
        "lake_persistent": geometry["manifest"].get("lake_persistent") is True,
        "lake_registry_id": (geometry["city_patch"]["lake_registry_id"]
                             if geometry.get("city_patch") else None),
        "lake_persistent_model_sha256": (
            geometry["manifest"].get("lake_persistent_model_sha256")
            if geometry["manifest"].get("lake_persistent") is True else None),
        "facade_style": ({
            "basis": "project_style_inference",
            "label": "Реконструкция",
            "styled_projects_total": geometry["facade_styles"]["styled_projects_total"],
            "styled_persistent_features_total":
                geometry["facade_styles"]["styled_persistent_features_total"],
            "texture_level": "near",
        } if geometry.get("facade_styles") else None),
        "bbox": [west, south, east, north],
        "features_total": geometry["manifest"]["features_total"],
        "matched_total": len(eligible), "limit": limit, "offset": offset,
        "truncated": offset + limit < len(eligible),
        "eligible_registry_ids": eligible[offset:offset + limit],
        "applied_filters": applied,
    })


@app.get("/v1/developer-overlay/{overlay_run_id}/tileset.json")
def developer_overlay_tileset(overlay_run_id: str) -> Response:
    _, _, geometry = _active_developer_overlay()
    if overlay_run_id != geometry["manifest"]["run_id"]:
        raise error("not_found", "Набор геометрии неактивен", 404)
    return Response(json.dumps(geometry["tileset"], ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"),
                    media_type="application/json",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/v1/developer-overlay/{overlay_run_id}/tiles/{sha256}.b3dm")
def developer_overlay_tile(overlay_run_id: str, sha256: str) -> Response:
    _, _, geometry = _active_developer_overlay()
    if overlay_run_id != geometry["manifest"]["run_id"]:
        raise error("not_found", "Набор геометрии неактивен", 404)
    try:
        content = developer_overlay.tile_bytes(geometry, sha256)
    except FileNotFoundError as exc:
        raise error("not_found", "Плитка геометрии отсутствует", 404) from exc
    except (OSError, ValueError) as exc:
        raise error("developer_overlay_unavailable",
                    "Проверенная геометрия временно недоступна", 503) from exc
    return Response(content, media_type="application/octet-stream",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/v1/developer-overlay/{overlay_run_id}/city-patch/tileset.json")
def developer_city_patch_tileset(overlay_run_id: str) -> Response:
    _, _, geometry = _active_developer_overlay()
    patch = geometry.get("city_patch")
    if overlay_run_id != geometry["manifest"]["run_id"] or patch is None:
        raise error("not_found", "Патч городской геометрии неактивен", 404)
    return Response(json.dumps(patch["tileset"], ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"),
                    media_type="application/json",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/v1/developer-overlay/{overlay_run_id}/city-patch/regions/{sha256}.json")
def developer_city_patch_region(overlay_run_id: str, sha256: str) -> Response:
    _, _, geometry = _active_developer_overlay()
    if overlay_run_id != geometry["manifest"]["run_id"]:
        raise error("not_found", "Патч городской геометрии неактивен", 404)
    try:
        content = developer_overlay.city_patch_bytes(geometry, "region", sha256)
    except FileNotFoundError as exc:
        raise error("not_found", "Патч городской геометрии отсутствует", 404) from exc
    except (OSError, ValueError) as exc:
        raise error("developer_overlay_unavailable",
                    "Патч городской геометрии временно недоступен", 503) from exc
    return Response(content, media_type="application/json",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/v1/developer-overlay/{overlay_run_id}/city-patch/tiles/{sha256}.glb")
def developer_city_patch_tile(overlay_run_id: str, sha256: str) -> Response:
    _, _, geometry = _active_developer_overlay()
    if overlay_run_id != geometry["manifest"]["run_id"]:
        raise error("not_found", "Патч городской геометрии неактивен", 404)
    try:
        content = developer_overlay.city_patch_bytes(geometry, "tile", sha256)
    except FileNotFoundError as exc:
        raise error("not_found", "Плитка патча отсутствует", 404) from exc
    except (OSError, ValueError) as exc:
        raise error("developer_overlay_unavailable", "Плитка патча временно недоступна", 503) from exc
    return Response(content, media_type="model/gltf-binary",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate",
                             "X-Content-Type-Options": "nosniff"})
