#!/usr/bin/env python3
"""Bounded provenance/priority audit for an estimated render-height overlay."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
DEFAULT_OVERLAY = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.parquet"
DEFAULT_MANIFEST = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    source = args.source.resolve()
    overlay = args.overlay.resolve()
    manifest_path = args.manifest.resolve()
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hash = sha256_file(source)
    overlay_hash = sha256_file(overlay)
    if manifest["input_sha256"] != source_hash or manifest["overlay_sha256"] != overlay_hash:
        raise ValueError("manifest/source or manifest/overlay SHA-256 mismatch")
    table = pq.read_table(overlay)
    rows = table.to_pylist()
    target_ids = [row["source_id"] for row in rows]
    if len(set(target_ids)) != len(target_ids):
        raise ValueError("duplicate overlay source IDs")
    required_ids = set(target_ids)
    for row in rows:
        required_ids.update(row["donor_ids"])
    source_records: dict[str, tuple[float | None, int | None]] = {}
    parquet = pq.ParquetFile(source)
    for batch in parquet.iter_batches(batch_size=16384, columns=["id", "height", "num_floors"]):
        ids = batch.column(0).to_pylist()
        heights = batch.column(1).to_pylist()
        floors = batch.column(2).to_pylist()
        for source_id, height, floor_count in zip(ids, heights, floors):
            source_id = str(source_id)
            if source_id in required_ids:
                if source_id in source_records:
                    raise ValueError(f"duplicate source row for {source_id}")
                source_records[source_id] = (height, floor_count)
    missing_ids = required_ids - source_records.keys()
    if missing_ids:
        raise ValueError(f"source IDs absent from input: {len(missing_ids)}")
    confidence = Counter()
    donor_counts = Counter()
    heights = []
    for row in rows:
        target_height, target_floors = source_records[row["source_id"]]
        if target_height is not None and target_height > 0:
            raise ValueError(f"overlay target has positive source height attribute: {row['source_id']}")
        if target_floors is not None and target_floors > 0:
            raise ValueError(f"overlay target has positive floors: {row['source_id']}")
        donor_heights = row["donor_heights_m"]
        donor_ids = row["donor_ids"]
        if not donor_ids or len(donor_ids) != len(donor_heights) or len(donor_ids) != row["donor_count"]:
            raise ValueError(f"invalid donor arrays/count: {row['source_id']}")
        observed = []
        for donor_id, donor_height in zip(donor_ids, donor_heights):
            source_height, _ = source_records[donor_id]
            if source_height is None or source_height <= 0 or float(source_height) != float(donor_height):
                raise ValueError(f"donor height mismatch: {donor_id}")
            observed.append(float(source_height))
        if float(row["estimated_render_height_m"]) != min(observed):
            raise ValueError(f"estimate is not the minimum accepted donor height: {row['source_id']}")
        if row["status"] != "estimated" or not row["source_height_missing"] or not row["source_floors_missing"]:
            raise ValueError(f"invalid estimate status/missingness: {row['source_id']}")
        if row["donor_method"] not in {"exact_geometry", "similar_footprint"}:
            raise ValueError(f"invalid donor method: {row['source_id']}")
        heights.append(float(row["estimated_render_height_m"]))
        confidence[row["confidence"]] += 1
        donor_counts[row["donor_count"]] += 1
    if len(rows) != manifest["overlay_rows"] or len(target_ids) != manifest["overlay_unique_source_ids"]:
        raise ValueError("manifest overlay count mismatch")
    print(json.dumps({
        "status": "overlay_audit_passed",
        "source_sha256": source_hash,
        "overlay_sha256": overlay_hash,
        "rows": len(rows),
        "unique_target_ids": len(set(target_ids)),
        "donor_id_coverage_checked": len(required_ids),
        "all_targets_missing_height_and_positive_floors": True,
        "all_donors_match_positive_source_height_attribute": True,
        "all_estimates_equal_minimum_suitable_donor": True,
        "height_range_m": [min(heights), max(heights)] if heights else None,
        "confidence_counts": dict(confidence),
        "donor_count_distribution": dict(donor_counts),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
