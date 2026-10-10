"""Unit tests for source-safe district CAMS candidate rows."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from sources.property.environment_air import (
    POLLUTANT_FIELDS,
    _write_enrichment_handoff,
    _write_json,
    _write_jsonl,
    _polygon_name_key,
    build_request_url,
    parse_model_response,
    sha256_file,
    sha256_bytes,
)


def _point(district_id: str, name: str, longitude: float) -> dict:
    return {
        "district_id": district_id,
        "district_name": name,
        "okrug_id": "okrug:central administrative",
        "okrug_name": "Центральный административный округ",
        "requested_point_lon_lat": [longitude, 55.75],
        "point_method": "synthetic representative point",
        "polygon_source_name": name,
        "polygon_source_id": "synthetic-" + district_id,
    }


def test_candidate_polygon_names_match_official_district_keys() -> None:
    assert _polygon_name_key("район Бирюлёво Восточное") == "бирюлево восточное"
    assert _polygon_name_key("Академический район") == "академический"
    assert _polygon_name_key("район Тропарево-Никулино") == "тропарево-никулино"


def test_request_url_is_bounded_and_uses_cams_hourly_variables() -> None:
    points = [_point("district:a", "А", 37.5), _point("district:b", "Б", 37.6)]
    url = build_request_url(points)
    assert url.startswith("https://air-quality-api.open-meteo.com/v1/air-quality?")
    assert "domains=cams_europe" in url
    assert "forecast_days=2" in url
    assert all(variable in url for variable in POLLUTANT_FIELDS)
    assert len(url) < 12_000


def test_parser_selects_future_hour_and_preserves_shared_grid_cell() -> None:
    points = [_point("district:a", "А", 37.5), _point("district:b", "Б", 37.6)]
    times = ["2026-10-11T02:00", "2026-10-11T03:00"]
    responses = []
    for value in (10.0, 12.0):
        responses.append({
            "latitude": 55.8,
            "longitude": 37.6,
            "timezone": "Europe/Moscow",
            "hourly_units": {"time": "iso8601", **{key: "μg/m³" for key in POLLUTANT_FIELDS}},
            "hourly": {
                "time": times,
                "pm2_5": [value + 1, value],
                "pm10": [value + 2, value + 1],
                "nitrogen_dioxide": [value + 3, value + 2],
            },
        })
    raw = json.dumps(responses).encode()
    rows, metadata = parse_model_response(
        raw,
        points,
        retrieved_at=datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc),
        request_url_sha256="a" * 64,
        response_sha256=sha256_bytes(raw),
    )
    assert len(rows) == 2
    assert rows[0]["forecast_at"] == "2026-10-11T03:00:00+03:00"
    assert rows[0]["request_refresh_after_utc"] == "2026-10-11T00:30:00Z"
    assert rows[0]["data_stale_after_utc"] == "2026-10-12T00:00:00Z"
    assert rows[0]["value_status"] == "modeled_forecast"
    assert rows[0]["measured_district_average"] is False
    assert rows[0]["requested_point_lon_lat"] == [37.5, 55.75]
    assert rows[0]["returned_grid_center_lon_lat"] == [37.6, 55.8]
    assert rows[0]["grid_cell_key"] == rows[1]["grid_cell_key"]
    assert metadata["model_grid_centers_unique"] == 1
    assert metadata["district_rows_per_grid_center"] == {2: 1}
    assert rows[0]["pm2_5_ug_m3"] == 10.0


def test_handoff_separates_request_refresh_from_forecast_staleness(tmp_path: Path) -> None:
    run_id = "air-cams-v1-test"
    run_dir = tmp_path / "runs" / run_id
    rows_path = run_dir / "district-air-values.jsonl"
    run_dir.mkdir(parents=True)
    original_expiry = "2026-10-11T00:15:31.274388Z"
    source_rows = [{
        "district_id": f"district:test-{index}",
        "district_name": f"Test {index}",
        "okrug_id": "okrug:test",
        "forecast_at": "2026-10-11T03:00:00+03:00",
        "forecast_at_utc": "2026-10-11T00:00:00Z",
        "retrieved_at": "2026-10-10T23:15:31.274388Z",
        "expires_at_utc": original_expiry,
        "returned_grid_center_lon_lat": [37.6, 55.7],
        "pm2_5_ug_m3": 8.5,
        "pm10_ug_m3": 10.5,
        "no2_ug_m3": 12.4,
        "value_status": "modeled_forecast",
        "measured_district_average": False,
        "source_response_sha256": "b" * 64,
    } for index in range(132)]
    _write_jsonl(rows_path, source_rows)
    source_manifest_path = run_dir / "manifest.json"
    _write_json(source_manifest_path, {"run_id": run_id, "rows_sha256": sha256_file(rows_path)})
    run = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "rows_path": rows_path.name,
        "rows_sha256": sha256_file(rows_path),
        "rows_total": 132,
        "manifest_sha256": sha256_file(source_manifest_path),
        "ata_geography_manifest_sha256": "a" * 64,
        "source_response_sha256": "b" * 64,
        "source_url_sha256": "c" * 64,
        "forecast_at": "2026-10-11T03:00:00+03:00",
        "forecast_at_utc": "2026-10-11T00:00:00Z",
        "retrieved_at": "2026-10-10T23:15:31.274388Z",
        "expires_at_utc": original_expiry,
        "coverage": {"distinct_returned_grid_centers": 1},
    }

    handoff_path, _ = _write_enrichment_handoff(tmp_path, run)
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff_rows = [json.loads(line) for line in (handoff_path.parent / handoff["rows_path"]).read_text(encoding="utf-8").splitlines()]
    assert handoff["request_refresh_after_utc"] == original_expiry
    assert handoff["data_stale_after_utc"] == "2026-10-12T00:00:00Z"
    assert handoff["source_rows_original_sha256"] == run["rows_sha256"]
    assert handoff["source_rows_reconstructed_from_pinned_raw"] is False
    assert handoff["rows_sha256"] == sha256_file(handoff_path.parent / handoff["rows_path"])
    assert len(handoff_rows) == 132
    assert handoff_rows[0]["request_refresh_after_utc"] == original_expiry
    assert handoff_rows[0]["data_stale_after_utc"] == "2026-10-12T00:00:00Z"
    assert "expires_at_utc" not in handoff_rows[0]
    assert source_rows[0]["expires_at_utc"] == original_expiry
