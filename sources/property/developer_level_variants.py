"""Export all source plan variants from a completed private Level harvest run."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from sources.property.developer_harvest import atomic_bytes, canonical_json


PLAN_ROLES = {"apartment_plan", "floor_plan", "visualized_apartment_plan",
              "apartment_plan_thumbnail"}


def build(run_dir: Path) -> dict:
    lock_path = run_dir / "run.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Level harvest is still active") from exc
        manifest_bytes = (run_dir / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        if (manifest.get("schema_version") != "developer-harvest-run/1"
                or set(manifest.get("providers", {})) != {"level"}
                or manifest["providers"]["level"]["catalog_pages_remaining"] != 0):
            raise ValueError("Completed Level catalog manifest required")
        db = sqlite3.connect(f"file:{run_dir / 'state.sqlite3'}?mode=ro", uri=True)
        try:
            versions = [json.loads(payload) for (payload,) in db.execute(
                "SELECT payload_json FROM observation_versions ORDER BY observation_id,payload_sha256")]
        finally:
            db.close()
        plan_refs: dict[tuple[str, str, str], dict] = {}
        version_counts: dict[str, set[str]] = defaultdict(set)
        for row in versions:
            if row.get("provider") != "level" or row.get("record_type") != "unit":
                continue
            unit_id = row.get("publisher_unit_id")
            if not isinstance(unit_id, str) or not unit_id:
                raise ValueError("Level unit version has no publisher ID")
            version_sha = hashlib.sha256(canonical_json({
                key: value for key, value in row.items()
                if key not in {"observed_at", "raw_sha256"}
            })).hexdigest()
            version_counts[unit_id].add(version_sha)
            for media in row.get("media", []):
                if not isinstance(media, dict) or media.get("role") not in PLAN_ROLES:
                    continue
                url = media.get("url")
                if not isinstance(url, str):
                    continue
                key = (unit_id, media["role"], url)
                plan_refs.setdefault(key, {
                    "source_group": "level", "publisher_unit_id": unit_id,
                    "role": media["role"], "source_url": url,
                    "source_page_url": row.get("source_page_url"),
                    "source_observation_id": row.get("observation_id"),
                    "source_version_sha256": version_sha,
                    "observed_at": row.get("observed_at"),
                    "source_raw_sha256": row.get("raw_sha256"),
                    "rights_status": "unknown", "public_display_allowed": False,
                    "variant_kind": "source_plan_reference_no_physical_unit_increment",
                })
        records = [plan_refs[key] for key in sorted(plan_refs)]
        content = b"".join(canonical_json(row) + b"\n" for row in records)
        output = run_dir / "level_plan_variants.jsonl"
        atomic_bytes(output, content)
        summary = {
            "schema_version": "level-plan-variants/1",
            "source_run_id": manifest["run_id"],
            "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "publisher_unit_ids": len(version_counts),
            "unit_ids_with_multiple_source_versions": sum(len(v) > 1 for v in version_counts.values()),
            "plan_reference_rows": len(records),
            "plan_reference_sha256": hashlib.sha256(content).hexdigest(),
            "all_catalog_units_fetched": bool(manifest["providers"]["level"]["catalog_exhausted"]),
            "rights_status": "unknown", "public_display_allowed": False,
        }
        atomic_bytes(run_dir / "level_plan_variants.manifest.json",
                     canonical_json(summary) + b"\n")
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.run_dir), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
