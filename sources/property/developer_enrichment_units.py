"""Project dated publisher details omitted from the active apartment menu.

No additional availability or payment qualification is inferred from a source
status code, a crossed-out price, or a raw promotional field.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from sources.property.developer_map import ROOT, _canonical, _file_sha, _fsync_dir, _rows, _sha
from sources.property.developer_enrichment_media import DELIVERY_MANIFEST, MENU_ROOT


FIELD_MAP = {
    "proplanirovki": {
        "finishing_label": "finishing",
        "delivery_deadline_raw": "deadline",
        "source_building_floors": "building_floors",
        "availability_claim": "availability_claim",
        "price_basis_evidence": "price_basis_evidence",
        "source_sale_status": "source_sale_status",
    },
    "a101": {
        "source_price_fields_rub": "a101_price_fields_rub",
        "source_sale_value": "a101_sale_value",
        "source_status_code": "a101_status_code",
        "source_building_number": "a101_building_number",
        "source_flatsplan_type": "a101_flatsplan_type",
        "availability_claim": "availability_assertion",
    },
    "fsk": {
        "delivery_deadline_raw": "api_delivery_date",
        "source_discount_percent": "api_discount_percent",
        "source_previous_price_rub": "api_previous_price_rub",
        "source_financial_labels": "api_financial_labels",
        "source_status_code": "api_status_code",
        "source_section_floors": "api_section_floors_count",
    },
}


def _source_rows(unit_run_id: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    menu_dir = MENU_ROOT / unit_run_id
    menu_manifest = json.loads((menu_dir / "manifest.json").read_text(encoding="utf-8"))
    if (menu_manifest.get("run_id") != unit_run_id or
            _file_sha(DELIVERY_MANIFEST) != menu_manifest.get("delivery_manifest_sha256")):
        raise ValueError("Unit details do not match the active delivery")
    delivery = json.loads(DELIVERY_MANIFEST.read_text(encoding="utf-8"))
    entry = delivery["files"]["observations"]
    path = DELIVERY_MANIFEST.parent / entry["path"]
    if (not path.is_file() or _file_sha(path) != entry["sha256"] or
            entry["sha256"] != menu_manifest.get("raw_observations_sha256")):
        raise ValueError("Raw unit observations changed")
    wanted = {(row["source_group"], row["publisher_unit_id"])
              for row in _rows(menu_dir / "units.jsonl")}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _rows(path):
        key = row.get("source_group"), row.get("publisher_unit_id")
        if row.get("record_type") != "unit" or key not in wanted:
            continue
        if key in result:
            raise ValueError("Duplicate raw publisher unit observation")
        result[key] = row
    if set(result) != wanted:
        raise ValueError("Raw delivery lacks an active publisher unit")
    return result, menu_manifest


def unit_detail_rows(unit_run_id: str) -> Iterable[dict[str, Any]]:
    menu_dir = MENU_ROOT / unit_run_id
    raw_by_key, _ = _source_rows(unit_run_id)
    for unit in _rows(menu_dir / "units.jsonl"):
        source_group = unit["source_group"]
        raw = raw_by_key[source_group, unit["publisher_unit_id"]]
        if (raw.get("observed_at") != unit["observed_at"] or
                raw.get("source_updated_at") != unit["source_updated_at"] or
                raw.get("publisher_project_id") != unit["publisher_project_id"]):
            raise ValueError("Unit detail source identity or date changed")
        fields = raw.get("source_fields") or {}
        selected = {target: fields.get(source) for target, source in
                    FIELD_MAP[source_group].items()}
        if source_group == "a101":
            selected["availability_claim"] = fields.get("availability_assertion")
        selected["promo_conditions_raw"] = raw.get("promo_conditions")
        yield {"unit_id": unit["unit_id"], "registry_id": unit["registry_id"],
               "source_group": source_group,
               "observed_at": unit["observed_at"],
               "source_updated_at": unit["source_updated_at"],
               "source_evidence_sha256": unit["source_evidence_sha256"],
               "raw_observation_sha256": _sha(_canonical(raw)),
               "assertion_basis": "captured_publisher_or_aggregator_fields",
               "availability_confirmed_current": False,
               "payment_terms_confirmed_unconditional": False,
               **selected}


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    for row in rows:
        counts[row["source_group"]] += 1
        for key, value in row.items():
            if value is not None and key not in {"unit_id", "registry_id", "source_group",
                                                  "observed_at", "source_updated_at",
                                                  "source_evidence_sha256", "raw_observation_sha256",
                                                  "assertion_basis", "availability_confirmed_current",
                                                  "payment_terms_confirmed_unconditional"}:
                fields[key] += 1
    return {"sources": dict(counts), "populated_fields": dict(fields)}


def stage_unit_details(unit_run_id: str, output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = output.with_suffix(".manifest.json")
    if any(path.exists() or path.is_symlink() for path in (output, manifest_path)):
        raise ValueError("Unit detail candidate already exists")
    digest = hashlib.sha256()
    totals: Counter[str] = Counter()
    populated: Counter[str] = Counter()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".units-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            for row in unit_detail_rows(unit_run_id):
                line = _canonical(row) + b"\n"
                stream.write(line)
                digest.update(line)
                totals[row["source_group"]] += 1
                for key, value in row.items():
                    if key not in {"unit_id", "registry_id", "source_group", "observed_at",
                                   "source_updated_at", "source_evidence_sha256",
                                   "raw_observation_sha256", "assertion_basis",
                                   "availability_confirmed_current",
                                   "payment_terms_confirmed_unconditional"} and value is not None:
                        populated[key] += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
        _fsync_dir(output.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    manifest = {"schema_version": "developer-unit-details/1",
                "unit_run_id": unit_run_id,
                "unit_manifest_sha256": _file_sha(MENU_ROOT / unit_run_id / "manifest.json"),
                "delivery_manifest_sha256": _file_sha(DELIVERY_MANIFEST),
                "file": output.name, "sha256": digest.hexdigest(),
                "records": sum(totals.values()), "source_counts": dict(totals),
                "populated_fields": dict(populated),
                "current_availability_inferred": False,
                "unconditional_promo_price_inferred": False}
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".manifest-",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(_canonical(manifest) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, manifest_path)
        _fsync_dir(output.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-run-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = (stage_unit_details(args.unit_run_id, args.output) if args.output else
              summary(unit_detail_rows(args.unit_run_id)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
