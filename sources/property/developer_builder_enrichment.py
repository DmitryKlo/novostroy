"""Add source-qualified builder display labels to an immutable enrichment run.

Only exact publisher project IDs are joined. A catalog builder label remains an
aggregator assertion, never a verified legal developer identity. No active
pointer is changed here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from backend import developer_map as map_reader
from backend import developer_models as model_reader
from backend import developer_units as unit_reader
from backend import developer_enrichment as enrichment_reader
from sources.property.developer_map import (
    ROOT, _canonical, _file_sha, _fsync_dir, _rows, _sha, _write_private,
)


OUTPUT = ROOT / "data/property/developer_enrichment"
RULES = "builder-display-assertions-v1"


def _evidence(manifest_path: Path, rows_path: Path,
              map_run: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str, str]:
    if manifest_path.is_symlink() or rows_path.is_symlink():
        raise ValueError("Builder evidence symlink is forbidden")
    source_manifest_sha = _file_sha(manifest_path)
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows_sha = _file_sha(rows_path)
    descriptor = source_manifest.get("outputs", {}).get("builder_assertions.jsonl")
    if (not isinstance(descriptor, dict) or descriptor.get("sha256") != rows_sha or
            descriptor.get("records") != 475 or descriptor.get("bytes") != rows_path.stat().st_size):
        raise ValueError("Builder manifest does not pin assertion rows")
    by_project: dict[str, dict[str, Any]] = {}
    for row in _rows(rows_path):
        project_id = row.get("publisher_project_id")
        if (row.get("schema_version") != "developer-builder-assertion/1" or
                row.get("publisher_role") != "aggregator" or
                row.get("builder_assertion_status") != "aggregator_asserted_unverified" or
                row.get("builder_assertion_basis") != "catalog.items[].builder" or
                not isinstance(project_id, str) or not project_id or project_id in by_project or
                not isinstance(row.get("builder_assertion"), str) or
                not row["builder_assertion"].strip() or row.get("legal_entity_verified") is not False):
            raise ValueError("Builder source assertion is invalid or duplicated")
        by_project[project_id] = row
    if len(by_project) != 475:
        raise ValueError("Pinned builder audit does not contain all catalog projects")
    for project_id, assertion in by_project.items():
        actual = sorted(registry_id for registry_id, point in map_run["by_registry"].items()
                        if point.get("source_group") == "proplanirovki" and
                        point.get("publisher_project_id") == project_id)
        if actual != sorted(assertion.get("active_map_registry_ids_exact_project_id") or []):
            raise ValueError("Builder project membership differs from exact map IDs")
    return by_project, rows_sha, source_manifest_sha


def _official_overrides(manifest_path: Path | None, rows_path: Path | None,
                        map_run: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    if (manifest_path is None) != (rows_path is None):
        raise ValueError("Official brand rows and manifest must be supplied together")
    if manifest_path is None or rows_path is None:
        return {}, None, None
    if manifest_path.is_symlink() or rows_path.is_symlink():
        raise ValueError("Official brand evidence symlink is forbidden")
    manifest_sha, rows_sha = _file_sha(manifest_path), _file_sha(rows_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "developer-brand-overrides-manifest/1" or
            manifest.get("output", {}).get("sha256") != rows_sha or
            manifest.get("output", {}).get("records") != 3 or
            manifest.get("inputs", {}).get("active_map_run_id") !=
            map_run["manifest"]["run_id"] or
            manifest.get("inputs", {}).get("active_map_points_sha256") !=
            map_run["manifest"]["points_sha256"]):
        raise ValueError("Official brand overrides do not pin active exact map")
    overrides: dict[str, dict[str, Any]] = {}
    for row in _rows(rows_path):
        registry_id = row.get("target_registry_id")
        point = map_run["by_registry"].get(registry_id)
        identity = row.get("target_identity") or {}
        if (point is None or registry_id in overrides or
                row.get("schema_version") != "developer-brand-override/1" or
                row.get("developer_brand_status") != "confirmed_official" or
                row.get("legal_entity") is not None or
                not isinstance(row.get("developer_brand"), str) or
                not row["developer_brand"].strip() or
                any(identity.get(key) != point.get(key) for key in
                    ("source_group", "publisher_project_id", "publisher_building_id"))):
            raise ValueError("Official brand override is not bound to one exact corpus")
        overrides[registry_id] = row
    if len(overrides) != 3:
        raise ValueError("Pinned official brand override count changed")
    return overrides, rows_sha, manifest_sha


def project_builder(point: dict[str, Any],
                    assertions: dict[str, dict[str, Any]],
                    override: dict[str, Any] | None = None) -> dict[str, Any]:
    if override is not None:
        return {"developer_display_name": override["developer_brand"].strip(),
                "developer_display_basis": "official_project_brand",
                "developer_display_evidence_sha256": _sha(_canonical(override)),
                "developer_legal_name": None}
    source = point.get("source_group")
    if source == "proplanirovki":
        assertion = assertions.get(point.get("publisher_project_id"))
        if assertion is None:
            raise ValueError("Mapped Proplanirovki corpus has no catalog builder assertion")
        return {"developer_display_name": assertion["builder_assertion"].strip(),
                "developer_display_basis": "aggregator_asserted_unverified",
                "developer_display_evidence_sha256": assertion["catalog_response_sha256"],
                "developer_legal_name": None}
    if source in {"a101", "fsk"}:
        return {"developer_display_name": "А101" if source == "a101" else "ФСК",
                "developer_display_basis": "first_party_source_brand",
                "developer_display_evidence_sha256": point["source_evidence_sha256"],
                "developer_legal_name": None}
    return {"developer_display_name": None, "developer_display_basis": None,
            "developer_display_evidence_sha256": None,
            "developer_legal_name": None}


def build(base_run_id: str, builder_manifest: Path, builder_rows: Path,
          map_run_id: str, unit_run_id: str, model_run_id: str,
          official_manifest: Path | None = None, official_rows: Path | None = None,
          output_root: Path = OUTPUT) -> dict[str, Any]:
    map_run = map_reader.load_staged(map_run_id)
    unit_run = unit_reader.load_staged(unit_run_id, map_run)
    model_run = model_reader.load_staged(model_run_id, map_run)
    base_manifest_path = enrichment_reader.BASE / "runs" / base_run_id / "manifest.json"
    base_sha = _file_sha(base_manifest_path)
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    base_model_run = model_reader.load_staged(base_manifest["model_run_id"], map_run)
    if set(model_run["by_registry"]) != set(base_model_run["by_registry"]):
        raise ValueError("Styled model run changes building coverage")
    for registry_id, old in base_model_run["by_registry"].items():
        new = model_run["by_registry"][registry_id]
        for field in ("height_m", "height_basis", "model_transform",
                      "source_geometry_basis", "source_geometry_evidence_sha256"):
            if old.get(field) != new.get(field):
                raise ValueError("Styled model run changes source geometry or height")
    base = enrichment_reader.load_staged(base_run_id, map_run, unit_run, base_model_run,
                                         expected_manifest_sha256=base_sha)
    assertions, rows_sha, assertion_manifest_sha = _evidence(
        builder_manifest, builder_rows, map_run)
    overrides, official_rows_sha, official_manifest_sha = _official_overrides(
        official_manifest, official_rows, map_run)
    source_dir = enrichment_reader.BASE / "runs" / base_run_id
    destination = output_root / "runs"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".builder-enrichment-", dir=destination) as temporary:
        stage = Path(temporary)
        building_rows = []
        for registry_id, source_row in sorted(base["buildings"].items()):
            point = map_run["by_registry"][registry_id]
            building_rows.append({**source_row, **project_builder(
                point, assertions, overrides.get(registry_id))})
        buildings_payload = b"".join(_canonical(row) + b"\n" for row in building_rows)
        _write_private(stage / "buildings.jsonl", buildings_payload)
        for name in ("units.jsonl", "media.jsonl"):
            shutil.copyfile(source_dir / name, stage / name)
            os.chmod(stage / name, 0o600)
            if _file_sha(stage / name) != base["manifest"][name.removesuffix(".jsonl") + "_sha256"]:
                raise ValueError("Copied enrichment rows differ from source run")
        identity = {key: value for key, value in base["manifest"].items()
                    if key not in {"run_id", "buildings_sha256"}}
        identity.update({"buildings_sha256": _sha(buildings_payload),
                         "model_run_id": model_run_id,
                         "model_manifest_sha256": _file_sha(
                             model_reader.BASE / "runs" / model_run_id / "manifest.json"),
                         "builder_assertions_sha256": rows_sha,
                         "builder_assertions_manifest_sha256": assertion_manifest_sha,
                         "official_brand_overrides_sha256": official_rows_sha,
                         "official_brand_overrides_manifest_sha256": official_manifest_sha,
                         "official_brand_overrides_total": len(overrides),
                         "builder_assertions_joined_buildings_total": sum(
                             row["developer_display_basis"] == "aggregator_asserted_unverified"
                             for row in building_rows),
                         "first_party_brand_buildings_total": sum(
                             row["developer_display_basis"] == "first_party_source_brand"
                             for row in building_rows),
                         "builder_rules_version": RULES,
                         "parent_enrichment_run_id": base_run_id,
                         "parent_enrichment_manifest_sha256": base_sha})
        run_id = "developer-enrichment-v1-" + _sha(_canonical(identity))[:16]
        manifest = {"run_id": run_id, **identity}
        _write_private(stage / "manifest.json", _canonical(manifest) + b"\n")
        target = destination / run_id
        if target.exists():
            for name in ("manifest.json", "buildings.jsonl", "units.jsonl", "media.jsonl"):
                if _file_sha(target / name) != _file_sha(stage / name):
                    raise ValueError("Immutable builder enrichment run collision")
        else:
            # TemporaryDirectory cleanup happens after rename, where its old
            # path no longer exists. The target receives only pinned local data.
            os.replace(stage, target)
            _fsync_dir(destination)
        return {"run_id": run_id, "manifest_sha256": _file_sha(target / "manifest.json"),
                "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-run-id", required=True)
    parser.add_argument("--builder-manifest", type=Path, required=True)
    parser.add_argument("--builder-rows", type=Path, required=True)
    parser.add_argument("--map-run-id", required=True)
    parser.add_argument("--unit-run-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--official-manifest", type=Path)
    parser.add_argument("--official-rows", type=Path)
    args = parser.parse_args()
    result = build(args.base_run_id, args.builder_manifest, args.builder_rows,
                   args.map_run_id, args.unit_run_id, args.model_run_id,
                   args.official_manifest, args.official_rows)
    print(json.dumps({key: result[key] for key in ("run_id", "manifest_sha256")},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
