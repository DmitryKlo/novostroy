"""Flat private tables preserve scope and prevent spreadsheet formula execution."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sources.property.developer_delivery_csv import UNIT_PARTITIONS, export_tables
from sources.property.developer_harvest import canonical_json, digest


def _fixture(root: Path) -> Path:
    root.mkdir()
    unit = {
        "source_group": "example", "region_partition": "unknown",
        "location_scope": "unknown", "project": {"name": "=HYPERLINK(1)",
                                                   "publisher_project_id": "P"},
        "building": {}, "unit": {"publisher_unit_id": "U", "rooms": "Студия"},
        "price_rub": 12000000, "price_basis": "asking_total",
        "source_url": "https://example.test/u", "public_display_allowed": False,
    }
    project = {"source_group": "example", "record_type": "project",
               "project_name": "Safe", "coordinates": {"longitude": 37.5,
                                                       "latitude": 55.7}}
    media = {"provider": "example", "role": "apartment_plan", "scope": "unit",
             "url": "https://example.test/plan.png", "public_display_allowed": False}
    files = {}
    rows = {**{key: [unit] if key == "units_unknown" else [] for key in UNIT_PARTITIONS},
            "units_moscow_spatial_candidates": [],
            "projects_corpora": [project], "media_refs": [media]}
    for key, contents in rows.items():
        raw = b"".join(canonical_json(row) + b"\n" for row in contents)
        (root / (key + ".jsonl")).write_bytes(raw)
        files[key] = {"path": key + ".jsonl", "sha256": digest(raw),
                      "records": len(contents)}
    (root / "manifest.json").write_bytes(canonical_json({
        "schema_version": "developer-delivery/1", "delivery_id": "fixture",
        "files": files,
    }) + b"\n")
    return root


def test_csv_tables_are_idempotent_private_and_excel_safe(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "delivery")
    manifest = export_tables(source, tmp_path / "exports")
    output = tmp_path / "exports" / ("developer-csv-" + manifest["delivery_manifest_sha256"][:16])
    assert manifest["private_only"] is True
    assert manifest["cross_provider_physical_unit_dedup_claimed"] is False
    assert {key: row["records"] for key, row in manifest["files"].items()} == {
        "units": 1, "moscow_spatial_candidates": 0,
        "projects_corpora": 1, "media_refs": 1}
    raw = (output / "units.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    with (output / "units.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream, delimiter=";"))
    assert row["project_name"] == "'=HYPERLINK(1)"
    assert row["location_scope"] == "unknown"
    assert export_tables(source, tmp_path / "exports") == manifest
    assert json.loads((output / "manifest.json").read_text()) == manifest


def test_csv_rejects_modified_delivery_file(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "delivery")
    (source / "units_unknown.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash/count mismatch"):
        export_tables(source, tmp_path / "exports")
