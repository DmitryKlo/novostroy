"""Tests for the flat city stage and compatibility publication contract."""

from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import Polygon

from pipeline import publish_public_assets
from pipeline.build_progressive_city import RasterSilhouette
from pipeline.build_tiles import ecef, enu_transform, region


class RasterSilhouetteTests(unittest.TestCase):
    def test_hole_stays_transparent_and_overlapping_building_remains_opaque(self) -> None:
        tile = RasterSilhouette(region(0, 0, 0.01, 0.01, 30))
        outer = [(0.001, 0.001), (0.009, 0.001), (0.009, 0.009), (0.001, 0.009)]
        hole = [(0.004, 0.004), (0.006, 0.004), (0.006, 0.006), (0.004, 0.006)]
        tile.polygon(Polygon(outer, [hole]))

        def alpha(lon: float, lat: float) -> int:
            x, y = tile._pixel(lon, lat)
            return tile.mask[math.floor(y) * tile.width + math.floor(x)]

        self.assertEqual(alpha(0.002, 0.002), 255)
        self.assertEqual(alpha(0.005, 0.005), 0)
        self.assertEqual(alpha(0.0005, 0.0005), 0)
        tile.polygon(Polygon([(0.0045, 0.0045), (0.0055, 0.0045),
                              (0.0055, 0.0055), (0.0045, 0.0055)]))
        self.assertEqual(alpha(0.005, 0.005), 255)

    def test_glb_embeds_palette_alpha_and_curved_surface_grid(self) -> None:
        tile = RasterSilhouette(region(37.0, 55.0, 37.05, 55.05, 100))
        tile.polygon(Polygon([(37.01, 55.01), (37.02, 55.01),
                              (37.02, 55.02), (37.01, 55.02)]))
        tile.footprints = 1
        blob = tile.to_glb()
        self.assertEqual(struct.unpack_from("<4sII", blob), (b"glTF", 2, len(blob)))
        json_length = struct.unpack_from("<I", blob, 12)[0]
        document = json.loads(blob[20:20 + json_length])
        self.assertEqual(document["accessors"][0]["count"], 25)
        self.assertEqual(document["accessors"][2]["count"], 96)
        self.assertEqual(document["materials"][0]["alphaMode"], "MASK")
        self.assertEqual(document["asset"]["extras"]["source_count"], 1)
        self.assertNotIn("source_ids", blob.decode("latin1"))
        binary_length = struct.unpack_from("<I", blob, 20 + json_length)[0]
        binary = blob[28 + json_length:28 + json_length + binary_length]
        east, up, south = struct.unpack_from("<3f", binary, 0)
        parent = enu_transform(37.025, 55.025)
        actual = [parent[12 + i] + east * parent[i] - south * parent[4 + i]
                  + up * parent[8 + i] for i in range(3)]
        expected = ecef(37.0, 55.05, 0.05)
        self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 0.05)
        uv_view = document["bufferViews"][1]
        self.assertEqual(struct.unpack_from("<2f", binary, uv_view["byteOffset"]), (0.0, 0.0))
        image_view = document["bufferViews"][3]
        png = binary[image_view["byteOffset"]:image_view["byteOffset"] + image_view["byteLength"]]
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((png[24], png[25]), (1, 3))  # one-bit indexed PNG
        offset = 8
        compressed = bytearray()
        transparency = None
        while offset < len(png):
            length = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4:offset + 8]
            if kind == b"IDAT":
                compressed.extend(png[offset + 8:offset + 8 + length])
            if kind == b"tRNS":
                transparency = png[offset + 8:offset + 8 + length]
            offset += length + 12
        self.assertEqual(transparency, bytes((0, 255)))
        pixels = zlib.decompress(compressed)
        self.assertIn(0, pixels)
        self.assertTrue(any(value != 0 for value in pixels))


class PublicationTests(unittest.TestCase):
    def test_publishing_city_v10_retains_previous_city_ground_and_roads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory)
            (asset_root / "manifest.json").write_text(json.dumps({
                "city_tileset_url": "/assets/public-citywide-v9/tileset.json",
                "ground_tileset_url": "/assets/public-ground-v3/tileset.json",
                "roads_tileset_url": "/assets/public-roads-v2/tileset.json",
                "previous_public_roots": ["public-citywide-v4", "public-citywide-v5",
                                          "public-citywide-v6", "public-citywide-v7",
                                          "public-citywide-v8", "public-ground-v2",
                                          "public-roads-v1"],
            }))
            prior_mappings = [
                {"kind": "city_volumes", "public_root": "public-citywide-v9"},
                {"kind": "ground_buildings", "public_root": "public-ground-v3"},
                {"kind": "roads", "public_root": "public-roads-v2"},
                {"kind": "parks", "public_root": "public-parks-v1"},
                {"kind": "water", "public_root": "public-water-v1"},
            ]
            (asset_root / "public_release_audit.json").write_text(json.dumps({
                "mappings": prior_mappings,
                "compatibility_mappings": [
                    {"kind": "city_volumes", "public_root": "public-citywide-v4"},
                    {"kind": "city_volumes", "public_root": "public-citywide-v5"},
                    {"kind": "city_volumes", "public_root": "public-citywide-v6"},
                    {"kind": "city_volumes", "public_root": "public-citywide-v7"},
                    {"kind": "city_volumes", "public_root": "public-citywide-v8"},
                    {"kind": "ground_buildings", "public_root": "public-ground-v2"},
                    {"kind": "roads", "public_root": "public-roads-v1"},
                ],
            }))

            def staged(kind: str, internal: str, public: str) -> dict[str, str]:
                return {"kind": kind, "internal_root": internal, "public_root": public}

            with patch.object(publish_public_assets, "ASSET_ROOT", asset_root), \
                    patch.object(publish_public_assets, "stage_public_root", side_effect=staged):
                audit = publish_public_assets.publish()
            pointer = json.loads((asset_root / "manifest.json").read_text())
            self.assertEqual(pointer["city_tileset_url"], "/assets/public-citywide-v10/tileset.json")
            self.assertEqual(pointer["ground_tileset_url"], "/assets/public-ground-v3/tileset.json")
            self.assertEqual(pointer["roads_tileset_url"], "/assets/public-roads-v2/tileset.json")
            self.assertEqual(pointer["previous_public_roots"],
                             ["public-citywide-v4", "public-citywide-v5", "public-citywide-v6",
                              "public-citywide-v7", "public-citywide-v8", "public-citywide-v9",
                              "public-ground-v2", "public-roads-v1"])
            self.assertEqual({row["public_root"] for row in audit["compatibility_mappings"]},
                             {"public-citywide-v4", "public-citywide-v5", "public-citywide-v6",
                              "public-citywide-v7", "public-citywide-v8", "public-ground-v2",
                              "public-citywide-v9", "public-roads-v1"})


if __name__ == "__main__":
    unittest.main()
