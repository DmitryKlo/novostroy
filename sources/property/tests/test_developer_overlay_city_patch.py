"""The city replacement must cover overview and detail without changing neighbours."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sources.property.developer_map import _canonical, _sha
from sources.property.developer_overlay_city_patch import assemble


def _write(path: Path, value: bytes | dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else _canonical(value) + b"\n"
    path.write_bytes(data)
    return _sha(data)


class ReviewedCityPatchTest(unittest.TestCase):
    def _fixture(self, root: Path, *, target: str | None = None
                 ) -> tuple[Path, Path, Path, set[str]]:
        city = root / "city"
        detail_dir = root / "detail"
        overview_dir = root / "overview"
        target = target or "registry-" + "a" * 24
        unaffected = "registry-" + "b" * 24
        overview_name = "massing_region_1_2.glb"
        region_name = "region_1_2.tileset.json"
        detail_name = "cell_3_4.glb"
        other_detail = "cell_4_4.glb"
        other_overview = "massing_region_2_2.glb"
        other_region = "region_2_2.tileset.json"
        base_sha = _write(city / "manifest.json", {"schema_version": "city/1"})
        _write(city / overview_name, b"original overview")
        _write(city / detail_name, b"original detail")
        _write(city / other_overview, b"untouched overview")
        _write(city / other_detail, b"untouched detail")
        _write(city / region_name, {
            "root": {"content": {"uri": overview_name}, "children": [
                {"content": {"uri": detail_name}},
                {"content": {"uri": other_detail}},
            ]}})
        _write(city / other_region, {"root": {"content": {"uri": other_overview}}})
        _write(city / "tileset.json", {"root": {"children": [
            {"content": {"uri": overview_name},
             "children": [{"content": {"uri": region_name}}]},
            {"content": {"uri": other_overview},
             "children": [{"content": {"uri": other_region}}]},
        ]}})
        patched_detail_sha = _write(detail_dir / "patched-detail" / detail_name,
                                    b"patched detail")
        detail_manifest = detail_dir / "manifest.json"
        _write(detail_manifest, {
            "inputs": {"public_city_manifest_sha256": base_sha},
            "target_city_feature_mapping": [{"registry_id": target,
                                             "city_feature_id_private": "source-id"}],
            "tiles": [{"tile": detail_name,
                       "overview_lod": {"external_tileset": region_name},
                       "original_public_sha256": _sha(b"original detail"),
                       "patched_private_sha256": patched_detail_sha,
                       "target_features": [{"registry_id": target}]}],
        })
        patched_overview_sha = _write(overview_dir / "patched" / overview_name,
                                      b"patched overview")
        overview_manifest = overview_dir / "manifest.json"
        pair = {"city_feature_id": "source-id", "registry_id": target}
        _write(overview_manifest, {
            "base_city_manifest_sha256": base_sha,
            "pair_count": 1,
            "pair_mapping_sha256": _sha(_canonical([pair]) + b"\n"),
            "regions": [{"name": overview_name,
                         "source_sha256": _sha(b"original overview"),
                         "patched_sha256": patched_overview_sha,
                         "removed_pairs": [pair],
                         "removed_registry_ids": [target],
                         "removed_source_ids": ["source-id"],
                         "reproduction_check": True}],
        })
        return city, detail_manifest, overview_manifest, {target, unaffected}

    @staticmethod
    def _source(target: str) -> dict[str, dict[str, str]]:
        return {target: {"source_group": "proplanirovki",
                         "publisher_project_id": "project-1",
                         "publisher_building_id": "corpus-1",
                         "source_polygon_sha256": "a" * 64}}

    def test_both_lods_replaced_and_unaffected_refs_remain_base_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            city, detail, overview, identities = self._fixture(Path(directory))
            target = sorted(identities)[0]
            descriptor, files = assemble(detail, overview, {target}, self._source(target),
                                         city_root=city)
            self.assertEqual(descriptor["replacement_tiles_total"], 2)
            self.assertEqual(descriptor["regions_total"], 1)
            self.assertTrue(descriptor["all_visible_lods_covered"])
            self.assertEqual(descriptor["pair_proof_total"], 1)
            pair_proof = json.loads(files["city-patch/pair-proof.jsonl"])
            self.assertEqual(pair_proof["geometry_source_id"],
                             "proplanirovki:project-1:corpus-1")
            root = json.loads(files["city-patch/tileset.json"])
            patched_root, unaffected_root = root["root"]["children"]
            self.assertTrue(patched_root["content"]["uri"].startswith("tiles/"))
            self.assertTrue(patched_root["children"][0]["content"]["uri"].startswith("regions/"))
            self.assertEqual(unaffected_root["content"]["uri"],
                             "/assets/public-citywide-v12/massing_region_2_2.glb")
            region_uri = patched_root["children"][0]["content"]["uri"]
            region = json.loads(files["city-patch/" + region_uri])
            self.assertTrue(region["root"]["content"]["uri"].startswith("../tiles/"))
            self.assertTrue(region["root"]["children"][0]["content"]["uri"].startswith("../tiles/"))
            self.assertEqual(region["root"]["children"][1]["content"]["uri"],
                             "/assets/public-citywide-v12/cell_4_4.glb")

    def test_missing_overview_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            city, detail, overview, identities = self._fixture(Path(directory))
            data = json.loads(overview.read_text(encoding="utf-8"))
            data["regions"][0]["removed_registry_ids"] = []
            _write(overview, data)
            with self.assertRaisesRegex(ValueError, "Overview removed IDs differ"):
                target = sorted(identities)[0]
                assemble(detail, overview, {target}, self._source(target), city_root=city)

    def test_unverified_fsk_geometry_cannot_enter_exact_lake_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            city, detail, overview, identities = self._fixture(Path(directory))
            target = sorted(identities)[0]
            fake_fsk = {target: {"source_group": "fsk",
                                 "publisher_project_id": "unrelated",
                                 "publisher_building_id": "unrelated",
                                 "source_polygon_sha256": "a" * 64}}
            with self.assertRaisesRegex(ValueError, "Only the exact The Lake"):
                assemble(detail, overview, {target}, fake_fsk, city_root=city)

    def test_exact_lake_osm_source_identity_is_pinned_in_pair_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = "registry-b1feab9d36bced349dfa4b1d"
            city, detail, overview, _ = self._fixture(Path(directory), target=target)
            lake = {target: {"source_group": "fsk",
                             "publisher_project_id": "636ce8f7f459625c9efe72ad",
                             "publisher_building_id": "639b34e2b176d8bf3ad3d6b1",
                             "source_polygon_sha256": "b" * 64}}
            descriptor, files = assemble(detail, overview, {target}, lake, city_root=city)
            self.assertEqual(descriptor["pair_proof_total"], 1)
            pair = json.loads(files["city-patch/pair-proof.jsonl"])
            self.assertEqual(pair["geometry_source_id"], "osm:way/1317814900@2")

    def test_wrong_pair_label_rejected_even_when_id_sets_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            city, detail, overview, identities = self._fixture(Path(directory))
            data = json.loads(overview.read_text(encoding="utf-8"))
            data["regions"][0]["removed_pairs"] = [{
                "city_feature_id": "different-source-id",
                "registry_id": sorted(identities)[0],
            }]
            _write(overview, data)
            target = sorted(identities)[0]
            with self.assertRaisesRegex(ValueError, "city/registry pair differs"):
                assemble(detail, overview, {target}, self._source(target), city_root=city)

    def test_permuted_detail_order_keeps_pairs_but_swapped_pair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            city, detail, overview, identities = self._fixture(Path(directory))
            first, second = sorted(identities)
            detail_data = json.loads(detail.read_text(encoding="utf-8"))
            detail_data["target_city_feature_mapping"] = [
                {"registry_id": first, "city_feature_id_private": "source-z"},
                {"registry_id": second, "city_feature_id_private": "source-a"},
            ]
            detail_data["tiles"][0]["target_features"] = [
                {"registry_id": first}, {"registry_id": second}]
            _write(detail, detail_data)
            overview_data = json.loads(overview.read_text(encoding="utf-8"))
            correct_pairs = [
                {"city_feature_id": "source-a", "registry_id": second},
                {"city_feature_id": "source-z", "registry_id": first},
            ]
            overview_data["pair_count"] = 2
            overview_data["pair_mapping_sha256"] = _sha(_canonical(correct_pairs) + b"\n")
            region = overview_data["regions"][0]
            region["removed_pairs"] = correct_pairs
            region["removed_registry_ids"] = [first, second]
            region["removed_source_ids"] = ["source-a", "source-z"]
            _write(overview, overview_data)
            sources = {**self._source(first), **self._source(second)}
            _, first_files = assemble(detail, overview, identities, sources, city_root=city)
            detail_data["target_city_feature_mapping"].reverse()
            detail_data["tiles"][0]["target_features"].reverse()
            _write(detail, detail_data)
            _, permuted_files = assemble(detail, overview, identities, sources, city_root=city)
            self.assertEqual(first_files["city-patch/pair-proof.jsonl"],
                             permuted_files["city-patch/pair-proof.jsonl"])
            region["removed_pairs"] = [
                {"city_feature_id": "source-a", "registry_id": first},
                {"city_feature_id": "source-z", "registry_id": second},
            ]
            _write(overview, overview_data)
            with self.assertRaisesRegex(ValueError, "city/registry pair differs"):
                assemble(detail, overview, identities, sources, city_root=city)


if __name__ == "__main__":
    unittest.main()
