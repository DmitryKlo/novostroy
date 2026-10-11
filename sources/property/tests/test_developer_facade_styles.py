"""Reviewed project appearance tokens make bounded, source-pinned tiles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from sources.property.developer_facade_styles import (
    BASIS,
    LOD_SIZES,
    RECONSTRUCTION_LABEL,
    InsufficientStyleEvidence,
    build_project_facade_style,
    load_frozen_style_batch,
    validate_material_record,
    write_frozen_style_batch,
)


def _observation(project_id: str = "project-123") -> dict:
    return {
        "schema_version": "developer-facade-style-observation/1",
        "provider": "sample-provider",
        "publisher_project_id": project_id,
        "project_name": "Sample Residential Project",
        "source_scope": "project_gallery",
        "sources": [{
            "page_url": "https://example.test/project",
            "page_sha256": "b" * 64,
            "asset_url": "https://cdn.example.test/render.jpg",
            "asset_sha256": "a" * 64,
            "role": "render",
            "access_method": "pinned private HTTPS capture",
            "observed_at": "2026-10-11T10:00:00Z",
            "source_updated_at": "2026-10-09T00:00:00Z",
            "rights_status": "unknown_private_research_only",
        }],
        "visual_review": {
            "palette": {"wall": "pale rose brick-like", "glazing": "dark blue-gray",
                        "frames": "dark bronze", "accents": ["pale vertical strips"],
                        "confidence": "high"},
            "appearance_materials": ["brick-like appearance"],
            "rhythm": {"windows": "regular rectangular grid", "balconies": "not visible",
                       "horizontal_bands": "subtle pale horizontal bands", "confidence": "high"},
            "confidence": "high",
            "limitations": ["project rendering; not verified as-built facade"],
        },
        "generator_tokens": {
            "palette": {"wall": "rose_brick", "glazing": "dark_blue_gray",
                        "frames": "dark_bronze", "accents": ["ivory"]},
            "rhythm": {"windows": "regular_grid", "balconies": "not_observed",
                       "horizontal_bands": "subtle", "vertical_strips": "periodic"},
        },
        "not_observed": ["building geometry", "orientation", "corpus-specific facade"],
        "checked_at": "2026-10-11T10:05:00Z",
    }


class DeveloperFacadeStylesTest(unittest.TestCase):
    def test_deterministic_project_keyed_material_and_bounded_tiers(self) -> None:
        observation = _observation()
        first = build_project_facade_style(observation)
        second = build_project_facade_style(observation)
        self.assertEqual(first.material_record, second.material_record)
        self.assertEqual(first.textures, second.textures)
        record = first.material_record
        validate_material_record(record)
        self.assertEqual(record["basis"], BASIS)
        self.assertEqual(record["label"], RECONSTRUCTION_LABEL)
        self.assertEqual(record["publisher_project_id"], "project-123")
        self.assertEqual([(item["level"], item["width"], item["height"])
                          for item in record["lods"]], list(LOD_SIZES))
        for item in record["lods"]:
            png = first.textures[item["asset_path"]]
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", png[16:24]), (item["width"], item["height"]))
            self.assertEqual(hashlib.sha256(png).hexdigest(), item["sha256"])
            self.assertLess(len(png), 300_000)
        self.assertNotIn("source_url", record)
        self.assertNotIn("asset_url", record)
        self.assertEqual(first.private_evidence["sources"][0]["source_updated_at"],
                         "2026-10-09T00:00:00Z")
        self.assertIn("asset_url", first.private_evidence["source_observation"]["sources"][0])
        self.assertEqual(
            first.private_evidence["source_observation"]["sources"][0]["source_updated_at"],
            "2026-10-09T00:00:00Z")

    def test_exact_project_id_separates_style_record_without_duplicate_pixels(self) -> None:
        first = build_project_facade_style(_observation("project-123"))
        second = build_project_facade_style(_observation("project-456"))
        self.assertNotEqual(first.material_record["style_id"], second.material_record["style_id"])
        self.assertEqual(first.textures, second.textures)

    def test_requires_project_scope_reviewed_tokens_and_medium_confidence(self) -> None:
        bad = _observation()
        bad["source_scope"] = "corpus"
        with self.assertRaises(InsufficientStyleEvidence):
            build_project_facade_style(bad)
        bad = _observation()
        bad["visual_review"]["confidence"] = "low"
        with self.assertRaises(InsufficientStyleEvidence):
            build_project_facade_style(bad)
        bad = _observation()
        bad["generator_tokens"]["rhythm"]["windows"] = "not_observed"
        with self.assertRaises(InsufficientStyleEvidence):
            build_project_facade_style(bad)

    def test_accepts_four_reviewed_accents_and_rejects_unbounded_palette(self) -> None:
        observation = _observation()
        observation["generator_tokens"]["palette"]["accents"] = [
            "rust", "ivory", "graphite", "green"]
        bundle = build_project_facade_style(observation)
        self.assertEqual(bundle.private_evidence["generator_tokens"]["palette"]["accents"],
                         ["rust", "ivory", "graphite", "green"])
        observation["generator_tokens"]["palette"]["accents"].append("blue")
        with self.assertRaises(InsufficientStyleEvidence):
            build_project_facade_style(observation)

    def test_frozen_batch_round_trip_pins_private_evidence_and_shared_textures(self) -> None:
        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "batch-v1"
            batch = write_frozen_style_batch(
                [_observation("project-123"), _observation("project-456")], batch_dir)
            self.assertEqual(batch.manifest["styles_count"], 2)
            self.assertEqual(batch.manifest["textures_count"], 3)
            self.assertEqual(len(batch.material_rows), 2)
            self.assertEqual(len(batch.evidence_rows), 2)
            material_bytes = (batch_dir / "materials.jsonl").read_bytes()
            self.assertNotIn(b"https://", material_bytes)
            self.assertIn(b"https://cdn.example.test/render.jpg",
                          (batch_dir / "style-evidence.jsonl").read_bytes())
            loaded = load_frozen_style_batch(batch_dir / "manifest.json")
            self.assertEqual(loaded.manifest, batch.manifest)
            self.assertEqual(loaded.material_rows, batch.material_rows)
            self.assertEqual(loaded.textures, batch.textures)

    def test_loader_rejects_changed_texture_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "batch-v1"
            batch = write_frozen_style_batch([_observation()], batch_dir)
            texture = batch_dir / next(iter(batch.textures))
            texture.write_bytes(texture.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_frozen_style_batch(batch_dir)

    def test_batch_destination_is_immutable(self) -> None:
        with TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "batch-v1"
            write_frozen_style_batch([_observation()], batch_dir)
            with self.assertRaises(FileExistsError):
                write_frozen_style_batch([_observation()], batch_dir)


if __name__ == "__main__":
    unittest.main()
