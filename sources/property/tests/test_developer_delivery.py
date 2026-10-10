"""Private developer delivery gates on synthetic harvest snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sources.property.developer_delivery import build_delivery
from sources.property.developer_harvest import canonical_json, digest


def _write_run(root: Path, provider: str, rows: list[dict], *, pending: int = 0) -> Path:
    directory = root / provider
    directory.mkdir()
    content = b"".join(canonical_json(row) + b"\n" for row in rows)
    (directory / "observations.jsonl").write_bytes(content)
    manifest = {
        "schema_version": "developer-harvest-run/1",
        "run_id": "developer-harvest-" + provider,
        "providers": {provider: {
            "catalog_pages_remaining": pending, "project_pages_remaining": 0,
            "pages": {"fetched": 2}, "blocked_hosts": [],
        }},
        "files": {"observations": {
            "path": "observations.jsonl", "sha256": digest(content),
            "records": len(rows),
        }},
    }
    (directory / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    return directory


def _row(provider: str, suffix: str, kind: str, *, scope: str = "unknown",
         project: str | None = None, building: str | None = None,
         district: str | None = None, address: str | None = None) -> dict:
    fields = {"location_scope": scope}
    if district:
        fields["aggregator_district"] = district
    return {
        "observation_id": provider + "-" + suffix,
        "provider": provider, "record_type": kind,
        "source_url": f"https://{provider}.example/offer/{suffix}",
        "source_page_url": f"https://{provider}.example/catalog",
        "publisher_domain": f"{provider}.example",
        "publisher_project_id": project,
        "publisher_building_id": building,
        "publisher_unit_id": suffix if kind == "unit" else None,
        "project_name": "Example", "address": address,
        "source_fields": fields,
        "identity_quality": "publisher_asserted",
        "observed_at": "2026-10-10T12:00:00+00:00",
        "source_updated_at": None,
        "raw_sha256": "a" * 64,
        "media": [{
            "url": "https://images.example/plan.png", "role": "apartment_plan",
            "scope": "unit", "source_field": "plan", "rights_status": "unknown",
        }] if kind == "unit" else [],
        "price_rub": 12000000 if kind == "unit" else None,
        "price_basis": "asking_total" if kind == "unit" else "unknown",
    }


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_delivery_partitions_exact_links_and_private_media(tmp_path: Path) -> None:
    rows_a = [
        _row("alpha", "project", "project", scope="in_scope_moscow",
             project="P", address="Москва, улица Тестовая"),
        _row("alpha", "corpus", "corpus", scope="in_scope_moscow",
             project="P", building="B"),
        _row("alpha", "u1", "unit", project="P", building="B"),
        _row("alpha", "u2", "unit", scope="out_of_scope", project="Q",
             district="ГО Химки, МО"),
    ]
    rows_b = [_row("beta", "u3", "unit", scope="out_of_scope",
                   district="Калужская область")]
    runs = [_write_run(tmp_path, "alpha", rows_a), _write_run(tmp_path, "beta", rows_b)]
    manifest = build_delivery(runs, tmp_path / "deliveries")
    directory = tmp_path / "deliveries" / manifest["delivery_id"]
    assert manifest["unit_partitions"] == {
        "moscow": 1, "moscow_oblast": 1, "other_region": 1,
        "outside_moscow_unresolved": 0, "unknown": 0,
        "geometry_conflict_quarantine": 0,
    }
    moscow = _lines(directory / "units_moscow.jsonl")[0]
    assert moscow["scope_evidence_observation_id"] == "alpha-project"
    assert moscow["project"]["address"] == "Москва, улица Тестовая"
    relationships = _lines(directory / "relationships.jsonl")
    assert any(row["source_observation_id"] == "alpha-u1"
               and row["relationship"] == "publisher_building_id"
               and row["target_observation_ids"] == ["alpha-corpus"]
               for row in relationships)
    media = _lines(directory / "media_refs.jsonl")
    assert len(media) == 3
    assert all(row["public_display_allowed"] is False
               and row["rights_status"] == "unknown" for row in media)
    assert build_delivery(runs, tmp_path / "deliveries") == manifest


def test_delivery_rejects_incomplete_or_tampered_source(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "alpha", [_row("alpha", "u1", "unit")], pending=1)
    with pytest.raises(ValueError, match="still has catalog"):
        build_delivery([run], tmp_path / "out")
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["providers"]["alpha"]["catalog_pages_remaining"] = 0
    (run / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    (run / "observations.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_delivery([run], tmp_path / "out")


def test_geometry_axis_is_separate_and_conflicts_are_quarantined(tmp_path: Path) -> None:
    pytest.importorskip("shapely")
    project = _row("alpha", "project", "project", scope="out_of_scope", project="P",
                   district="ГО Химки, МО")
    project["coordinates"] = {"longitude": 37.5, "latitude": 55.7}
    unit = _row("alpha", "u1", "unit", scope="out_of_scope", project="P",
                district="ГО Химки, МО")
    run = _write_run(tmp_path, "alpha", [project, unit])
    geojson = tmp_path / "boundary.geojson"
    boundary = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [37, 55], [38, 55], [38, 56], [37, 56], [37, 55],
        ]]},
    }]}
    geojson.write_bytes(canonical_json(boundary))
    boundary_manifest = tmp_path / "boundary.manifest.json"
    boundary_manifest.write_bytes(canonical_json({
        "relation_id": 102269, "output_file": geojson.name,
        "output_sha256": digest(geojson.read_bytes()),
    }))
    manifest = build_delivery([run], tmp_path / "out", geojson, boundary_manifest)
    assert manifest["unit_partitions"]["geometry_conflict_quarantine"] == 1
    row = _lines(tmp_path / "out" / manifest["delivery_id"]
                 / "units_geometry_conflict_quarantine.jsonl")[0]
    assert row["location_scope"] == "out_of_scope"
    assert row["geometry_inside_moscow"] == "inside"
    assert row["location_geometry_conflict"] is True
    assert row["geometry_evidence_observation_id"] == "alpha-project"


def test_spatial_candidate_keeps_unknown_source_scope_and_pinned_boundary(tmp_path: Path) -> None:
    pytest.importorskip("shapely")
    project = _row("alpha", "project", "project", project="P")
    project["coordinates"] = {"longitude": 37.5, "latitude": 55.7}
    unit = _row("alpha", "u1", "unit", project="P")
    run = _write_run(tmp_path, "alpha", [project, unit])
    boundary = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [37, 55], [38, 55], [38, 56], [37, 56], [37, 55],
        ]]},
    }]}
    geojson = tmp_path / "boundary.geojson"
    geojson.write_bytes(canonical_json(boundary))
    source_sha = digest(geojson.read_bytes())
    boundary_manifest = tmp_path / "boundary.manifest.json"
    boundary_manifest.write_bytes(canonical_json({
        "relation_id": 102269, "output_file": geojson.name,
        "output_sha256": source_sha,
    }))
    manifest = build_delivery([run], tmp_path / "out", geojson, boundary_manifest)
    destination = tmp_path / "out" / manifest["delivery_id"]
    assert manifest["unit_partitions"]["unknown"] == 1
    assert manifest["unit_partitions"]["moscow"] == 0
    assert manifest["spatial_candidates_overlap_unknown_partition"] == 1
    spatial = _lines(destination / "units_moscow_spatial_candidates.jsonl")[0]
    assert spatial["source_location_scope"] == "unknown"
    assert spatial["address_verified_by_spatial_test"] is False
    assert spatial["moscow_boundary_sha256"] == source_sha
    assert spatial["spatial_selection_basis"] == "source_project_point_unverified"
