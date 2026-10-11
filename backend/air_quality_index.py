"""Internal 0–100 air pollution score derived from dated CAMS grid forecasts.

This is not the official European Air Quality Index. It reuses the EEA hourly
concentration band anchors for three available pollutants and maps the six
bands to six evenly spaced score anchors. The EEA index uses up to five
pollutants and the worst pollutant; this internal partial score uses PM2.5,
PM10, and NO2 only.

Primary methodology source: https://airindex.eea.europa.eu/AQI/?webgl=0
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any


SCALE_ID = "internal_3pollutant_0_100_v1"
MODEL_BASIS = "open_meteo_cams_europe_grid_cell_forecast"
SAMPLE_SCOPE = "requested_district_candidate_point_to_returned_grid_cell"

# EEA category upper-bound anchors, µg/m³. Each interval maps to 20 score
# points, producing a continuous internal scale rather than an official EAQI.
_POLLUTANTS: tuple[tuple[str, str, tuple[float, ...]], ...] = (
    ("pm2_5_ug_m3", "pm2_5", (0, 5, 15, 50, 90, 140)),
    ("pm10_ug_m3", "pm10", (0, 15, 45, 120, 195, 270)),
    ("no2_ug_m3", "no2", (0, 10, 25, 60, 100, 150)),
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _score_concentration(value: float, anchors: tuple[float, ...]) -> float:
    """Linearly map consecutive EEA concentration anchors to 0,20,...,100."""
    if value <= anchors[0]:
        return 0.0
    if value >= anchors[-1]:
        return 100.0
    for index, (lower, upper) in enumerate(zip(anchors, anchors[1:])):
        if value <= upper:
            return 20.0 * index + 20.0 * (value - lower) / (upper - lower)
    # The upper-bound branch above handles all finite values.
    return 100.0


def _rounded_score(value: float) -> int:
    # Python's round() uses ties-to-even; the public integer score uses the
    # more predictable half-up rule for nonnegative scores.
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _empty_components() -> dict[str, float | None]:
    return {public_name: None for _, public_name, _ in _POLLUTANTS}


def _source_metadata(model: Any) -> dict[str, Any]:
    """Copy dated/source identity fields without changing the input mapping."""
    if model is None:
        source: Mapping[str, Any] = {}
    elif isinstance(model, Mapping):
        source = model
    else:
        source = {}
    stale_after = source.get("data_stale_after_utc")
    if not isinstance(stale_after, str):
        forecast = _parse_timestamp(source.get("forecast_at"))
        stale_after = ((forecast + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
                       if forecast is not None else None)
    center = source.get("returned_grid_center_lon_lat")
    if (not isinstance(center, (list, tuple)) or len(center) != 2 or
            any(type(value) not in {int, float} or not math.isfinite(value)
                for value in center) or
            not -180 <= center[0] <= 180 or not -90 <= center[1] <= 90):
        center = None
    else:
        center = [float(center[0]), float(center[1])]
    resolution = source.get("cell_resolution_km_approx")
    if (type(resolution) not in {int, float} or not math.isfinite(resolution) or
            resolution <= 0):
        resolution = None
    return {
        "valid_at": source.get("forecast_at") if isinstance(source.get("forecast_at"), str) else None,
        "retrieved_at": source.get("retrieved_at") if isinstance(source.get("retrieved_at"), str) else None,
        "stale_after": stale_after,
        "spatial_scope": source.get("sample_scope") if isinstance(source.get("sample_scope"), str) else None,
        "grid_cell_center": center,
        "cell_resolution_km_approx": float(resolution) if resolution is not None else None,
        # Kept in the private pure result so the integration layer can apply
        # its own public allowlist and preserve source lineage internally.
        "source_identity": {
            key: source.get(key)
            for key in ("model_basis", "sample_scope", "source_response_sha256", "attribution")
            if source.get(key) is not None
        },
        "measurement": False,
        "measured_district_average": False,
        "official_index": False,
    }


def compute_air_quality_index(
    air_model: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic internal score, or a truthful null state.

    A score is produced only for a current, supported CAMS grid-cell forecast
    with valid values for all three pollutants. Missing, malformed, or stale
    source data never becomes a zero score. The input is not mutated.
    """
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    reference_time = reference_time.astimezone(timezone.utc)

    result: dict[str, Any] = {
        "status": "unavailable",
        "value": None,
        "scale_id": SCALE_ID,
        "higher_is_worse": True,
        "component_scores": _empty_components(),
        "dominant_pollutants": [],
        "reason": None,
        **_source_metadata(air_model),
    }
    if air_model is None:
        result["reason"] = "no_model_sample"
        return result
    if not isinstance(air_model, Mapping):
        result["reason"] = "invalid_model_record"
        return result
    if (air_model.get("model_basis") != MODEL_BASIS or
            air_model.get("sample_scope") != SAMPLE_SCOPE or
            air_model.get("measured_district_average") is not False):
        result["reason"] = "unsupported_or_non_grid_source"
        return result

    forecast_at = _parse_timestamp(air_model.get("forecast_at"))
    retrieved_at = _parse_timestamp(air_model.get("retrieved_at"))
    stale_after = _parse_timestamp(air_model.get("data_stale_after_utc"))
    if stale_after is None and forecast_at is not None:
        stale_after = forecast_at + timedelta(hours=24)
        result["stale_after"] = stale_after.isoformat().replace("+00:00", "Z")
    if forecast_at is None or retrieved_at is None or stale_after is None:
        result["reason"] = "invalid_or_missing_timestamp"
        return result
    if reference_time > stale_after:
        result["status"] = "stale"
        result["reason"] = "forecast_expired"
        return result

    concentrations: list[tuple[str, float | None, tuple[float, ...]]] = []
    invalid_concentration = False
    missing_concentration = False
    for source_name, public_name, anchors in _POLLUTANTS:
        raw = air_model.get(source_name)
        if raw is None:
            missing_concentration = True
            concentrations.append((public_name, None, anchors))
        elif (type(raw) not in {int, float} or not math.isfinite(raw) or
              not 0 <= raw <= 1000):
            invalid_concentration = True
            concentrations.append((public_name, None, anchors))
        else:
            concentrations.append((public_name, float(raw), anchors))

    if invalid_concentration:
        result["reason"] = "invalid_concentration"
        return result

    raw_component_scores: dict[str, float | None] = {}
    for public_name, concentration, anchors in concentrations:
        raw_component_scores[public_name] = (
            _score_concentration(concentration, anchors)
            if concentration is not None else None
        )
    component_scores = {
        name: round(value, 4) if value is not None else None
        for name, value in raw_component_scores.items()
    }
    result["component_scores"] = component_scores
    if missing_concentration:
        result["status"] = "incomplete"
        result["reason"] = "missing_pollutant"
        return result

    maximum = max(value for value in raw_component_scores.values() if value is not None)
    result["value"] = _rounded_score(maximum)
    result["dominant_pollutants"] = [
        public_name for public_name, value in raw_component_scores.items()
        if value is not None and abs(value - maximum) < 1e-9
    ]
    result["status"] = "modeled_forecast"
    return result
