"""Identity and floor evidence gates for the private apartment menu."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sources.property import developer_units as menu


def point(source: str = "fsk") -> dict:
    return {"registry_id": "registry-example", "source_group": source,
            "publisher_project_id": "project-1", "publisher_building_id": "building-1",
            "source_corpus_label": "1"}


def selected(source: str = "fsk", floor: int = 5, section: str | None = "2") -> dict:
    row = {"observation_id": "observation-1", "source_group": source,
           "observed_at": "2026-10-10T12:00:00Z", "source_updated_at": None,
           "source_rights_status": "unknown", "price_basis": "asking_total",
           "price_rub": 100_000, "unit": {"number": "107", "rooms": "1-к",
                                    "section": section, "floor": floor, "area_m2": 40}}
    return {"source_row": row, "point": point(source),
            "unit_id": menu._unit_id(source, "u-107"), "publisher_unit_id": "u-107"}


def raw(media: list[dict], *, building_floors: int | None = None,
        section_floors: int | None = None) -> dict:
    return {"observation_id": "observation-1", "source_group": "fsk",
            "publisher_unit_id": "u-107", "media": media,
            "source_fields": {"building_floors": building_floors,
                              "api_section_floors_count": section_floors}}


class DeveloperUnitsTest(unittest.TestCase):
    def test_floor_plan_requires_exact_floor_scope(self) -> None:
        refs = [
            {"role": "apartment_plan", "scope": "unit", "url": "https://example.test/u.png"},
            {"role": "floor_plan", "scope": "floor", "url": "https://example.test/f.png"},
            {"role": "floor_plan", "scope": "project", "url": "https://example.test/other.png"},
        ]
        output = menu._menu_outputs([selected()], [], {"observation-1": raw(refs)},
                                    [point()], "developer-map-v1-example")
        self.assertEqual(len(output["plans"]), 2)
        by_role = {plan["role"]: plan for plan in output["plans"]}
        self.assertEqual(by_role["apartment_plan"]["unit_id"], menu._unit_id("fsk", "u-107"))
        self.assertIsNone(by_role["apartment_plan"]["floor_id"])
        self.assertEqual(by_role["floor_plan"]["floor_id"], output["floors"][0]["floor_id"])
        self.assertIsNone(by_role["floor_plan"]["unit_id"])

    def test_section_floor_is_not_building_height_when_sections_differ(self) -> None:
        first = selected()
        second = selected(floor=10, section="1")
        second["source_row"]["observation_id"] = "observation-2"
        second["publisher_unit_id"] = "u-118"
        second["unit_id"] = menu._unit_id("fsk", "u-118")
        outputs = menu._menu_outputs(
            [first, second], [],
            {"observation-1": raw([], section_floors=10),
             "observation-2": {**raw([], section_floors=16),
                               "observation_id": "observation-2",
                               "publisher_unit_id": "u-118"}},
            [point()], "developer-map-v1-example")
        building = outputs["buildings"][0]
        self.assertIsNone(building["total_floors"])
        self.assertIsNone(building["estimated_height_m"])
        self.assertEqual(building["source_section_total_floors"], {"1": [16], "2": [10]})
        self.assertEqual(building["max_observed_unit_floor"], 10)

    def test_a101_numeric_id_and_exact_corpus_label_share_one_point(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "developer-map-v1-example"
            run = root / "runs" / run_id
            run.mkdir(parents=True)
            candidate = point("a101")
            candidate["source_corpus_label"] = "7.1.2"
            (run / "points.jsonl").write_text(json.dumps(candidate) + "\n")
            (run / "manifest.json").write_text("{}\n")
            digest = menu._file_sha(run / "manifest.json")
            (root / "active.json").write_text(json.dumps({"run_id": run_id,
                                                            "manifest_sha256": digest}))
            with patch.object(menu, "MAP_ROOT", root), patch.object(
                    menu, "validate_run", return_value={"snapshot_id": "snapshot"}):
                index, _, _, _, _ = menu._map_index(run_id)
            self.assertEqual(index[("a101", "project-1", "id:building-1")], candidate)
            self.assertIs(index[("a101", "project-1", "corpus:7.1.2")],
                          index[("a101", "project-1", "id:building-1")])

    def test_reviewed_project_section_range_blocks_uniform_corpus_height(self) -> None:
        item = selected("proplanirovki", floor=20, section=None)
        source_point = item["point"]
        source_point.update({"registry_id": menu.REVIEWED_SECTION_RANGE["registry_id"],
                             "publisher_project_id": menu.REVIEWED_SECTION_RANGE["publisher_project_id"],
                             "publisher_building_id": menu.REVIEWED_SECTION_RANGE["publisher_building_id"]})
        raw_row = raw([], building_floors=29)
        raw_row["source_group"] = "proplanirovki"
        output = menu._menu_outputs([item], [], {"observation-1": raw_row},
                                    [source_point], "developer-map-v1-example")
        building = output["buildings"][0]
        self.assertIsNone(building["total_floors"])
        self.assertIsNone(building["estimated_height_m"])
        self.assertTrue(building["height_conflict"])
        self.assertEqual(building["reviewed_section_range"]["project_section_floor_range"],
                         [24, 29])


if __name__ == "__main__":
    unittest.main()
