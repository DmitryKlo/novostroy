"""Focused JSON-only city palette revision checks on real small samples."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from pipeline.build_city_palette_revision import BASE, NEW_FACTOR, recolor_glb


def chunks(path: Path) -> tuple[dict, bytes]:
    blob = path.read_bytes()
    json_length = struct.unpack_from("<I", blob, 12)[0]
    return json.loads(blob[20:20 + json_length]), blob[20 + json_length:]


class CityPaletteRevisionTests(unittest.TestCase):
    def test_sample_overview_and_detail_change_only_massing_factors(self) -> None:
        for pattern, overview in (("massing_region_*.glb", True), ("cell_*.glb", False)):
            source = next(BASE.glob(pattern))
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / source.name
                self.assertTrue(recolor_glb(source, target, overview=overview))
                self.assertFalse(recolor_glb(source, target, overview=overview))
                self.assertFalse(os.path.samefile(source, target))
                old_document, old_binary = chunks(source)
                new_document, new_binary = chunks(target)
                self.assertEqual(old_binary, new_binary)
                indexes = (0, 1) if overview else (4,)
                for index in indexes:
                    self.assertEqual(new_document["materials"][index]["pbrMetallicRoughness"]["baseColorFactor"], NEW_FACTOR)
                    old_document["materials"][index]["pbrMetallicRoughness"]["baseColorFactor"] = NEW_FACTOR
                self.assertEqual(old_document, new_document)


if __name__ == "__main__":
    unittest.main()
