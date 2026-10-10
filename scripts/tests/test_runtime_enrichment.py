from pathlib import Path
import json
import hashlib
import pytest

from scripts.package_deploy_data import apply_plan, make_plan, developer_enrichment_files
from deploy.apply_release import verify_release
from scripts.tests.test_package_deploy_data import _fixture


def stage_overlay(root: Path):
    path = root / "data/property/developer_experience/active.json"
    pointer = json.loads(path.read_text())
    run_id = "developer-enrichment-v1-" + "1"*16
    run = root / "data/property/developer_enrichment/runs" / run_id
    run.mkdir(parents=True)
    unit = root / "data/property/developer_units/runs" / pointer["unit_run_id"]
    menu = json.loads((unit / "manifest.json").read_text())
    plan = json.loads((unit / "plans.jsonl").read_text())
    asset = unit / "assets" / (plan["asset_sha256"]+".png")
    rows = {"buildings": b'{"registry_id":"r1"}\n{"registry_id":"r2"}\n',
            "units": b'{"unit_id":"u1","registry_id":"r1"}\n',
            "media": (json.dumps({"media_id": "m1", "prepared_source": "menu_asset",
                                  "prepared_sha256": plan["asset_sha256"], "prepared_mime": "image/png",
                                  "prepared_bytes": asset.stat().st_size})+"\n").encode()}
    manifest = {k: v for k, v in pointer.items() if k != "schema_version"}
    manifest.update(schema_version="developer-enrichment-snapshot/1", run_id=run_id,
                    geography_input_sha256=None, noise_input_sha256=None)
    for label, body in rows.items():
        (run / f"{label}.jsonl").write_bytes(body)
        manifest[f"{label}_sha256"] = hashlib.sha256(body).hexdigest()
        manifest[f"{label}_total"] = body.count(b"\n")
    assert menu["buildings_total"] == 2
    (run / "manifest.json").write_text(json.dumps(manifest))
    pointer.update(schema_version="developer-experience-pointer/2", enrichment_run_id=run_id,
                   enrichment_manifest_sha256=hashlib.sha256((run / "manifest.json").read_bytes()).hexdigest())
    path.write_text(json.dumps(pointer))
    return path, pointer, run, manifest


def repin(path: Path, pointer: dict, run: Path, manifest: dict):
    (run / "manifest.json").write_text(json.dumps(manifest))
    pointer["enrichment_manifest_sha256"] = hashlib.sha256((run / "manifest.json").read_bytes()).hexdigest()
    path.write_text(json.dumps(pointer))


def test_v2_export_keeps_only_pinned_overlay_and_existing_menu_asset(tmp_path: Path):
    source = tmp_path / "source"
    _fixture(source)
    _, pointer, run, _ = stage_overlay(source)
    cache = source / "data/runtime/media/private.bin"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"not release state")
    plan, asset_pointer = make_plan(source, "full-compat")
    files = [p.relative for p in plan]
    overlay_files = developer_enrichment_files(source, pointer)
    assert len(overlay_files) == 4
    assert all(str(run.relative_to(source)) in name for name in overlay_files)
    assert not any("data/runtime/" in name for name in files)
    output = apply_plan(source, tmp_path / "release", "full-compat", plan, asset_pointer)
    assert verify_release(output)["file_count"] == len(files)


@pytest.mark.parametrize("failure", ["map", "count", "evidence_sha", "unknown_media_source", "media_bytes"])
def test_overlay_rejects_inconsistent_pins_and_prepared_media(tmp_path: Path, failure: str):
    source = tmp_path / "source"
    _fixture(source)
    path, pointer, run, manifest = stage_overlay(source)
    if failure == "map":
        manifest["map_run_id"] = "developer-map-v1-"+"2"*16
    elif failure == "count":
        manifest["units_total"] = 2
    elif failure == "evidence_sha":
        manifest["geography_input_sha256"] = "bad"
    else:
        media = json.loads((run / "media.jsonl").read_text())
        if failure == "unknown_media_source":
            media["prepared_source"] = "runtime_cache"
        else:
            media["prepared_bytes"] += 1
        body = (json.dumps(media)+"\n").encode()
        (run / "media.jsonl").write_bytes(body)
        manifest["media_sha256"] = hashlib.sha256(body).hexdigest()
    repin(path, pointer, run, manifest)
    with pytest.raises(ValueError):
        make_plan(source, "full-compat")


def test_unlisted_overlay_file_and_competing_pointer_are_rejected(tmp_path: Path):
    source = tmp_path / "source"
    _fixture(source)
    _, pointer, run, _ = stage_overlay(source)
    files = developer_enrichment_files(source, pointer)
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
    hashes.pop(str((run / "media.jsonl").relative_to(source)))
    with pytest.raises(ValueError, match="hash mismatch"):
        developer_enrichment_files(source, pointer, hashes)
    competing = source / "data/property/developer_enrichment/active.json"
    competing.write_text("{}")
    with pytest.raises(ValueError, match="Separate"):
        make_plan(source, "full-compat")
