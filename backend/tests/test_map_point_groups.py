"""Fail-closed validation for source-backed map complex-group sidecars."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import backend.map_point_groups as groups


RUN_ID = "models-v1-0123456789abcdef"
SNAPSHOT_ID = "building-registry-synthetic"
POINT_A = "mappt_" + "a" * 24
POINT_B = "mappt_" + "b" * 24
DATASET = "osm-moscow-pinned"
SOURCE_SNAPSHOT = "osm-snapshot-2026-10-08"
SOURCE_RELATION = "relation/19782953/v2"
IDENTITY_KEY = f"{DATASET}:{SOURCE_RELATION}"
GROUP_ID = "group_" + hashlib.sha256(
    f"explicit_relation_membership\0{IDENTITY_KEY}".encode("utf-8")).hexdigest()[:24]


def _document() -> dict:
    return {
        "schema_version": groups.SIDECAR_SCHEMA,
        "registry_snapshot_id": SNAPSHOT_ID,
        "model_run_id": RUN_ID,
        "groups": [{
            "identity_key": IDENTITY_KEY,
            "group_scope": "residential_complex",
            "identity_basis": "explicit_relation_membership",
            "group_label": "ЖК «Проверенный пример»",
            "point_ids": [POINT_A, POINT_B],
            "public_display": True,
            "members": [
                {"point_id": POINT_A, "source_dataset_id": DATASET,
                 "source_snapshot_id": SOURCE_SNAPSHOT,
                 "source_record_id": "way/1450746348/v3"},
                {"point_id": POINT_B, "source_dataset_id": DATASET,
                 "source_snapshot_id": SOURCE_SNAPSHOT,
                 "source_record_id": "relation/19782953/v2"},
            ],
            "proof": {"proof_kind": "explicit_relation_membership",
                      "source_dataset_id": DATASET,
                      "source_snapshot_id": SOURCE_SNAPSHOT,
                      "source_record_id": SOURCE_RELATION},
        }],
    }


def _write(root: Path, document: dict) -> None:
    run_dir = root / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    sidecar = run_dir / groups.SIDECAR_NAME
    sidecar.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    sidecar.chmod(0o600)


@pytest.fixture
def sidecar_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(groups, "MODELS_ROOT", tmp_path)
    (tmp_path / RUN_ID).mkdir()
    return tmp_path


def test_missing_sidecar_is_valid_empty_evidence(sidecar_root: Path) -> None:
    result = groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})
    assert result == {
        "schema_version": 1, "visibility": "public",
        "registry_snapshot_id": SNAPSHOT_ID, "model_run_id": RUN_ID, "groups": [],
    }


def test_verified_group_projects_only_public_fields(sidecar_root: Path) -> None:
    _write(sidecar_root, _document())
    result = groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, [POINT_A, POINT_B])
    assert result["groups"] == [{
        "group_id": GROUP_ID,
        "group_scope": "residential_complex",
        "identity_basis": "explicit_relation_membership",
        "group_label": "ЖК «Проверенный пример»",
        "point_ids": [POINT_A, POINT_B],
    }]
    serialized = json.dumps(result, ensure_ascii=False)
    assert set(result["groups"][0]) == {
        "group_id", "group_scope", "identity_basis", "group_label", "point_ids",
    }
    assert '"proof"' not in serialized and '"source_record_id"' not in serialized
    assert '"source_snapshot_id"' not in serialized
    assert DATASET not in serialized and SOURCE_RELATION not in serialized


@pytest.mark.parametrize("mutate", [
    lambda doc: doc.update(model_run_id="models-v1-stale"),
    lambda doc: doc.update(registry_snapshot_id="other-snapshot"),
    lambda doc: doc["groups"][0].update(public_display=False),
    lambda doc: doc["groups"][0].update(identity_key="name-derived-id"),
    lambda doc: doc["groups"][0].update(members=[doc["groups"][0]["members"][0]]),
    lambda doc: doc["groups"].append(doc["groups"][0].copy()),
])
def test_stale_or_unsafe_sidecar_fails_closed(
        sidecar_root: Path, mutate) -> None:
    document = _document()
    mutate(document)
    _write(sidecar_root, document)
    with pytest.raises(groups.GroupEvidenceError):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})


def test_overlapping_membership_is_rejected(sidecar_root: Path) -> None:
    document = _document()
    second = _document()["groups"][0]
    second["identity_key"] = f"{DATASET}:relation/999/v1"
    second["proof"]["source_record_id"] = "relation/999/v1"
    document["groups"].append(second)
    _write(sidecar_root, document)
    with pytest.raises(groups.GroupEvidenceError, match="multiple groups"):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})


def test_point_removed_from_public_visibility_keeps_group_ineligible(
        sidecar_root: Path) -> None:
    _write(sidecar_root, _document())
    result = groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A})
    assert result["groups"] == []


def test_crosswalk_requires_multiple_source_datasets(sidecar_root: Path) -> None:
    document = _document()
    group = document["groups"][0]
    group["identity_basis"] = "verified_crosswalk"
    group["proof"]["proof_kind"] = "verified_crosswalk"
    _write(sidecar_root, document)
    with pytest.raises(groups.GroupEvidenceError, match="two source identities"):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})


def test_symlink_sidecar_is_rejected(sidecar_root: Path, tmp_path: Path) -> None:
    run_dir = sidecar_root / RUN_ID
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(_document()), encoding="utf-8")
    (run_dir / groups.SIDECAR_NAME).symlink_to(target)
    with pytest.raises(groups.GroupEvidenceError):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})


def test_duplicate_json_fields_rejected_and_read_only_package_mode_allowed(
        sidecar_root: Path) -> None:
    run_dir = sidecar_root / RUN_ID
    path = run_dir / groups.SIDECAR_NAME
    path.write_text('{"schema_version":"bad","schema_version":"bad"}',
                    encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(groups.GroupEvidenceError, match="Duplicate"):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})
    _write(sidecar_root, _document())
    path.chmod(0o644)
    assert len(groups.load_group_evidence(SNAPSHOT_ID, RUN_ID,
                                         {POINT_A, POINT_B})["groups"]) == 1
    path.chmod(0o664)
    with pytest.raises(groups.GroupEvidenceError, match="sidecar file"):
        groups.load_group_evidence(SNAPSHOT_ID, RUN_ID, {POINT_A, POINT_B})
