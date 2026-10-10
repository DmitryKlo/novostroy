from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts import agent_data_bundle as bundle


def _zstd_or_skip() -> str:
    executable = shutil.which("zstd")
    if not executable:
        pytest.skip("zstd executable is required for archive roundtrip tests")
    return executable


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _fake_release(root: Path) -> Path:
    release = root / "release"
    run_id = "developer-map-v1-" + "a" * 16
    prefix = f"data/property/developer_map/runs/{run_id}"
    model_run = "models-v1-" + "c" * 16
    model_root = "public-project-models-v1-" + "d" * 16
    model_prefix = f"data/models/{model_run}"
    public_manifest = json.dumps({"public_redacted_source_ids": True,
                                  "files": [{"path": "model_" + "e" * 64 + ".glb"}]}).encode() + b"\n"
    public_manifest_sha = hashlib.sha256(public_manifest).hexdigest()
    files = {
        "data/property/estateline/projects.jsonl": b'{"project_id":"p1"}\n',
        "data/property/estateline/source_manifest.json": b'{"schema_version":"fixture"}\n',
        "data/models/active.json": b"",
        "assets/manifest.json": json.dumps({
            "public_redacted_source_ids": True,
            "previous_public_roots": [],
            "facade_models": 0,
            "project_models_root": model_root,
        }, sort_keys=True).encode() + b"\n",
        f"assets/{model_root}/public_manifest.json": public_manifest,
        f"assets/{model_root}/model_{'e' * 64}.glb": b"model fixture",
    }
    model_rows = {
        "assessments": b'{"assessment_id":"a1"}\n',
        "candidates": b'{"candidate_id":"c1"}\n',
        "source_evaluations": b'{"source_id":"s1"}\n',
    }
    files.update({f"{model_prefix}/{label}.jsonl": body
                  for label, body in model_rows.items()})
    model_manifest = {
        "schema_version": "model-candidates/1",
        "run_id": model_run,
        "public_root": model_root,
        "records_total": 1,
        "assessments_sha256": hashlib.sha256(model_rows["assessments"]).hexdigest(),
        "candidates_sha256": hashlib.sha256(model_rows["candidates"]).hexdigest(),
        "source_evaluations_sha256": hashlib.sha256(
            model_rows["source_evaluations"]).hexdigest(),
        "public_manifest_sha256": public_manifest_sha,
    }
    model_manifest_body = json.dumps(model_manifest, sort_keys=True).encode() + b"\n"
    files[f"{model_prefix}/manifest.json"] = model_manifest_body
    files["data/models/active.json"] = json.dumps({
        "run_id": model_run,
        "public_root": model_root,
        "manifest_sha256": hashlib.sha256(model_manifest_body).hexdigest(),
    }, sort_keys=True).encode() + b"\n"
    map_rows = {
        "points": b'{"building_id":"b1","point_id":"p1"}\n',
        "groups": b'{"group_id":"g1","point_ids":["p1"]}\n',
        "rejections": b'{"building_id":"b2","reason":"no_coordinates"}\n',
    }
    map_manifest = {
        "schema_version": "developer-map-snapshot/1",
        "run_id": run_id,
        "snapshot_id": "developer-map-snapshot-" + "b" * 16,
        "buildings_total": 2,
        "map_points_total": 1,
    }
    for label, body in map_rows.items():
        files[f"{prefix}/{label}.jsonl"] = body
        map_manifest[f"{label}_sha256"] = hashlib.sha256(body).hexdigest()
        map_manifest[f"{label}_total"] = body.count(b"\n")
    map_manifest_body = json.dumps(map_manifest, sort_keys=True).encode() + b"\n"
    files[f"{prefix}/manifest.json"] = map_manifest_body
    files["data/property/developer_map/active.json"] = json.dumps({
        "schema_version": "developer-map-pointer/1",
        "run_id": run_id,
        "manifest_sha256": hashlib.sha256(map_manifest_body).hexdigest(),
    }, sort_keys=True).encode() + b"\n"

    map_manifest_sha = hashlib.sha256(map_manifest_body).hexdigest()
    unit_run = "developer-units-v1-" + "f" * 16
    unit_prefix = f"data/property/developer_units/runs/{unit_run}"
    png = b"\x89PNG\r\n\x1a\nfixture"
    png_sha = hashlib.sha256(png).hexdigest()
    unit_rows = {
        "buildings": b'{"registry_id":"r1"}\n{"registry_id":"r2"}\n',
        "units": b'{"unit_id":"unit_000000000000000000000001"}\n',
        "floors": b"",
        "plans": json.dumps({
            "role": "apartment_plan", "unit_id": "unit_000000000000000000000001",
            "floor_id": None, "asset_sha256": png_sha, "asset_mime": "image/png",
        }, sort_keys=True).encode() + b"\n",
        "unit_aliases": b"",
    }
    unit_manifest = {
        "schema_version": "developer-units-snapshot/1",
        "run_id": unit_run,
        "map_run_id": run_id,
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_manifest_sha,
        "plan_assets_prepared": 1,
        "plan_asset_bytes": len(png),
    }
    for label, body in unit_rows.items():
        files[f"{unit_prefix}/{label}.jsonl"] = body
        unit_manifest[f"{label}_sha256"] = hashlib.sha256(body).hexdigest()
        unit_manifest[f"{label}_total"] = body.count(b"\n")
    files[f"{unit_prefix}/assets/{png_sha}.png"] = png
    unit_manifest_body = json.dumps(unit_manifest, sort_keys=True).encode() + b"\n"
    files[f"{unit_prefix}/manifest.json"] = unit_manifest_body

    selected_model_run = "developer-models-v1-" + "9" * 16
    selected_model_prefix = f"data/property/developer_models/runs/{selected_model_run}"
    glb = b"glTF" + b"\x02\x00\x00\x00" + (12).to_bytes(4, "little")
    glb_sha = hashlib.sha256(glb).hexdigest()
    selected_model_rows = json.dumps({
        "registry_id": "r1", "asset_sha256": glb_sha, "asset_bytes": len(glb),
    }, sort_keys=True).encode() + b"\n"
    files[f"{selected_model_prefix}/models.jsonl"] = selected_model_rows
    files[f"{selected_model_prefix}/assets/{glb_sha}.glb"] = glb
    selected_model_manifest = {
        "schema_version": "developer-models-snapshot/1",
        "run_id": selected_model_run,
        "map_run_id": run_id,
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_manifest_sha,
        "menu_run_id": unit_run,
        "menu_manifest_sha256": hashlib.sha256(unit_manifest_body).hexdigest(),
        "models_sha256": hashlib.sha256(selected_model_rows).hexdigest(),
        "models_total": 1,
        "assets_total": 1,
        "assets_bytes": len(glb),
    }
    selected_model_manifest_body = json.dumps(selected_model_manifest, sort_keys=True).encode() + b"\n"
    files[f"{selected_model_prefix}/manifest.json"] = selected_model_manifest_body
    files["data/property/developer_experience/active.json"] = json.dumps({
        "schema_version": "developer-experience-pointer/1",
        "map_run_id": run_id,
        "map_snapshot_id": map_manifest["snapshot_id"],
        "map_manifest_sha256": map_manifest_sha,
        "unit_run_id": unit_run,
        "unit_manifest_sha256": hashlib.sha256(unit_manifest_body).hexdigest(),
        "model_run_id": selected_model_run,
        "model_manifest_sha256": hashlib.sha256(selected_model_manifest_body).hexdigest(),
    }, sort_keys=True).encode() + b"\n"

    from scripts.tests.runtime_fixture import routing_contents
    files.update(routing_contents())
    records = []
    for relative, body in files.items():
        _write(release / relative, body)
        records.append({
            "path": relative,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "group": "fixture",
        })
    manifest = {
        "schema_version": "novostroy-deploy-data/1",
        "profile": "full-compat",
        "data_mode": "real",
        "registry_mode": "estateline",
        "file_count": len(records),
        "bytes_total": sum(row["bytes"] for row in records),
        "files": records,
    }
    _write(release / "release-manifest.json", json.dumps(manifest, sort_keys=True).encode() + b"\n")
    return release


def test_inventory_hashes_data_and_excludes_headers_credentials_and_process_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write(source / "data/property/developer_map/active.json", b'{"run_id":"r1"}')
    _write(source / ".runtime/research/developer-harvest/unit.jsonl", b'{"unit_id":"u1"}\n')
    _write(source / ".runtime/research/capture.headers", b"Set-Cookie: do-not-read\n")
    _write(source / ".runtime/research/run.lock", b"active process marker")
    _write(source / ".runtime/research/client-secret.json", b"never read")
    _write(source / "assets/geometry.glb", b"model")
    called: list[str] = []
    original_hash = bundle._hash_file

    def tracked_hash(path: Path, **kwargs: object):
        called.append(path.name)
        assert path.name not in {"capture.headers", "run.lock", "client-secret.json"}
        return original_hash(path, **kwargs)

    monkeypatch.setattr(bundle, "_hash_file", tracked_hash)
    files, excluded = bundle._scan_roots(source)
    paths = {row.logical_path for row in files}
    assert paths == {
        "data/property/developer_map/active.json",
        ".runtime/research/developer-harvest/unit.jsonl",
        "assets/geometry.glb",
    }
    assert len(called) == 3
    assert excluded["response_headers_may_contain_cookies"] == 1
    assert excluded["process_or_temporary_state"] == 1
    assert excluded["credential_or_secret_name"] == 1

    inventory = bundle.create_inventory(source, tmp_path / "inventory")
    manifest = json.loads((inventory / "manifest.json").read_text())
    coverage = json.loads((inventory / "coverage.json").read_text())
    assert manifest["schema_version"] == bundle.INVENTORY_SCHEMA
    assert manifest["file_count"] == 3
    assert coverage["by_category"]["catalogs_units_and_geocode_ledgers"]["files"] == 1
    assert (inventory / "manifest.sha256").is_file()


def test_inventory_preserves_only_allowlisted_hidden_historical_assets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for folder in (".citywide-v9-sample", ".citywide-v9-sample-5m"):
        _write(source / "assets" / folder / "manifest.json", b'{"version":"fixture"}\n')
        _write(source / "assets" / folder / "massing_region_752_1115.glb", b"fixture GLB")
    _write(source / "assets/.citywide-v9-sample/unlisted.bin", b"not allowlisted")
    _write(source / "assets/.unknown-hidden/manifest.json", b"not allowlisted")

    files, excluded = bundle._scan_roots(source)
    assert {item.logical_path for item in files} == {
        "assets/.citywide-v9-sample/manifest.json",
        "assets/.citywide-v9-sample/massing_region_752_1115.glb",
        "assets/.citywide-v9-sample-5m/manifest.json",
        "assets/.citywide-v9-sample-5m/massing_region_752_1115.glb",
    }
    assert excluded["unsafe_path"] == 2
    assert bundle._safe_relative(
        "evidence/assets/.citywide-v9-sample/manifest.json", allow_runtime_root=True
    ) == "evidence/assets/.citywide-v9-sample/manifest.json"
    with pytest.raises(bundle.BundleError, match="Hidden bundle path"):
        bundle._safe_relative("deployment/assets/.citywide-v9-sample/manifest.json",
                              allow_runtime_root=True)


def test_inventory_publishes_atomically_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _write(source / "data/one.json", b"one")
    output_root = tmp_path / "inventories"
    original = bundle._atomic_write

    def fail_before_manifest(path: Path, body: bytes, mode: int = 0o644) -> None:
        if path.name == "manifest.json":
            raise OSError("simulated write failure")
        original(path, body, mode)

    monkeypatch.setattr(bundle, "_atomic_write", fail_before_manifest)
    with pytest.raises(OSError, match="simulated write failure"):
        bundle.create_inventory(source, output_root)
    assert list(output_root.iterdir()) == []


def test_atomic_directory_publication_refuses_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "staging"
    target = tmp_path / "release"
    source.mkdir()
    target.mkdir()
    with pytest.raises(FileExistsError):
        bundle._rename_directory_noreplace(source, target)
    assert source.is_dir()
    assert target.is_dir()


def test_evidence_bundle_roundtrip_is_content_addressed_and_reproducible(tmp_path: Path) -> None:
    zstd = _zstd_or_skip()
    source = tmp_path / "source"
    _write(source / "data/research/duplicate.bin", b"same-content" * 20)
    _write(source / "assets/old/duplicate-copy.bin", b"same-content" * 20)
    _write(source / ".runtime/research/fr-mos-20261010/map.body", b"registry map payload")
    output_root = tmp_path / "exports"
    first = bundle.create_bundle(
        source_root=source,
        output_root=output_root,
        bundle_id="evidence-a",
        release_dir=None,
        chunk_bytes=64,
        zstd_bin=zstd,
    )
    second = bundle.create_bundle(
        source_root=source,
        output_root=output_root,
        bundle_id="evidence-b",
        release_dir=None,
        chunk_bytes=64,
        zstd_bin=zstd,
    )
    result = bundle.verify_bundle(first, zstd_bin=zstd)
    assert result["bundle_type"] == "evidence_snapshot"
    assert result["files"] == 3
    assert result["blobs"] == 2
    assert result["unique_bytes"] < result["logical_bytes"]
    first_manifest = json.loads((first / "manifest.json").read_text())
    second_manifest = json.loads((second / "manifest.json").read_text())
    assert [row["sha256"] for row in first_manifest["chunks"]] == [
        row["sha256"] for row in second_manifest["chunks"]
    ]
    assert first_manifest["chunks"] == second_manifest["chunks"]
    with pytest.raises(bundle.BundleError, match="cannot be imported"):
        bundle.import_deployment(first, tmp_path, zstd_bin=zstd)


def test_import_verifies_stages_and_atomically_installs_only_runtime_files(tmp_path: Path) -> None:
    zstd = _zstd_or_skip()
    source = tmp_path / "source"
    _write(source / "data/research/source-catalog.json", b"source-only evidence")
    _write(source / "assets/old/geometry.glb", b"historical geometry")
    release = _fake_release(tmp_path)
    deploy = tmp_path / "exports"
    ready = bundle.create_bundle(
        source_root=source,
        output_root=deploy,
        bundle_id="runtime-20261010-test",
        release_dir=release,
        chunk_bytes=64,
        zstd_bin=zstd,
    )
    assert bundle.verify_bundle(ready, zstd_bin=zstd)["bundle_type"] == "deployment_and_evidence"
    release_root = tmp_path / "releases"
    release_root.mkdir()
    installed = bundle.import_deployment(ready, release_root, zstd_bin=zstd)

    assert installed == release_root / "runtime-20261010-test"
    assert (installed / "release-manifest.json").is_file()
    assert (installed / "data/property/estateline/projects.jsonl").read_bytes() == b'{"project_id":"p1"}\n'
    assert not (installed / "evidence").exists()
    assert not list(release_root.glob(".staging-*"))
    assert not list(release_root.glob(".objects-*"))
    with pytest.raises(FileExistsError):
        bundle.import_deployment(ready, release_root, zstd_bin=zstd)


def test_bundle_rejects_traversal_and_tampered_chunk(tmp_path: Path) -> None:
    zstd = _zstd_or_skip()
    source = tmp_path / "source"
    _write(source / "data/one.json", b"one")
    output_root = tmp_path / "exports"
    ready = bundle.create_bundle(
        source_root=source,
        output_root=output_root,
        bundle_id="evidence-unsafe-test",
        release_dir=None,
        chunk_bytes=1024,
        zstd_bin=zstd,
    )
    manifest_path = ready / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "evidence/../escape"
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    manifest_path.write_bytes(body)
    (ready / "manifest.sha256").write_text(hashlib.sha256(body).hexdigest() + "  manifest.json\n")
    with pytest.raises(bundle.BundleError, match="Unsafe bundle path"):
        bundle.verify_bundle(ready, zstd_bin=zstd)

    manifest = json.loads(body)
    manifest["files"][0]["path"] = "evidence/data/one.json"
    body = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    manifest_path.write_bytes(body)
    (ready / "manifest.sha256").write_text(hashlib.sha256(body).hexdigest() + "  manifest.json\n")
    chunk_path = ready / manifest["chunks"][0]["path"]
    chunk_path.write_bytes(chunk_path.read_bytes() + b"tampered")
    with pytest.raises(bundle.BundleError, match="wrong-sized chunk|checksum mismatch"):
        bundle.verify_bundle(ready, zstd_bin=zstd)
