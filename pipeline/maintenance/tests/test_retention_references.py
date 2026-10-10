"""Reference-graph checks for retention; no test invokes its apply command."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from pipeline.maintenance.refresh import write_json
from pipeline.maintenance.retention import GRACE_SECONDS, PUBLIC_SECONDS, make_plan


class RetentionReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "novostroy"
        self.root.mkdir()
        self.now = 2_000_000_000.0

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _model_id(name: str) -> str:
        return f"models-v1-{hashlib.sha256(name.encode()).hexdigest()[:16]}"

    def _generation(self, name: str) -> dict[str, str]:
        snapshot = f"data/property/estateline/normalized/snapshots/estate-{name}"
        visibility = f"data/property/estateline/visibility/runs/visibility-v1-{name}"
        model = f"data/models/{self._model_id(name)}"
        public = f"assets/public-{name}"
        for relative in (snapshot, visibility, model, public):
            path = self.root / relative
            path.mkdir(parents=True)
            (path / "record.json").write_text("{}\n", encoding="utf-8")
        bundle = {"schema_version": "estate-generation/1",
                  "normalized_snapshot": {"path": snapshot},
                  "visibility": {"run_id": f"visibility-v1-{name}"},
                  "model": {"run_id": self._model_id(name), "public_root": f"public-{name}"}}
        payload = (json.dumps(bundle, sort_keys=True) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        generation_id = f"generation-{digest[:16]}"
        bundle_path = (self.root / "data/property/estateline/normalized/generations"
                       / generation_id / "bundle.json")
        bundle_path.parent.mkdir(parents=True)
        bundle_path.write_bytes(payload)
        return {"schema_version": "estate-generation-pointer/1", "generation_id": generation_id,
                "bundle_sha256": digest}

    def _ledger(self, *relative_paths: str) -> None:
        ledger = self.root / "data/runtime/refresh/published.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("".join(json.dumps({"path": path, "published_epoch":
                                              self.now - PUBLIC_SECONDS - 1}) + "\n"
                                  for path in relative_paths), encoding="utf-8")

    def test_active_previous_and_pending_generation_pin_all_referents(self) -> None:
        previous = self._generation("previous")
        active = self._generation("active")
        pending = self._generation("pending")
        write_json(self.root / "data/property/estateline/normalized/active.json", active)
        write_json(self.root / "data/runtime/refresh/generation-transitions"
                   / f"{active['bundle_sha256']}.json",
                   {"from_pointer": previous, "to_pointer": active})
        write_json(self.root / "data/runtime/refresh/pending/next.json",
                   {**pending, "expected_previous_bundle_sha256": active["bundle_sha256"]})
        self._generation("orphan")
        self._ledger(*(f"assets/public-{name}" for name in ("previous", "active", "pending", "orphan")),
                     *(f"data/models/{self._model_id(name)}" for name in
                       ("previous", "active", "pending", "orphan")))

        plan = make_plan(self.root, now=self.now)
        candidates = {entry["path"] for entry in plan["candidates"]}
        self.assertEqual(candidates, {"assets/public-orphan", f"data/models/{self._model_id('orphan')}"})
        refs = plan["protected_references"]
        for name, pointer in (("previous", previous), ("active", active), ("pending", pending)):
            for relative in (f"data/property/estateline/normalized/snapshots/estate-{name}",
                             f"data/property/estateline/visibility/runs/visibility-v1-{name}",
                             f"data/models/{self._model_id(name)}", f"assets/public-{name}",
                             f"data/property/estateline/normalized/generations/{pointer['generation_id']}"):
                self.assertIn(relative, refs)

    def test_latest_rollback_journal_pins_backup_referents(self) -> None:
        write_json(self.root / "assets/manifest.json", {"project_models_root": "public-current"})
        write_json(self.root / "data/models/active.json", {"run_id": "models-v1-current"})
        old = self.root / "data/runtime/refresh/journal-old"
        new = self.root / "data/runtime/refresh/journal-new"
        for path in (old, new):
            write_json(path / "state.json", {"status": "committed"})
            write_json(path / "journal.json", {"files": [
                {"path": "assets/manifest.json", "backup": "backup-0", "existed": True},
                {"path": "data/models/active.json", "backup": "backup-1", "existed": True}]})
            write_json(path / "backup-0", {"project_models_root": f"public-{path.name}"})
            write_json(path / "backup-1", {"run_id": self._model_id(path.name)})
        os.utime(old, (self.now - GRACE_SECONDS - 20, self.now - GRACE_SECONDS - 20))
        os.utime(new, (self.now - GRACE_SECONDS - 10, self.now - GRACE_SECONDS - 10))
        for name in ("journal-old", "journal-new"):
            (self.root / f"assets/public-{name}").mkdir(parents=True)
            (self.root / f"data/models/{self._model_id(name)}").mkdir(parents=True)
        self._ledger(*(f"assets/public-{name}" for name in ("journal-old", "journal-new")),
                     *(f"data/models/{self._model_id(name)}" for name in ("journal-old", "journal-new")))

        plan = make_plan(self.root, now=self.now)
        candidates = {entry["path"] for entry in plan["candidates"]}
        self.assertIn("data/runtime/refresh/journal-old", candidates)
        self.assertNotIn("data/runtime/refresh/journal-new", candidates)
        self.assertIn("assets/public-journal-old", candidates)
        self.assertIn(f"data/models/{self._model_id('journal-old')}", candidates)
        self.assertNotIn("assets/public-journal-new", candidates)
        self.assertNotIn(f"data/models/{self._model_id('journal-new')}", candidates)

    def test_operator_pinned_generation_retains_its_model_and_public_root(self) -> None:
        pinned = self._generation("operator")
        self._generation("unused")
        relative = ("data/property/estateline/normalized/generations/"
                    f"{pinned['generation_id']}/bundle.json")
        write_json(self.root / "data/runtime/refresh/release_pins.json", {"paths": [relative]})
        self._ledger("assets/public-operator", f"data/models/{self._model_id('operator')}",
                     "assets/public-unused", f"data/models/{self._model_id('unused')}")

        plan = make_plan(self.root, now=self.now)
        candidates = {entry["path"] for entry in plan["candidates"]}
        self.assertEqual(candidates, {"assets/public-unused", f"data/models/{self._model_id('unused')}"})
        self.assertIn("data/property/estateline/normalized/snapshots/estate-operator",
                      plan["protected_references"])

    def test_pin_inside_stage_blocks_parent_candidate_and_symlink_ancestor(self) -> None:
        stage = self.root / "data/runtime/refresh/estate-old"
        stage.mkdir(parents=True)
        (stage / "pinned.json").write_text("{}\n", encoding="utf-8")
        os.utime(stage, (self.now - GRACE_SECONDS - 1, self.now - GRACE_SECONDS - 1))
        write_json(self.root / "data/runtime/refresh/release_pins.json",
                   {"paths": ["data/runtime/refresh/estate-old/pinned.json"]})
        self.assertNotIn("data/runtime/refresh/estate-old",
                         {entry["path"] for entry in make_plan(self.root, now=self.now)["candidates"]})

        # A path under a symlinked runtime ancestor must never become a candidate.
        linked = self.root / "data/runtime/refresh/estate-linked"
        linked.symlink_to(stage, target_is_directory=True)
        self.assertNotIn("data/runtime/refresh/estate-linked",
                         {entry["path"] for entry in make_plan(self.root, now=self.now)["candidates"]})


if __name__ == "__main__":
    unittest.main()
