import hashlib
import json
import shutil
from pathlib import Path

import pytest

from deploy.apply_release import verify_release
from scripts.package_deploy_data import apply_plan, make_plan
from scripts.runtime_model_styles import developer_model_style_files
from scripts.tests.test_package_deploy_data import _fixture
from sources.property.developer_facade_styles import build_project_facade_style
from sources.property.tests.test_developer_facade_styles import _observation


def _body(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def stage_model_styles(root: Path):
    pointer_path = root / "data/property/developer_experience/active.json"
    pointer = json.loads(pointer_path.read_text())
    prefix = f"data/property/developer_models/runs/{pointer['model_run_id']}"
    run = root / prefix
    manifest = json.loads((run / "manifest.json").read_text())
    baseline_id = "developer-models-v1-" + "0" * 16
    baseline = run.parent / baseline_id
    shutil.copytree(run, baseline)
    base_manifest = {**manifest, "run_id": baseline_id}
    (baseline / "manifest.json").write_bytes(_body(base_manifest))
    manifest.update(base_model_run_id=baseline_id,
                    base_model_manifest_sha256=_sha(_body(base_manifest)))
    bundle = build_project_facade_style(_observation())
    material = {**bundle.material_record,
                "source_evidence_sha256": bundle.private_evidence["source_evidence_sha256"]}
    inventory = []
    (run / "styles/textures").mkdir(parents=True)
    for lod in material["lods"]:
        png = bundle.textures[lod["asset_path"]]
        path = "styles/" + lod["asset_path"]
        (run / path).write_bytes(png)
        inventory.append({"path": path, "sha256": lod["sha256"], "bytes": len(png),
                          "style_id": material["style_id"], "level": lod["level"]})
    model = json.loads((run / "models.jsonl").read_text())
    model.update(style_id=material["style_id"], source_project_id=material["publisher_project_id"],
                 facade_style_basis="project_style_inference",
                 facade_style_evidence_sha256=material["source_evidence_sha256"],
                 texture_basis="generated_project_style_repeat_unoriented",
                 texture_sha256=material["lods"][1]["sha256"])
    manifest.update(style_batch_manifest_sha256="a" * 64, styles_materials_total=1,
                    styles_textures_total=3, styled_models_total=1)
    def save():
        (run / "styles/materials.jsonl").write_bytes(_body(material))
        rows = b"".join(map(_body, inventory))
        (run / "styles/texture-inventory.jsonl").write_bytes(rows)
        (run / "models.jsonl").write_bytes(_body(model))
        manifest.update(styles_materials_sha256=_sha(_body(material)),
                        styles_texture_inventory_sha256=_sha(rows), models_sha256=_sha(_body(model)))
        (run / "manifest.json").write_bytes(_body(manifest))
        pointer["model_manifest_sha256"] = _sha(_body(manifest))
        pointer_path.write_bytes(_body(pointer))
    save()
    return prefix, run, manifest, material, inventory, model, save


def test_style_release_includes_three_generated_tiers_without_private_captures(tmp_path):
    _fixture(tmp_path)
    prefix, run, manifest, _, _, _, _ = stage_model_styles(tmp_path)
    (run / "styles/private-source.jpg").write_bytes(b"private capture")
    files = developer_model_style_files(tmp_path, prefix, manifest)
    assert len(files) == 8  # Five style files plus baseline manifest, rows and GLB.
    plan, assets = make_plan(tmp_path, "full-compat")
    assert set(files).issubset({row.relative for row in plan})
    assert not any("private-source" in row.relative for row in plan)
    output = apply_plan(tmp_path, tmp_path / "release", "full-compat", plan, assets)
    assert verify_release(output)["file_count"] == len(plan)


@pytest.mark.parametrize("failure", ["missing_tier", "wrong_project", "private_url",
                                     "corrupt_png", "wrong_dimensions", "symlink", "mixed_pin",
                                     "missing_baseline", "changed_height", "wrong_baseline_pin"])
def test_style_inventory_rejects_incomplete_private_or_inconsistent_assets(tmp_path, failure):
    _fixture(tmp_path)
    prefix, run, manifest, material, inventory, model, save = stage_model_styles(tmp_path)
    if failure == "missing_tier":
        inventory.pop()
        manifest["styles_textures_total"] -= 1
    elif failure == "wrong_project":
        model["source_project_id"] = "neighbor-project"
    elif failure == "private_url":
        material["source_url"] = "https://private.example/capture"
    elif failure in {"corrupt_png", "symlink"}:
        path = run / inventory[0]["path"]
        if failure == "corrupt_png":
            path.write_bytes(path.read_bytes() + b"changed")
        else:
            other = run / "saved.png"
            path.rename(other)
            path.symlink_to(other)
    elif failure == "wrong_dimensions":
        # Even a newly pinned replacement with internally consistent SHA references
        # cannot masquerade as a different required resolution tier.
        old = run / inventory[0]["path"]
        png = bytearray(old.read_bytes())
        png[16:20] = (999).to_bytes(4, "big")
        digest = _sha(png)
        path = "styles/textures/" + digest + ".png"
        (run / path).write_bytes(png)
        inventory[0].update(path=path, sha256=digest)
        material["lods"][0].update(sha256=digest, asset_path="textures/" + digest + ".png",
                                  asset_uri="facade-style://sha256/" + digest)
    elif failure == "mixed_pin":
        manifest["style_batch_manifest_sha256"] = "invalid"
    elif failure == "missing_baseline":
        (run.parent / manifest["base_model_run_id"] / "models.jsonl").unlink()
    elif failure == "changed_height":
        model["height_m"] = 999
    elif failure == "wrong_baseline_pin":
        manifest["base_model_manifest_sha256"] = "f" * 64
    save()
    with pytest.raises(ValueError):
        make_plan(tmp_path, "full-compat")
