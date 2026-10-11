"""Selected reconstruction is pinned to one map/menu and one local GLB."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend import (app as api, developer_enrichment, developer_experience,
                     developer_map, developer_models, developer_units)


MAP_RUN = "developer-map-v1-0123456789abcdef"
MAP_SNAPSHOT = "developer-map-snapshot-0123456789abcdef"
MENU_RUN = "developer-units-v1-0123456789abcdef"
MODEL_RUN = "developer-models-v1-0123456789abcdef"
STYLED_RUN = "developer-models-v1-fedcba9876543210"
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
               "by_registry": {REGISTRY_ID: {"map_eligible": True,
                                              "source_group": "proplanirovki",
                                              "publisher_project_id": "project-one"}}}
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
    assert result["height_basis"] == "source_total_floors_times_3m_estimate"
    assert "aggregator_unverified" not in json.dumps(result)
    assert api.developer_model_asset(asset_sha).body.startswith(b"glTF")
    with pytest.raises(Exception) as exc_info:
        api.developer_model_asset("0" * 64)
    assert getattr(exc_info.value, "status_code", None) == 404


def test_project_style_requires_exact_material_and_project_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, _, _, _ = _fixture(monkeypatch, tmp_path)
    baseline = developer_models.BASE / "runs" / MODEL_RUN
    baseline_sha = hashlib.sha256((baseline / "manifest.json").read_bytes()).hexdigest()
    run = developer_models.BASE / "runs" / STYLED_RUN
    source_evidence = "e" * 64
    lods = []
    texture_rows = []
    for level in ("near", "closer", "close"):
        body = ("png:" + level).encode()
        sha = hashlib.sha256(body).hexdigest()
        path = f"styles/textures/{sha}.png"
        _write(run / path, body)
        lods.append({"level": level, "width": 16, "height": 16,
                     "mime_type": "image/png", "sha256": sha,
                     "asset_path": path.removeprefix("styles/"),
                     "asset_uri": f"facade-style://sha256/{sha}"})
        texture_rows.append({"path": path, "sha256": sha, "bytes": len(body),
                             "style_id": "style-one", "level": level})
    material = {"style_id": "style-one", "publisher_project_id": "project-one",
                "basis": "project_style_inference", "label": "Реконструкция",
                "geometry_scope": "appearance_only_no_geometry_change",
                "source_evidence_sha256": source_evidence, "lods": lods}
    materials_sha = _write(run / "styles/materials.jsonl",
                           (json.dumps(material, ensure_ascii=False) + "\n").encode())
    texture_inventory_sha = _write(run / "styles/texture-inventory.jsonl",
                                   ("\n".join(json.dumps(row) for row in texture_rows) + "\n").encode())
    row = json.loads((baseline / "models.jsonl").read_text())
    row.update({"style_id": "style-one", "source_project_id": "project-one",
                "facade_style_basis": "project_style_inference",
                "facade_style_evidence_sha256": source_evidence,
                "texture_basis": "generated_project_style_repeat_unoriented",
                "texture_sha256": lods[1]["sha256"],
                "photo_facade_reconstruction": False,
                "model_quality": "project_style_inferred_schematic_reconstruction"})
    models_sha = _write(run / "models.jsonl", (json.dumps(row) + "\n").encode())
    manifest = json.loads((baseline / "manifest.json").read_text())
    manifest.update({"run_id": STYLED_RUN, "models_sha256": models_sha,
                     "base_model_run_id": MODEL_RUN,
                     "base_model_manifest_sha256": baseline_sha,
                     "style_batch_manifest_sha256": "a" * 64,
                     "styles_materials_sha256": materials_sha,
                     "styles_materials_total": 1,
                     "styles_texture_inventory_sha256": texture_inventory_sha,
                     "styles_textures_total": 3,
                     "styled_models_total": 1})
    manifest_sha = _write(run / "manifest.json", json.dumps(manifest).encode())
    model = developer_models.load_staged(
        STYLED_RUN, map_run, expected_manifest_sha256=manifest_sha)
    public = developer_models.public_model(map_run, REGISTRY_ID, model, MENU_RUN)
    assert public["facade_style_basis"] == "project_style_inference"
    assert public["texture_basis"] == "generated_project_style_repeat_unoriented"
    manifest["base_model_manifest_sha256"] = "0" * 64
    invalid_sha = _write(run / "manifest.json", json.dumps(manifest).encode())
    with pytest.raises(ValueError, match="baseline manifest differs"):
        developer_models.load_staged(STYLED_RUN, map_run, expected_manifest_sha256=invalid_sha)
    manifest["base_model_manifest_sha256"] = baseline_sha
    row["source_project_id"] = "different-project"
    manifest["models_sha256"] = _write(run / "models.jsonl", (json.dumps(row) + "\n").encode())
    manifest_sha = _write(run / "manifest.json", json.dumps(manifest).encode())
    with pytest.raises(ValueError, match="Invalid selected"):
        developer_models.load_staged(STYLED_RUN, map_run, expected_manifest_sha256=manifest_sha)
    row["source_project_id"] = "project-one"
    row["height_m"] = 48.0
    manifest["models_sha256"] = _write(run / "models.jsonl", (json.dumps(row) + "\n").encode())
    manifest_sha = _write(run / "manifest.json", json.dumps(manifest).encode())
    with pytest.raises(ValueError, match="changes source geometry or height"):
        developer_models.load_staged(STYLED_RUN, map_run, expected_manifest_sha256=manifest_sha)


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


def test_v2_experience_resolves_one_exact_enrichment_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    map_run, menu_run, model_run, _ = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(developer_experience, "ACTIVE_PATH", tmp_path / "experience-v2.json")
    monkeypatch.setattr(developer_units, "load_staged", lambda *_args, **_kwargs: menu_run)
    monkeypatch.setattr(developer_models, "load_staged", lambda *_args, **_kwargs: model_run)
    calls: list[tuple] = []
    overlay = {"manifest": {"run_id": "developer-enrichment-v1-" + "e" * 16}}

    def load_enrichment(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return overlay

    monkeypatch.setattr(developer_enrichment, "load_staged", load_enrichment)
    pointer = {"schema_version": "developer-experience-pointer/2",
               "map_run_id": MAP_RUN, "map_snapshot_id": MAP_SNAPSHOT,
               "map_manifest_sha256": hashlib.sha256(
                   (developer_map.BASE / "runs" / MAP_RUN / "manifest.json").read_bytes()).hexdigest(),
               "unit_run_id": MENU_RUN, "unit_manifest_sha256": hashlib.sha256(
                   (developer_units.BASE / "runs" / MENU_RUN / "manifest.json").read_bytes()).hexdigest(),
               "model_run_id": MODEL_RUN, "model_manifest_sha256": hashlib.sha256(
                   (developer_models.BASE / "runs" / MODEL_RUN / "manifest.json").read_bytes()).hexdigest(),
               "enrichment_run_id": overlay["manifest"]["run_id"],
               "enrichment_manifest_sha256": "f" * 64}
    _write(developer_experience.ACTIVE_PATH, json.dumps(pointer).encode())
    resolved_menu, resolved_models = developer_experience.resolve_active(map_run)
    assert resolved_models is model_run
    assert resolved_menu["enrichment"] is overlay
    assert "enrichment" not in menu_run
    assert calls[0][0][:4] == (pointer["enrichment_run_id"], map_run, menu_run, model_run)
    assert calls[0][1]["expected_manifest_sha256"] == pointer["enrichment_manifest_sha256"]
    pointer["enrichment_manifest_sha256"] = "bad"
    _write(developer_experience.ACTIVE_PATH, json.dumps(pointer).encode())
    with pytest.raises(ValueError, match="Invalid developer experience pointer"):
        developer_experience.resolve_active(map_run)
