"""The publisher must distinguish an actual building from a project marker."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sources.property.developer_map import (_apply_reviewed_crosswalk, build, publish,
                                            rollback_legacy, validate_run)


def _write(path: Path, rows: list[dict]) -> dict:
    payload = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True).encode() + b"\n"
                       for row in rows)
    path.write_bytes(payload)
    return {"path": path.name, "records": len(rows),
            "sha256": hashlib.sha256(payload).hexdigest()}


def _polygon(west: float) -> dict:
    return {"type": "Polygon", "coordinates": [[[west, 55.5], [west + .001, 55.5],
            [west + .001, 55.501], [west, 55.501], [west, 55.5]]]}


def _unit(source: str, project: str, building: str | None, unit: str,
          *, scope: str = "in_scope_moscow", corpus: str = "1") -> dict:
    return {"source_group": source, "location_scope": scope,
            "observed_at": "2026-10-10T12:00:00Z", "observation_id": "obs-" + unit,
            "project": {"publisher_project_id": project, "name": "Тестовый ЖК",
                        "address": "Москва, ул. Тестовая, д. 1"},
            "building": {"publisher_building_id": building, "corpus": corpus,
                         "full_address": None},
            "unit": {"publisher_unit_id": unit}}


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "delivery"
    source.mkdir()
    boundary = tmp_path / "boundary.geojson"
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon",
         "coordinates": [[[37, 55], [38, 55], [38, 56], [37, 56], [37, 55]]]}}]}))
    boundary_sha = hashlib.sha256(boundary.read_bytes()).hexdigest()
    boundary_manifest = tmp_path / "boundary.manifest.json"
    boundary_manifest.write_text(json.dumps({"output_file": boundary.name,
                                             "output_sha256": boundary_sha,
                                             "relation_id": 102269}))
    projects = [{"source_group": "proplanirovki", "record_type": "project",
                 "publisher_project_id": "P", "project_name": "Тестовый ЖК",
                 "address": "Москва, ул. Тестовая, д. 1",
                 "coordinates": {"longitude": 37.9, "latitude": 55.9}},
                *[{"source_group": "proplanirovki", "record_type": "corpus",
                   "publisher_project_id": "P", "publisher_building_id": label,
                   "corpus": label, "address": "ул. Тестовая",
                   "source_geometry": _polygon(37.1 if label == "A" else 37.2),
                   "geometry_basis": "aggregator_unverified",
                   "observation_id": "corpus-" + label} for label in ("A", "B")],
                {"source_group": "fsk", "record_type": "project",
                 "publisher_project_id": "Q", "project_name": "Тестовый ЖК",
                 "address": "Москва, ул. Тестовая, д. 1"}]
    files = {"projects_corpora": _write(source / "projects_corpora.jsonl", projects)}
    partitions = {"units_moscow": [
        _unit("proplanirovki", "P", "A", "1", corpus="A"),
        _unit("proplanirovki", "P", "B", "2", corpus="B"),
        _unit("fsk", "Q", "F1", "3"),
        _unit("a101", "R", None, "4"),
        _unit("a101", "R", "A101-ID", "6"),
        _unit("proplanirovki", "P", "A", "5", scope="out_of_scope", corpus="A")],
        "units_unknown": [], "units_moscow_oblast": [], "units_other_region": [],
        "units_outside_moscow_unresolved": []}
    for name, rows in partitions.items():
        files[name] = _write(source / f"{name}.jsonl", rows)
    manifest = source / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": "developer-delivery/1", "files": files,
                                    "moscow_boundary_sha256": boundary_sha}))
    return manifest, boundary, boundary_manifest


def test_build_is_immutable_and_never_uses_project_marker(tmp_path: Path) -> None:
    delivery, boundary, boundary_manifest = _fixture(tmp_path)
    output = tmp_path / "publisher"
    manifest = build(delivery, output, boundary, boundary_manifest)
    assert manifest["buildings_total"] == 3  # proplan polygon, FSK ID, A101 project+corpus
    assert manifest["map_points_total"] == 1
    assert manifest["coverage"]["proplanirovki"]["excluded_unit_ids"] == 2
    directory = output / "runs" / manifest["run_id"]
    points = [json.loads(line) for line in (directory / "points.jsonl").read_text().splitlines()]
    by_source = {point["source_group"]: point for point in points}
    assert by_source["proplanirovki"]["location"][0] < 37.3
    assert by_source["proplanirovki"]["location"] != [37.9, 55.9]
    assert by_source["fsk"]["location"] is None
    assert by_source["fsk"]["address_complete"] is False
    assert by_source["a101"]["location"] is None
    assert by_source["a101"]["identity_basis"] == "publisher_building_id"
    assert by_source["a101"]["unit_count"] == 2
    before = (directory / "manifest.json").stat().st_mtime_ns
    assert build(delivery, output, boundary, boundary_manifest) == manifest
    assert (directory / "manifest.json").stat().st_mtime_ns == before
    assert validate_run(output, manifest["run_id"]) == manifest


def test_pointer_is_atomic_and_tampered_run_refused(tmp_path: Path) -> None:
    delivery, boundary, boundary_manifest = _fixture(tmp_path)
    output = tmp_path / "publisher"
    manifest = build(delivery, output, boundary, boundary_manifest)
    pointer = publish(output, manifest["run_id"])
    assert pointer["schema_version"] == "developer-map-pointer/1"
    assert json.loads((output / "active.json").read_text()) == pointer
    with pytest.raises(ValueError, match="changed"):
        publish(output, manifest["run_id"], expected_active_sha="0" * 64)
    run = output / "runs" / manifest["run_id"]
    (run / "points.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_run(output, manifest["run_id"])


def test_cas_rollback_keeps_immutable_run(tmp_path: Path) -> None:
    delivery, boundary, boundary_manifest = _fixture(tmp_path)
    output = tmp_path / "publisher"
    manifest = build(delivery, output, boundary, boundary_manifest)
    publish(output, manifest["run_id"])
    pointer = output / "active.json"
    pointer_sha = hashlib.sha256(pointer.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="changed"):
        rollback_legacy(output, expected_active_sha="0" * 64)
    assert pointer.exists()
    result = rollback_legacy(output, expected_active_sha=pointer_sha)
    assert result["retained_run_id"] == manifest["run_id"]
    assert not pointer.exists()
    assert validate_run(output, manifest["run_id"]) == manifest
    assert (output / "pointer_history" / f"{pointer_sha}.json").exists()
    publish(output, manifest["run_id"],
            expected_active_sha=hashlib.sha256(b"").hexdigest())
    assert pointer.exists()


def test_afi_cards_are_audited_without_inventing_buildings(tmp_path: Path) -> None:
    delivery, boundary, boundary_manifest = _fixture(tmp_path)
    source = tmp_path / "afi"
    source.mkdir()
    card = {"unit_source_id": "A-1", "project_source_path": "/project/p",
            "source_body_sha256": "a" * 64}
    _write(source / "unit_cards.jsonl", [card])
    afi_manifest = source / "manifest.json"
    afi_manifest.write_text(json.dumps({"schema_version": "afi-homepage-card-capture/1",
        "artifacts": {"unit_cards": "unit_cards.jsonl"},
        "source_body_sha256": "a" * 64, "counts": {"product_cards": 1}}))
    output = tmp_path / "publisher"
    result = build(delivery, output, boundary, boundary_manifest,
                   afi_manifest=afi_manifest)
    assert result["coverage"]["afi"]["excluded_unit_ids"] == 1
    assert result["buildings_total"] == 3
    card["source_body_sha256"] = "b" * 64
    _write(source / "unit_cards.jsonl", [card])
    with pytest.raises(ValueError, match="source hash mismatch"):
        build(delivery, output, boundary, boundary_manifest, afi_manifest=afi_manifest)


def test_exact_building_geocode_affects_only_its_publisher_id(tmp_path: Path) -> None:
    delivery, boundary, boundary_manifest = _fixture(tmp_path)
    evidence = tmp_path / "evidence"
    (evidence / "geometry").mkdir(parents=True)
    ring = [[37.3, 55.5], [37.301, 55.5], [37.301, 55.501],
            [37.3, 55.501], [37.3, 55.5]]
    snapshot = {"source_response_sha256": "b" * 64, "osm_object": {
        "osm_type": "way", "osm_id": 42, "osm_version": 2,
        "closed_ring_nodes": [{"longitude": x, "latitude": y} for x, y in ring]}}
    snapshot_path = evidence / "geometry" / "snap.json"
    snapshot_path.write_text(json.dumps(snapshot))
    row = {"schema_version": "geocode-evidence/1", "status": "verified_point",
           "source_group": "fsk", "publisher_project_id": "Q",
           "publisher_building_id": "F1", "target_ids": ["geocode:fsk:F1"],
           "point_type": "building_footprint_centroid",
           "coordinates": {"crs": "EPSG:4326", "order": "lon_lat",
                           "longitude": 37.3005, "latitude": 55.5005},
           "resolved_buildings": [{"publisher_project_id": "Q", "publisher_building_id": "F1",
                                   "point_type": "building_footprint_centroid",
                                   "coordinates": [37.3005, 55.5005]}],
           "pages_consulted": {"count": 2, "urls": ["https://example.test/a",
                                                     "https://example.test/b"]},
           "evidence": [{"exact_address_match": True}, {"exact_address_match": True}],
           "local_crosswalk": {"index_entry": {"osm_id": 42, "candidate_count": 1,
                                                   "ambiguous_index_match": False}},
           "osm_id": 42, "osm_version": 2,
           "normalized_address": "Москва, ул. Тестовая, д. 1"}
    result_path = evidence / "result.jsonl"
    result = _write(result_path, [row])
    manifest_path = evidence / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": "geocode-evidence-manifest/1",
        "record_count": 1, "result_path": "result.jsonl", "result_sha256": result["sha256"],
        "parent_delivery": {"manifest_sha256": hashlib.sha256(delivery.read_bytes()).hexdigest()},
        "source_capture": {"retained_derived_snapshot": "geometry/snap.json",
                           "derived_snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                           "response_sha256": "b" * 64}}))
    output = tmp_path / "publisher"
    manifest = build(delivery, output, boundary, boundary_manifest,
                     geocode_manifests=(manifest_path,))
    assert manifest["map_points_total"] == 2
    points = [json.loads(line) for line in
              (output / "runs" / manifest["run_id"] / "points.jsonl").read_text().splitlines()]
    fsk = next(point for point in points if point["source_group"] == "fsk")
    a101 = next(point for point in points if point["source_group"] == "a101")
    assert fsk["location"] == [37.3005, 55.5005]
    assert fsk["address_complete"] is True
    assert a101["location"] is None
    snapshot_path.write_text("{}")
    with pytest.raises(ValueError, match="snapshot SHA mismatch"):
        build(delivery, output, boundary, boundary_manifest,
              geocode_manifests=(manifest_path,))


def test_reviewed_crosswalk_merges_only_unique_overlapping_corpus(tmp_path: Path) -> None:
    evidence = tmp_path / "crosswalk"
    evidence.mkdir()
    row = {"schema_version": "developer-map-crosswalk/1",
           "decision": "same_physical_building", "official_single_corpus": True,
           "fsk_project_id": "Q", "fsk_building_id": "F1",
           "proplan_project_id": "P", "proplan_building_id": "A",
           "osm_id": 42, "osm_version": 2}
    entry = _write(evidence / "crosswalks.jsonl", [row])
    ring = _polygon(37.1)["coordinates"][0]
    snapshot = {"osm_object": {"osm_type": "way", "osm_id": 42, "osm_version": 2,
        "closed_ring_nodes": [{"longitude": x, "latitude": y} for x, y in ring]}}
    snapshot_path = evidence / "osm-footprint.json"
    snapshot_path.write_text(json.dumps(snapshot))
    manifest_path = evidence / "manifest.json"
    manifest_path.write_text(json.dumps({"schema_version": "developer-map-crosswalk-manifest/1",
        "source_delivery_manifest_sha256": "a" * 64,
        "geocode_manifest_sha256": "b" * 64,
        "records_total": 1, "crosswalks_sha256": entry["sha256"],
        "osm_footprint_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest()}))
    fsk = {"source_group": "fsk", "publisher_project_id": "Q", "publisher_building_id": "F1",
           "source_corpus_label": "1", "project_name": "Клубный дом Лэйк",
           "address": "Москва, улица Снежная, 20А", "address_complete": True,
           "location_basis": "exact_address_osm_building_footprint_centroid",
           "map_eligible": True, "unit_count": 3, "source_evidence_sha256": "c" * 64}
    pro = {"source_group": "proplanirovki", "publisher_project_id": "P",
           "publisher_building_id": "A", "source_corpus_label": "1", "project_name": "Лэйк",
           "address": "ул Снежная", "map_eligible": True, "unit_count": 4,
           "source_evidence_sha256": "d" * 64}
    points = [fsk, pro]
    corpus = {("proplanirovki", "P", "id:A"): {"source_geometry": _polygon(37.1)}}
    proof = _apply_reviewed_crosswalk(points, corpus, manifest_path, "a" * 64,
                                      [{"manifest_sha256": "b" * 64}])
    assert proof["role"] == "reviewed_crosswalk"
    assert points == [fsk]
    assert fsk["unit_count"] == 3
    assert fsk["source_observed_unit_counts"] == {"fsk": 3, "proplanirovki": 4}
    other = {("proplanirovki", "X", "id:B"): {"source_geometry": _polygon(37.1)}}
    with pytest.raises(ValueError, match="intersects other"):
        _apply_reviewed_crosswalk([fsk.copy(), pro.copy()], {**corpus, **other},
                                  manifest_path, "a" * 64,
                                  [{"manifest_sha256": "b" * 64}])
