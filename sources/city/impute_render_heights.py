#!/usr/bin/env python3
"""Calibrate and build a conservative Overture render-height overlay.

This tool never edits Overture `height` or `num_floors`. It only estimates a
separate render value for features where both fields are absent. Positive
floors remain the renderer's preferred fallback (3 m/floor).

Run with pinned dependencies, for example:
  rtk uvx --with pyarrow==26.0.0 --with shapely==2.2.0 --with pyproj==3.7.2 \
    python sources/city/impute_render_heights.py validate
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer
from shapely import affinity
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "data/city/overture_moscow_cityclipped_2026-09-23.1.parquet"
DEFAULT_REPORT = ROOT / "data/city/render_height_calibration_replay.json"
DEFAULT_OVERLAY = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.parquet"
DEFAULT_MANIFEST = ROOT / "data/city/overture_moscow_render_height_imputation_v1_1.manifest.json"
SEED = "moscow-overture-render-height-holdout-v1"
HOLDOUT_FRACTION = 0.20
CRS_TRANSFORMER = Transformer.from_crs("OGC:CRS84", "EPSG:32637", always_xy=True)
PROFILE = {
    "profile_id": "shape-local-v1",
    "source_crs": "OGC:CRS84",
    "metric_crs": "EPSG:32637",
    "max_polygon_distance_m": 35.0,
    "max_centroid_distance_m": 35.0,
    "area_ratio_min": 0.80,
    "long_dimension_ratio_min": 0.80,
    "short_dimension_ratio_min": 0.80,
    "normalized_shape_iou_min": 0.90,
    "same_class_required": True,
    "same_subtype_if_both_known": True,
    "similarity_donor_height_range_m": [2.0, 300.0],
    "max_similarity_target_area_m2": 40000.0,
    "min_similarity_target_area_m2": 8.0,
    "exclude_underground_or_has_parts": True,
    "similarity_value_policy": "minimum positive source height attribute among all donors passing every gate",
    "confidence_semantics": "ordinal heuristic; not a calibrated probability",
}
DONOR_SHAPE_CACHE: dict[int, tuple[Any, ...]] = {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_fraction(source_id: str) -> float:
    digest = hashlib.sha256(f"{SEED}:{source_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def normalize_geometry(geometry: Any) -> Any:
    """Canonical ring/component order for exact-geometry identity tests."""
    return shapely.normalize(geometry)


def geometry_key(geometry: Any) -> bytes:
    return hashlib.sha256(shapely.to_wkb(normalize_geometry(geometry), byte_order=1)).digest()


def metric_geometries(wkb: Iterable[bytes]) -> np.ndarray:
    geoms = shapely.from_wkb(list(wkb))
    return shapely.transform(
        geoms,
        lambda x, y: CRS_TRANSFORMER.transform(x, y, errcheck=True),
        interleaved=False,
    )


def shape_dimensions(geometry: Any) -> tuple[float, float]:
    rectangle = geometry.minimum_rotated_rectangle
    points = list(rectangle.exterior.coords)
    lengths = [math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]) for i in range(4)]
    lengths.sort(reverse=True)
    return lengths[0], lengths[-1]


def canonical_shape(geometry: Any) -> tuple[Any, tuple[Any, ...]]:
    area = geometry.area
    if area <= 0:
        raise ValueError("non-positive footprint area")
    long_dim, _ = shape_dimensions(geometry)
    rectangle = geometry.minimum_rotated_rectangle
    points = list(rectangle.exterior.coords)
    edges = [(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]) for i in range(4)]
    dx, dy = max(edges, key=lambda pair: math.hypot(*pair))
    angle = math.degrees(math.atan2(dy, dx))
    centroid = geometry.centroid
    oriented = affinity.rotate(geometry, -angle, origin=centroid)
    scaled = affinity.scale(oriented, xfact=1 / math.sqrt(area), yfact=1 / math.sqrt(area), origin=centroid)
    normalized = affinity.translate(scaled, xoff=-centroid.x, yoff=-centroid.y)
    variants = tuple(affinity.rotate(normalized, i * 90, origin="centroid") for i in range(4))
    return normalized, variants


def normalized_shape_iou(target_shape: Any, donor_variants: tuple[Any, ...]) -> float:
    best = 0.0
    for candidate in donor_variants:
        intersection = target_shape.intersection(candidate).area
        union = target_shape.area + candidate.area - intersection
        if union > 0:
            best = max(best, intersection / union)
    return best


def class_compatible(target: dict[str, Any], donor: dict[str, Any]) -> bool:
    if target["class"] != donor["class"]:
        return False
    if target["subtype"] and donor["subtype"] and target["subtype"] != donor["subtype"]:
        return False
    return True


def records_from_batch(batch: pa.RecordBatch) -> list[dict[str, Any]]:
    cols = {name: batch.column(batch.schema.get_field_index(name)).to_pylist() for name in batch.schema.names}
    rows = []
    for i, source_id in enumerate(cols["id"]):
        sources = cols["sources"][i] or []
        rows.append({
            "id": str(source_id),
            "wkb": cols["geometry"][i],
            "height": cols["height"][i],
            "floors": cols["num_floors"][i],
            "class": cols["class"][i],
            "subtype": cols["subtype"][i],
            "is_underground": cols["is_underground"][i],
            "has_parts": cols["has_parts"][i],
            "source_dataset": sources[0].get("dataset") if sources else None,
        })
    return rows


def read_profile_population(path: Path, split: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Keep only the ~6k positive source-height donors and reproducible holdout."""
    parquet = pq.ParquetFile(path)
    donors: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    counts = defaultdict(int)
    columns = ["id", "geometry", "height", "num_floors", "class", "subtype", "is_underground", "has_parts", "sources"]
    for batch in parquet.iter_batches(batch_size=8192, columns=columns):
        for row in records_from_batch(batch):
            counts["rows"] += 1
            height = row["height"]
            floors = row["floors"]
            if height is not None and math.isfinite(float(height)) and float(height) > 0:
                counts["positive_height_rows"] += 1
                no_positive_floors = floors is None or int(floors) <= 0
                fraction = stable_fraction(row["id"])
                is_holdout = (
                    (split == "calibration" and fraction < 0.20)
                    or (split == "final_test" and 0.20 <= fraction < 0.40)
                )
                excluded_from_donors = (
                    split == "calibration" and fraction < 0.20
                    or split == "final_test" and fraction < 0.40
                )
                if split in {"calibration", "final_test"} and no_positive_floors and is_holdout:
                    row["truth_height_m"] = float(height)
                    holdout.append(row)
                    counts[f"{split}_positive_height_no_floors"] += 1
                    continue
                # Known-height rows with floors are never test targets under this
                # render policy, so retain them as donors regardless of their hash.
                if not excluded_from_donors:
                    donors.append(row)
                    counts["donor_positive_height_rows"] += 1
            elif floors is None or int(floors) <= 0:
                counts["neither_positive_height_nor_floors"] += 1
            elif height is None or not (math.isfinite(float(height)) and float(height) > 0):
                counts["floors_available_source_height_missing_or_invalid"] += 1
    return donors, holdout, dict(counts)


def donor_index(rows: list[dict[str, Any]], similarity_only: bool = True) -> tuple[list[int], list[Any], STRtree, dict[bytes, list[int]], int]:
    DONOR_SHAPE_CACHE.clear()
    included: list[int] = []
    wkb: list[bytes] = []
    excluded_height = 0
    for i, row in enumerate(rows):
        height = float(row["height"])
        if similarity_only and not (2.0 <= height <= 300.0):
            excluded_height += 1
            continue
        if similarity_only and (row["is_underground"] is True or row["has_parts"] is True):
            continue
        included.append(i)
        wkb.append(row["wkb"])
    geometries = metric_geometries(wkb)
    tree = STRtree(geometries)
    exact: dict[bytes, list[int]] = defaultdict(list)
    for tree_index, geom in enumerate(geometries):
        exact[geometry_key(geom)].append(tree_index)
    return included, list(geometries), tree, exact, excluded_height


def candidate_matches(
    target: dict[str, Any],
    target_geom: Any,
    donor_rows: list[dict[str, Any]],
    donor_geoms: list[Any],
    tree: STRtree,
    exact_map: dict[bytes, list[int]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return exact same-footprint matches first, else strict local form matches."""
    stats = defaultdict(int)
    target_key = geometry_key(target_geom)
    exact_ids = exact_map.get(target_key, [])
    exact_candidates = []
    for tree_idx in exact_ids:
        donor = donor_rows[tree_idx]
        if donor["id"] == target["id"]:
            continue
        exact_candidates.append({
            "tree_index": tree_idx,
            "donor_id": donor["id"],
            "donor_height_m": float(donor["height"]),
            "method": "exact_geometry",
            "distance_m": 0.0,
            "shape_iou": 1.0,
            "area_ratio": 1.0,
            "long_dimension_ratio": 1.0,
            "short_dimension_ratio": 1.0,
            "class_conflict": donor["class"] != target["class"],
        })
    if exact_candidates:
        stats["exact_geometry_candidates"] += len(exact_candidates)
        return exact_candidates, dict(stats)

    if target["is_underground"] is True or target["has_parts"] is True:
        stats["excluded_underground_or_parts"] += 1
        return [], dict(stats)
    target_area = target_geom.area
    if not (PROFILE["min_similarity_target_area_m2"] <= target_area <= PROFILE["max_similarity_target_area_m2"]):
        stats["excluded_target_area"] += 1
        return [], dict(stats)

    target_long, target_short = shape_dimensions(target_geom)
    if target_short <= 0 or target_long <= 0:
        stats["excluded_target_dimensions"] += 1
        return [], dict(stats)
    tree_hits = tree.query(target_geom, predicate="dwithin", distance=PROFILE["max_polygon_distance_m"])
    if len(tree_hits) == 0:
        return [], dict(stats)
    try:
        target_shape, _ = canonical_shape(target_geom)
    except ValueError:
        stats["excluded_target_shape"] += 1
        return [], dict(stats)
    compatible = []
    for tree_idx in tree_hits.tolist():
        donor = donor_rows[tree_idx]
        if donor["id"] == target["id"] or not class_compatible(target, donor):
            stats["rejected_class_or_identity"] += 1
            continue
        donor_geom = donor_geoms[tree_idx]
        distance = target_geom.centroid.distance(donor_geom.centroid)
        if distance > PROFILE["max_centroid_distance_m"]:
            stats["rejected_centroid_distance"] += 1
            continue
        donor_area = donor_geom.area
        if donor_area <= 0:
            continue
        area_ratio = min(target_area / donor_area, donor_area / target_area)
        if area_ratio < PROFILE["area_ratio_min"]:
            stats["rejected_area_ratio"] += 1
            continue
        donor_long, donor_short = shape_dimensions(donor_geom)
        long_ratio = min(target_long / donor_long, donor_long / target_long)
        short_ratio = min(target_short / donor_short, donor_short / target_short)
        if long_ratio < PROFILE["long_dimension_ratio_min"] or short_ratio < PROFILE["short_dimension_ratio_min"]:
            stats["rejected_dimension_ratio"] += 1
            continue
        donor_variants = DONOR_SHAPE_CACHE.get(tree_idx)
        if donor_variants is None:
            _, donor_variants = canonical_shape(donor_geom)
            DONOR_SHAPE_CACHE[tree_idx] = donor_variants
        shape_iou = normalized_shape_iou(target_shape, donor_variants)
        if shape_iou < PROFILE["normalized_shape_iou_min"]:
            stats["rejected_normalized_shape"] += 1
            continue
        compatible.append({
            "tree_index": tree_idx,
            "donor_id": donor["id"],
            "donor_height_m": float(donor["height"]),
            "method": "similar_footprint",
            "distance_m": round(distance, 3),
            "shape_iou": round(shape_iou, 6),
            "area_ratio": round(area_ratio, 6),
            "long_dimension_ratio": round(long_ratio, 6),
            "short_dimension_ratio": round(short_ratio, 6),
            "class_conflict": False,
        })
    stats["similarity_candidates"] += len(compatible)
    return compatible, dict(stats)


def confidence_for(candidates: list[dict[str, Any]]) -> str:
    if candidates and candidates[0]["method"] == "exact_geometry":
        return "high_exact_geometry"
    if not candidates:
        return "none"
    best_shape = max(x["shape_iou"] for x in candidates)
    best_area = max(x["area_ratio"] for x in candidates)
    if len(candidates) >= 2 and best_shape >= 0.97 and best_area >= 0.90:
        return "moderate_multiple_similar_donors"
    if best_shape >= 0.95 and best_area >= 0.90:
        return "moderate_single_similar_donor"
    return "low_similar_donor"


def estimate(target: dict[str, Any], target_geom: Any, donor_rows: list[dict[str, Any]], donor_geoms: list[Any], tree: STRtree, exact_map: dict[bytes, list[int]]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    matches, stats = candidate_matches(target, target_geom, donor_rows, donor_geoms, tree, exact_map)
    if not matches:
        return None, stats
    matches.sort(key=lambda x: (x["donor_height_m"], x["donor_id"]))
    heights = [m["donor_height_m"] for m in matches]
    chosen_height = min(heights)
    row = {
        "source_id": target["id"],
        "estimated_render_height_m": chosen_height,
        "donor_ids": [m["donor_id"] for m in matches],
        "donor_heights_m": heights,
        "donor_method": matches[0]["method"],
        "donor_count": len(matches),
        "donor_height_source": "overture_height_attribute",
        "confidence": confidence_for(matches),
        "nearest_donor_distance_m": min(m["distance_m"] for m in matches),
        "max_normalized_shape_iou": max(m["shape_iou"] for m in matches),
        "minimum_area_ratio": min(m["area_ratio"] for m in matches),
        "minimum_long_dimension_ratio": min(m["long_dimension_ratio"] for m in matches),
        "minimum_short_dimension_ratio": min(m["short_dimension_ratio"] for m in matches),
        "source_height_missing": target["height"] is None or float(target["height"]) <= 0,
        "source_floors_missing": target["floors"] is None or int(target["floors"]) <= 0,
        "source_class": target["class"],
        "source_subtype": target["subtype"],
        "profile_id": PROFILE["profile_id"],
        "thresholds_json": json.dumps(PROFILE, sort_keys=True, separators=(",", ":")),
    }
    return row, stats


def metrics(errors: list[float], overestimate_gt5: int, overestimate_gt10: int, underestimate_gt10: int, total: int) -> dict[str, Any]:
    abs_errors = [abs(x) for x in errors]
    if not errors:
        return {"n": 0, "coverage": 0.0, "mae_m": None, "median_absolute_error_m": None, "p90_absolute_error_m": None}
    ordered = sorted(abs_errors)
    p90 = ordered[math.ceil(0.9 * len(ordered)) - 1]
    return {
        "n": len(errors),
        "coverage": round(len(errors) / total, 6) if total else 0.0,
        "mae_m": round(statistics.mean(abs_errors), 3),
        "median_absolute_error_m": round(statistics.median(abs_errors), 3),
        "p90_absolute_error_m": round(p90, 3),
        "mean_signed_error_m": round(statistics.mean(errors), 3),
        "overestimate_gt5m_count": overestimate_gt5,
        "overestimate_gt5m_rate": round(overestimate_gt5 / len(errors), 6),
        "overestimate_gt10m_count": overestimate_gt10,
        "overestimate_gt10m_rate": round(overestimate_gt10 / len(errors), 6),
        "underestimate_gt10m_count": underestimate_gt10,
        "underestimate_gt10m_rate": round(underestimate_gt10 / len(errors), 6),
    }


def evaluate_holdout(path: Path, split: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    donor_rows, holdout, counts = read_profile_population(path, split)
    donor_local, donor_geoms, tree, exact_map, excluded_height = donor_index(donor_rows)
    filtered_donors = [donor_rows[i] for i in donor_local]
    if len(filtered_donors) != len(donor_geoms):
        raise AssertionError("donor geometry/record mismatch")
    holdout_geoms = metric_geometries([row["wkb"] for row in holdout])
    outputs: list[dict[str, Any]] = []
    errors_est: list[float] = []
    errors_fallback_all: list[float] = []
    errors_fallback_matched: list[float] = []
    over5 = over10 = under10 = false_candidates = 0
    shape_counters = defaultdict(int)
    for row, geom in zip(holdout, holdout_geoms):
        truth = float(row["truth_height_m"])
        errors_fallback_all.append(15.0 - truth)
        predicted, stats = estimate(row, geom, filtered_donors, donor_geoms, tree, exact_map)
        for key, value in stats.items():
            shape_counters[key] += value
        item = {
            "source_id": row["id"],
            "truth_height_m": truth,
            "fallback_height_m": 15.0,
            "source_class": row["class"],
            "source_subtype": row["subtype"],
            "donor_ids": [],
            "donor_heights_m": [],
            "estimated_render_height_m": None,
            "absolute_error_m": None,
            "signed_error_m": None,
        }
        if predicted:
            signed = predicted["estimated_render_height_m"] - truth
            ae = abs(signed)
            item.update(predicted)
            item["absolute_error_m"] = round(ae, 3)
            item["signed_error_m"] = round(signed, 3)
            errors_est.append(signed)
            errors_fallback_matched.append(15.0 - truth)
            over5 += signed > 5
            over10 += signed > 10
            under10 += signed < -10
            false_candidates += ae > 10
        outputs.append(item)
    in_range_indices = [i for i, row in enumerate(holdout) if 2.0 <= float(row["truth_height_m"]) <= 250.0]
    in_range_matches = [outputs[i] for i in in_range_indices if outputs[i]["estimated_render_height_m"] is not None]
    in_range_errors = [x["estimated_render_height_m"] - x["truth_height_m"] for x in in_range_matches]
    in_range_over5 = sum(x > 5 for x in in_range_errors)
    in_range_over10 = sum(x > 10 for x in in_range_errors)
    in_range_under10 = sum(x < -10 for x in in_range_errors)
    report = {
        "status": "holdout_validated",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_file": str(path.relative_to(ROOT)),
        "source_sha256": sha256_file(path),
        "seed": SEED,
        "holdout_fraction_per_bucket": HOLDOUT_FRACTION,
        "split": split,
        "holdout_definition": (
            "positive source height attribute and absent/non-positive num_floors; deterministic SHA256(ID) fraction bucket; "
            "calibration=[0.0,0.2), final_test=[0.2,0.4); final-test donors exclude both earlier buckets"
        ),
        "source_height_population": counts,
        "method": PROFILE,
        "donor_pool": {
            "training_donors_positive_height": len(donor_rows),
            "eligible_similarity_donors": len(filtered_donors),
            "donors_excluded_height_outside_2_300m": excluded_height,
            "exact_canonical_geometry_duplicate_groups_in_donor_pool": sum(len(v) > 1 for v in exact_map.values()),
        },
        "candidate_gate_counts": dict(shape_counters),
        "holdout_count": len(holdout),
        "holdout_in_height_range_2_250m_count": len(in_range_indices),
        "estimated_matches_count": len(errors_est),
        "candidate_coverage": round(len(errors_est) / len(holdout), 6) if holdout else 0.0,
        "estimator_vs_truth_all_matches": metrics(errors_est, over5, over10, under10, len(holdout)),
        "estimator_vs_truth_in_range_2_250m": metrics(in_range_errors, in_range_over5, in_range_over10, in_range_under10, len(in_range_indices)),
        "fallback_15m_vs_truth_all_holdout": metrics(errors_fallback_all, sum(x > 5 for x in errors_fallback_all), sum(x > 10 for x in errors_fallback_all), sum(x < -10 for x in errors_fallback_all), len(holdout)),
        "fallback_15m_vs_truth_matched_subset": metrics(errors_fallback_matched, sum(x > 5 for x in errors_fallback_matched), sum(x > 10 for x in errors_fallback_matched), sum(x < -10 for x in errors_fallback_matched), len(errors_est)),
        "false_candidates_absolute_error_gt10m_count": false_candidates,
        "exact_match_count": sum(1 for x in outputs if x.get("donor_method") == "exact_geometry"),
        "similar_match_count": sum(1 for x in outputs if x.get("donor_method") == "similar_footprint"),
        "decision_note": "Metrics estimate visual render-height usefulness, not geodetic accuracy. Imputation remains status=estimated and never replaces source facts.",
    }
    return report, outputs


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def validate(args: argparse.Namespace) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    report, examples = evaluate_holdout(source, args.split)
    report_path = args.report.resolve()
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite {report_path}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["examples_file"] = report_path.with_suffix(".cases.json").name
    write_json_atomic(report_path, report)
    write_json_atomic(report_path.with_suffix(".cases.json"), {
        "source_sha256": report["source_sha256"],
        "seed": SEED,
        "cases": examples,
    })
    return source, report, examples


def build_overlay(source: Path, report: dict[str, Any], args: argparse.Namespace) -> tuple[int, str]:
    if report["source_sha256"] != sha256_file(source):
        raise ValueError("holdout report source SHA does not match input parquet")
    donor_rows, _, counts = read_profile_population(source, "build")
    donor_indexes, donor_geoms, tree, exact_map, excluded_height = donor_index(donor_rows)
    filtered_donors = [donor_rows[i] for i in donor_indexes]
    results: list[dict[str, Any]] = []
    rejected = defaultdict(int)
    parquet = pq.ParquetFile(source)
    columns = ["id", "geometry", "height", "num_floors", "class", "subtype", "is_underground", "has_parts", "sources"]
    for batch in parquet.iter_batches(batch_size=2048, columns=columns):
        rows = records_from_batch(batch)
        eligible_positions = [i for i, row in enumerate(rows)
                              if (row["height"] is None or float(row["height"]) <= 0)
                              and (row["floors"] is None or int(row["floors"]) <= 0)]
        if not eligible_positions:
            continue
        geometries = metric_geometries([rows[i]["wkb"] for i in eligible_positions])
        pairs = tree.query(geometries, predicate="dwithin", distance=PROFILE["max_polygon_distance_m"])
        candidate_targets: set[int] = set(pairs[0].tolist()) if pairs.size else set()
        # Exact canonical matches need not be spatial-index hits only if identical geometry;
        # identical polygons are necessarily within the same index distance, but retain an
        # explicit lookup so the method distinction remains auditable.
        for local_i, source_row_i in enumerate(eligible_positions):
            if local_i not in candidate_targets:
                geom_key = geometry_key(geometries[local_i])
                if exact_map.get(geom_key):
                    candidate_targets.add(local_i)
        for local_i in sorted(candidate_targets):
            source_row = rows[eligible_positions[local_i]]
            estimated, stats = estimate(source_row, geometries[local_i], filtered_donors, donor_geoms, tree, exact_map)
            for key, value in stats.items():
                rejected[key] += value
            if estimated is None:
                continue
            estimated.update({
                "status": "estimated",
                "source_height_value": source_row["height"],
                "source_floors_value": source_row["floors"],
                "source_dataset": source_row["source_dataset"],
                "holdout_calibration_id": report["source_sha256"][:16] + ":" + report["seed"],
            })
            results.append(estimated)
    # There can be a valid exact match not surfaced by dwithin for unusual geometry engines;
    # source geometries have polygonal types, so the indexed pass is otherwise complete.
    output = args.output.resolve()
    manifest_path = args.manifest.resolve()
    if output.exists() or manifest_path.exists():
        raise FileExistsError(f"refusing to overwrite overlay or manifest: {output}, {manifest_path}")
    if results:
        table = pa.Table.from_pylist(results)
    else:
        table = pa.table({
            "source_id": pa.array([], type=pa.string()),
            "estimated_render_height_m": pa.array([], type=pa.float64()),
            "donor_ids": pa.array([], type=pa.list_(pa.string())),
            "donor_heights_m": pa.array([], type=pa.list_(pa.float64())),
            "status": pa.array([], type=pa.string()),
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        pq.write_table(table, temp_path, compression="zstd", version="2.6")
        os.replace(temp_path, output)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    manifest = {
        "status": "estimated_render_height_overlay",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input_file": str(source.relative_to(ROOT)),
        "input_sha256": sha256_file(source),
        "input_release": "2026-09-23.1",
        "input_rows": counts.get("rows"),
        "holdout_report_file": str(args.report.resolve().relative_to(ROOT)),
        "holdout_report_sha256": sha256_file(args.report.resolve()),
        "overlay_file": str(output.relative_to(ROOT)),
        "overlay_sha256": sha256_file(output),
        "overlay_rows": len(results),
        "overlay_unique_source_ids": len({x["source_id"] for x in results}),
        "duplicate_overlay_ids": len(results) - len({x["source_id"] for x in results}),
        "target_policy": "source height attribute absent/non-positive AND num_floors absent/non-positive; positive source height attribute and positive floors always take precedence",
        "priority": ["positive_source_height_attribute", "positive_num_floors_times_3m", "approved_render_height_imputation", "renderer_fallback_15m"],
        "imputed_value_status": "estimated; never observed; must not be surfaced as source height fact",
        "donor_policy": "positive source Overture height attribute only; donor itself is not modified; minimum among all suitable donors is selected",
        "donors": {
            "positive_height_rows": len(donor_rows),
            "eligible_similarity_donors": len(filtered_donors),
            "excluded_height_outside_2_300m": excluded_height,
            "exact_canonical_geometry_duplicate_groups": sum(len(v) > 1 for v in exact_map.values()),
        },
        "method": PROFILE,
        "candidate_rejection_counts": dict(rejected),
        "holdout_summary": {
            "holdout_count": report["holdout_count"],
            "coverage": report["candidate_coverage"],
            "similar_match_count": report["similar_match_count"],
            "exact_match_count": report["exact_match_count"],
            "estimator_vs_truth": report["estimator_vs_truth_all_matches"],
            "fallback_15m_vs_truth": report["fallback_15m_vs_truth_matched_subset"],
        },
        "confidence_semantics": PROFILE["confidence_semantics"],
        "license": "ODbL-1.0 source-derived attributes/geometries; retain Overture and upstream OSM attribution",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
    }
    write_json_atomic(manifest_path, manifest)
    return len(results), sha256_file(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate", help="run deterministic holdout and write report/cases")
    validate_parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    validate_parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    validate_parser.add_argument("--split", choices=["calibration", "final_test"], default="calibration")
    validate_parser.add_argument("--shape-iou-min", type=float, default=PROFILE["normalized_shape_iou_min"])
    validate_parser.add_argument("--area-ratio-min", type=float, default=PROFILE["area_ratio_min"])
    validate_parser.add_argument("--dimension-ratio-min", type=float, default=PROFILE["long_dimension_ratio_min"])
    validate_parser.add_argument("--max-distance-m", type=float, default=PROFILE["max_polygon_distance_m"])
    build_parser = sub.add_parser("build", help="build overlay after a matching holdout report exists")
    build_parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    build_parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    build_parser.add_argument("--output", type=Path, default=DEFAULT_OVERLAY)
    build_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    if args.command == "validate":
        if not (0.5 <= args.shape_iou_min <= 1.0 and 0.5 <= args.area_ratio_min <= 1.0
                and 0.5 <= args.dimension_ratio_min <= 1.0 and 1.0 <= args.max_distance_m <= 500.0):
            raise ValueError("profile parameters out of safe bounds")
        PROFILE["normalized_shape_iou_min"] = args.shape_iou_min
        PROFILE["area_ratio_min"] = args.area_ratio_min
        PROFILE["long_dimension_ratio_min"] = args.dimension_ratio_min
        PROFILE["short_dimension_ratio_min"] = args.dimension_ratio_min
        PROFILE["max_polygon_distance_m"] = args.max_distance_m
        PROFILE["max_centroid_distance_m"] = args.max_distance_m
        PROFILE["profile_id"] = f"shape-local-iou{args.shape_iou_min:.2f}-area{args.area_ratio_min:.2f}-d{args.max_distance_m:.0f}"
        _, report, _ = validate(args)
        print(json.dumps({
            "report": str(args.report),
            "source_sha256": report["source_sha256"],
            "holdout_count": report["holdout_count"],
            "matches": report["estimated_matches_count"],
            "coverage": report["candidate_coverage"],
            "similar_matches": report["similar_match_count"],
            "exact_matches": report["exact_match_count"],
            "estimator": report["estimator_vs_truth_all_matches"],
            "fallback15": report["fallback_15m_vs_truth_matched_subset"],
        }, ensure_ascii=False, indent=2))
        return 0
    report_path = args.report.resolve()
    if not report_path.is_file():
        raise FileNotFoundError(f"run validate first: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    PROFILE.update(report["method"])
    count, digest = build_overlay(args.input.resolve(), report, args)
    print(json.dumps({"overlay_rows": count, "overlay_sha256": digest, "manifest": str(args.manifest)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
