"""Small structural checks; no production-sized data is copied by these tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from deploy.apply_release import verify_release as verify_deploy_release
from scripts.package_deploy_data import DATA_FILES, apply_plan, make_plan, verify_release
from scripts.tests.runtime_fixture import routing_contents


ACTIVE = ("public-citywide-v11", "public-ground-v3", "public-roads-v2",
          "public-parks-v1", "public-water-v1")
MODEL_ROOT = "public-project-models-v1-" + "a" * 16
RUN_ID = "models-v1-" + "a" * 16


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _developer_map_fixture(root: Path) -> None:
    run_id = "developer-map-v1-" + "c" * 16
    snapshot_id = "developer-map-snapshot-" + "d" * 16
    prefix = root / "data/property/developer_map/runs" / run_id
    rows = {
        "points": '{"building_id":"b1","point_id":"p1"}\n',
        "groups": '{"group_id":"g1","point_ids":["p1"]}\n',
        "rejections": '{"building_id":"b2","reason":"no_coordinates"}\n',
    }
    descriptors = {}
    for label, body in rows.items():
        path = prefix / f"{label}.jsonl"
        _write(path, body)
        raw = body.encode()
        descriptors[f"{label}_sha256"] = hashlib.sha256(raw).hexdigest()
        descriptors[f"{label}_total"] = body.count("\n")
    manifest = {
        "schema_version": "developer-map-snapshot/1",
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "buildings_total": 2,
        "map_points_total": 1,
        **descriptors,
    }
    _write(prefix / "manifest.json", json.dumps(manifest, sort_keys=True))
    manifest_sha = hashlib.sha256((prefix / "manifest.json").read_bytes()).hexdigest()
    pointer = {
        "schema_version": "developer-map-pointer/1",
        "run_id": run_id,
        "manifest_sha256": manifest_sha,
    }
    _write(root / "data/property/developer_map/active.json",
           json.dumps(pointer, sort_keys=True))


def _developer_experience_fixture(root: Path) -> None:
    map_pointer = json.loads((root / "data/property/developer_map/active.json").read_text())
    map_manifest_path = (root / "data/property/developer_map/runs" /
                         map_pointer["run_id"] / "manifest.json")
    map_manifest = json.loads(map_manifest_path.read_text())
    unit_run = "developer-units-v1-" + "e" * 16
    model_run = "developer-models-v1-" + "f" * 16
    unit_prefix = root / "data/property/developer_units/runs" / unit_run
    image = b"\x89PNG\r\n\x1a\nfixture"
    image_sha = hashlib.sha256(image).hexdigest()
    unit_rows = {
        "buildings": '{"registry_id":"r1"}\n{"registry_id":"r2"}\n',
        "units": '{"unit_id":"unit_000000000000000000000001"}\n',
        "floors": "",
        "plans": json.dumps({"role": "apartment_plan", "unit_id": "unit_000000000000000000000001",
                             "floor_id": None, "asset_sha256": image_sha,
                             "asset_mime": "image/png"}) + "\n",
        "unit_aliases": "",
    }
    unit_descriptors = {}
    for label, body in unit_rows.items():
        _write(unit_prefix / f"{label}.jsonl", body)
        unit_descriptors[f"{label}_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        unit_descriptors[f"{label}_total"] = body.count("\n")
    _write_bytes(unit_prefix / "assets" / f"{image_sha}.png", image)
    unit_manifest = {
        "schema_version": "developer-units-snapshot/1",
        "run_id": unit_run,
        "map_run_id": map_pointer["run_id"],
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_pointer["manifest_sha256"],
        **unit_descriptors,
        "plan_assets_prepared": 1,
        "plan_asset_bytes": len(image),
    }
    unit_manifest_bytes = json.dumps(unit_manifest, sort_keys=True).encode()
    _write_bytes(unit_prefix / "manifest.json", unit_manifest_bytes)
    unit_manifest_sha = hashlib.sha256(unit_manifest_bytes).hexdigest()

    model_prefix = root / "data/property/developer_models/runs" / model_run
    glb = b"glTF" + b"\x02\x00\x00\x00" + (12).to_bytes(4, "little")
    glb_sha = hashlib.sha256(glb).hexdigest()
    model_row = {"registry_id": "r1", "asset_sha256": glb_sha, "asset_bytes": len(glb)}
    models_body = json.dumps(model_row).encode() + b"\n"
    _write_bytes(model_prefix / "models.jsonl", models_body)
    _write_bytes(model_prefix / "assets" / f"{glb_sha}.glb", glb)
    model_manifest = {
        "schema_version": "developer-models-snapshot/1",
        "run_id": model_run,
        "map_run_id": map_pointer["run_id"],
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_pointer["manifest_sha256"],
        "menu_run_id": unit_run,
        "menu_manifest_sha256": unit_manifest_sha,
        "models_sha256": hashlib.sha256(models_body).hexdigest(),
        "models_total": 1,
        "assets_total": 1,
        "assets_bytes": len(glb),
    }
    model_manifest_bytes = json.dumps(model_manifest, sort_keys=True).encode()
    _write_bytes(model_prefix / "manifest.json", model_manifest_bytes)
    experience_pointer = {
        "schema_version": "developer-experience-pointer/1",
        "map_run_id": map_pointer["run_id"],
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_pointer["manifest_sha256"],
        "unit_run_id": unit_run,
        "unit_manifest_sha256": unit_manifest_sha,
        "model_run_id": model_run,
        "model_manifest_sha256": hashlib.sha256(model_manifest_bytes).hexdigest(),
    }
    _write(root / "data/property/developer_experience/active.json",
           json.dumps(experience_pointer, sort_keys=True))


def _fixture(root: Path) -> None:
    pointer = {key: f"/assets/{name}/tileset.json" for key, name in zip(
        ("city_tileset_url", "ground_tileset_url", "roads_tileset_url",
         "parks_tileset_url", "water_tileset_url"), ACTIVE, strict=True)}
    pointer.update({"public_redacted_source_ids": True, "project_models_root": MODEL_ROOT,
                    "previous_public_roots": ["public-citywide-v10"], "facade_models": 3})
    _write(root / "assets/manifest.json", json.dumps(pointer))
    for name in (*ACTIVE, "public-citywide-v10"):
        _write(root / "assets" / name / "manifest.json",
               json.dumps({"public_redacted_source_ids": True}))
        _write(root / "assets" / name / "tileset.json",
               json.dumps({"root": {"content": {"uri": "cell_1_2.glb"}}}))
        _write(root / "assets" / name / "cell_1_2.glb")
    _write(root / "assets" / MODEL_ROOT / "public_manifest.json",
           json.dumps({"public_redacted_source_ids": True,
                       "files": [{"path": "model_" + "b" * 64 + ".glb"}]}))
    _write(root / "assets" / MODEL_ROOT / ("model_" + "b" * 64 + ".glb"))
    for name in DATA_FILES:
        _write(root / name)
    for relative, content in routing_contents().items():
        _write_bytes(root / relative, content)
    _developer_map_fixture(root)
    model_rows = {
        "assessments": '{"assessment_id":"a1"}\n',
        "candidates": '{"candidate_id":"c1"}\n',
        "source_evaluations": '{"source_id":"s1"}\n',
    }
    for name, body in model_rows.items():
        _write(root / "data/models" / RUN_ID / f"{name}.jsonl", body)
    public_manifest_sha = hashlib.sha256(
        (root / "assets" / MODEL_ROOT / "public_manifest.json").read_bytes()).hexdigest()
    model_manifest = {
        "schema_version": "model-candidates/1",
        "run_id": RUN_ID,
        "public_root": MODEL_ROOT,
        "records_total": 1,
        "assessments_sha256": hashlib.sha256(model_rows["assessments"].encode()).hexdigest(),
        "candidates_sha256": hashlib.sha256(model_rows["candidates"].encode()).hexdigest(),
        "source_evaluations_sha256": hashlib.sha256(
            model_rows["source_evaluations"].encode()).hexdigest(),
        "public_manifest_sha256": public_manifest_sha,
    }
    _write(root / "data/models" / RUN_ID / "manifest.json",
           json.dumps(model_manifest, sort_keys=True))
    model_manifest_sha = hashlib.sha256(
        (root / "data/models" / RUN_ID / "manifest.json").read_bytes()).hexdigest()
    _write(root / "data/models/active.json", json.dumps({
        "run_id": RUN_ID,
        "public_root": MODEL_ROOT,
        "manifest_sha256": model_manifest_sha,
    }))
    _developer_experience_fixture(root)
    _write(root / "data/social/valhalla_moscow/tiles/0/123.gph")
    _write(root / "data/property/estateline/private-key.pem", "must not copy")
    _write(root / ".runtime/private.env", "must not copy")
    _write(root / "assets/private-source/manifest.json", "must not copy")


def test_current_only_release_is_explicit_and_excludes_unrelated_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    planned, pointer = make_plan(source, "current-only")
    output = apply_plan(source, tmp_path / "release", "current-only", planned, pointer)
    manifest = json.loads((output / "release-manifest.json").read_text())
    paths = {row["path"] for row in manifest["files"]}
    release_pointer = json.loads((output / "assets/manifest.json").read_text())
    assert release_pointer["previous_public_roots"] == []
    assert release_pointer["facade_models"] == 0
    assert manifest["previous_public_roots_excluded"] == ["public-citywide-v10"]
    assert not any("private-key" in path or ".runtime" in path or
                   "public-citywide-v10" in path or "private-source" in path for path in paths)
    assert (output / "data/social/valhalla_moscow/tiles/0/123.gph").is_file()
    assert (output / "data/runtime").is_dir()
    assert not (output / "data/runtime/analytics.sqlite3").exists()
    assert manifest["file_count"] == len(paths)
    assert verify_release(output)["file_count"] == len(paths)
    assert verify_deploy_release(output)["file_count"] == len(paths)
    assert {
        "data/property/developer_map/active.json",
        "data/property/developer_map/runs/developer-map-v1-cccccccccccccccc/manifest.json",
        "data/property/developer_map/runs/developer-map-v1-cccccccccccccccc/points.jsonl",
        "data/property/developer_map/runs/developer-map-v1-cccccccccccccccc/groups.jsonl",
        "data/property/developer_map/runs/developer-map-v1-cccccccccccccccc/rejections.jsonl",
    } <= paths
    experience_paths = {row["path"] for row in manifest["files"]
                         if row["group"] == "developer_experience"}
    assert any(path == "data/property/developer_experience/active.json"
               for path in experience_paths)
    assert any("/developer_units/runs/" in path and path.endswith("units.jsonl")
               for path in experience_paths)
    assert any("/developer_models/runs/" in path and path.endswith(".glb")
               for path in experience_paths)


def test_full_compat_keeps_previous_immutable_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    planned, pointer = make_plan(source, "full-compat")
    assert pointer["previous_public_roots"] == ["public-citywide-v10"]
    assert any(row.relative == "assets/public-citywide-v10/tileset.json" for row in planned)


def test_optional_private_group_evidence_is_included_without_public_exposure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    relative = f"data/models/{RUN_ID}/map_point_groups.private.json"
    _write(source / relative, '{"schema_version":"map-point-groups-private/1"}')
    planned, _ = make_plan(source, "current-only")
    assert any(row.relative == relative and row.group == "models_private" for row in planned)
    assert all(not row.relative.startswith("assets/") or
               "map_point_groups" not in row.relative for row in planned)
    (source / relative).unlink()
    (source / relative).symlink_to(source / ".runtime/private.env")
    with pytest.raises(ValueError, match="Symlink"):
        make_plan(source, "current-only")


def test_visibility_release_includes_only_active_checked_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    run_id = "visibility-v1-" + "b" * 16
    prefix = source / "data/property/estateline/visibility"
    manifest = prefix / "runs" / run_id / "manifest.json"
    records = prefix / "runs" / run_id / "records.jsonl"
    _write(manifest, '{"schema_version":"registry-visibility/1"}')
    _write(records, '{}\n')
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    pointer = prefix / "active.json"
    _write(pointer, json.dumps({"run_id": run_id, "manifest_sha256": digest}))
    planned, _ = make_plan(source, "current-only")
    paths = {row.relative for row in planned}
    assert {"data/property/estateline/visibility/active.json",
            f"data/property/estateline/visibility/runs/{run_id}/manifest.json",
            f"data/property/estateline/visibility/runs/{run_id}/records.jsonl"} <= paths
    _write(manifest, '{}')
    with pytest.raises(ValueError, match="differs from pointer"):
        make_plan(source, "current-only")


def test_symlink_and_unsanitized_asset_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    target = source / "data/property/estateline/projects.jsonl"
    target.unlink()
    target.symlink_to(source / ".runtime/private.env")
    with pytest.raises(ValueError, match="Symlink"):
        make_plan(source, "current-only")
    target.unlink()
    _write(target)
    _write(source / "assets/public-ground-v3/manifest.json", "{}")
    with pytest.raises(ValueError, match="Unsanitized"):
        make_plan(source, "current-only")


def test_unreferenced_public_asset_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    _write(source / "assets/public-ground-v3/secret.glb", "must not package")
    with pytest.raises(ValueError, match="Tileset references differ"):
        make_plan(source, "current-only")


def test_developer_map_active_pointer_and_rows_are_pinned(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    planned, _ = make_plan(source, "current-only")
    point_path = source / "data/property/developer_map/runs/developer-map-v1-cccccccccccccccc/points.jsonl"
    _write(point_path, '{"building_id":"b1","point_id":"p2"}\n')
    with pytest.raises(ValueError, match="points data differs"):
        make_plan(source, "current-only")

    _fixture(source)
    pointer_path = source / "data/property/developer_map/active.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["manifest_sha256"] = "0" * 64
    _write(pointer_path, json.dumps(pointer))
    with pytest.raises(ValueError, match="manifest differs from active pointer"):
        make_plan(source, "current-only")


def test_release_inventory_can_exceed_previous_ten_megabyte_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fixture(source)
    planned, pointer = make_plan(source, "current-only")
    output = apply_plan(source, tmp_path / "release", "current-only", planned, pointer)
    manifest = output / "release-manifest.json"
    body = manifest.read_bytes()
    manifest.write_bytes(body + b" " * (10_000_001 - len(body)))
    assert manifest.stat().st_size > 10_000_000
    assert verify_release(output)["file_count"] == len(planned)
