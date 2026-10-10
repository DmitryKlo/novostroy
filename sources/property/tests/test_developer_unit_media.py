"""Bounded media preparation rejects active SVG and unbound floor regions."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sources.property import developer_unit_media as media


class DeveloperUnitMediaTest(unittest.TestCase):
    def test_inert_svg_rasterizes_to_bounded_png(self) -> None:
        source = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                  b'<rect width="10" height="10" fill="red"/></svg>')
        safe = media._inert_svg(source)
        self.assertIn(b":svg", safe)
        import subprocess
        result = subprocess.run(["rsvg-convert", "--format", "png"], input=safe,
                                capture_output=True, check=True, timeout=15)
        self.assertEqual(media._png_dimensions(result.stdout), (10, 10))

    def test_external_and_active_svg_content_is_rejected(self) -> None:
        unsafe = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<image xlink:href="https://evil.example/track.png"/></svg>'
        )
        with self.assertRaisesRegex(ValueError, "unsupported active"):
            media._inert_svg(unsafe)
        with self.assertRaisesRegex(ValueError, "external-document"):
            media._inert_svg(b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                             b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_floor_regions_require_matching_image_and_floor(self) -> None:
        evidence = {"source_image_sha256": "a" * 64,
                    "source_image_url": "https://cdn.fsk.ru/plan.png",
                    "registry_id": "registry-example", "section": 2, "floor": 5,
                    "geometry_basis": "raster_traced_floor_plan",
                    "reconstruction": True,
                    "regions": [{"region_id": "r1", "polygon_normalized_xy":
                                 [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
                                 "source_label": "1 room", "source_rooms": 1,
                                 "source_area_m2": 30.0}]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "regions.json"
            path.write_text(json.dumps(evidence))
            capture = {"sha256": "a" * 64, "source_url": evidence["source_image_url"],
                       "registry_id": "registry-example", "section_label": "2",
                       "floor_number": 5}
            with patch.object(media, "_private_source", return_value=path):
                rows = media._regions(path, capture, "floor-example", "b" * 64)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["unit_id"])
                self.assertEqual(rows[0]["match_status"], "unconfirmed")
                with self.assertRaisesRegex(ValueError, "not tied"):
                    media._regions(path, {**capture, "floor_number": 6},
                                   "floor-example", "b" * 64)


if __name__ == "__main__":
    unittest.main()
