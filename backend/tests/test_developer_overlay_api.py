"""The visible batch tiles and the filter list share one immutable generation."""

from __future__ import annotations

import hashlib
import inspect
import json
import struct
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from starlette.requests import Request

from backend import (app as api, developer_enrichment, developer_experience,
                     developer_map, developer_models, developer_overlay,
                     developer_units)


MAP_ID = "developer-map-v1-" + "a" * 16
SNAPSHOT_ID = "developer-map-snapshot-" + "b" * 16
UNIT_ID = "developer-units-v1-" + "c" * 16
MODEL_ID = "developer-models-v1-" + "d" * 16
ENRICHMENT_ID = "developer-enrichment-v1-" + "e" * 16
OVERLAY_ID = "developer-overlay-v1-" + "f" * 16
REGISTRY_ID = "registry-" + "1" * 24
POINT_ID = "mappt_" + "1" * 24


def _bytes(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _json(path: Path, value: object) -> str:
    return _bytes(path, json.dumps(value, ensure_ascii=False,
                                   sort_keys=True).encode("utf-8"))


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
             ) -> tuple[dict, dict, dict, dict, dict, str]:
    root = tmp_path
    base = root / "data/property/developer_overlay"
    monkeypatch.setattr(developer_overlay, "ROOT", root)
    monkeypatch.setattr(developer_overlay, "BASE", base)
    developer_overlay._validated.cache_clear()
    map_run = {"manifest": {"run_id": MAP_ID, "snapshot_id": SNAPSHOT_ID},
               "rows": [{"registry_id": REGISTRY_ID, "point_id": POINT_ID,
                         "map_eligible": True, "location": [37.6, 55.7],
                         "source_group": "proplanirovki",
                         "publisher_project_id": "project-one",
                         "publisher_building_id": "building-one"}]}
    menu = {"manifest": {"run_id": UNIT_ID}}
    models = {"manifest": {"run_id": MODEL_ID}, "by_registry": {}}
    enrichment = {"manifest": {"run_id": ENRICHMENT_ID}}
    pins = {}
    for name, run_id in (("developer_map", MAP_ID), ("developer_units", UNIT_ID),
                         ("developer_models", MODEL_ID),
                         ("developer_enrichment", ENRICHMENT_ID)):
        pins[name] = _json(root / "data/property" / name / "runs" / run_id /
                           "manifest.json", {"run_id": run_id})
    run = base / "runs" / OVERLAY_ID
    tile = b"b3dmfixture"
    tile_sha = _bytes(run / "tiles" / (hashlib.sha256(tile).hexdigest() + ".b3dm"), tile)
    path = f"tiles/{tile_sha}.b3dm"
    features_sha = _bytes(run / "features.jsonl", (json.dumps({
        "registry_id": REGISTRY_ID, "point_id": POINT_ID,
        "polygon_sha256": "2" * 64, "bounds_lonlat": [37.59, 55.69, 37.61, 55.71],
        "render_policy": "persistent_flat", "source_group": "proplanirovki",
        "publisher_project_id": "project-one", "publisher_building_id": "building-one",
        "city_feature_id_candidate": "city-one"}) + "\n").encode())
    inventory_sha = _bytes(run / "tile-inventory.jsonl", (json.dumps({
        "path": path, "sha256": tile_sha, "bytes": len(tile),
        "lod": "flat", "feature_count": 1, "cell": [1, 1],
        "bbox_lonlat": [37.59, 55.69, 37.61, 55.71]}) + "\n").encode())
    tileset = {"asset": {"version": "1.0"},
               "root": {"content": {"uri": path}, "geometricError": 0}}
    tileset_sha = _json(run / "tileset.json", tileset)
    manifest = {"schema_version": "developer-overlay-snapshot/1", "run_id": OVERLAY_ID,
                "map_run_id": MAP_ID, "map_snapshot_id": SNAPSHOT_ID,
                "map_manifest_sha256": pins["developer_map"],
                "unit_run_id": UNIT_ID, "unit_manifest_sha256": pins["developer_units"],
                "model_run_id": MODEL_ID, "model_manifest_sha256": pins["developer_models"],
                "enrichment_run_id": ENRICHMENT_ID,
                "enrichment_manifest_sha256": pins["developer_enrichment"],
                "features_sha256": features_sha, "features_total": 1,
                "tileset_sha256": tileset_sha, "tile_inventory_sha256": inventory_sha,
                "tiles_total": 1, "tile_bytes": len(tile), "city_patch": None}
    manifest_sha = _json(run / "manifest.json", manifest)
    return map_run, menu, models, enrichment, manifest, manifest_sha


def test_overlay_reader_hashes_and_pins_each_batch_tile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, _, manifest_sha = _fixture(tmp_path, monkeypatch)
    snapshot = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    assert list(snapshot["features"]) == [REGISTRY_ID]
    tile_sha = next(iter(snapshot["tiles"])).split("/")[1].split(".")[0]
    assert developer_overlay.tile_bytes(snapshot, tile_sha) == b"b3dmfixture"
    tile_path = snapshot["run_path"] / f"tiles/{tile_sha}.b3dm"
    tile_path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="tile"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def test_inferred_facade_requires_exact_model_and_detail_batch_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    run = tmp_path / "data/property/developer_overlay/runs" / OVERLAY_ID
    style_id = "style-one"
    near_sha = "a" * 64
    models["manifest"]["styles_materials_sha256"] = "b" * 64
    models["by_registry"][REGISTRY_ID] = {
        "asset_sha256": "c" * 64, "source_project_id": "project-one",
        "style_id": style_id}
    models["styles"] = {style_id: {"by_level": {"near": {"sha256": near_sha}}}}
    feature_path = run / "features.jsonl"
    feature = json.loads(feature_path.read_text())
    feature.update({"style_id": style_id,
                    "facade_style_basis": "project_style_inference",
                    "facade_texture_sha256": near_sha, "volume_height_m": 30.0})
    manifest["features_sha256"] = _bytes(feature_path, (json.dumps(feature) + "\n").encode())

    def tile(style: str | None) -> bytes:
        feature_json = b'{"BATCH_LENGTH":1}'
        batch_json = json.dumps({"registry_id": [REGISTRY_ID],
                                 "facade_style_id": [style]}).encode()
        payload = feature_json + batch_json + b"glTF"
        return b"b3dm" + struct.pack("<6I", 1, 28 + len(payload), len(feature_json),
                                      0, len(batch_json), 0) + payload

    inventory = []
    for lod, style in (("flat", None), ("detail", style_id)):
        body = tile(style)
        sha = hashlib.sha256(body).hexdigest()
        path = f"tiles/{sha}.b3dm"
        _bytes(run / path, body)
        inventory.append({"path": path, "sha256": sha, "bytes": len(body),
                          "lod": lod, "feature_count": 1, "cell": [1, 1],
                          "bbox_lonlat": [37.59, 55.69, 37.61, 55.71]})
    manifest["tile_inventory_sha256"] = _bytes(
        run / "tile-inventory.jsonl",
        ("\n".join(json.dumps(row) for row in inventory) + "\n").encode())
    manifest["tiles_total"] = 2
    manifest["tile_bytes"] = sum(row["bytes"] for row in inventory)
    manifest["tileset_sha256"] = _json(run / "tileset.json", {
        "root": {"children": [{"content": {"uri": row["path"]}} for row in inventory]}})
    manifest["facade_styles"] = {
        "basis": "project_style_inference", "label": "Реконструкция",
        "source_model_run_id": MODEL_ID,
        "styles_materials_sha256": models["manifest"]["styles_materials_sha256"],
        "texture_level": "near", "styled_projects_total": 1,
        "styled_persistent_features_total": 1, "far_untextured": True,
        "near_textures_embedded_in_detail_tiles": True}
    manifest_sha = _json(run / "manifest.json", manifest)
    snapshot = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    assert snapshot["facade_styles"]["styled_persistent_features_total"] == 1
    menu["enrichment"] = enrichment
    menu["overlay"] = snapshot
    monkeypatch.setattr(api, "_active_developer_overlay", lambda: (map_run, menu, snapshot))
    monkeypatch.setattr(api, "_developer_list", lambda *_args, **_kwargs: (
        map_run["rows"], {REGISTRY_ID: 1}, UNIT_ID, {}, enrichment, menu))
    request = Request({"type": "http", "method": "GET", "path": "/v1/developer-overlay/viewport",
                       "query_string": b"west=37.5&south=55.6&east=37.7&north=55.8",
                       "headers": []})
    filters = {key: None for key in (
        "query", "district", "okrug_id", "max_noise_index", "max_air_index",
        "price_min_rub", "price_max_rub", "price_per_m2_min_rub",
        "price_per_m2_max_rub", "rooms", "area_min_m2", "area_max_m2",
        "unit_floor_min", "unit_floor_max")}
    view = api.developer_overlay_viewport(
        request, west=37.5, south=55.6, east=37.7, north=55.8,
        limit=1200, offset=0, **filters)
    assert view["facade_style"]["styled_persistent_features_total"] == 1
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperOverlayViewport"}).validate(view)
    feature["facade_texture_sha256"] = "d" * 64
    manifest["features_sha256"] = _bytes(feature_path, (json.dumps(feature) + "\n").encode())
    manifest_sha = _json(run / "manifest.json", manifest)
    with pytest.raises(ValueError, match="not bound to its source project"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def _city_patch_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        manifest: dict) -> str:
    city = tmp_path / "assets/public-citywide-v12"
    monkeypatch.setattr(developer_overlay, "BASE_CITY_ROOT", city)
    base_manifest_sha = _json(city / "manifest.json", {"version": "12"})
    base_tileset_sha = _json(city / "tileset.json", {"root": {"children": []}})
    region_name = "region_1_1.tileset.json"
    overview_name = "massing_region_1_1.glb"
    detail_name = "cell_1_1.glb"
    region_source_sha = _bytes(city / region_name, b"original region")
    overview_source_sha = _bytes(city / overview_name, b"original overview")
    detail_source_sha = _bytes(city / detail_name, b"original detail")
    run = tmp_path / "data/property/developer_overlay/runs" / OVERLAY_ID
    overview_sha = _bytes(run / "city-patch/tiles" / (hashlib.sha256(b"overview").hexdigest()
                                                  + ".glb"), b"overview")
    detail_sha = _bytes(run / "city-patch/tiles" / (hashlib.sha256(b"detail").hexdigest()
                                                + ".glb"), b"detail")
    region_doc = {"root": {"content": {"uri": f"../tiles/{overview_sha}.glb"},
                           "children": [{"content": {"uri": f"../tiles/{detail_sha}.glb"}}]}}
    region_path = run / "city-patch/regions"
    region_bytes = json.dumps(region_doc, sort_keys=True).encode()
    region_sha = _bytes(region_path / (hashlib.sha256(region_bytes).hexdigest() + ".json"),
                        region_bytes)
    patch_root = {"root": {"children": [
        {"content": {"uri": f"tiles/{overview_sha}.glb"}},
        {"content": {"uri": f"regions/{region_sha}.json"}},
        {"content": {"uri": "/assets/public-citywide-v12/cell_1_1.glb"}},
    ]}}
    patch_sha = _json(run / "city-patch/tileset.json", patch_root)
    region_rows = [{"path": f"city-patch/regions/{region_sha}.json", "sha256": region_sha,
                    "bytes": len(region_bytes), "source_region": region_name,
                    "source_sha256": region_source_sha}]
    tile_rows = [
        {"path": f"city-patch/tiles/{overview_sha}.glb", "sha256": overview_sha,
         "bytes": len(b"overview"), "source_tile": overview_name,
         "source_sha256": overview_source_sha, "lod": "overview",
         "suppressed_registry_ids": [REGISTRY_ID]},
        {"path": f"city-patch/tiles/{detail_sha}.glb", "sha256": detail_sha,
         "bytes": len(b"detail"), "source_tile": detail_name,
         "source_sha256": detail_source_sha, "lod": "detail",
         "suppressed_registry_ids": [REGISTRY_ID]},
    ]
    region_inventory_sha = _bytes(run / "city-patch/region-inventory.jsonl",
                                  (json.dumps(region_rows[0]) + "\n").encode())
    tile_inventory_sha = _bytes(run / "city-patch/tile-inventory.jsonl",
                                ("\n".join(json.dumps(row) for row in tile_rows) + "\n").encode())
    suppressed_sha = _bytes(run / "city-patch/suppressed-registry-ids.json",
                            (json.dumps([REGISTRY_ID]) + "\n").encode())
    pair_sha = _bytes(run / "city-patch/pair-proof.jsonl", (json.dumps({
        "registry_id": REGISTRY_ID, "city_source_id": "city-one",
        "geometry_source_id": "proplanirovki:project-one:building-one",
        "source_polygon_sha256": "2" * 64}) + "\n").encode())
    manifest["city_patch"] = {
        "base_city_manifest_sha256": base_manifest_sha,
        "base_city_tileset_sha256": base_tileset_sha,
        "tileset_sha256": patch_sha,
        "region_inventory_sha256": region_inventory_sha,
        "tile_inventory_sha256": tile_inventory_sha,
        "suppressed_registry_ids_sha256": suppressed_sha,
        "pair_proof_sha256": pair_sha, "pair_proof_total": 1,
        "regions_total": 1, "replacement_tiles_total": 2,
        "suppressed_total": 1, "all_visible_lods_covered": True,
        "detail_proof_manifest_sha256": "3" * 64,
        "overview_proof_manifest_sha256": "4" * 64,
    }
    return _json(run / "manifest.json", manifest)


def test_city_patch_requires_both_lods_and_verified_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    manifest_sha = _city_patch_fixture(tmp_path, monkeypatch, manifest)
    snapshot = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    assert snapshot["city_patch"]["suppressed_registry_ids"] == [REGISTRY_ID]
    overview_sha = next(row["sha256"] for row in snapshot["city_patch"]["files"].values()
                        if row.get("lod") == "overview")
    assert developer_overlay.city_patch_bytes(snapshot, "tile", overview_sha) == b"overview"
    file = snapshot["run_path"] / f"city-patch/tiles/{overview_sha}.glb"
    file.write_bytes(b"changed")
    with pytest.raises(ValueError, match="City patch"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def test_city_patch_rejects_suppression_missing_from_detail_lod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    _city_patch_fixture(tmp_path, monkeypatch, manifest)
    run = tmp_path / "data/property/developer_overlay/runs" / OVERLAY_ID
    inventory_path = run / "city-patch/tile-inventory.jsonl"
    rows = [json.loads(line) for line in inventory_path.read_text().splitlines()]
    rows[1]["suppressed_registry_ids"] = []
    manifest["city_patch"]["tile_inventory_sha256"] = _bytes(
        inventory_path, ("\n".join(json.dumps(row) for row in rows) + "\n").encode())
    manifest_sha = _json(run / "manifest.json", manifest)
    with pytest.raises(ValueError, match="both LODs"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def test_city_patch_rejects_wrong_registry_to_city_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    _city_patch_fixture(tmp_path, monkeypatch, manifest)
    run = tmp_path / "data/property/developer_overlay/runs" / OVERLAY_ID
    pair_path = run / "city-patch/pair-proof.jsonl"
    pair = json.loads(pair_path.read_text())
    pair["city_source_id"] = "different-city-building"
    manifest["city_patch"]["pair_proof_sha256"] = _bytes(
        pair_path, (json.dumps(pair) + "\n").encode())
    manifest_sha = _json(run / "manifest.json", manifest)
    with pytest.raises(ValueError, match="pair identity"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def test_registry_map_uses_only_active_verified_city_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    manifest_sha = _city_patch_fixture(tmp_path, monkeypatch, manifest)
    geometry = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    menu.update({"enrichment": enrichment, "overlay": geometry})
    map_run["manifest"]["buildings_total"] = 1
    monkeypatch.setattr(api, "active_developer_map", lambda: map_run)
    monkeypatch.setattr(api, "_developer_filters", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_developer_list", lambda *_args, **_kwargs: (
        map_run["rows"], {REGISTRY_ID: 1}, UNIT_ID, {}, enrichment, menu))
    monkeypatch.setattr(api, "_developer_as_of", lambda *_args: "2026-10-11T00:00:00Z")
    monkeypatch.setattr(api, "_developer_public_environment", lambda *_args: {})
    monkeypatch.setattr(api, "_developer_public_structure", lambda *_args: {})
    monkeypatch.setattr(developer_map, "public_building", lambda row: {
        "registry_id": row["registry_id"], "point_id": row["point_id"]})
    monkeypatch.setattr(api, "load_catalogue", lambda: {})
    monkeypatch.setattr(api, "background_tilesets", lambda *_args: [
        {"kind": "city_volumes", "url": "/assets/public-citywide-v12/tileset.json"},
        {"kind": "roads", "url": "/assets/public-roads-v2/tileset.json"}])
    monkeypatch.setattr(api, "asset_manifest", lambda: {"city_buildings": 1})
    kwargs = {name: None for name in inspect.signature(api.registry_map).parameters}
    result = api.registry_map(**kwargs)
    assert result["overlay_run_id"] == OVERLAY_ID
    assert result["tilesets"][0]["url"] == (
        f"/v1/developer-overlay/{OVERLAY_ID}/city-patch/tileset.json")
    assert result["tilesets"][1]["url"] == "/assets/public-roads-v2/tileset.json"


def test_persistent_lake_requires_exact_selected_model_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, _ = _fixture(tmp_path, monkeypatch)
    _city_patch_fixture(tmp_path, monkeypatch, manifest)
    run = tmp_path / "data/property/developer_overlay/runs" / OVERLAY_ID
    map_run["rows"][0]["source_group"] = "fsk"
    feature_path = run / "features.jsonl"
    feature = json.loads(feature_path.read_text())
    feature.update({"source_group": "fsk", "source_geometry_id": "osm:way/1317814900@2",
                    "source_geometry_basis": "osm_exact_building_footprint_way_1317814900_v2"})
    manifest["features_sha256"] = _bytes(feature_path, (json.dumps(feature) + "\n").encode())
    pair_path = run / "city-patch/pair-proof.jsonl"
    pair = json.loads(pair_path.read_text())
    pair["geometry_source_id"] = "osm:way/1317814900@2"
    manifest["city_patch"]["pair_proof_sha256"] = _bytes(
        pair_path, (json.dumps(pair) + "\n").encode())
    model_sha = "a" * 64
    models["by_registry"][REGISTRY_ID] = {"asset_sha256": model_sha}
    manifest.update({"lake_persistent": True,
                     "lake_persistent_model_sha256": model_sha,
                     "lake_persistent_policy": "near_only_reuse_selected_model_instance"})
    manifest_sha = _json(run / "manifest.json", manifest)
    snapshot = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    assert snapshot["manifest"]["lake_persistent"] is True
    assert snapshot["city_patch"]["lake_registry_id"] == REGISTRY_ID
    menu["enrichment"] = enrichment
    menu["overlay"] = snapshot
    monkeypatch.setattr(api, "_active_developer_overlay", lambda: (map_run, menu, snapshot))
    monkeypatch.setattr(api, "_developer_list", lambda *_args, **_kwargs: (
        map_run["rows"], {REGISTRY_ID: 1}, UNIT_ID, {}, enrichment, menu))
    request = Request({"type": "http", "method": "GET", "path": "/v1/developer-overlay/viewport",
                       "query_string": b"west=37.5&south=55.6&east=37.7&north=55.8",
                       "headers": []})
    filters = {key: None for key in (
        "query", "district", "okrug_id", "max_noise_index", "max_air_index",
        "price_min_rub", "price_max_rub", "price_per_m2_min_rub",
        "price_per_m2_max_rub", "rooms", "area_min_m2", "area_max_m2",
        "unit_floor_min", "unit_floor_max")}
    view = api.developer_overlay_viewport(
        request, west=37.5, south=55.6, east=37.7, north=55.8,
        limit=1200, offset=0, **filters)
    assert view["lake_persistent"] is True
    assert view["lake_registry_id"] == REGISTRY_ID
    assert view["lake_persistent_model_sha256"] == model_sha
    manifest["lake_persistent_model_sha256"] = "b" * 64
    manifest_sha = _json(run / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Persistent Lake"):
        developer_overlay.load_staged(
            OVERLAY_ID, map_run, menu, models, enrichment,
            expected_manifest_sha256=manifest_sha)


def test_viewport_returns_only_filtered_ids_and_versioned_tileset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, _, manifest_sha = _fixture(tmp_path, monkeypatch)
    geometry = developer_overlay.load_staged(
        OVERLAY_ID, map_run, menu, models, enrichment,
        expected_manifest_sha256=manifest_sha)
    menu["enrichment"] = enrichment
    menu["overlay"] = geometry
    monkeypatch.setattr(api, "_active_developer_overlay", lambda: (map_run, menu, geometry))
    monkeypatch.setattr(api, "_developer_list", lambda *_args, **_kwargs: (
        map_run["rows"], {REGISTRY_ID: 1}, UNIT_ID,
        {"max_air_index": 40}, enrichment, menu))
    request = Request({"type": "http", "method": "GET", "path": "/v1/developer-overlay/viewport",
                       "query_string": b"west=37.5&south=55.6&east=37.7&north=55.8",
                       "headers": []})
    kwargs = {key: None for key in (
        "query", "district", "okrug_id", "max_noise_index", "price_min_rub",
        "price_max_rub", "price_per_m2_min_rub", "price_per_m2_max_rub", "rooms",
        "area_min_m2", "area_max_m2", "unit_floor_min", "unit_floor_max")}
    view = api.developer_overlay_viewport(
        request, west=37.5, south=55.6, east=37.7, north=55.8,
        max_air_index=40, limit=1200, offset=0, **kwargs)
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperOverlayViewport"}).validate(view)
    assert view["eligible_registry_ids"] == [REGISTRY_ID]
    assert view["tileset_url"] == f"/v1/developer-overlay/{OVERLAY_ID}/tileset.json"
    assert view["lake_persistent"] is False
    assert view["lake_registry_id"] is None
    assert view["lake_persistent_model_sha256"] is None
    assert view["facade_style"] is None
    assert view["applied_filters"] == {"max_air_index": 40}
    outside = api.developer_overlay_viewport(
        request, west=38.0, south=55.6, east=38.1, north=55.8,
        max_air_index=40, limit=1200, offset=0, **kwargs)
    assert outside["eligible_registry_ids"] == []


def test_unified_v3_pointer_fails_closed_on_overlay_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    map_run, menu, models, enrichment, manifest, manifest_sha = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(developer_experience, "ACTIVE_PATH", tmp_path / "active.json")
    monkeypatch.setattr(developer_map, "BASE", tmp_path / "data/property/developer_map")
    monkeypatch.setattr(developer_units, "load_staged", lambda *_args, **_kwargs: menu)
    monkeypatch.setattr(developer_models, "load_staged", lambda *_args, **_kwargs: models)
    monkeypatch.setattr(developer_enrichment, "load_staged", lambda *_args, **_kwargs:
                        enrichment)
    models["manifest"]["menu_run_id"] = UNIT_ID
    unit_sha = manifest["unit_manifest_sha256"]
    models["manifest"]["menu_manifest_sha256"] = unit_sha
    pointer = {key: manifest[key] for key in (
        "map_run_id", "map_snapshot_id", "map_manifest_sha256",
        "unit_run_id", "unit_manifest_sha256", "model_run_id",
        "model_manifest_sha256", "enrichment_run_id",
        "enrichment_manifest_sha256")}
    pointer.update({"schema_version": "developer-experience-pointer/3",
                    "overlay_run_id": OVERLAY_ID,
                    "overlay_manifest_sha256": manifest_sha})
    _json(tmp_path / "active.json", pointer)
    resolved_menu, resolved_models = developer_experience.resolve_active(map_run)
    assert resolved_models is models
    assert resolved_menu["overlay"]["manifest"]["run_id"] == OVERLAY_ID
    pointer["overlay_manifest_sha256"] = "0" * 64
    _json(tmp_path / "active.json", pointer)
    with pytest.raises(ValueError, match="manifest hash"):
        developer_experience.resolve_active(map_run)
