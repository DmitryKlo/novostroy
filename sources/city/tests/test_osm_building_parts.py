import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "osm_building_parts.py"
SPEC = importlib.util.spec_from_file_location("osm_building_parts", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class OSMBuildingPartsTests(unittest.TestCase):
    def test_supported_height_units_normalize_to_metres(self) -> None:
        cases = {
            "30": 30.0,
            "30 m": 30.0,
            "0.03 km": 30.0,
            "328 ft": 99.9744,
            "12'5\"": 3.7846,
            "3 yd": 2.7432,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                actual, status = module.parse_dimension(raw, positive=True)
                self.assertEqual(status, "parsed")
                self.assertAlmostEqual(actual, expected, places=4)

    def test_unsupported_unit_syntax_is_preserved_as_unparsed(self) -> None:
        value, status = module.parse_dimension("12m", positive=True)
        self.assertIsNone(value)
        self.assertEqual(status, "unsupported_syntax_or_unit")

    def test_floor_tags_accept_only_scalar_counts(self) -> None:
        self.assertEqual(module.parse_count("5", positive=True), (5.0, "parsed"))
        self.assertEqual(module.parse_count("5 m", positive=True), (None, "unsupported_syntax_or_unit"))
        self.assertEqual(module.parse_count("5-7", positive=True), (None, "unsupported_syntax_or_unit"))

    def test_overture_source_record_id_retains_typed_id_and_version(self) -> None:
        self.assertEqual(module.source_identity("w159755858@1"), ("way", 159755858, 1))
        self.assertEqual(module.source_identity("r2138156@1"), ("relation", 2138156, 1))
        self.assertIsNone(module.source_identity("not-an-osm-record"))


if __name__ == "__main__":
    unittest.main()
