"""Selected reconstruction is pinned to one map/menu and one local GLB."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend import app as api, developer_experience, developer_map, developer_models, developer_units


MAP_RUN = "developer-map-v1-0123456789abcdef"
MAP_SNAPSHOT = "developer-map-snapshot-0123456789abcdef"
MENU_RUN = "developer-units-v1-0123456789abcdef"
MODEL_RUN = "developer-models-v1-0123456789abcdef"
REGISTRY_ID = "registry-" + "a" * 24


def _write(path: Path, body: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict, dict, dict, str]:
    monkeypatch.setattr(developer_models, "ROOT", tmp_path)
    monkeypatch.setattr(developer_models, "BASE", tmp_path / "data/property/developer_models")
    monkeypatch.setattr(developer_models, "ACTIVE_PATH", developer_models.BASE / "active.json")
    monkeypatch.setattr(developer_map, "BASE", tmp_path / "data/property/developer_map")
    monkeypatch.setattr(developer_units, "BASE", tmp_path / "data/property/developer_units")
    map_manifest = {"run_id": MAP_RUN, "snapshot_id": MAP_SNAPSHOT}
    map_sha = _write(developer_map.BASE / "runs" / MAP_RUN / "manifest.json",
                     json.dumps(map_manifest).encode())
    menu_manifest = {"run_id": MENU_RUN}
    menu_sha = _write(developer_units.BASE / "runs" / MENU_RUN / "manifest.json",
                      json.dumps(menu_manifest).encode())
    map_run = {"manifest": map_manifest,
               "by_registry": {REGISTRY_ID: {"map_eligible": True}}}
    menu_run = {"manifest": menu_manifest}
    glb = b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little")
    asset_sha = _write(developer_models.BASE / "runs" / MODEL_RUN /
                       "assets" / (hashlib.sha256(glb).hexdigest() + ".glb"), glb)
    row = {"registry_id": REGISTRY_ID, "asset_sha256": asset_sha,
           "asset_bytes": len(glb), "height_m": 45.0,
           "height_basis": "source_total_floors_times_3m_estimate",
           "source_geometry_basis": "aggregator_unverified",
           "depiction": "schematic_massing", "label": "реконструкция",
           "model_transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}
    models_sha = _write(developer_models.BASE / "runs" / MODEL_RUN / "models.jsonl",
                        (json.dumps(row, ensure_ascii=False) + "\n").encode())
    manifest = {"schema_version": "developer-models-snapshot/1", "run_id": MODEL_RUN,
                "map_run_id": MAP_RUN, "map_snapshot_id": MAP_SNAPSHOT,
                "map_manifest_sha256": map_sha, "menu_run_id": MENU_RUN,
                "menu_manifest_sha256": menu_sha,
                "models_sha256": models_sha, "models_total": 1}
    manifest_sha = _write(developer_models.BASE / "runs" / MODEL_RUN / "manifest.json",
                          json.dumps(manifest).encode())
    model_run = developer_models.load_staged(
        MODEL_RUN, map_run, expected_manifest_sha256=manifest_sha)
    return map_run, menu_run, model_run, asset_sha


def test_selected_model_route_is_local_and_source_limited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, menu_run, model_run, asset_sha = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "active_selected_models", lambda: (map_run, menu_run, model_run))
    result = api.developer_building_model(REGISTRY_ID)
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/schemas.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperSelectedModel"}).validate(result)
    assert result["unit_run_id"] == MENU_RUN
    assert result["asset_url"] == f"/v1/developer-model-assets/{asset_sha}"
    assert result["label"] == "реконструкция"
    assert result["geometry_basis"] == "source_building_footprint"
    assert result["height_basis"] == "source_floor_count_times_3m_estimate"
    assert "aggregator_unverified" not in json.dumps(result)
    assert api.developer_model_asset(asset_sha).body.startswith(b"glTF")
    with pytest.raises(Exception) as exc_info:
        api.developer_model_asset("0" * 64)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_mismatched_menu_generation_does_not_serve_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, menu_run, model_run, _ = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(developer_experience, "BASE", tmp_path / "data/property/developer_experience")
    monkeypatch.setattr(developer_experience, "ACTIVE_PATH",
                        developer_experience.BASE / "active.json")
    monkeypatch.setattr(developer_units, "load_staged", lambda *_args, **_kwargs: menu_run)
    monkeypatch.setattr(developer_models, "load_staged", lambda *_args, **_kwargs: model_run)
    map_sha = hashlib.sha256((developer_map.BASE / "runs" / MAP_RUN /
                              "manifest.json").read_bytes()).hexdigest()
    menu_sha = hashlib.sha256((developer_units.BASE / "runs" / MENU_RUN /
                               "manifest.json").read_bytes()).hexdigest()
    model_sha = hashlib.sha256((developer_models.BASE / "runs" / MODEL_RUN /
                                "manifest.json").read_bytes()).hexdigest()
    pointer = {"schema_version": "developer-experience-pointer/1",
               "map_run_id": MAP_RUN, "map_snapshot_id": MAP_SNAPSHOT,
               "map_manifest_sha256": map_sha,
               "unit_run_id": MENU_RUN, "unit_manifest_sha256": menu_sha,
               "model_run_id": MODEL_RUN, "model_manifest_sha256": model_sha}
    _write(developer_experience.ACTIVE_PATH, json.dumps(pointer).encode())
    assert developer_experience.resolve_active(map_run) == (menu_run, model_run)
    model_run["manifest"]["menu_run_id"] = "developer-units-v1-fedcba9876543210"
    with pytest.raises(ValueError, match="generations differ"):
        developer_experience.resolve_active(map_run)
    pointer["map_manifest_sha256"] = "0" * 64
    _write(developer_experience.ACTIVE_PATH, json.dumps(pointer).encode())
    with pytest.raises(ValueError, match="active map generation"):
        developer_experience.resolve_active(map_run)
