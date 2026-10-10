"""Generated texture is embedded only in a bounded, labelled GLB."""

from __future__ import annotations

import base64
import json
import struct
import unittest

from shapely.geometry import Polygon

from sources.property.textured_model import textured_glb


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9l0WQAAAAASUVORK5CYII="
)


class TexturedModelTest(unittest.TestCase):
    def test_uv_texture_and_reconstruction_metadata_are_embedded(self) -> None:
        polygon = Polygon([(37.0, 55.0), (37.0001, 55.0),
                           (37.0001, 55.0001), (37.0, 55.0001)])
        blob, stats = textured_glb(polygon, 37.00005, 55.00005, 30.0, PNG_1PX)
        self.assertEqual(blob[:4], b"glTF")
        self.assertEqual(struct.unpack_from("<I", blob, 8)[0], len(blob))
        json_length = struct.unpack_from("<I", blob, 12)[0]
        document = json.loads(blob[20:20 + json_length])
        self.assertTrue(document["asset"]["extras"]["reconstruction"])
        self.assertEqual(document["images"][0]["mimeType"], "image/png")
        self.assertIn("TEXCOORD_0", document["meshes"][0]["primitives"][1]["attributes"])
        self.assertEqual(stats["wall_triangles"], 8)


if __name__ == "__main__":
    unittest.main()
