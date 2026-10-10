"""Focused evidence and visibility gates for the enrichment producer."""

from __future__ import annotations

import pytest

from sources.property.developer_enrichment import (
    _air_model, _building_overlay, _check_air_identity, _check_geography_identity,
    _check_noise_identity,
)


POINT = {"registry_id": "registry-123", "map_eligible": True}


def test_unlocated_is_unknown_not_outside_or_quiet() -> None:
    row = _building_overlay({**POINT, "map_eligible": False}, None, None)
    assert row["municipality_scope"] == "uncertain"
    assert row["visibility_eligible"] is True
    assert row["noise_index"] is None
    assert row["noise_coverage"] == "unlocated"


def test_boundary_contradiction_cannot_hide_mapped_building() -> None:
    with pytest.raises(ValueError, match="contradicts pinned Moscow point"):
        _building_overlay(POINT, {
            "municipality_scope": "outside",
            "evidence_basis": "verified_boundary_polygon",
        }, None)


def test_unlocated_outside_requires_exact_evidence() -> None:
    unlocated = {**POINT, "map_eligible": False}
    with pytest.raises(ValueError, match="lacks exact evidence"):
        _building_overlay(unlocated, {
            "municipality_scope": "outside", "evidence_basis": "nearby_name"
        }, None)
    row = _building_overlay(unlocated, {
        "municipality_scope": "outside",
        "evidence_basis": "verified_source_address_boundary",
    }, None)
    assert row["visibility_eligible"] is False


def test_noise_zero_is_observed_score_not_unknown() -> None:
    row = _building_overlay(POINT, None, {
        "noise_index": 0,
        "noise_coverage": "covered",
        "noise_basis": "internal_osm_proximity_v1",
        "noise_inputs": {"nearest_major_road_m": 2000},
    })
    assert row["noise_index"] == 0
    assert row["noise_coverage"] == "covered"
    with pytest.raises(ValueError, match="score/coverage mismatch"):
        _building_overlay(POINT, None, {"noise_index": 0, "noise_coverage": "unknown"})
    with pytest.raises(ValueError, match="Invalid noise index"):
        _building_overlay(POINT, None, {"noise_index": 101, "noise_coverage": "covered"})


def test_geography_join_needs_exact_point_and_publisher_identity() -> None:
    point = {**POINT, "point_id": "mappt_123", "location": [37.5, 55.7],
             "publisher_project_id": "project-a", "publisher_building_id": "building-a"}
    row = {"point_id": "mappt_123", "lon": 37.5, "lat": 55.7,
           "publisher_project_id": "project-a", "publisher_building_id": "building-a"}
    _check_geography_identity({"registry-123": point}, {"registry-123": row})
    with pytest.raises(ValueError, match="exact map point"):
        _check_geography_identity({"registry-123": point},
                                  {"registry-123": {**row, "lon": 37.50001}})
    with pytest.raises(ValueError, match="exact map point"):
        _check_geography_identity({"registry-123": point},
                                  {"registry-123": {**row, "publisher_building_id": "building-b"}})


def test_noise_needs_exact_location_and_discards_raw_tags() -> None:
    point = {**POINT, "location": [37.5, 55.7]}
    noise = {"noise_index": 45, "noise_coverage": "covered",
             "noise_basis": "internal_osm_proximity_v1",
             "noise_inputs": {
                 "location_lon_lat": [37.5, 55.7],
                 "components": {"railway": {
                     "distance_m": 200, "component_points": 20,
                     "nearest_source": {"source_id": "osm:way:123",
                                        "source_tags": {"name": "large raw evidence"}},
                 }},
             }}
    _check_noise_identity({"registry-123": point}, {"registry-123": noise})
    projected = _building_overlay(point, None, noise)
    assert projected["noise_index"] == 45
    assert projected["noise_inputs"]["components"]["railway"]["nearest_source"][
        "source_id"] == "osm:way:123"
    assert "source_tags" not in str(projected["noise_inputs"])
    with pytest.raises(ValueError, match="exact map point"):
        _check_noise_identity({"registry-123": point},
                              {"registry-123": {**noise, "noise_inputs": {
                                  **noise["noise_inputs"], "location_lon_lat": [37.6, 55.7]}}})


def test_air_model_is_forecast_at_grid_cell_not_district_measurement() -> None:
    row = {"district_id": "district:example",
           "value_status": "modeled_forecast",
           "model_basis": "open_meteo_cams_europe_grid_cell_forecast",
           "sample_scope": "requested_district_candidate_point_to_returned_grid_cell",
           "requested_point_lon_lat": [37.5, 55.7],
           "returned_grid_center_lon_lat": [37.6, 55.8],
           "forecast_at": "2026-10-11T01:00:00+03:00",
           "retrieved_at": "2026-10-10T22:00:00Z",
           "expires_at_utc": "2026-10-10T23:00:00Z",
           "data_stale_after_utc": "2026-10-11T01:00:00+03:00",
           "pm2_5_ug_m3": 19.8, "pm10_ug_m3": 26.4, "no2_ug_m3": 34.9,
           "measured_district_average": False,
           "attribution": "Open-Meteo.com (CC BY 4.0)",
           "source_response_sha256": "a" * 64}
    model = _air_model(row)
    assert model is not None and model["pm2_5_ug_m3"] == 19.8
    assert model["measured_district_average"] is False
    assert model["request_refresh_after_utc"] == "2026-10-10T23:00:00Z"
    assert model["data_stale_after_utc"] == "2026-10-11T01:00:00+03:00"
    with pytest.raises(ValueError, match="not a labeled grid-cell forecast"):
        _air_model({**row, "measured_district_average": True})
    with pytest.raises(ValueError, match="no timezone"):
        _air_model({**row, "forecast_at": "2026-10-11T01:00:00"})
    with pytest.raises(ValueError, match="no timezone"):
        _air_model({**row, "expires_at_utc": "2026-10-10T23:00:00"})


def test_air_district_identity_is_exact() -> None:
    geography = {"registry-123": {"district_id": "district:example",
                                   "district_name": "Example",
                                   "okrug_id": "okrug:example"}}
    air = {"district:example": {"district_id": "district:example",
                                "district_name": "Example",
                                "okrug_id": "okrug:example"}}
    _check_air_identity(geography, air)
    with pytest.raises(ValueError, match="exact district identity"):
        _check_air_identity(geography, {"district:example": {
            **air["district:example"], "okrug_id": "okrug:other"}})
