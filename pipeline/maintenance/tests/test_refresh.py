from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from pipeline.maintenance.refresh import (
    _active_generation, _estate_inventory, _transaction, _validate_estate,
    build_generation_bundle, cleanup_committed_pending, exclusive_lock, promote_generation, read_json,
    rollback_active_generation, rollback_last, run, sha256, stage_bootstrap_generation, write_json,
)
from pipeline.maintenance.retention import apply_plan, make_plan, save_plan
from pipeline.maintenance.visibility import AUDITED_MAINTENANCE, build, decide


def _row(identifier: str, observed: str, *, category: str = "Многоквартирные дома") -> dict:
    return {"registry_id": identifier, "latest_known_date": observed,
            "provenance": {"source_category": category, "source_report_sha256": "a" * 64}}


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "novostroy"
        self.root.mkdir()
        self.estate = self.root / "data/property/estateline"
        self.estate.mkdir(parents=True)
        self.raw = self.root.parent / "Загрузки/1_xls_estateline"
        self.raw.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _estate_fixture(self) -> None:
        raw = self.raw / "moskva-week-1.xlsx"
        raw.write_bytes(b"approved fixture")
        _jsonl(self.estate / "projects.jsonl", [_row("estate:1", "2025-10-09")])
        _jsonl(self.estate / "review_required.jsonl", [])
        write_json(self.estate / "schema.json", {"schema": 1})
        write_json(self.estate / "source_manifest.json", {
            "source_files": [{"path": raw.name, "bytes": raw.stat().st_size, "sha256": sha256(raw)}],
            "snapshot_at": "2025-10-09", "rights_status": "internal_use",
            "source_files_total": 1, "source_files_parsed": 1, "parse_errors": [],
            "outputs": {key: {"path": name, "sha256": sha256(self.estate / name)} for key, name in
                        (("projects", "projects.jsonl"), ("review_required", "review_required.jsonl"),
                         ("schema", "schema.json"))}})

    def test_inventory_stable_changed_and_missing(self) -> None:
        self._estate_fixture()
        self.assertEqual(_estate_inventory(self.root)["state"], "current")
        raw = self.raw / "moskva-week-1.xlsx"
        raw.write_bytes(b"new local snapshot")
        self.assertEqual(_estate_inventory(self.root)["state"], "changed")
        raw.unlink()
        self.assertEqual(_estate_inventory(self.root)["state"], "input_unavailable")
        self.assertTrue((self.estate / "projects.jsonl").is_file())

    def test_partial_snapshot_cannot_drop_prior_identity(self) -> None:
        self._estate_fixture()
        stage = self.root / "stage"
        stage.mkdir()
        _jsonl(stage / "projects.jsonl", [_row("estate:2", "2026-10-01")])
        _jsonl(stage / "review_required.jsonl", [])
        write_json(stage / "schema.json", {"schema": 1})
        manifest = read_json(self.estate / "source_manifest.json")
        manifest["snapshot_at"] = "2026-10-01"
        for value in manifest["outputs"].values():
            value["sha256"] = sha256(stage / value["path"])
        write_json(stage / "source_manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "prior source IDs disappeared"):
            _validate_estate(stage, self.estate)
        self.assertEqual(read_json(stage / "stale_records.json")["unresolved_prior_ids"], ["estate:1"])
        self.assertEqual(_estate_inventory(self.root)["state"], "current")

    def test_transaction_rollback_and_changed_target_gate(self) -> None:
        current = self.root / "data/derived.json"
        write_json(current, {"version": 1})
        stage = self.root / "new.json"
        write_json(stage, {"version": 2})
        _transaction(self.root, {current: stage})
        self.assertEqual(read_json(current)["version"], 2)
        current.write_text('{"version":3}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since transaction"):
            rollback_last(self.root)
        write_json(current, {"version": 2})
        rollback_last(self.root)
        self.assertEqual(read_json(current)["version"], 1)

    def test_exclusive_lock_rejects_overlap(self) -> None:
        with exclusive_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "another refresh"):
                with exclusive_lock(self.root):
                    pass

    def _generation_fixture(self, *, suffix: str = "a", source: Path | None = None) -> tuple[str, str]:
        source = source or self.estate
        model_id = f"models-v1-{suffix * 16}"
        public_root = f"public-project-models-v1-{suffix * 16}"
        public = self.root / "assets" / public_root
        write_json(public / "public_manifest.json", {"public_redacted_source_ids": True})
        model = self.root / "data/models" / model_id
        write_json(model / "manifest.json", {
            "registry_snapshot_id": f"building-registry-{sha256(source / 'projects.jsonl')[:16]}",
            "input_hashes": {"estateline_projects": sha256(source / "projects.jsonl")},
            "public_root": public_root,
            "public_manifest_sha256": sha256(public / "public_manifest.json"),
        })
        visibility_id = build(self.root, as_of=date(2026, 10, 10),
                              source_path=source / "projects.jsonl",
                              source_manifest_path=source / "source_manifest.json")["run_id"]
        return model_id, visibility_id

    def test_generation_bootstrap_commit_and_cas_rollback(self) -> None:
        self._estate_fixture()
        model_id, visibility_id = self._generation_fixture()
        write_json(self.root / "data/models/active.json", {"run_id": model_id,
                   "manifest_sha256": sha256(self.root / "data/models" / model_id / "manifest.json")})
        write_json(self.estate / "visibility/active.json", {"run_id": visibility_id,
                   "manifest_sha256": sha256(self.estate / "visibility/runs" / visibility_id / "manifest.json")})
        write_json(self.root / "assets/manifest.json", {"project_models_root": f"public-project-models-v1-{'a' * 16}"})
        staged = stage_bootstrap_generation(self.root)
        self.assertIsNone(_active_generation(self.root))
        pointer = promote_generation(self.root, self.root / staged["pending_path"])
        self.assertEqual(pointer["bundle_sha256"], staged["bundle_sha256"])
        self.assertEqual(_estate_inventory(self.root)["state"], "current")
        with self.assertRaisesRegex(ValueError, "active generation changed"):
            promote_generation(self.root, self.root / staged["pending_path"])
        # A crash after atomic pointer replace and before pending unlink is
        # idempotently repaired; an uncommitted candidate remains untouched.
        unrelated = self.root / "data/runtime/refresh/pending/unpublished.json"
        write_json(unrelated, {"generation_id": "generation-" + "f" * 16,
                               "bundle_sha256": "f" * 64})
        self.assertEqual(cleanup_committed_pending(self.root), 1)
        self.assertFalse((self.root / staged["pending_path"]).exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(cleanup_committed_pending(self.root), 0)
        self.assertEqual(rollback_active_generation(self.root)["rolled_back_to"], "legacy_validated_files")
        self.assertIsNone(_active_generation(self.root))

    def test_changed_generation_single_flip_and_previous_snapshot_retained(self) -> None:
        self._estate_fixture()
        model_id, visibility_id = self._generation_fixture()
        write_json(self.root / "data/models/active.json", {"run_id": model_id})
        write_json(self.estate / "visibility/active.json", {"run_id": visibility_id})
        write_json(self.root / "assets/manifest.json", {"project_models_root": f"public-project-models-v1-{'a' * 16}"})
        first = stage_bootstrap_generation(self.root)
        promote_generation(self.root, self.root / first["pending_path"])
        old = _active_generation(self.root)
        old_snapshot = self.root / old["bundle"]["normalized_snapshot"]["path"]
        new_snapshot = self.estate / "normalized/snapshots/.estate-fixture-new"
        new_snapshot.mkdir(parents=True)
        for name in ("projects.jsonl", "review_required.jsonl", "schema.json", "source_manifest.json"):
            (new_snapshot / name).write_bytes((old_snapshot / name).read_bytes())
        _jsonl(new_snapshot / "projects.jsonl", [_row("estate:1", "2026-10-01")])
        manifest = read_json(new_snapshot / "source_manifest.json")
        manifest["outputs"]["projects"]["sha256"] = sha256(new_snapshot / "projects.jsonl")
        write_json(new_snapshot / "source_manifest.json", manifest)
        final_snapshot = new_snapshot.with_name(f"estate-{sha256(new_snapshot / 'source_manifest.json')[:16]}")
        new_snapshot.rename(final_snapshot)
        new_snapshot = final_snapshot
        next_model, next_visibility = self._generation_fixture(suffix="b", source=new_snapshot)
        next_bundle = build_generation_bundle(self.root, new_snapshot, next_model, next_visibility)
        pending = self.root / "data/runtime/refresh/pending/next.json"
        write_json(pending, {"kind": "refresh", "raw_inventory_sha256": _estate_inventory(self.root)["raw_inventory_sha256"],
                             "expected_previous_bundle_sha256": old["bundle_sha256"], **next_bundle})
        promote_generation(self.root, pending)
        self.assertEqual(_active_generation(self.root)["bundle_sha256"], next_bundle["bundle_sha256"])
        self.assertNotEqual(sha256(old_snapshot / "projects.jsonl"), sha256(new_snapshot / "projects.jsonl"))
        self.assertEqual(rollback_active_generation(self.root)["rolled_back_to"], old["generation_id"])
        self.assertEqual(_active_generation(self.root)["bundle_sha256"], old["bundle_sha256"])

    def test_changed_pending_generation_auto_promotes_on_next_run(self) -> None:
        self._estate_fixture()
        model_id, visibility_id = self._generation_fixture()
        write_json(self.root / "data/models/active.json", {"run_id": model_id})
        write_json(self.estate / "visibility/active.json", {"run_id": visibility_id})
        write_json(self.root / "assets/manifest.json", {"project_models_root": f"public-project-models-v1-{'a' * 16}"})
        first = stage_bootstrap_generation(self.root)
        promote_generation(self.root, self.root / first["pending_path"])
        old = _active_generation(self.root)
        old_snapshot = self.root / old["bundle"]["normalized_snapshot"]["path"]
        stage = self.estate / "normalized/snapshots/.incoming"
        stage.mkdir(parents=True)
        for name in ("projects.jsonl", "review_required.jsonl", "schema.json", "source_manifest.json"):
            (stage / name).write_bytes((old_snapshot / name).read_bytes())
        _jsonl(stage / "projects.jsonl", [_row("estate:1", "2026-10-01")])
        raw = self.raw / "moskva-week-1.xlsx"
        raw.write_bytes(b"new approved local report")
        manifest = read_json(stage / "source_manifest.json")
        manifest["source_files"][0].update(bytes=raw.stat().st_size, sha256=sha256(raw))
        manifest["outputs"]["projects"]["sha256"] = sha256(stage / "projects.jsonl")
        write_json(stage / "source_manifest.json", manifest)
        snapshot = stage.with_name(f"estate-{sha256(stage / 'source_manifest.json')[:16]}")
        stage.rename(snapshot)
        next_model, next_visibility = self._generation_fixture(suffix="b", source=snapshot)
        next_bundle = build_generation_bundle(self.root, snapshot, next_model, next_visibility)
        inventory = _estate_inventory(self.root)
        pending = (self.root / "data/runtime/refresh/pending"
                   / f"estate-{inventory['raw_inventory_sha256'][:16]}.json")
        write_json(pending, {"kind": "refresh", "raw_inventory_sha256": inventory["raw_inventory_sha256"],
                             "expected_previous_bundle_sha256": old["bundle_sha256"],
                             "active_registry_source_sha256": old["bundle"]["normalized_snapshot"]["projects_sha256"],
                             "normalized_snapshot": snapshot.relative_to(self.root).as_posix(),
                             "normalized_manifest_sha256": sha256(snapshot / "source_manifest.json"),
                             "model_run_id": next_model,
                             "model_manifest_sha256": sha256(self.root / "data/models" / next_model / "manifest.json"),
                             "visibility_run_id": next_visibility,
                             "visibility_manifest_sha256": sha256(self.estate / "visibility/runs" / next_visibility / "manifest.json"),
                             **next_bundle})
        with mock.patch("pipeline.maintenance.refresh.ROOT", self.root):
            result = run(self.root)
        self.assertIn("pending_registry_generation_published", result["actions"])
        self.assertEqual(_active_generation(self.root)["bundle_sha256"], next_bundle["bundle_sha256"])
        self.assertFalse(pending.exists())
        self.assertEqual(rollback_active_generation(self.root)["rolled_back_to"], old["generation_id"])


class VisibilityTests(unittest.TestCase):
    setUp = RefreshTests.setUp
    tearDown = RefreshTests.tearDown
    _estate_fixture = RefreshTests._estate_fixture

    def test_strict_age_boundary_and_typed_restoration(self) -> None:
        as_of = date(2026, 10, 10)
        self.assertEqual(decide(_row("a", "2025-10-10"), [], as_of=as_of)["visibility"], "visible")
        old = decide(_row("b", "2025-10-09"), [], as_of=as_of)
        self.assertEqual(old["reason_code"], "source_observation_older_than_365d")
        self.assertEqual(old["visibility"], "archived")
        restored = decide(_row("c", "2026-10-01", category="Реставрация фасада"), [], as_of=as_of)
        self.assertEqual(restored["reason_code"], "outside_new_build_category")

    def test_bankruptcy_is_review_and_terminal_requires_exact_evidence(self) -> None:
        row = _row("a", "2026-10-01")
        event = {"registry_id": "a", "event_kind": "developer_bankruptcy", "official_source_url": "https://official.example/1",
                 "official_document_id": "1", "identity_scope": "exact_corpus_or_project", "evidence_as_of": "2026-10-05"}
        decision = decide(row, [event], as_of=date(2026, 10, 10))
        self.assertEqual((decision["visibility"], decision["reason_code"]), ("visible", "terminal_status_unverified"))
        event["event_kind"] = "project_terminated"
        self.assertEqual(decide(row, [event], as_of=date(2026, 10, 10))["visibility"], "archived")
        event["identity_scope"] = "estate_context"
        with self.assertRaisesRegex(ValueError, "exact project identity"):
            decide(row, [event], as_of=date(2026, 10, 10))

    def test_immutable_run_reused_until_age_transition(self) -> None:
        self._estate_fixture()
        _jsonl(self.estate / "projects.jsonl", [_row("a", "2025-10-11")])
        first = build(self.root, as_of=date(2026, 10, 10))
        same = build(self.root, as_of=date(2026, 10, 11))
        changed = build(self.root, as_of=date(2026, 10, 12))
        self.assertEqual(first["run_id"], same["run_id"])
        self.assertEqual(first["as_of_date"], same["as_of_date"])
        self.assertNotEqual(first["run_id"], changed["run_id"])
        self.assertEqual(changed["decisions"], {"archived": 1})

    def test_interrupted_unpublished_visibility_run_retries_without_mutating_active(self) -> None:
        self._estate_fixture()
        first = build(self.root, as_of=date(2026, 10, 10))
        run = self.estate / "visibility/runs" / first["run_id"]
        (run / "records.jsonl").unlink()
        retried = build(self.root, as_of=date(2026, 10, 10))
        self.assertEqual(retried["run_id"], first["run_id"])
        self.assertEqual(sha256(run / "records.jsonl"), retried["records_sha256"])
        self.assertTrue((run.parent / f".incomplete-{first['run_id']}").is_dir())

    def test_four_audited_maintenance_rows_and_mixed_build_guard(self) -> None:
        for identifier, (name, source_hash, source_row_id) in AUDITED_MAINTENANCE.items():
            row = _row(identifier, "2026-09-01")
            row.update(name=name, source_row_id=source_row_id, status="Фасадные работы и остекление")
            row["provenance"]["source_report_sha256"] = source_hash
            decision = decide(row, [], as_of=date(2026, 10, 10))
            self.assertEqual((decision["visibility"], decision["reason_code"]), ("archived", "maintenance_only"))
            row["provenance"]["source_report_sha256"] = "b" * 64
            if name.startswith("Жилой"):
                self.assertEqual(decide(row, [], as_of=date(2026, 10, 10))["visibility"], "visible")
        ordinary = _row("ordinary", "2026-09-01")
        ordinary.update(name="Новый жилой комплекс", status="Фасадные работы и остекление")
        self.assertEqual(decide(ordinary, [], as_of=date(2026, 10, 10))["visibility"], "visible")
        mixed = _row("mixed", "2026-09-01")
        mixed["name"] = "Комплекс апартаментов (новое строительство с сохранением исторического фасада здания)"
        self.assertEqual(decide(mixed, [], as_of=date(2026, 10, 10))["visibility"], "visible")

    def test_real_facade_stage_is_not_maintenance(self) -> None:
        source = Path(__file__).resolve().parents[3] / "data/property/estateline/projects.jsonl"
        if not source.is_file():
            self.skipTest("real local EstateLine snapshot absent")
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        stages = [row for row in rows if row.get("status") == "Фасадные работы и остекление"]
        self.assertEqual(len(stages), 236)
        self.assertEqual(sum(decide(row, [], as_of=date(2026, 10, 10))["reason_code"] == "maintenance_only"
                             for row in stages), 1)


class RetentionTests(unittest.TestCase):
    setUp = RefreshTests.setUp
    tearDown = RefreshTests.tearDown
    _estate_fixture = RefreshTests._estate_fixture

    def test_plan_pins_public_root_and_apply_only_expired_owned_stage(self) -> None:
        assets = self.root / "assets"
        (assets / "public-live").mkdir(parents=True)
        (assets / "public-live/a.glb").write_bytes(b"live")
        (assets / "public-old").mkdir()
        (assets / "public-old/a.glb").write_bytes(b"unknown past use")
        write_json(assets / "manifest.json", {"city_tileset_url": "/assets/public-live/tileset.json"})
        stage = self.root / "data/runtime/refresh/estate-expired"
        stage.mkdir(parents=True)
        (stage / "scratch").write_bytes(b"temporary")
        ancient = date(2020, 1, 1).strftime("%s")
        os.utime(stage, (float(ancient), float(ancient)))
        plan = make_plan(self.root)
        self.assertEqual([x["path"] for x in plan["candidates"]], ["data/runtime/refresh/estate-expired"])
        self.assertEqual(plan["logical_reclaimable_bytes"], len(b"temporary"))
        self.assertEqual(plan["physical_reclaimable_upper_bound_bytes"], len(b"temporary"))
        self.assertEqual([x["path"] for x in plan["protected_unknown_public_roots"]], ["assets/public-old"])
        saved = save_plan(self.root)
        result = apply_plan(saved, self.root)
        self.assertEqual(result["removed"], ["data/runtime/refresh/estate-expired"])
        self.assertTrue((assets / "public-live/a.glb").is_file())
        self.assertTrue((assets / "public-old/a.glb").is_file())

    def test_apply_refuses_changed_reference_graph(self) -> None:
        (self.root / "assets").mkdir()
        write_json(self.root / "assets/manifest.json", {"city_tileset_url": "/assets/public-one/tileset.json"})
        saved = save_plan(self.root)
        write_json(self.root / "assets/manifest.json", {"city_tileset_url": "/assets/public-two/tileset.json"})
        with self.assertRaisesRegex(ValueError, "reference graph changed"):
            apply_plan(saved, self.root)


if __name__ == "__main__":
    unittest.main()
