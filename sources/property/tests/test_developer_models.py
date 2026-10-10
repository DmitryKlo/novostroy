"""Selected model gates must preserve exact footprint and uncertain heights."""

from __future__ import annotations

import unittest

from sources.property import developer_models as models


def corpus() -> dict:
    return {
        "record_type": "corpus", "source_group": "proplanirovki",
        "geometry_inside_moscow": "inside", "geometry_basis": "aggregator_unverified",
        "source_geometry": {"type": "Polygon", "coordinates": [[
            [37.0, 55.0], [37.0001, 55.0], [37.0001, 55.0001],
            [37.0, 55.0001], [37.0, 55.0]]]},
        "observed_at": "2026-10-10T12:00:00Z",
    }


def point() -> dict:
    return {"registry_id": "registry-test", "location": [37.00005, 55.00005]}


def floors() -> dict:
    return {"registry_id": "registry-test", "total_floors": 10,
            "total_floors_basis": "source_apartment_record_building_floors",
            "estimated_height_m": 30.0, "height_conflict": False}


class DeveloperModelsTest(unittest.TestCase):
    def test_exact_corpus_emits_selected_schematic_glb(self) -> None:
        row, blob = models._model(point(), floors(), corpus())
        self.assertEqual(blob[:4], b"glTF")
        self.assertEqual(row["height_m"], 30.0)
        self.assertEqual(row["height_basis"], models.HEIGHT_BASIS)
        self.assertEqual(row["label"], "реконструкция")
        self.assertEqual(len(row["model_transform"]), 16)
        self.assertEqual(row["asset_sha256"], models._sha(blob))

    def test_project_center_or_conflicting_floors_are_rejected(self) -> None:
        wrong_point = point()
        wrong_point["location"] = [37.5, 55.5]
        with self.assertRaisesRegex(ValueError, "covered"):
            models._model(wrong_point, floors(), corpus())
        conflict = floors()
        conflict["height_conflict"] = True
        with self.assertRaisesRegex(ValueError, "eligible source floor"):
            models._model(point(), conflict, corpus())


if __name__ == "__main__":
    unittest.main()
