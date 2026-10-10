from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, shape

from sources.models.build import _asset, _height, _preview, _preview_geometry, candidate_id
from sources.models.geometry import ModelMesh, validated_part_profile


class ModelCandidateFixtureTests(unittest.TestCase):
    def test_height_evidence_never_turns_unknown_into_uniform_volume(self) -> None:
        unknown, height, codes = _height({"building:levels:underground": "3"})
        self.assertIsNone(height)
        self.assertIn("height_unknown_flat_only", codes)
        self.assertIsNone(unknown["mapped_levels"])
        tagged, height, codes = _height({"height": "66 m", "building:levels": "22"})
        self.assertEqual(height, 66)
        self.assertEqual(tagged["mapped_height_m"], 66)
        self.assertIsNone(tagged["estimated_render_height_m"])
        self.assertNotIn("height_tag_conflict_flat_only", codes)
        conflict, height, codes = _height({"height": "120", "building:levels": "12"})
        self.assertIsNone(height)
        self.assertIn("height_tag_conflict_flat_only", codes)
        self.assertEqual(conflict["mapped_height_m"], 120)

    def test_courtyard_is_preserved_in_selected_flat_and_volume(self) -> None:
        parent = Polygon(
            [(37.6, 55.7), (37.6003, 55.7), (37.6003, 55.7003), (37.6, 55.7003)],
            [[(37.6001, 55.7001), (37.6002, 55.7001),
              (37.6002, 55.7002), (37.6001, 55.7002)]],
        )
        self.assertIsNone(_preview(parent))
        self.assertTrue(shape(_preview_geometry(parent)).equals(parent))
        for height in (None, 42.0):
            mesh = ModelMesh(37.60015, 55.70015, height)
            mesh.add(parent)
            self.assertEqual(mesh.holes, 1)
            self.assertGreater(mesh.triangles, 2)
            self.assertEqual(bool(mesh.walls), height is not None)
            blob = mesh.to_glb()
            magic, version, length = struct.unpack_from("<4sII", blob, 0)
            self.assertEqual((magic, version, length), (b"glTF", 2, len(blob)))
            json_length = struct.unpack_from("<I", blob, 12)[0]
            document = json.loads(blob[20:20 + json_length])
            self.assertTrue(document["asset"]["extras"]["source_ids_redacted"])
            self.assertEqual(len(document["meshes"][0]["primitives"]), 1 if height is None else 2)

    def test_only_exact_validated_parts_create_stepped_volume(self) -> None:
        parent = Polygon([(37.6, 55.7), (37.6002, 55.7),
                          (37.6002, 55.7001), (37.6, 55.7001)])
        first = Polygon([(37.6, 55.7), (37.6001, 55.7),
                         (37.6001, 55.7001), (37.6, 55.7001)])
        second = Polygon([(37.6001, 55.7), (37.6002, 55.7),
                          (37.6002, 55.7001), (37.6001, 55.7001)])
        key = ("way", 123, 4)
        rows = [{"parent_osm_key": list(key), "validation_profile": "clean_multilevel_parts_v1",
                 "rights_status": "cleared", "geometry": part,
                 "height_top_m": top, "vertical_base_m": 0.0}
                for part, top in ((first, 18.0), (second, 31.0))]
        profile = validated_part_profile(key, parent, rows)
        self.assertIsNotNone(profile)
        self.assertIsNone(validated_part_profile(("way", 123, 5), parent, rows))
        self.assertIsNone(validated_part_profile(key, parent, rows[:1]))
        assert profile is not None
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            cache, public = directory / "cache", directory / "public"
            cache.mkdir()
            public.mkdir()
            selected, _ = _asset(parent, None, cache, public, "public-project-models-v1-test",
                                  {}, validated_parts=profile)
            tileset = json.loads((public / selected["tileset_url"].split("/")[-1]).read_text())
            self.assertEqual(len(tileset["root"]["children"]), 1)
            self.assertEqual(tileset["root"]["boundingVolume"]["region"][5], 33.65)

    def test_candidate_id_is_opaque_and_source_namespaced(self) -> None:
        first = candidate_id("osm_construction", "way/42")
        self.assertEqual(first, candidate_id("osm_construction", "way/42"))
        self.assertNotEqual(first, candidate_id("estateline_accepted", "way/42"))
        self.assertEqual(len(first), 29)
        self.assertNotIn("way", first)

    def test_preview_geometry_keeps_all_multipolygon_components(self) -> None:
        one = Polygon([(37.6, 55.7), (37.6001, 55.7),
                       (37.6001, 55.7001), (37.6, 55.7001)])
        two = Polygon([(37.6002, 55.7), (37.6003, 55.7),
                       (37.6003, 55.7001), (37.6002, 55.7001)])
        both = MultiPolygon([one, two])
        preview = _preview_geometry(both)
        self.assertEqual(preview["type"], "MultiPolygon")
        self.assertTrue(shape(preview).equals(both))
        self.assertIsNone(_preview(both))


if __name__ == "__main__":
    unittest.main()
