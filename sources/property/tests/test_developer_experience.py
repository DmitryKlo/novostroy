"""One-pointer publication and rollback preserve consistent generations."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sources.property import developer_experience as experience


def pointer(unit: str, model: str) -> dict:
    return {"schema_version": experience.POINTER_SCHEMA,
            "map_run_id": "map", "map_snapshot_id": "snapshot",
            "map_manifest_sha256": "a" * 64,
            "unit_run_id": unit, "unit_manifest_sha256": "b" * 64,
            "model_run_id": model, "model_manifest_sha256": "c" * 64}


def enriched_pointer(unit: str, model: str, enrichment: str) -> dict:
    value = pointer(unit, model)
    value.update({"schema_version": experience.POINTER_SCHEMA_V2,
                  "enrichment_run_id": enrichment,
                  "enrichment_manifest_sha256": "d" * 64})
    return value


class DeveloperExperienceTest(unittest.TestCase):
    def test_atomic_cas_and_rollback_to_absent_pointer(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            empty_sha = experience.pointer_sha(base)
            with patch.object(experience, "_verify_bundle", return_value=pointer("units1", "models1")):
                experience.publish("map", "units1", "models1",
                                   expected_active_sha=empty_sha, base=base)
            active_sha = experience.pointer_sha(base)
            self.assertNotEqual(active_sha, empty_sha)
            with self.assertRaisesRegex(ValueError, "changed"):
                experience.publish("map", "units2", "models2",
                                   expected_active_sha=empty_sha, base=base)
            self.assertIsNone(experience.rollback(expected_active_sha=active_sha, base=base))
            self.assertEqual(experience.pointer_sha(base), empty_sha)
            self.assertFalse((base / "active.json").exists())

    def test_rollback_keeps_prior_valid_pair(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.object(experience, "_verify_bundle", side_effect=[
                    pointer("units1", "models1"), pointer("units2", "models2"),
                    pointer("units1", "models1")]):
                experience.publish("map", "units1", "models1",
                                   expected_active_sha=experience.pointer_sha(base), base=base)
                first_sha = experience.pointer_sha(base)
                experience.publish("map", "units2", "models2",
                                   expected_active_sha=first_sha, base=base)
                experience.rollback(expected_active_sha=experience.pointer_sha(base), base=base)
            self.assertEqual(experience.pointer_sha(base), first_sha)

    def test_v2_commit_and_rollback_keep_exact_v1_generation(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.object(experience, "_verify_bundle", side_effect=[
                    pointer("units1", "models1"),
                    enriched_pointer("units1", "models1", "enrichment1"),
                    pointer("units1", "models1")]) as verify:
                experience.publish("map", "units1", "models1",
                                   expected_active_sha=experience.pointer_sha(base), base=base)
                previous_sha = experience.pointer_sha(base)
                experience.publish("map", "units1", "models1",
                                   enrichment_run_id="enrichment1",
                                   expected_enrichment_manifest_sha="d" * 64,
                                   expected_active_sha=previous_sha, base=base)
                self.assertEqual(
                    json.loads((base / "active.json").read_text(encoding="utf-8"))[
                        "schema_version"], experience.POINTER_SCHEMA_V2)
                with self.assertRaisesRegex(ValueError, "changed"):
                    experience.publish("map", "units1", "models1",
                                       enrichment_run_id="enrichment2",
                                       expected_enrichment_manifest_sha="e" * 64,
                                       expected_active_sha=previous_sha, base=base)
                experience.rollback(expected_active_sha=experience.pointer_sha(base), base=base)
                self.assertEqual(experience.pointer_sha(base), previous_sha)
                self.assertEqual(verify.call_count, 3)


if __name__ == "__main__":
    unittest.main()
