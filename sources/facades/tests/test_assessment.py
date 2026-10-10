"""Focused offline checks for private facade evidence intake."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from sources.facades.assessment import assess_candidate, assess_jsonl


def _png(width: int = 8, height: int = 5) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    image = zlib.compress((b"\x00" + b"\xff\x00\x00" * width) * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", image) + chunk(b"IEND", b""))


def _glb(*, nonfinite: bool = False, bad_index: bool = False) -> bytes:
    positions = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, math.nan if nonfinite else 1.0, 0.0)
    binary = struct.pack("<9f3H", *positions, 0, 1, 9 if bad_index else 2) + b"\x00\x00"
    document = {
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                        {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
                      {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
    }
    encoded = json.dumps(document, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(encoded) + 8 + len(binary))
            + struct.pack("<I4s", len(encoded), b"JSON") + encoded
            + struct.pack("<I4s", len(binary), b"BIN\x00") + binary)


def _candidate(**changes: object) -> dict:
    row = {
        "candidate_id": "sample-001", "source_url": "https://example.org/building",
        "artifact_url": "https://cdn.example.org/media/sample", "observed_at": "2026-10-10T12:00:00Z",
        "artifact_kind_claim": "unknown", "caption": None,
        "identity": {"match_level": "project", "match_basis": "shared_plot",
                     "registry_id": "estateline:1", "corpus_label": "Корпус 1",
                     "evidence_urls": ["https://example.org/plot"]},
        "rights": {"storage": "unknown", "display": "unknown", "derivatives": "unknown"},
        "geometry": {}, "privacy": {},
    }
    row.update(changes)
    return row


class FacadeAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_bound_local_facade_render_is_still_private_review(self) -> None:
        data = _png()
        (self.root / "render.dat").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        row = _candidate(local_path="render.dat", expected_sha256=digest, expected_bytes=len(data),
                         artifact_kind_claim="facade_image", caption="Архитектура корпуса 1: фасад",
                         visual_review={"kind": "render", "artifact_sha256": digest,
                                        "note": "Manual visual check"})
        result = assess_candidate(row, input_root=self.root)
        self.assertEqual(result["artifact_kind"], "facade_image")
        self.assertEqual(result["artifact"]["visual_kind"], "render")
        self.assertEqual(result["artifact"]["image_pixels"], [8, 5])
        self.assertEqual(result["status"], "review")
        self.assertFalse(result["identity"]["corpus_verified"])
        self.assertIsNone(result["public_model_url"])
        self.assertIn("image_not_metric_mesh", result["reason_codes"])

    def test_mesh_structure_does_not_verify_dimensions_or_rights(self) -> None:
        (self.root / "shape.glb").write_bytes(_glb())
        row = _candidate(local_path="shape.glb", artifact_kind_claim="mesh",
                         caption="3D model of corpus 1",
                         identity={"match_level": "corpus", "match_basis": "official_corpus_id",
                                   "corpus_label": "1", "design_revision": "A",
                                   "evidence_urls": ["https://example.org/corpus"]},
                         rights={"storage": "granted", "display": "granted", "derivatives": "granted",
                                 "evidence_url": "https://example.org/license"},
                         privacy={"external_shell_only": True, "contains_personal_data": False},
                         geometry={"units": "m", "crs": "EPSG:4978", "vertical_datum": "WGS84",
                                   "dimensions_m": [10, 20, 30], "control_lengths_m": [10, 20]})
        result = assess_candidate(row, input_root=self.root)
        self.assertEqual(result["artifact_kind"], "mesh")
        self.assertEqual(result["artifact"]["triangles"], 1)
        self.assertTrue(result["identity"]["corpus_claims_complete"])
        self.assertFalse(result["identity"]["corpus_verified"])
        self.assertTrue(result["rights"]["publication_rights_claims_complete"])
        self.assertFalse(result["rights"]["publication_rights_verified"])
        self.assertTrue(result["geometry"]["metric_claims_complete"])
        self.assertFalse(result["geometry"]["metric_verified"])
        self.assertEqual(result["status"], "review")
        self.assertIn("metric_geometry_not_verified", result["reason_codes"])

    def test_corrupt_mesh_and_path_escape_are_rejected(self) -> None:
        (self.root / "bad.glb").write_bytes(_glb(bad_index=True))
        invalid = assess_candidate(_candidate(local_path="bad.glb", artifact_kind_claim="mesh"),
                                   input_root=self.root)
        self.assertEqual(invalid["status"], "rejected")
        self.assertIn("local_artifact_invalid_or_unsafe", invalid["reason_codes"])
        escaped = assess_candidate(_candidate(local_path="../escape.glb", artifact_kind_claim="mesh"),
                                   input_root=self.root)
        self.assertEqual(escaped["status"], "rejected")

    def test_elevation_and_panorama_require_explicit_label(self) -> None:
        (self.root / "page.bin").write_bytes(b"%PDF-1.4\n%%EOF\n")
        drawing = assess_candidate(_candidate(local_path="page.bin", caption="Фасад в осях 1–5",
                                                artifact_kind_claim="elevation_plan"), input_root=self.root)
        self.assertEqual(drawing["artifact_kind"], "elevation_plan")
        self.assertIn("elevation_scale_not_calibrated", drawing["reason_codes"])
        (self.root / "image.bin").write_bytes(_png())
        general = assess_candidate(_candidate(local_path="image.bin", caption="Галерея ЖК",
                                               artifact_kind_claim="facade_image"), input_root=self.root)
        self.assertEqual(general["artifact_kind"], "unknown")
        panorama = assess_candidate(_candidate(local_path="image.bin", caption="Панорама двора",
                                                artifact_kind_claim="panorama"), input_root=self.root)
        self.assertEqual(panorama["artifact_kind"], "panorama")

    def test_hash_mismatch_and_denied_rights_block_candidate(self) -> None:
        (self.root / "image.bin").write_bytes(_png())
        mismatch = assess_candidate(_candidate(local_path="image.bin", expected_sha256="0" * 64),
                                    input_root=self.root)
        self.assertEqual(mismatch["status"], "rejected")
        self.assertIn("observed_hash_mismatch", mismatch["reason_codes"])
        denied = assess_candidate(_candidate(local_path="image.bin",
                                              rights={"storage": "denied", "display": "unknown",
                                                      "derivatives": "unknown"}), input_root=self.root)
        self.assertEqual(denied["status"], "rejected")

    def test_site_plan_and_design_height_do_not_become_facade_geometry(self) -> None:
        (self.root / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        plan = assess_candidate(_candidate(local_path="report.pdf", artifact_kind_claim="site_plan",
                                            caption="План проведения археологических работ (сети)",
                                            source_assertions=[{"type": "site_plan_scale_denominator", "value": 500,
                                                                "unit": "ratio", "subject": "works area", "source_page": 147}]),
                                input_root=self.root)
        self.assertEqual(plan["artifact_kind"], "site_plan")
        self.assertFalse(plan["geometry"]["metric_verified"])
        self.assertEqual(plan["source_assertions"][0]["status"], "source_assertion_not_mesh_measurement")
        design = assess_candidate(_candidate(local_path="report.pdf", artifact_kind_claim="design_report",
                                              caption="Отчёт по параметрам проекта и котлована",
                                              source_assertions=[{"type": "project_design_max_height", "value": 35.1,
                                                                  "unit": "m", "subject": "whole project", "source_page": 32}]),
                                  input_root=self.root)
        self.assertEqual(design["artifact_kind"], "design_report")
        self.assertEqual(design["source_assertions"][0]["value"], 35.1)
        self.assertFalse(design["geometry"]["metric_verified"])
        self.assertIsNone(design["public_model_url"])

    def test_broad_licensed_photo_does_not_identify_a_corpus(self) -> None:
        payload = _png()
        (self.root / "district-photo.bin").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        result = assess_candidate(_candidate(local_path="district-photo.bin",
                                              caption="Construction in quarters 13 and 15",
                                              rights={"storage": "granted", "display": "granted",
                                                      "derivatives": "granted",
                                                      "evidence_url": "https://example.org/cc0-file"},
                                              visual_review={"kind": "photo", "artifact_sha256": digest}),
                                  input_root=self.root)
        self.assertEqual(result["artifact_kind"], "context_photo")
        self.assertTrue(result["rights"]["publication_rights_claims_complete"])
        self.assertFalse(result["rights"]["publication_rights_verified"])
        self.assertFalse(result["identity"]["corpus_verified"])
        self.assertEqual(result["status"], "review")

    def test_immutable_run_reuses_identical_bytes_and_rejects_duplicates(self) -> None:
        input_file = self.root / "leads.jsonl"
        input_file.write_text(json.dumps(_candidate()) + "\n", encoding="utf-8")
        output = self.root / "runs"
        first = assess_jsonl(input_file, output, input_root=self.root)
        second = assess_jsonl(input_file, output, input_root=self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["public_assets_created"], 0)
        self.assertEqual(len(list(output.iterdir())), 1)
        input_file.write_text((json.dumps(_candidate()) + "\n") * 2, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
            assess_jsonl(input_file, output, input_root=self.root)


if __name__ == "__main__":
    unittest.main()
