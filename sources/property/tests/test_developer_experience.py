"""One-pointer publication and rollback preserve consistent generations."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
