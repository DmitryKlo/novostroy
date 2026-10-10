"""Excel-safe private Level unit and plan-reference tables from a checked run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sources.property.developer_delivery_csv import _write_table
from sources.property.developer_harvest import atomic_bytes, canonical_json


UNIT_FIELDS = (
    "source_group", "publisher_unit_id", "publisher_project_id",
    "publisher_building_id", "publisher_section_id", "project_name",
    "building_label", "unit_number", "floor", "rooms", "area_m2",
    "price_rub", "old_price_rub", "discount_source", "price_basis",
    "location_scope", "catalog_filter_city", "observed_at", "source_updated_at",
    "source_status_modified", "source_page_url", "rights_status",
    "public_display_allowed", "observation_id",
)
PLAN_FIELDS = (
    "source_group", "publisher_unit_id", "role", "source_url",
    "source_page_url", "source_version_sha256", "observed_at",
    "rights_status", "public_display_allowed", "variant_kind",
)


def build(run_dir: Path) -> dict:
    run_bytes = (run_dir / "manifest.json").read_bytes()
    run = json.loads(run_bytes)
    variants_bytes = (run_dir / "level_plan_variants.manifest.json").read_bytes()
    variants = json.loads(variants_bytes)
    run_sha = hashlib.sha256(run_bytes).hexdigest()
    if (set(run.get("providers", {})) != {"level"}
            or not run["providers"]["level"]["catalog_exhausted"]
            or variants.get("source_manifest_sha256") != run_sha):
        raise ValueError("Completed Level run and matching variant export required")
    observations_bytes = (run_dir / "observations.jsonl").read_bytes()
    if hashlib.sha256(observations_bytes).hexdigest() != run["files"]["observations"]["sha256"]:
        raise ValueError("Level observations SHA mismatch")
    rows = [json.loads(line) for line in observations_bytes.splitlines()]
    if len(rows) != run["files"]["observations"]["records"]:
        raise ValueError("Level observation count mismatch")
    unit_rows = [row for row in rows if row.get("record_type") == "unit"]
    if len(unit_rows) != run["providers"]["level"]["catalog_distinct_unit_ids"]:
        raise ValueError("Level unit count mismatch")
    plan_bytes = (run_dir / "level_plan_variants.jsonl").read_bytes()
    if hashlib.sha256(plan_bytes).hexdigest() != variants["plan_reference_sha256"]:
        raise ValueError("Level plan variant SHA mismatch")
    plans = [json.loads(line) for line in plan_bytes.splitlines()]
    if len(plans) != variants["plan_reference_rows"]:
        raise ValueError("Level plan reference count mismatch")
    output = run_dir / "supplemental-export"
    files = {
        "units": _write_table(output / "units.csv", UNIT_FIELDS, iter(unit_rows),
                              lambda row: {**row, "source_group": "level",
                                           "old_price_rub": row["source_fields"].get("old_price_rub"),
                                           "discount_source": row["source_fields"].get("discount"),
                                           "location_scope": row["source_fields"].get("location_scope"),
                                           "catalog_filter_city": row["source_fields"].get("catalog_filter_city"),
                                           "source_status_modified": row["source_fields"].get("status_modified")}),
        "plans": _write_table(output / "plan_references.csv", PLAN_FIELDS,
                              iter(plans), lambda row: row),
    }
    manifest = {
        "schema_version": "level-private-csv/1", "source_run_id": run["run_id"],
        "source_manifest_sha256": run_sha,
        "variant_manifest_sha256": hashlib.sha256(variants_bytes).hexdigest(),
        "unit_ids": len(unit_rows), "source_result_slots": run["providers"]["level"]["catalog_source_unit_slots"],
        "source_count_without_alterplans": run["providers"]["level"]["catalog_count_without_alterplans_claimed"],
        "files": files, "format": "UTF-8 BOM, semicolon, CRLF, spreadsheet formula escaped",
        "location_scope_note": "publisher city=moscow query only; municipality unverified",
        "rights_status": "unknown", "public_display_allowed": False,
    }
    atomic_bytes(output / "manifest.json", canonical_json(manifest) + b"\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.run_dir), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
