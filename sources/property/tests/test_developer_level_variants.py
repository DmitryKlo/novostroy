"""Different plan assets for one publisher ID remain separate references."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sources.property.developer_level_variants import build


class LevelVariantsTest(unittest.TestCase):
    def test_preserves_plans_without_incrementing_unit_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run.lock").write_bytes(b"")
            (root / "manifest.json").write_text(json.dumps({
                "schema_version": "developer-harvest-run/1", "run_id": "level-test",
                "providers": {"level": {"catalog_pages_remaining": 0,
                                         "catalog_exhausted": False}},
            }), encoding="utf-8")
            db = sqlite3.connect(root / "state.sqlite3")
            db.execute("CREATE TABLE observation_versions (observation_id TEXT,"
                       "payload_sha256 TEXT, payload_json TEXT)")
            for index, asset in enumerate(("original.svg", "alternate.svg")):
                row = {"provider": "level", "record_type": "unit",
                       "publisher_unit_id": "10", "observation_id": "devobs_10",
                       "source_page_url": f"https://level.ru/api/filter/?offset={index}",
                       "observed_at": "2026-10-10T17:01:45Z",
                       "raw_sha256": str(index),
                       "media": [{"role": "apartment_plan",
                                  "url": f"https://cdn.level.ru/{asset}"}]}
                db.execute("INSERT INTO observation_versions VALUES(?,?,?)",
                           ("devobs_10", str(index), json.dumps(row)))
            db.commit()
            db.close()
            summary = build(root)
            rows = [json.loads(line) for line in (root / "level_plan_variants.jsonl")
                    .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(summary["publisher_unit_ids"], 1)
            self.assertEqual(summary["unit_ids_with_multiple_source_versions"], 1)
            self.assertEqual(summary["plan_reference_rows"], 2)
            self.assertEqual({row["source_url"] for row in rows}, {
                "https://cdn.level.ru/original.svg",
                "https://cdn.level.ru/alternate.svg",
            })
            self.assertTrue(all(not row["public_display_allowed"] for row in rows))


if __name__ == "__main__":
    unittest.main()
