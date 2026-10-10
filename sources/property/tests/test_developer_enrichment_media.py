"""Source media allowlist keeps exact menu membership and rejects unsafe URLs."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sources.property import developer_enrichment_media as media
from sources.property.developer_map import _sha


class DeveloperEnrichmentMediaTest(unittest.TestCase):
    def test_exact_https_host_without_credentials_or_query(self) -> None:
        host, mime = media._exact_url("https://cdn.fsk.ru/production/a.sizes.svg")
        self.assertEqual((host, mime), ("cdn.fsk.ru", "image/svg+xml"))
        for url in ("http://cdn.fsk.ru/a.svg", "https://cdn.fsk.ru.evil.test/a.svg",
                    "https://user@cdn.fsk.ru/a.svg", "https://cdn.fsk.ru/a.svg?token=x",
                    "https://cdn.fsk.ru:444/a.svg", "https://cdn.fsk.ru/../a.svg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                media._exact_url(url)

    def test_plan_is_bound_to_exact_unit_and_menu_generation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            map_dir = base / "map" / "map1"
            menu_dir = base / "menu" / "menu1"
            map_dir.mkdir(parents=True)
            menu_dir.mkdir(parents=True)
            map_manifest = b'{"run_id":"map1"}\n'
            (map_dir / "manifest.json").write_bytes(map_manifest)
            (map_dir / "points.jsonl").write_text(
                json.dumps({"registry_id": "registry-one"}) + "\n")
            (menu_dir / "manifest.json").write_text(json.dumps({
                "run_id": "menu1", "map_manifest_sha256": _sha(map_manifest)}) + "\n")
            (menu_dir / "units.jsonl").write_text(json.dumps({
                "unit_id": "unit-one", "registry_id": "registry-one",
                "observed_at": "2026-10-10T10:00:00Z"}) + "\n")
            (menu_dir / "floors.jsonl").write_text("")
            plan = {"plan_id": "plan_one", "registry_id": "registry-one",
                    "unit_id": "unit-one", "floor_id": None,
                    "role": "apartment_plan", "source_role": "apartment_plan",
                    "source_scope": "unit", "source_field": "plan",
                    "source_url": "https://cdn.fsk.ru/production/unit.svg",
                    "source_evidence_sha256": "a" * 64, "rights_status": "unknown",
                    "asset_sha256": None, "asset_mime": None}
            plan_path = menu_dir / "plans.jsonl"
            plan_path.write_text(json.dumps(plan) + "\n")
            with (patch.object(media, "MAP_ROOT", base / "map"),
                  patch.object(media, "MENU_ROOT", base / "menu"),
                  patch.object(media.map_reader, "load_staged", return_value={}),
                  patch.object(media.menu_reader, "load_staged", return_value={})):
                rows = list(media.plan_media_rows("map1", "menu1"))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["unit_id"], "unit-one")
                self.assertEqual(rows[0]["scope"], "unit_plan")
                self.assertIsNone(rows[0]["prepared_sha256"])
                plan["registry_id"] = "registry-other"
                plan_path.write_text(json.dumps(plan) + "\n")
                with self.assertRaisesRegex(ValueError, "absent building"):
                    list(media.plan_media_rows("map1", "menu1"))


if __name__ == "__main__":
    unittest.main()
