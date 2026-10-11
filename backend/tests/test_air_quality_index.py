from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from backend.air_quality_index import (
    SCALE_ID,
    compute_air_quality_index,
)


NOW = datetime(2026, 10, 11, 0, 16, tzinfo=timezone.utc)


def _model(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "model_basis": "open_meteo_cams_europe_grid_cell_forecast",
        "sample_scope": "requested_district_candidate_point_to_returned_grid_cell",
        "requested_point_lon_lat": [37.52, 55.87],
        "returned_grid_center_lon_lat": [37.5, 55.9],
        "forecast_at": "2026-10-11T00:00:00Z",
        "retrieved_at": "2026-10-10T23:15:31Z",
        "data_stale_after_utc": "2026-10-12T00:00:00Z",
        "pm2_5_ug_m3": 7.7,
        "pm10_ug_m3": 11.0,
        "no2_ug_m3": 18.6,
        "measured_district_average": False,
        "attribution": "Open-Meteo.com (CC BY 4.0) / CAMS European Air Quality Forecast",
        "source_response_sha256": "a" * 64,
    }
    model.update(overrides)
    return model


@pytest.mark.parametrize(
    ("concentrations", "expected", "dominant"),
    [
        ({"pm2_5_ug_m3": 4.8, "pm10_ug_m3": 6.1, "no2_ug_m3": 2.5},
         19, ["pm2_5"]),
        ({"pm2_5_ug_m3": 7.7, "pm10_ug_m3": 11.0, "no2_ug_m3": 18.6},
         31, ["no2"]),
        ({"pm2_5_ug_m3": 18.6, "pm10_ug_m3": 23.1, "no2_ug_m3": 35.4},
         46, ["no2"]),
    ],
)
def test_three_snapshot_fixtures_have_expected_score_and_driver(
    concentrations: dict[str, float], expected: int, dominant: list[str]
) -> None:
    result = compute_air_quality_index(_model(**concentrations), now=NOW)

    assert result["status"] == "modeled_forecast"
    assert result["scale_id"] == SCALE_ID
    assert result["value"] == expected
    assert result["dominant_pollutants"] == dominant
    assert result["higher_is_worse"] is True
    assert result["official_index"] is False
    assert result["measurement"] is False
    assert result["measured_district_average"] is False


def test_integer_rounding_is_half_up_and_concentrations_clamp() -> None:
    half_tie = compute_air_quality_index(
        _model(pm2_5_ug_m3=1.125, pm10_ug_m3=0, no2_ug_m3=0), now=NOW
    )
    high_clamped = compute_air_quality_index(
        _model(pm2_5_ug_m3=1000, pm10_ug_m3=0, no2_ug_m3=0), now=NOW
    )

    assert half_tie["component_scores"]["pm2_5"] == 4.5
    assert half_tie["value"] == 5
    assert high_clamped["component_scores"]["pm2_5"] == 100.0
    assert high_clamped["value"] == 100


def test_dominant_pollutant_uses_unrounded_component_scores() -> None:
    result = compute_air_quality_index(
        _model(pm2_5_ug_m3=5, pm10_ug_m3=0, no2_ug_m3=10.00001), now=NOW
    )

    assert result["component_scores"]["pm2_5"] == result["component_scores"]["no2"]
    assert result["dominant_pollutants"] == ["no2"]


def test_incomplete_pollutant_set_is_unknown_not_zero() -> None:
    result = compute_air_quality_index(_model(no2_ug_m3=None), now=NOW)

    assert result["status"] == "incomplete"
    assert result["value"] is None
    assert result["component_scores"]["no2"] is None
    assert result["dominant_pollutants"] == []
    assert result["reason"] == "missing_pollutant"


def test_stale_sample_keeps_identity_but_has_no_current_score() -> None:
    model = _model(data_stale_after_utc="2026-10-11T00:15:00Z")
    result = compute_air_quality_index(model, now=NOW)

    assert result["status"] == "stale"
    assert result["value"] is None
    assert result["source_identity"]["source_response_sha256"] == "a" * 64
    assert result["valid_at"] == model["forecast_at"]
    assert result["retrieved_at"] == model["retrieved_at"]
    assert result["grid_cell_center"] == [37.5, 55.9]


def test_missing_sample_and_nonforecast_source_are_unavailable() -> None:
    missing = compute_air_quality_index(None, now=NOW)
    measured = compute_air_quality_index(
        _model(measured_district_average=True), now=NOW
    )

    assert missing["status"] == "unavailable" and missing["value"] is None
    assert missing["reason"] == "no_model_sample"
    assert measured["status"] == "unavailable" and measured["value"] is None
    assert measured["reason"] == "unsupported_or_non_grid_source"


def test_function_does_not_mutate_source_and_derives_legacy_stale_cutoff() -> None:
    model = _model()
    model.pop("data_stale_after_utc")
    before = deepcopy(model)
    result = compute_air_quality_index(model, now=NOW)

    assert model == before
    assert result["status"] == "modeled_forecast"
    assert result["stale_after"] == "2026-10-12T00:00:00Z"


def test_malformed_value_or_timestamp_does_not_produce_index() -> None:
    malformed_value = compute_air_quality_index(
        _model(pm10_ug_m3=float("nan")), now=NOW
    )
    malformed_time = compute_air_quality_index(
        _model(retrieved_at="not-a-time"), now=NOW
    )

    assert malformed_value["status"] == "unavailable"
    assert malformed_value["value"] is None
    assert malformed_value["reason"] == "invalid_concentration"
    assert malformed_time["status"] == "unavailable"
    assert malformed_time["value"] is None
    assert malformed_time["reason"] == "invalid_or_missing_timestamp"


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_air_quality_index(_model(), now=NOW.replace(tzinfo=None))
