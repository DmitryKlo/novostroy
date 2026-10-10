"""Produce private, Excel-friendly tables from one verified developer delivery.

The original content-addressed JSONL delivery remains immutable. This export
never contacts a provider and never changes source rights or location scope.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

from sources.property.developer_harvest import atomic_bytes, canonical_json, digest


UNIT_FIELDS = (
    "source_group", "region_partition", "location_scope", "location_scope_basis",
    "project_name", "project_address", "publisher_project_id",
    "publisher_building_id", "corpus", "building_full_address", "section",
    "publisher_unit_id", "unit_number", "floor", "rooms", "area_m2",
    "price_rub", "price_basis", "promo_conditions", "observed_at",
    "source_updated_at", "source_updated_at_basis", "source_url",
    "geometry_inside_moscow", "geometry_evidence_basis", "moscow_boundary_sha256",
    "source_location_scope", "identity_quality", "identity_conflict",
    "source_rights_status", "public_display_allowed", "observation_id",
)
PROJECT_FIELDS = (
    "source_group", "record_type", "project_name", "corpus", "address",
    "publisher_project_id", "publisher_building_id", "longitude", "latitude",
    "coordinate_scope", "geometry_basis", "geometry_inside_moscow",
    "location_scope", "price_from_rub", "price_basis", "observed_at",
    "source_updated_at", "source_url", "identity_quality",
    "source_rights_status", "public_display_allowed", "observation_id",
)
MEDIA_FIELDS = (
    "provider", "record_type", "observation_id", "role", "scope",
    "visual_kind", "label", "url", "source_page_url", "source_field",
    "observed_at", "source_updated_at", "rights_status",
    "public_display_allowed",
)
UNIT_PARTITIONS = (
    "units_moscow", "units_moscow_oblast", "units_other_region",
    "units_outside_moscow_unresolved", "units_unknown",
    "units_geometry_conflict_quarantine",
)


def _verified_rows(delivery: Path, manifest: dict[str, Any], key: str) -> Iterator[dict]:
    info = manifest["files"].get(key)
    if not isinstance(info, dict) or info.get("path") != key + ".jsonl":
        raise ValueError(f"Missing delivery file: {key}")
    source = delivery / info["path"]
    sha = hashlib.sha256()
    count = 0
    with source.open("rb") as stream:
        for line in stream:
            sha.update(line)
            count += 1
            yield json.loads(line)
    if sha.hexdigest() != info["sha256"] or count != info["records"]:
        raise ValueError(f"Delivery file hash/count mismatch: {key}")


def _cell(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        if len(value) > 32000:
            raise ValueError("Excel cell exceeds bounded length")
        # A private source label can still execute a spreadsheet formula.
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def _unit(row: dict) -> dict[str, Any]:
    project, building, unit = (row.get(key) or {} for key in ("project", "building", "unit"))
    return {
        **{key: row.get(key) for key in UNIT_FIELDS},
        "project_name": project.get("name"), "project_address": project.get("address"),
        "publisher_project_id": project.get("publisher_project_id"),
        "publisher_building_id": building.get("publisher_building_id"),
        "corpus": building.get("corpus"),
        "building_full_address": building.get("full_address"),
        "section": unit.get("section"),
        "publisher_unit_id": unit.get("publisher_unit_id"),
        "unit_number": unit.get("number"), "floor": unit.get("floor"),
        "rooms": unit.get("rooms"), "area_m2": unit.get("area_m2"),
    }


def _project(row: dict) -> dict[str, Any]:
    coords = row.get("coordinates") or {}
    return {**row, "longitude": coords.get("longitude"),
            "latitude": coords.get("latitude")}


def _write_table(output: Path, fields: tuple[str, ...], rows: Iterator[dict],
                 project: Callable[[dict], dict]) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                     dir=output.parent, prefix=output.name + ".tmp-",
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            writer = csv.writer(handle, delimiter=";", lineterminator="\r\n")
            writer.writerow(fields)
            for row in rows:
                projected = project(row)
                writer.writerow([_cell(projected.get(key)) for key in fields])
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, output)
    raw = output.read_bytes()
    return {"path": output.name, "records": count, "bytes": len(raw),
            "sha256": digest(raw)}


def export_tables(delivery: Path, output_root: Path) -> dict[str, Any]:
    manifest_raw = (delivery / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("schema_version") != "developer-delivery/1":
        raise ValueError("Unsupported developer delivery")
    source_sha = digest(manifest_raw)
    output = output_root / ("developer-csv-" + source_sha[:16])
    prior_path = output / "manifest.json"
    if prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if prior.get("delivery_manifest_sha256") != source_sha:
            raise ValueError("Existing export has a different source delivery")
        for item in prior["files"].values():
            if digest((output / item["path"]).read_bytes()) != item["sha256"]:
                raise ValueError("Existing CSV export hash mismatch")
        return prior
    def units() -> Iterator[dict]:
        for key in UNIT_PARTITIONS:
            yield from _verified_rows(delivery, manifest, key)
    files = {
        "units": _write_table(output / "units.csv", UNIT_FIELDS, units(), _unit),
        "moscow_spatial_candidates": _write_table(
            output / "moscow_spatial_candidates.csv", UNIT_FIELDS,
            _verified_rows(delivery, manifest, "units_moscow_spatial_candidates"), _unit),
        "projects_corpora": _write_table(
            output / "projects_corpora.csv", PROJECT_FIELDS,
            _verified_rows(delivery, manifest, "projects_corpora"), _project),
        "media_refs": _write_table(
            output / "media_refs.csv", MEDIA_FIELDS,
            _verified_rows(delivery, manifest, "media_refs"), lambda row: row),
    }
    result = {
        "schema_version": "developer-csv/1", "delivery_id": manifest["delivery_id"],
        "delivery_manifest_sha256": source_sha, "private_only": True,
        "cross_provider_physical_unit_dedup_claimed": False,
        "format": "UTF-8 with BOM, semicolon separator, CRLF; spreadsheet formula escaped",
        "files": files,
    }
    atomic_bytes(prior_path, canonical_json(result) + b"\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_tables(args.delivery_dir, args.output_root),
                     ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
