"""Socket-free checks for a VM data release and rollback."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import apply_release, build_local_images, smoke_local
from scripts.tests.runtime_fixture import routing_contents


def fixture_release(root: Path) -> Path:
    release = root / "release"
    run_id = "developer-map-v1-" + "a" * 16
    run_prefix = f"data/property/developer_map/runs/{run_id}/"
    map_rows = {"points": b"{}\n", "groups": b"", "rejections": b""}
    map_manifest = {"schema_version": "developer-map-snapshot/1", "run_id": run_id,
                    "snapshot_id": "developer-map-snapshot-" + "b" * 16,
                    "buildings_total": 1, "map_points_total": 1}
    for name, content in map_rows.items():
        map_manifest[f"{name}_sha256"] = hashlib.sha256(content).hexdigest()
        map_manifest[f"{name}_total"] = 1 if name == "points" else 0
    map_manifest_bytes = json.dumps(map_manifest).encode("utf-8")
    map_pointer = json.dumps({"schema_version": "developer-map-pointer/1",
                              "run_id": run_id,
                              "manifest_sha256": hashlib.sha256(map_manifest_bytes).hexdigest()}).encode("utf-8")
    model_run = "models-v1-" + "c" * 16
    model_root = "public-project-models-v1-" + "c" * 16
    model_rows = {"assessments": b"{}\n", "candidates": b"",
                  "source_evaluations": b"{}\n"}
    public_manifest = b"{}\n"
    model_manifest = {"schema_version": "model-candidates/1", "run_id": model_run,
                      "public_root": model_root, "records_total": 1,
                      "public_manifest_sha256": hashlib.sha256(public_manifest).hexdigest()}
    for name, body in model_rows.items():
        model_manifest[name + "_sha256"] = hashlib.sha256(body).hexdigest()
    model_manifest_bytes = json.dumps(model_manifest).encode("utf-8")
    model_pointer = json.dumps({"run_id": model_run,
                                "manifest_sha256": hashlib.sha256(model_manifest_bytes).hexdigest()}).encode("utf-8")
    identity = {"map_run_id": run_id,
                "map_snapshot_id": map_manifest["snapshot_id"],
                "map_manifest_sha256": hashlib.sha256(map_manifest_bytes).hexdigest()}
    unit_run = "developer-units-v1-" + "d" * 16
    unit_rows = {"buildings": b"{}\n", "units": b"{}\n", "floors": b"{}\n",
                 "plans": b"", "unit_aliases": b""}
    unit_manifest = {"schema_version": "developer-units-snapshot/1",
                     "run_id": unit_run, **identity,
                     "plan_assets_prepared": 0, "plan_asset_bytes": 0}
    for name, body in unit_rows.items():
        unit_manifest[name + "_sha256"] = hashlib.sha256(body).hexdigest()
        unit_manifest[name + "_total"] = body.count(b"\n")
    unit_manifest_bytes = json.dumps(unit_manifest).encode("utf-8")
    unit_manifest_sha = hashlib.sha256(unit_manifest_bytes).hexdigest()
    selected_run = "developer-models-v1-" + "e" * 16
    selected_asset = b"glTF" + b"\x00" * 8
    selected_asset_sha = hashlib.sha256(selected_asset).hexdigest()
    selected_row = (json.dumps({"asset_sha256": selected_asset_sha,
                                "asset_bytes": len(selected_asset)}) + "\n").encode("utf-8")
    selected_manifest = {"schema_version": "developer-models-snapshot/1",
                         "run_id": selected_run, **identity,
                         "menu_run_id": unit_run,
                         "menu_manifest_sha256": unit_manifest_sha,
                         "models_sha256": hashlib.sha256(selected_row).hexdigest(),
                         "models_total": 1, "assets_total": 1,
                         "assets_bytes": len(selected_asset)}
    selected_manifest_bytes = json.dumps(selected_manifest).encode("utf-8")
    experience_pointer = {"schema_version": "developer-experience-pointer/1",
                          **identity, "unit_run_id": unit_run,
                          "unit_manifest_sha256": unit_manifest_sha,
                          "model_run_id": selected_run,
                          "model_manifest_sha256": hashlib.sha256(selected_manifest_bytes).hexdigest()}
    contents = {
        "assets/manifest.json": json.dumps({"project_models_root": model_root}).encode("utf-8"),
        f"assets/{model_root}/public_manifest.json": public_manifest,
        "data/property/estateline/projects.jsonl": b"{}\n",
        "data/property/estateline/source_manifest.json": b"{}\n",
        "data/models/active.json": model_pointer,
        f"data/models/{model_run}/manifest.json": model_manifest_bytes,
        **{f"data/models/{model_run}/{name}.jsonl": content
           for name, content in model_rows.items()},
        "data/property/developer_map/active.json": map_pointer,
        run_prefix + "manifest.json": map_manifest_bytes,
        **{run_prefix + name + ".jsonl": content for name, content in map_rows.items()},
        "data/property/developer_experience/active.json": json.dumps(experience_pointer).encode("utf-8"),
        f"data/property/developer_units/runs/{unit_run}/manifest.json": unit_manifest_bytes,
        **{f"data/property/developer_units/runs/{unit_run}/{name}.jsonl": body
           for name, body in unit_rows.items()},
        f"data/property/developer_models/runs/{selected_run}/manifest.json": selected_manifest_bytes,
        f"data/property/developer_models/runs/{selected_run}/models.jsonl": selected_row,
        f"data/property/developer_models/runs/{selected_run}/assets/{selected_asset_sha}.glb": selected_asset,
    }
    contents.update(routing_contents())
    files = []
    for relative, body in contents.items():
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files.append({"path": relative, "bytes": len(body),
                      "sha256": hashlib.sha256(body).hexdigest()})
    (release / "release-manifest.json").write_text(json.dumps({
        "schema_version": "novostroy-deploy-data/1", "data_mode": "real",
        "registry_mode": "estateline", "profile": "full-compat",
        "file_count": len(files), "bytes_total": sum(item["bytes"] for item in files), "files": files,
    }), encoding="utf-8")
    return release


class DeployReleaseTests(unittest.TestCase):
    def test_local_builder_requires_exact_clean_branch1_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "branch1", str(checkout)], check=True)
            readme = checkout / "README.md"
            readme.write_text("reviewed source\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c",
                            "user.email=test@example.invalid", "commit", "-qm", "source"],
                           cwd=checkout, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                 check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            with patch.object(build_local_images, "ROOT", checkout), \
                    patch.object(build_local_images, "validate_checkout",
                                 return_value={"tracked_files": 1, "tracked_bytes": 16}):
                self.assertEqual(build_local_images.check_source(sha)["source_sha"], sha)
                readme.write_text("unreviewed edit\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "dirty"):
                    build_local_images.check_source(sha)

    def test_local_images_require_matching_revision_labels(self) -> None:
        tag = "a" * 40
        valid = subprocess.CompletedProcess([], 0,
                                            json.dumps({"org.opencontainers.image.revision": tag}), "")
        with patch.object(apply_release.subprocess, "run", return_value=valid) as inspect:
            apply_release._verify_local_images(tag)
        self.assertEqual(inspect.call_count, 2)
        mismatch = subprocess.CompletedProcess([], 0,
                                               json.dumps({"org.opencontainers.image.revision": "b" * 40}), "")
        with patch.object(apply_release.subprocess, "run", return_value=mismatch):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                apply_release._verify_local_images(tag)

    def test_local_apply_skips_registry_pull_and_requires_both_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            settings = apply_release._settings(release, "a" * 40, "127.0.0.1", 8080)
            with patch.object(apply_release, "_compose") as compose, \
                    patch.object(apply_release, "_compose_output", return_value=""), \
                    patch.object(apply_release, "_verify_local_images") as images, \
                    patch.object(apply_release, "_smoke"), \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "full-compat"}):
                apply_release.apply(settings, control, local_images=True)
            images.assert_called_once_with("a" * 40)
            commands = [call.args[1:] for call in compose.call_args_list]
            self.assertFalse(any(command[0] == "pull" for command in commands))
            self.assertTrue(any(command[:2] == ("up", "-d") and
                                command[-3:] == ("never", "api", "web") for command in commands))

    def test_nearby_smoke_accepts_partial_availability_but_requires_real_routes(self) -> None:
        point_id = "mappt_" + "a" * 24
        snapshot = "developer-map-snapshot-" + "b" * 16
        run_id = "developer-map-v1-" + "c" * 16
        route = {"status": "routed", "distance_m": 480,
                 "estimated_minutes": 7, "geometry": [[37.6, 55.7], [37.61, 55.71]]}
        item = {"id": "poi_" + "d" * 24, "category": "metro_station", "route": route}
        payload = {"status": "ok", "point_id": point_id,
                   "registry_snapshot_id": snapshot, "map_run_id": run_id,
                   "layers": {"metro": {"items": [item]},
                              "education": {"items": []}, "medical": {"items": []}}}
        self.assertTrue(smoke_local._verify_nearby(payload, point_id, snapshot, run_id))
        with self.assertRaisesRegex(RuntimeError, "selected map generation"):
            smoke_local._verify_nearby({**payload, "map_run_id": "other"},
                                       point_id, snapshot, run_id)
        ferry = {**item, "route": {"status": "contains_ferry", "distance_m": None,
                                   "estimated_minutes": 7, "geometry": None}}
        with self.assertRaisesRegex(RuntimeError, "failed route"):
            smoke_local._verify_nearby({**payload, "layers": {
                **payload["layers"], "metro": {"items": [ferry]}}},
                point_id, snapshot, run_id)
        no_route = {**item, "route": {"status": "no_route", "distance_m": None,
                                     "estimated_minutes": None, "geometry": None}}
        self.assertFalse(smoke_local._verify_nearby({**payload, "layers": {
            **payload["layers"], "metro": {"items": [no_route]}}},
            point_id, snapshot, run_id))

    def test_live_developer_payloads_pin_one_generation(self) -> None:
        run_id = "developer-map-v1-" + "a" * 16
        snapshot_id = "developer-map-snapshot-" + "b" * 16
        unit_run_id = "developer-units-v1-" + "d" * 16
        health = {"data_mode": "real", "catalog_view": "developer_buildings",
                  "status": "developer_buildings_ready", "map_run_id": run_id,
                  "registry_snapshot_id": snapshot_id, "unit_run_id": unit_run_id}
        buildings = {"dataset_id": "developer_building_registry",
                     "snapshot_id": snapshot_id, "map_run_id": run_id,
                     "unit_run_id": unit_run_id,
                     "dataset_total": 3, "source_total": 3, "archived_total": 0,
                     "facets": {"map_eligible_count": 2}}
        points = {"dataset_id": "developer_map_points",
                  "registry_snapshot_id": snapshot_id, "map_run_id": run_id,
                  "unit_run_id": unit_run_id,
                  "dataset_total": 3,
                  "counts": {"points": 2, "developer_building_points": 2,
                             "developer_building_points_total": 2,
                             "unlocated_buildings": 1, "mapped_construction_points": 0},
                  "group_evidence": {"schema_version": 1, "visibility": "public",
                                     "registry_snapshot_id": snapshot_id, "map_run_id": run_id,
                                     "groups": []},
                  "items": [{"point_type": "developer_building"} for _ in range(2)]}
        self.assertEqual(apply_release.verify_developer_payloads(health, buildings, points),
                         {"map_run_id": run_id, "unit_run_id": unit_run_id,
                          "buildings": 3, "points": 2})
        with self.assertRaisesRegex(RuntimeError, "ready real developer-building map"):
            apply_release.verify_developer_payloads(
                {**health, "catalog_view": "building_registry"}, buildings, points)
        with self.assertRaisesRegex(RuntimeError, "active map generation"):
            apply_release.verify_developer_payloads(
                health, {**buildings, "map_run_id": "developer-map-v1-" + "c" * 16}, points)
        with self.assertRaisesRegex(RuntimeError, "active building list"):
            apply_release.verify_developer_payloads(
                health, buildings, {**points, "counts": {**points["counts"],
                                                         "mapped_construction_points": 2}})
        with self.assertRaisesRegex(RuntimeError, "active building list"):
            apply_release.verify_developer_payloads(
                health, buildings,
                {**points, "group_evidence": {**points["group_evidence"],
                                               "map_run_id": "developer-map-v1-" + "c" * 16}})

        selected_id = "registry-" + "e" * 24
        common = {"registry_snapshot_id": snapshot_id, "map_run_id": run_id,
                  "unit_run_id": unit_run_id, "registry_id": selected_id}
        pointer = {"schema_version": "developer-experience-pointer/1",
                   "map_snapshot_id": snapshot_id, "map_run_id": run_id,
                   "unit_run_id": unit_run_id,
                   "model_run_id": "developer-models-v1-" + "f" * 16}
        model = {**common, "model_run_id": pointer["model_run_id"],
                 "status": "unavailable", "asset_url": None}
        verified = apply_release.verify_developer_payloads(health, buildings, points)
        apply_release.verify_experience_payloads(verified, selected_id, common,
                                                 model, pointer)
        with self.assertRaisesRegex(RuntimeError, "selected generation"):
            apply_release.verify_experience_payloads(
                verified, selected_id, common,
                {**model, "model_run_id": "developer-models-v1-" + "0" * 16}, pointer)

    def test_manifest_hashes_and_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = fixture_release(Path(temporary))
            expected = len(json.loads((release / "release-manifest.json").read_text())["files"])
            self.assertEqual(apply_release.verify_release(release)["file_count"], expected)
            (release / "assets/private.txt").write_text("not listed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unlisted file"):
                apply_release.verify_release(release)
            (release / "assets/private.txt").unlink()
            (release / "assets/manifest.json").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "wrong size|hash mismatch"):
                apply_release.verify_release(release)

    def test_release_rejects_mixed_developer_map_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = fixture_release(Path(temporary))
            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pointer_path = release / "data/property/developer_map/active.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["manifest_sha256"] = "f" * 64
            body = json.dumps(pointer).encode("utf-8")
            pointer_path.write_bytes(body)
            for record in manifest["files"]:
                if record["path"] == "data/property/developer_map/active.json":
                    record["bytes"] = len(body)
                    record["sha256"] = hashlib.sha256(body).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not pin its manifest"):
                apply_release.verify_release(release)

    def test_release_rejects_mixed_unit_menu_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = fixture_release(Path(temporary))
            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pointer_path = release / "data/property/developer_experience/active.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["unit_manifest_sha256"] = "f" * 64
            body = json.dumps(pointer).encode("utf-8")
            pointer_path.write_bytes(body)
            for record in manifest["files"]:
                if record["path"] == "data/property/developer_experience/active.json":
                    record.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "active pointer"):
                apply_release.verify_release(release)

    def test_generation_pins_registry_visibility_and_model_without_legacy_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = fixture_release(Path(temporary))
            manifest_path = release / "release-manifest.json"
            inventory = json.loads(manifest_path.read_text(encoding="utf-8"))
            inventory["files"] = [record for record in inventory["files"]
                                  if record["path"] != "data/models/active.json"]
            (release / "data/models/active.json").unlink()

            def add(relative: str, body: bytes) -> str:
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                digest = hashlib.sha256(body).hexdigest()
                inventory["files"].append({"path": relative, "bytes": len(body),
                                           "sha256": digest})
                return digest

            snapshot = "data/property/estateline/normalized/snapshots/estate-" + "e" * 16
            normalized = {"path": snapshot}
            for field, filename in (("projects_sha256", "projects.jsonl"),
                                    ("source_manifest_sha256", "source_manifest.json"),
                                    ("review_required_sha256", "review_required.jsonl"),
                                    ("schema_sha256", "schema.json")):
                normalized[field] = add(snapshot + "/" + filename, b"{}\n")
            visibility = {"run_id": "visibility-v1-" + "f" * 16}
            vis_prefix = "data/property/estateline/visibility/runs/" + visibility["run_id"]
            visibility["manifest_sha256"] = add(vis_prefix + "/manifest.json", b"{}\n")
            visibility["records_sha256"] = add(vis_prefix + "/records.jsonl", b"{}\n")
            model_run = "models-v1-" + "c" * 16
            model_manifest_path = f"data/models/{model_run}/manifest.json"
            model_manifest = json.loads((release / model_manifest_path).read_text())
            model_manifest["registry_snapshot_id"] = "building-registry-" + "d" * 16
            body = json.dumps(model_manifest).encode("utf-8")
            (release / model_manifest_path).write_bytes(body)
            model_digest = hashlib.sha256(body).hexdigest()
            for record in inventory["files"]:
                if record["path"] == model_manifest_path:
                    record.update(bytes=len(body), sha256=model_digest)
            bundle = {"schema_version": "estate-generation/1",
                      "source_snapshot_id": model_manifest["registry_snapshot_id"],
                      "normalized_snapshot": normalized, "visibility": visibility,
                      "model": {"run_id": model_run, "manifest_sha256": model_digest,
                                "public_root": "public-project-models-v1-" + "c" * 16,
                                "public_manifest_sha256": model_manifest["public_manifest_sha256"]}}
            bundle_bytes = json.dumps(bundle).encode("utf-8")
            bundle_digest = hashlib.sha256(bundle_bytes).hexdigest()
            generation = "generation-" + bundle_digest[:16]
            add(f"data/property/estateline/normalized/generations/{generation}/bundle.json",
                bundle_bytes)
            pointer = {"schema_version": "estate-generation-pointer/1",
                       "generation_id": generation, "bundle_sha256": bundle_digest}
            add("data/property/estateline/normalized/active.json",
                json.dumps(pointer).encode("utf-8"))
            inventory["file_count"] = len(inventory["files"])
            inventory["bytes_total"] = sum(item["bytes"] for item in inventory["files"])
            manifest_path.write_text(json.dumps(inventory), encoding="utf-8")

            self.assertEqual(apply_release.verify_release(release)["file_count"],
                             len(inventory["files"]))
            # The copied bytes still match the outer inventory, but the nested
            # model pointer must reject a different active run digest.
            wrong = {**bundle, "model": {**bundle["model"], "manifest_sha256": "0" * 64}}
            wrong_bytes = json.dumps(wrong).encode("utf-8")
            wrong_sha = hashlib.sha256(wrong_bytes).hexdigest()
            wrong_generation = "generation-" + wrong_sha[:16]
            old_bundle = release / f"data/property/estateline/normalized/generations/{generation}/bundle.json"
            old_bundle.unlink()
            for record in inventory["files"]:
                if record["path"].endswith("/bundle.json"):
                    record.update(path=(f"data/property/estateline/normalized/generations/"
                                        f"{wrong_generation}/bundle.json"),
                                  bytes=len(wrong_bytes), sha256=wrong_sha)
                elif record["path"] == "data/property/estateline/normalized/active.json":
                    pointer = {**pointer, "generation_id": wrong_generation,
                               "bundle_sha256": wrong_sha}
                    pointer_bytes = json.dumps(pointer).encode("utf-8")
                    (release / record["path"]).write_bytes(pointer_bytes)
                    record.update(bytes=len(pointer_bytes),
                                  sha256=hashlib.sha256(pointer_bytes).hexdigest())
            bundle_path = release / f"data/property/estateline/normalized/generations/{wrong_generation}/bundle.json"
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_bytes(wrong_bytes)
            inventory["file_count"] = len(inventory["files"])
            inventory["bytes_total"] = sum(item["bytes"] for item in inventory["files"])
            manifest_path.write_text(json.dumps(inventory), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "active pointer"):
                apply_release.verify_release(release)

    def test_failed_update_restores_recorded_release_without_deleting_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            old = root / "old-release"
            old.mkdir()
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            old_settings = apply_release._settings(old, "a" * 40, "127.0.0.1", 8080)
            new_settings = apply_release._settings(release, "b" * 40, "127.0.0.1", 8080)
            state = {"current": old_settings, "previous": None}
            (control / "deploy-state.json").write_text(json.dumps(state), encoding="utf-8")
            with patch.object(apply_release, "_compose") as compose, \
                    patch.object(apply_release, "_smoke",
                                 side_effect=[RuntimeError("unhealthy"), None]), \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "full-compat"}):
                with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                    apply_release.apply(new_settings, control)
            updates = [(args[0]["NOVOSTROY_IMAGE_TAG"], args[1:])
                       for args, _ in compose.call_args_list if args[1] == "up"]
            self.assertEqual([item[0] for item in updates], ["b" * 40, "a" * 40])
            self.assertEqual(json.loads((control / "deploy-state.json").read_text()), state)
            self.assertTrue(release.exists())
            self.assertTrue(old.exists())

    def test_update_rejects_current_only_release_before_compose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            old_release = root / "old-release"
            old_release.mkdir()
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            previous = apply_release._settings(old_release, "a" * 40,
                                               "127.0.0.1", 8080)
            (control / "deploy-state.json").write_text(
                json.dumps({"current": previous}), encoding="utf-8")
            upcoming = apply_release._settings(release, "b" * 40,
                                               "127.0.0.1", 8080)
            with patch.object(apply_release, "_compose") as compose, \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "current-only"}):
                with self.assertRaisesRegex(ValueError, "full-compat"):
                    apply_release.apply(upcoming, control)
            compose.assert_not_called()

    def test_image_only_update_keeps_current_only_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            previous = apply_release._settings(release, "a" * 40, "127.0.0.1", 8080)
            (control / "deploy-state.json").write_text(
                json.dumps({"current": previous}), encoding="utf-8")
            upcoming = apply_release._settings(release, "b" * 40, "127.0.0.1", 8080)
            with patch.object(apply_release, "_compose"), \
                    patch.object(apply_release, "_smoke"), \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "current-only"}):
                apply_release.apply(upcoming, control)
            state = json.loads((control / "deploy-state.json").read_text())
            self.assertEqual(state["current"], upcoming)
            self.assertEqual(state["previous"], previous)

    def test_first_apply_refuses_unmanaged_compose_containers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            settings = apply_release._settings(release, "a" * 40, "127.0.0.1", 8080)
            with patch.object(apply_release, "_compose") as compose, \
                    patch.object(apply_release, "_compose_output", return_value="container-id"), \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "current-only"}):
                with self.assertRaisesRegex(RuntimeError, "without a recorded deploy state"):
                    apply_release.apply(settings, control)
            self.assertEqual([args[1] for args, _ in compose.call_args_list], ["config"])

    def test_state_write_failure_rolls_back_running_containers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = fixture_release(root)
            old = root / "old-release"
            old.mkdir()
            control = root / "deploy-control"
            control.mkdir(mode=0o700)
            previous = apply_release._settings(old, "a" * 40, "127.0.0.1", 8080)
            (control / "deploy-state.json").write_text(
                json.dumps({"current": previous}), encoding="utf-8")
            upcoming = apply_release._settings(release, "b" * 40, "127.0.0.1", 8080)
            with patch.object(apply_release, "_compose") as compose, \
                    patch.object(apply_release, "_smoke"), \
                    patch.object(apply_release, "_write_state",
                                 side_effect=OSError("disk full")), \
                    patch.object(apply_release, "verify_release",
                                 return_value={"profile": "full-compat"}):
                with self.assertRaisesRegex(OSError, "disk full"):
                    apply_release.apply(upcoming, control)
            updates = [args[0]["NOVOSTROY_IMAGE_TAG"]
                       for args, _ in compose.call_args_list if args[1] == "up"]
            self.assertEqual(updates, ["b" * 40, "a" * 40])


if __name__ == "__main__":
    unittest.main()
