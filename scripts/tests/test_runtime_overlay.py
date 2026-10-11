from pathlib import Path
import hashlib
import json
import struct

import pytest

from scripts.package_deploy_data import apply_plan, make_plan
from scripts.runtime_overlay import developer_overlay_files
from scripts.tests.test_package_deploy_data import _fixture
from scripts.tests.test_runtime_enrichment import stage_overlay as stage_enrichment
from deploy.apply_release import verify_release


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _body(value) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _tile(registry_id: str = "r1") -> bytes:
    feature = json.dumps({"BATCH_LENGTH": 1}).encode()
    feature += b" " * (-(28 + len(feature)) % 8)
    batch = json.dumps({"registry_id": [registry_id]}).encode()
    batch += b" " * (-len(batch) % 8)
    document = json.dumps({"asset": {"version": "2.0"}, "scenes": [{}], "scene": 0}).encode()
    document += b" " * (-len(document) % 4)
    glb = struct.pack("<4s4I", b"glTF", 2, 20 + len(document), len(document), 0x4E4F534A) + document
    return struct.pack("<4s6I", b"b3dm", 1, 28 + len(feature) + len(batch) + len(glb),
                       len(feature), 0, len(batch), 0) + feature + batch + glb


def stage_geometry_overlay(root: Path, *, tile_registry_id: str = "r1"):
    pointer_path, pointer, _, _ = stage_enrichment(root)
    run_id = "developer-overlay-v1-" + "3" * 16
    run = root / "data/property/developer_overlay/runs" / run_id
    (run / "tiles").mkdir(parents=True)
    tile = _tile(tile_registry_id)
    tile_path = "tiles/" + _sha(tile) + ".b3dm"
    (run / tile_path).write_bytes(tile)
    feature_bytes = _body({"registry_id": "r1", "render_policy": "persistent_flat"})
    (run / "features.jsonl").write_bytes(feature_bytes)
    rows = [{"path": tile_path, "sha256": _sha(tile), "bytes": len(tile),
             "lod": lod, "feature_count": 1, "cell": [0, 0],
             "bbox_lonlat": [37.5, 55.6, 37.51, 55.61]} for lod in ("flat", "detail")]
    inventory = b"".join(_body(row) for row in rows)
    (run / "tile-inventory.jsonl").write_bytes(inventory)
    tileset = {"asset": {"version": "1.0"}, "root": {
        "content": {"uri": tile_path}, "children": [{"content": {"uri": tile_path}}]}}
    (run / "tileset.json").write_bytes(_body(tileset))
    manifest = {key: value for key, value in pointer.items() if key != "schema_version"}
    manifest.update(schema_version="developer-overlay-snapshot/1", run_id=run_id,
                    features_sha256=_sha(feature_bytes), features_total=1,
                    tile_inventory_sha256=_sha(inventory), tiles_total=2,
                    tile_bytes=len(tile), tileset_sha256=_sha(_body(tileset)),
                    alignment_audit_sha256="a" * 64, city_patch=None)
    pointer.update(schema_version="developer-experience-pointer/3", overlay_run_id=run_id)
    repin(pointer_path, pointer, run, manifest)
    return pointer_path, pointer, run, manifest, rows, tileset


def repin(path, pointer, run, manifest):
    (run / "manifest.json").write_bytes(_body(manifest))
    pointer["overlay_manifest_sha256"] = _sha(_body(manifest))
    path.write_bytes(_body(pointer))


def stage_city_patch(root: Path):
    staged = stage_geometry_overlay(root)
    path, pointer, run, manifest, _, _ = staged
    feature = {"registry_id": "r1", "source_group": "publisher", "publisher_project_id": "p1",
               "publisher_building_id": "b1", "city_feature_id_candidate": "city1",
               "polygon_sha256": "f" * 64}
    (run / "features.jsonl").write_bytes(_body(feature))
    manifest["features_sha256"] = _sha(_body(feature))
    base = root / "assets/public-citywide-v12"
    base.mkdir(parents=True, exist_ok=True)
    asset_pointer = json.loads((root / "assets/manifest.json").read_text())
    asset_pointer["city_tileset_url"] = "/assets/public-citywide-v12/tileset.json"
    (root / "assets/manifest.json").write_bytes(_body(asset_pointer))
    (base / "manifest.json").write_bytes(_body({"public_redacted_source_ids": True}))
    base_children = [{"content": {"uri": "region_0_0.tileset.json"}},
                     {"content": {"uri": "old-overview.glb"}}]
    if (base / "cell_1_2.glb").exists():
        base_children.append({"content": {"uri": "cell_1_2.glb"}})
    (base / "tileset.json").write_bytes(_body({"root": {"children": base_children}}))
    (base / "region_0_0.tileset.json").write_bytes(_body({"root": {"content": {"uri": "old-detail.glb"}}}))
    patch = run / "city-patch"
    (patch / "tiles").mkdir(parents=True)
    (patch / "regions").mkdir()
    tile_rows = []
    for lod, number in (("overview", 1), ("detail", 2)):
        source = f"old-{lod}.glb"
        (base / source).write_bytes(b"old city geometry " + lod.encode())
        body = struct.pack("<4s2I", b"glTF", 2, 16) + bytes([number]) * 4
        name = f"city-patch/tiles/{_sha(body)}.glb"
        (run / name).write_bytes(body)
        tile_rows.append({"path": name, "sha256": _sha(body), "bytes": len(body),
                          "source_tile": source, "source_sha256": _sha((base / source).read_bytes()),
                          "lod": lod, "suppressed_registry_ids": ["r1"]})
    region_body = _body({"asset": {"version": "1.0"}, "root": {
        "content": {"uri": "../tiles/" + Path(tile_rows[1]["path"]).name}}})
    region_name = f"city-patch/regions/{_sha(region_body)}.json"
    (run / region_name).write_bytes(region_body)
    region_rows = [{"path": region_name, "sha256": _sha(region_body), "bytes": len(region_body),
                    "source_region": "region_0_0.tileset.json", "source_sha256": _sha((base / "region_0_0.tileset.json").read_bytes())}]
    document = {"asset": {"version": "1.0"}, "root": {
        "content": {"uri": tile_rows[0]["path"].removeprefix("city-patch/")},
        "children": [{"content": {"uri": region_name.removeprefix("city-patch/")}}]}}
    descriptor = {"base_city_manifest_sha256": _sha((base / "manifest.json").read_bytes()),
                  "base_city_tileset_sha256": _sha((base / "tileset.json").read_bytes()),
                  "all_visible_lods_covered": True, "regions_total": 1,
                  "replacement_tiles_total": 2, "suppressed_total": 1,
                  "pair_proof_total": 1,
                  "detail_proof_manifest_sha256": "d" * 64,
                  "overview_proof_manifest_sha256": "e" * 64}
    for name, body, key in [
        ("tileset.json", _body(document), "tileset_sha256"),
        ("suppressed-registry-ids.json", _body(["r1"]), "suppressed_registry_ids_sha256"),
        ("pair-proof.jsonl", _body({"registry_id": "r1", "city_source_id": "city1",
          "geometry_source_id": "publisher:p1:b1", "source_polygon_sha256": "f" * 64}), "pair_proof_sha256"),
        ("region-inventory.jsonl", b"".join(map(_body, region_rows)), "region_inventory_sha256"),
        ("tile-inventory.jsonl", b"".join(map(_body, tile_rows)), "tile_inventory_sha256"),
    ]:
        (patch / name).write_bytes(body)
        descriptor[key] = _sha(body)
    manifest["city_patch"] = descriptor
    repin(path, pointer, run, manifest)
    return staged, tile_rows, document


def test_city_patch_exports_all_lods_and_pins_unchanged_base(tmp_path):
    _fixture(tmp_path)
    (path, pointer, run, manifest, _, _), _, _ = stage_city_patch(tmp_path)
    files = developer_overlay_files(tmp_path, pointer)
    assert len([name for name in files if "/city-patch/" in name]) == 8
    plan, assets = make_plan(tmp_path, "full-compat")
    output = apply_plan(tmp_path, tmp_path / "release", "full-compat", plan, assets)
    assert verify_release(output)["file_count"] == len(plan)


@pytest.mark.parametrize("failure", ["source_changed", "missing_overview", "old_geometry",
                                     "unsafe_uri", "wrong_patch_bytes", "unknown_suppression", "wrong_pair"])
def test_city_patch_rejects_partial_or_unpinned_replacement(tmp_path, failure):
    _fixture(tmp_path)
    (path, pointer, run, manifest, _, _), rows, document = stage_city_patch(tmp_path)
    patch = manifest["city_patch"]
    if failure == "source_changed":
        (tmp_path / "assets/public-citywide-v12/old-detail.glb").write_bytes(b"different base")
    elif failure == "missing_overview":
        rows.pop(0)
        body = b"".join(map(_body, rows))
        (run / "city-patch/tile-inventory.jsonl").write_bytes(body)
        patch.update(tile_inventory_sha256=_sha(body), replacement_tiles_total=len(rows))
    elif failure in {"old_geometry", "unsafe_uri"}:
        uri = ("/assets/public-citywide-v12/old-overview.glb" if failure == "old_geometry"
               else "../../outside.glb")
        document["root"]["children"].append({"content": {"uri": uri}})
        body = _body(document)
        (run / "city-patch/tileset.json").write_bytes(body)
        patch["tileset_sha256"] = _sha(body)
    elif failure == "wrong_patch_bytes":
        (run / rows[0]["path"]).write_bytes(b"wrong")
    elif failure == "unknown_suppression":
        body = _body(["neighbor"])
        (run / "city-patch/suppressed-registry-ids.json").write_bytes(body)
        patch["suppressed_registry_ids_sha256"] = _sha(body)
    elif failure == "wrong_pair":
        pair = json.loads((run / "city-patch/pair-proof.jsonl").read_text())
        pair["city_source_id"] = "different-city-building"
        body = _body(pair)
        (run / "city-patch/pair-proof.jsonl").write_bytes(body)
        patch["pair_proof_sha256"] = _sha(body)
    repin(path, pointer, run, manifest)
    with pytest.raises(ValueError):
        make_plan(tmp_path, "full-compat")


@pytest.mark.parametrize("failure", [None, "missing_model_pin", "wrong_model_pin"])
def test_lake_city_patch_requires_same_selected_model(tmp_path, failure):
    _fixture(tmp_path)
    (path, pointer, run, manifest, _, _), _, _ = stage_city_patch(tmp_path)
    feature = json.loads((run / "features.jsonl").read_text())
    feature.update(source_group="fsk", source_geometry_id="osm:way/1317814900@2")
    body = _body(feature)
    (run / "features.jsonl").write_bytes(body)
    manifest["features_sha256"] = _sha(body)
    pair = json.loads((run / "city-patch/pair-proof.jsonl").read_text())
    pair["geometry_source_id"] = feature["source_geometry_id"]
    body = _body(pair)
    (run / "city-patch/pair-proof.jsonl").write_bytes(body)
    manifest["city_patch"]["pair_proof_sha256"] = _sha(body)
    model = json.loads((tmp_path / "data/property/developer_models/runs" /
                        pointer["model_run_id"] / "models.jsonl").read_text())
    if failure != "missing_model_pin":
        manifest.update(lake_persistent=True,
                        lake_persistent_policy="near_only_reuse_selected_model_instance",
                        lake_persistent_model_sha256=("a" * 64 if failure == "wrong_model_pin"
                                                      else model["asset_sha256"]))
    repin(path, pointer, run, manifest)
    if failure is None:
        make_plan(tmp_path, "full-compat")
    else:
        with pytest.raises(ValueError, match="Lake"):
            make_plan(tmp_path, "full-compat")


def test_v3_package_preserves_v2_and_includes_only_pinned_derived_overlay(tmp_path):
    source = tmp_path / "source"
    _fixture(source)
    _, pointer, run, _, _, _ = stage_geometry_overlay(source)
    (run / "private-input.txt").write_text("private producer evidence must not be copied")
    files = developer_overlay_files(source, pointer)
    assert len(files) == 5  # One blob serves two logical LOD inventory rows.
    assert sum(name.endswith(".b3dm") for name in files) == 1
    plan, assets = make_plan(source, "full-compat")
    names = {item.relative for item in plan}
    assert set(files).issubset(names)
    assert not any("private-input" in name for name in names)
    assert any("developer_enrichment/runs/" in name for name in names)
    release = apply_plan(source, tmp_path / "release", "full-compat", plan, assets)
    assert verify_release(release)["file_count"] == len(names)


@pytest.mark.parametrize("failure", [
    "mixed_enrichment", "missing_tile", "corrupt_tile", "symlink_tile",
    "external_tileset", "extra_tileset_content", "duplicate_cell_lod",
    "byte_count", "duplicate_registry", "missing_manifest_hash", "schema_downgrade",
    "competing_pointer", "unvalidated_city_patch",
])
def test_v3_overlay_fails_closed_before_export(tmp_path, failure):
    source = tmp_path / "source"
    _fixture(source)
    path, pointer, run, manifest, rows, tileset = stage_geometry_overlay(source)
    tile = run / rows[0]["path"]
    if failure == "mixed_enrichment":
        manifest["enrichment_run_id"] = "developer-enrichment-v1-" + "f" * 16
    elif failure == "missing_tile":
        tile.unlink()
    elif failure == "corrupt_tile":
        tile.write_bytes(tile.read_bytes() + b"changed")
    elif failure == "symlink_tile":
        target = run / "saved-tile.bin"
        tile.rename(target)
        tile.symlink_to(target)
    elif failure in {"external_tileset", "extra_tileset_content"}:
        uri = ("https://external.invalid/tile.b3dm" if failure == "external_tileset"
               else "tiles/" + "f" * 64 + ".b3dm")
        tileset["root"]["children"].append({"content": {"uri": uri}})
        body = _body(tileset)
        (run / "tileset.json").write_bytes(body)
        manifest["tileset_sha256"] = _sha(body)
    elif failure == "duplicate_cell_lod":
        rows.append(rows[0])
        body = b"".join(_body(row) for row in rows)
        (run / "tile-inventory.jsonl").write_bytes(body)
        manifest.update(tiles_total=3, tile_inventory_sha256=_sha(body))
    elif failure == "byte_count":
        manifest["tile_bytes"] += 1
    elif failure == "duplicate_registry":
        body = (run / "features.jsonl").read_bytes() * 2
        (run / "features.jsonl").write_bytes(body)
        manifest.update(features_total=2, features_sha256=_sha(body))
    elif failure == "competing_pointer":
        (run.parent.parent / "active.json").write_text("{}")
    elif failure == "unvalidated_city_patch":
        manifest["city_patch"] = {"all_visible_lods_covered": False}
    repin(path, pointer, run, manifest)
    if failure == "missing_manifest_hash":
        del pointer["overlay_manifest_sha256"]
        path.write_bytes(_body(pointer))
    elif failure == "schema_downgrade":
        pointer["schema_version"] = "developer-experience-pointer/2"
        path.write_bytes(_body(pointer))
    with pytest.raises(ValueError):
        make_plan(source, "full-compat")


def test_tile_batch_identity_must_belong_to_overlay(tmp_path):
    _fixture(tmp_path)
    _, pointer, _, _, _, _ = stage_geometry_overlay(tmp_path, tile_registry_id="neighbor")
    with pytest.raises(ValueError, match="unknown registry"):
        developer_overlay_files(tmp_path, pointer)


def test_deploy_inventory_cannot_omit_a_hashed_overlay_blob(tmp_path):
    _fixture(tmp_path)
    _, pointer, _, _, _, _ = stage_geometry_overlay(tmp_path)
    files = developer_overlay_files(tmp_path, pointer)
    hashes = {name: _sha((tmp_path / name).read_bytes()) for name in files}
    hashes.pop(next(name for name in files if name.endswith(".b3dm")))
    with pytest.raises(ValueError, match="hash mismatch"):
        developer_overlay_files(tmp_path, pointer, hashes)
