"""Assess every pinned model-candidate source row and stage selected OSM geometry.

No network access, nearest-neighbour project join, fabricated unit inventory, or
unlicensed project facade is used. Run with ``python -m sources.models.build``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq
from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon
from shapely.strtree import STRtree

from pipeline.build_tiles import enu_transform, region, save_bytes
from sources.city.local_address_index import address_key_from_tags
from sources.models.geometry import BASE_HEIGHT_M, ModelMesh, polygon_parts
from sources.property.estateline import _parse_source_address

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "data/property"
OUTPUT = ROOT / "data/models"
ASSETS = ROOT / "assets"
RULES_VERSION = "model-candidates-v1.3"
SCHEMA_VERSION = "model-candidates/1"
OSM_PARQUET = SOURCES / "osm_construction/osm_moscow_residential_construction_2026-10-08.parquet"
OSM_JSONL = OSM_PARQUET.with_suffix(".jsonl")
CLEAN_PARTS = ROOT / "data/city/osm_overture_clean_multilevel_parts_full_261009.parquet"
INPUTS = {
    "estateline_projects": SOURCES / "estateline/projects.jsonl",
    "estateline_review": SOURCES / "estateline/review_required.jsonl",
    "osm_construction_parquet": OSM_PARQUET,
    "osm_construction_jsonl": OSM_JSONL,
    "validated_clean_parts": CLEAN_PARTS,
    "minstroy_residential_events": SOURCES / "minstroy/residential_registry.jsonl",
    "minstroy_moscow_text_audit": SOURCES / "minstroy/moscow_housing_mentions_audit.json",
}
REASONS_RU = {
    "no_measured_geometry": "Для этой записи нет подтверждённого контура здания.",
    "unverified_location": "Местоположение объекта не подтверждено для показа модели.",
    "rights_unconfirmed": "Право на публикацию индивидуальной модели не подтверждено.",
    "no_corpus_link": "Связь с конкретным корпусом не подтверждена.",
    "not_moscow_residential": "Запись не описывает московский жилой строящийся объект.",
    "mapped_status_not_current_verified": "Статус стройки взят из карты и не проверен на текущую дату.",
    "source_snapshot_historical": "Источник фиксирует историческое наблюдение, а не текущую стадию.",
    "height_unknown_flat_only": "Высота неизвестна; показан только плоский контур.",
    "height_from_floors_estimate": "Высота для показа оценена по числу этажей, не измерена.",
    "height_tag_conflict_flat_only": "Высотные теги расходятся; показан только плоский контур.",
    "invalid_height_tag_flat_only": "Высотный тег не удалось безопасно разобрать; показан плоский контур.",
    "geometry_invalid": "Исходный контур геометрически некорректен.",
    "geometry_missing": "Исходный контур отсутствует.",
    "geometry_outside_moscow": "Контур вне проверенной московской выборки.",
    "source_duplicate_review": "В источнике возможен повтор того же контура; требуется проверка.",
    "scope_review_required": "Назначение объекта требует ручной проверки.",
    "ambiguous_match": "Найдено несколько возможных контуров; связь с корпусом требует проверки.",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def candidate_id(namespace: str, key: str) -> str:
    return "cand_" + digest(f"{namespace}\0{key}".encode("utf-8"))[:24]


def _title(reason_codes: list[str]) -> str:
    return " ".join(REASONS_RU[code] for code in reason_codes if code in REASONS_RU)


def _height(tags: dict[str, Any]) -> tuple[dict[str, Any], float | None, list[str]]:
    """Keep mapped height, mapped floors and display estimate as distinct facts."""
    raw_height = tags.get("height")
    raw_levels = tags.get("building:levels")
    mapped_height = None
    mapped_levels = None
    codes: list[str] = []
    if raw_height is not None:
        match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:m|м)?\s*", str(raw_height), re.I)
        if match:
            mapped_height = float(match.group(1).replace(",", "."))
            if not 1 <= mapped_height <= 400:
                mapped_height = None
        if mapped_height is None:
            codes.append("invalid_height_tag_flat_only")
    if raw_levels is not None:
        match = re.fullmatch(r"\s*(\d{1,3})\s*", str(raw_levels))
        if match:
            mapped_levels = int(match.group(1))
            if not 1 <= mapped_levels <= 100:
                mapped_levels = None
        if mapped_levels is None and "invalid_height_tag_flat_only" not in codes:
            codes.append("invalid_height_tag_flat_only")
    estimated = mapped_levels * 3.0 if mapped_levels is not None else None
    conflict = (mapped_height is not None and estimated is not None
                and abs(mapped_height - estimated) > max(10.0, estimated * 0.35))
    if conflict:
        codes.append("height_tag_conflict_flat_only")
    elif mapped_height is None and estimated is not None:
        codes.append("height_from_floors_estimate")
    elif mapped_height is None and estimated is None and not codes:
        codes.append("height_unknown_flat_only")
    render_height = None if conflict or "invalid_height_tag_flat_only" in codes else (mapped_height or estimated)
    return ({"mapped_height_m": mapped_height, "mapped_levels": mapped_levels,
             "estimated_render_height_m": estimated if mapped_height is None else None,
             "render_height_m": render_height,
             "render_height_basis": ("mapped_height_tag" if render_height == mapped_height and mapped_height is not None
                                     else "mapped_floors_times_3m" if render_height is not None else None)},
            render_height, codes)


def _preview(geometry: Polygon | MultiPolygon) -> list[list[float]] | None:
    if not isinstance(geometry, Polygon) or geometry.interiors:
        return None
    points = list(geometry.exterior.coords)
    if len(points) > 128:
        return None
    return [[float(x), float(y)] for x, y, *_ in points]


def _preview_geometry(geometry: Polygon | MultiPolygon) -> dict[str, Any] | None:
    """Bounded exact GeoJSON geometry, retaining multipart shapes and holes."""
    polygons = polygon_parts(geometry)
    vertices = sum(len(ring.coords) for polygon in polygons
                   for ring in (polygon.exterior, *polygon.interiors))
    if vertices > 512:
        return None
    def rings(polygon: Polygon) -> list[list[list[float]]]:
        return [[[float(x), float(y)] for x, y, *_ in ring.coords]
                for ring in (polygon.exterior, *polygon.interiors)]
    return ({"type": "Polygon", "coordinates": rings(polygons[0])}
            if isinstance(geometry, Polygon)
            else {"type": "MultiPolygon", "coordinates": [rings(polygon) for polygon in polygons]})


def _check_geometry(geometry: Any) -> list[str]:
    if geometry is None or geometry.is_empty:
        return ["geometry_missing"]
    if not isinstance(geometry, (Polygon, MultiPolygon)) or not geometry.is_valid or geometry.area <= 0:
        return ["geometry_invalid"]
    west, south, east, north = geometry.bounds
    if not all(math.isfinite(value) for value in geometry.bounds):
        return ["geometry_invalid"]
    # The input is already clipped to the pinned Moscow relation. This broad
    # envelope catches accidental CRS swaps, not a substitute for that clip.
    if not (36.6 < west < east < 38.2 and 54.9 < south < north < 56.3):
        return ["geometry_outside_moscow"]
    return []


def _asset(geometry: Polygon | MultiPolygon, height_m: float | None,
           cache: Path, public: Path, public_root: str,
           files: dict[str, dict[str, Any]],
           validated_parts: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    if validated_parts:
        height_m = max(part["height_top_m"] for part in validated_parts)
    west, south, east, north = geometry.bounds
    origin_lon, origin_lat = (west + east) / 2, (south + north) / 2

    def emit_mesh(model_height: float | None) -> tuple[str, ModelMesh]:
        mesh = ModelMesh(origin_lon, origin_lat, model_height)
        if model_height is not None and validated_parts:
            for part in validated_parts:
                mesh.add(part["geometry"], top_height_m=BASE_HEIGHT_M + part["height_top_m"],
                         base_height_m=BASE_HEIGHT_M + part["vertical_base_m"])
        else:
            mesh.add(geometry)
        blob = mesh.to_glb()
        sha = digest(blob)
        name = f"model_{sha}.glb"
        cache_path = cache / name
        if cache_path.exists():
            if file_sha(cache_path) != sha:
                raise ValueError(f"Content cache hash mismatch: {name}")
        else:
            save_bytes(cache_path, blob)
        if not (public / name).exists():
            os.link(cache_path, public / name)
        files[name] = {"path": name, "sha256": sha, "bytes": len(blob)}
        return name, mesh

    flat_name, flat = emit_mesh(None)
    if flat.holes != sum(len(part.interiors) for part in polygon_parts(geometry)):
        raise ValueError("Courtyard count changed in flat mesh")
    children: list[dict[str, Any]] = []
    volume_bytes = 0
    if height_m is not None:
        volume_name, volume = emit_mesh(height_m)
        volume_bytes = files[volume_name]["bytes"]
        children = [{"boundingVolume": {"region": region(west, south, east, north, height_m + BASE_HEIGHT_M + 2)},
                     "geometricError": 0, "content": {"uri": volume_name}}]
        if not validated_parts and volume.holes != flat.holes:
            raise ValueError("Courtyard count changed in massing")
    bound_top = (height_m or 0) + BASE_HEIGHT_M + 2
    tile = {"asset": {"version": "1.1", "tilesetVersion": "mapped-construction-models-v1"},
            "geometricError": max(20.0, min(80.0, (height_m or 0) / 4)) if height_m else 0,
            "root": {"boundingVolume": {"region": region(west, south, east, north, bound_top)},
                     "transform": enu_transform(origin_lon, origin_lat),
                     "geometricError": max(20.0, min(80.0, height_m / 4)) if height_m else 0,
                     "refine": "REPLACE", "content": {"uri": flat_name}, "children": children},
            "extras": {"entity_type": "mapped_construction", "depiction": "construction_outline" if height_m is None else "schematic_massing",
                       "source_ids_redacted": True, "attribution": "© OpenStreetMap contributors",
                       "not_approved_project_model": True}}
    blob = canonical(tile)
    sha = digest(blob)
    name = f"model_{sha}.tileset.json"
    save_bytes(public / name, blob)
    files[name] = {"path": name, "sha256": sha, "bytes": len(blob)}
    return ({"tileset_url": f"/assets/{public_root}/{name}", "kind": "mapped_construction",
             "model_quality": "mapped_footprint" if height_m is None else "mapped_estimate",
             "attribution": "© OpenStreetMap contributors", "public_attribution": "© OpenStreetMap contributors; ODbL 1.0",
             "content_hash": sha,
             "rights_evidence": {"license": "ODbL-1.0", "display_cleared": True,
                                 "attribution_required": True}},
            {"flat_glb_bytes": files[flat_name]["bytes"], "volume_glb_bytes": volume_bytes,
             "flat_triangles": flat.triangles, "holes": flat.holes})


def _assessment(identifier: str, registry_id: str | None, entity_type: str,
                decision: str, reasons: list[str], *, name: str | None = None,
                address: str | None = None, location: list[float] | None = None,
                bbox: list[float] | None = None, geometry_as_of: str | None = None,
                height_evidence: dict[str, Any] | None = None,
                selected_model: dict[str, Any] | None = None,
                preview: list[list[float]] | None = None,
                preview_geometry: dict[str, Any] | None = None) -> dict[str, Any]:
    mapped = entity_type == "mapped_construction"
    asset_ready = selected_model is not None
    return {"candidate_id": identifier, "registry_id": registry_id,
            "entity_type": entity_type, "name": name, "address": address,
            "decision": decision, "asset_readiness": "ready_to_render" if asset_ready else "unavailable",
            "project_link_status": "unlinked",
            "reason_codes": sorted(set(reasons)), "explanation_ru": _title(reasons),
            "depiction": (("schematic_massing" if height_evidence and height_evidence.get("render_height_m") is not None
                           else "construction_outline") if mapped and asset_ready else "none"),
            "model_quality": selected_model["model_quality"] if selected_model else None,
            "geometry_quality": "mapped_footprint" if mapped and asset_ready else None,
            "geometry_as_of": geometry_as_of, "location": location, "bbox": bbox,
            "height_evidence": height_evidence, "preview_footprint": preview,
            "preview_geometry": preview_geometry,
            "selected_model": selected_model}


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> tuple[int, str]:
    count = 0
    with path.open("wb") as stream:
        for record in records:
            stream.write(canonical(record))
            count += 1
        stream.flush()
        os.fsync(stream.fileno())
    return count, file_sha(path)


def build(output_root: Path = OUTPUT, asset_root: Path = ASSETS) -> dict[str, Any]:
    input_hashes = {key: file_sha(path) for key, path in INPUTS.items()}
    run_hash = digest(canonical({"inputs": input_hashes, "rules": RULES_VERSION}))[:16]
    run_id = f"models-v1-{run_hash}"
    public_root = f"public-project-models-v1-{run_hash}"
    final_run = output_root / run_id
    final_public = asset_root / public_root
    if final_run.exists() and final_public.exists():
        manifest = json.loads((final_run / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("input_hashes") != input_hashes or manifest.get("public_root") != public_root:
            raise ValueError("Existing immutable model run has different inputs")
        for name in ("assessments", "candidates", "source_evaluations"):
            if file_sha(final_run / f"{name}.jsonl") != manifest[f"{name}_sha256"]:
                raise ValueError(f"Existing {name} checksum mismatch")
        if file_sha(final_public / "public_manifest.json") != manifest["public_manifest_sha256"]:
            raise ValueError("Existing public model manifest checksum mismatch")
        return manifest
    # A hard stop can land after the complete public tree is atomically renamed
    # but before the private run rename. Rebuild to staging and adopt the
    # already complete public bytes only if every file is identical.
    orphan_public = final_public.exists() and not final_run.exists()
    if final_run.exists() and not final_public.exists():
        raise ValueError("Immutable model run exists without its public assets")
    output_root.mkdir(parents=True, exist_ok=True)
    asset_root.mkdir(parents=True, exist_ok=True)
    cache = output_root / "cache/geometry"
    cache.mkdir(parents=True, exist_ok=True)
    staging_run = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root))
    staging_public = Path(tempfile.mkdtemp(prefix=f".{public_root}.", dir=asset_root))
    try:
        assessments: list[dict[str, Any]] = []
        evaluations: list[dict[str, Any]] = []
        hypotheses: list[dict[str, Any]] = []
        files: dict[str, dict[str, Any]] = {}
        attempt_logs: dict[str, dict[str, Any]] = {}
        source_counts: Counter[str] = Counter()
        geometry_counts: Counter[str] = Counter()
        estate_keys: dict[tuple[str | None, ...], list[str]] = defaultdict(list)
        estate_points: list[tuple[str, Polygon | MultiPolygon | Any]] = []
        estate_osm_ids: dict[tuple[str, int], list[str]] = defaultdict(list)
        estate_records = jsonl(INPUTS["estateline_projects"])
        review_records = jsonl(INPUTS["estateline_review"])
        for source_label, rows, accepted in (("estateline_accepted", estate_records, True),
                                              ("estateline_review", review_records, False)):
            for row in rows:
                key = row["registry_id"]
                identifier = candidate_id(source_label, key)
                public_registry_id = "registry-" + digest(key.encode("utf-8"))[:24] if accepted else None
                loc = row.get("location") if accepted and row.get("location_quality") == "verified" else None
                reasons = ["no_measured_geometry", "rights_unconfirmed", "source_snapshot_historical"]
                if loc is None:
                    reasons.append("unverified_location")
                if accepted:
                    reasons.append("no_corpus_link")
                if not accepted:
                    reasons.append("scope_review_required")
                assessment = _assessment(identifier, public_registry_id,
                                         "registry_project" if accepted else "registry_review_record",
                                         "unavailable" if accepted else "review", reasons,
                                         name=row.get("name"), address=row.get("address"), location=loc,
                                         geometry_as_of=row.get("latest_known_date") or row.get("source_report_period_end"),
                                         height_evidence={"mapped_height_m": None, "mapped_levels": row.get("floors"),
                                                          "estimated_render_height_m": None, "render_height_m": None,
                                                          "render_height_basis": None})
                assessments.append(assessment)
                evaluations.append({"candidate_id": identifier, "source_group": source_label,
                                    "source_record_id": key, "source_url": row.get("provenance", {}).get("source_url"),
                                    "rights_status": row.get("provenance", {}).get("rights_status"),
                                    "decision": assessment["decision"], "reason_codes": assessment["reason_codes"]})
                source_counts[source_label] += 1
                if accepted:
                    address_key, parse_reason = _parse_source_address(row.get("address") or "")
                    attempt_logs[identifier] = {
                        "estate_project_geometry": "absent_in_source",
                        "strict_address_key": "parsed" if address_key is not None else parse_reason,
                        "exact_osm_location_object_matches": 0,
                        "strict_osm_address_matches": 0,
                        "verified_point_in_polygon_matches": 0,
                        "validated_parts_parent_matches": 0,
                        "donor_geometry": "not_admitted_without_verified_project_corpus_identity",
                    }
                    if address_key is not None:
                        estate_keys[address_key].append(identifier)
                    if loc is not None:
                        from shapely.geometry import Point
                        estate_points.append((identifier, Point(*loc)))
                    locator = row.get("provenance", {}).get("location_source") or {}
                    if locator.get("osm_type") and locator.get("osm_id"):
                        estate_osm_ids[(locator["osm_type"], int(locator["osm_id"]))].append(identifier)
                else:
                    attempt_logs[identifier] = {
                        "source_scope": "review_required_before_project_geometry_search",
                        "estate_project_geometry": "absent_in_source",
                        "donor_geometry": "not_admitted_without_residential_and_corpus_identity",
                    }

        osm_rows = pq.read_table(INPUTS["osm_construction_parquet"]).to_pylist()
        part_id_rows = pq.read_table(INPUTS["validated_clean_parts"],
                                     columns=["source_osm_type", "source_osm_id", "source_osm_version"]).to_pylist()
        clean_part_source_ids = {(part["source_osm_type"], int(part["source_osm_id"]),
                                  int(part["source_osm_version"])) for part in part_id_rows}
        osm_index = {(row["osm_type"], int(row["osm_id"]), int(row["osm_version"])): row for row in osm_rows}
        audit_rows = jsonl(INPUTS["osm_construction_jsonl"])
        if len(osm_rows) != len(audit_rows) or len(osm_index) != len(osm_rows):
            raise ValueError("OSM polygon/audit record count or typed identity mismatch")
        osm_geometries: list[Any] = []
        osm_ids: list[str] = []
        for audit in sorted(audit_rows, key=lambda row: (row["osm_type"], int(row["osm_id"]))):
            key = (audit["osm_type"], int(audit["osm_id"]), int(audit["osm_version"]))
            row = osm_index.get(key)
            if row is None or row["source_record_id"] != audit["source_record_id"]:
                raise ValueError("OSM audit and GeoParquet typed identity mismatch")
            identifier = candidate_id("osm_construction", f"{key[0]}/{key[1]}")
            raw_wkb = row.get("geometry")
            geometry = wkb.loads(raw_wkb) if raw_wkb else None
            reasons = _check_geometry(geometry)
            tags = json.loads(row.get("raw_tags") or "{}")
            if tags.get("building") != "construction" or tags.get("construction") not in {"apartments", "residential"}:
                reasons.append("not_moscow_residential")
            if row.get("rights_status") != "cleared" or row.get("rights_license") != "ODbL-1.0":
                reasons.append("rights_unconfirmed")
            height_info, render_height, height_codes = _height(tags)
            reasons.extend(height_codes)
            reasons.append("mapped_status_not_current_verified")
            attempt_logs[identifier] = {
                "typed_osm_identity": "verified_against_audit_jsonl",
                "source_polygon": "valid" if not _check_geometry(geometry) else "failed_geometry_gate",
                "display_rights": "ODbL-1.0_cleared" if row.get("rights_status") == "cleared" else "unconfirmed",
                "height_tag": "mapped" if height_info["mapped_height_m"] is not None else "absent_or_invalid",
                "above_ground_levels": "mapped" if height_info["mapped_levels"] is not None else "absent_or_invalid",
                "validated_parts_parent_match": False,
                "validated_part_source_id_matches": int(key in clean_part_source_ids),
                "donor_geometry": "not_used",
            }
            ready = not any(code in reasons for code in ("geometry_missing", "geometry_invalid", "geometry_outside_moscow",
                                                         "not_moscow_residential", "rights_unconfirmed"))
            selected = None
            metrics: dict[str, int] = {}
            bbox = list(geometry.bounds) if geometry is not None and ready else None
            location = None
            preview = None
            preview_geometry = None
            if ready:
                point = geometry.representative_point()
                location = [point.x, point.y]
                preview = _preview(geometry)
                preview_geometry = _preview_geometry(geometry)
                selected, metrics = _asset(geometry, render_height, cache, staging_public, public_root, files)
                geometry_counts["ready_flat"] += 1
                if render_height is not None:
                    geometry_counts["ready_volume"] += 1
                    geometry_counts[height_info["render_height_basis"]] += 1
                else:
                    geometry_counts["flat_only"] += 1
                geometry_counts["holes"] += metrics["holes"]
                geometry_counts["flat_triangles"] += metrics["flat_triangles"]
            assessment = _assessment(identifier, None, "mapped_construction",
                                     "ready" if ready else "review", reasons,
                                     name=row.get("name"), address=None, location=location, bbox=bbox,
                                     geometry_as_of=row.get("source_updated_at"),
                                     height_evidence=height_info, selected_model=selected, preview=preview,
                                     preview_geometry=preview_geometry)
            assessments.append(assessment)
            evaluations.append({"candidate_id": identifier, "source_group": "osm_construction",
                                "source_record_id": row["source_record_id"], "source_url": row.get("source_url"),
                                "rights_status": row.get("rights_status"), "source_version": row.get("source_version"),
                                "geometry_sha256": digest(raw_wkb) if raw_wkb else None,
                                "decision": assessment["decision"], "reason_codes": assessment["reason_codes"],
                                "geometry_metrics": metrics})
            source_counts["osm_construction"] += 1
            if ready:
                osm_geometries.append(geometry)
                osm_ids.append(identifier)
                for registry_candidate in estate_osm_ids.get((key[0], key[1]), []):
                    hypotheses.append({"kind": "project_link", "left_candidate_id": registry_candidate,
                                       "right_candidate_id": identifier, "evidence": "same_typed_osm_location_object",
                                       "decision": "review", "project_identity_verified": False})
                address_key = address_key_from_tags(tags)
                if address_key is not None:
                    for registry_candidate in estate_keys.get(address_key, []):
                        hypotheses.append({"kind": "project_link", "left_candidate_id": registry_candidate,
                                           "right_candidate_id": identifier, "evidence": "strict_exact_address_tuple",
                                           "decision": "review", "project_identity_verified": False})

        if osm_geometries:
            tree = STRtree(osm_geometries)
            for registry_candidate, point in estate_points:
                for index in tree.query(point, predicate="intersects"):
                    hypotheses.append({"kind": "project_link", "left_candidate_id": registry_candidate,
                                       "right_candidate_id": osm_ids[int(index)],
                                       "evidence": "verified_address_point_inside_mapped_polygon",
                                       "decision": "review", "project_identity_verified": False})
            # Distinct OSM objects can be overlapping way/relation geometries.
            # Flag near-identical shapes without deleting a source row.
            for i, shape in enumerate(osm_geometries):
                for j in tree.query(shape, predicate="intersects"):
                    j = int(j)
                    if j <= i:
                        continue
                    other = osm_geometries[j]
                    union = shape.union(other).area
                    if union and shape.intersection(other).area / union >= 0.95:
                        hypotheses.append({"kind": "possible_osm_duplicate", "left_candidate_id": osm_ids[i],
                                           "right_candidate_id": osm_ids[j], "evidence": "polygon_iou_ge_0_95",
                                           "decision": "review", "project_identity_verified": False})

        permits = jsonl(INPUTS["minstroy_residential_events"])
        audit_mentions = json.loads(INPUTS["minstroy_moscow_text_audit"].read_text(encoding="utf-8"))["records"]
        permit_numbers: dict[str, int] = defaultdict(int)
        for row in permits + audit_mentions:
            source = row["source"]
            key = source["source_record_id"]
            identifier = candidate_id("minstroy_event", key)
            mention = row in audit_mentions
            permit_number = (row.get("registry_event") or {}).get("permit_number") or row.get("permit_number_source_text")
            if permit_number:
                permit_numbers[permit_number] += 1
            assessment = _assessment(identifier, None, "external_event", "rejected",
                                     ["not_moscow_residential", "no_measured_geometry"],
                                     name=(row.get("project_reference") or {}).get("name") if not mention else None,
                                     geometry_as_of=row.get("dataset_release_date"))
            assessments.append(assessment)
            evaluations.append({"candidate_id": identifier,
                                "source_group": "minstroy_text_audit" if mention else "minstroy_residential_event",
                                "source_record_id": key, "source_url": source.get("url"),
                                "permit_number": permit_number, "rights_status": source.get("rights_status"),
                                "decision": assessment["decision"], "reason_codes": assessment["reason_codes"]})
            source_counts["minstroy_text_audit" if mention else "minstroy_residential_event"] += 1
            attempt_logs[identifier] = {
                "event_identity": "source_record_id_preserved",
                "moscow_residential_scope": "incidental_gas_pipeline_mention" if mention else "sochi_event",
                "individual_polygon": "absent_in_source",
                "donor_geometry": "not_admitted_for_non_moscow_or_non_building_event",
            }

        if dict(source_counts) != {"estateline_accepted": 1473, "estateline_review": 59,
                                   "osm_construction": 756, "minstroy_residential_event": 27,
                                   "minstroy_text_audit": 2}:
            raise ValueError(f"Pinned input counts changed: {dict(source_counts)}")
        if any(log.get("validated_part_source_id_matches") for log in attempt_logs.values()):
            raise ValueError("Clean part source-ID intersection changed; parent identity requires new audit")
        assessments.sort(key=lambda row: row["candidate_id"])
        evaluations.sort(key=lambda row: row["candidate_id"])
        hypotheses = sorted({(row["kind"], row["left_candidate_id"], row["right_candidate_id"], row["evidence"]):
                             row for row in hypotheses}.values(),
                            key=lambda row: (row["kind"], row["left_candidate_id"], row["right_candidate_id"], row["evidence"]))
        if len(assessments) != 2317 or len({row["candidate_id"] for row in assessments}) != 2317:
            raise ValueError("Assessment IDs are missing or duplicated")
        linked_registry_candidates: dict[str, set[str]] = defaultdict(set)
        for hypothesis in hypotheses:
            if hypothesis["kind"] != "project_link":
                continue
            linked_registry_candidates[hypothesis["left_candidate_id"]].add(hypothesis["right_candidate_id"])
            counts_key = {
                "same_typed_osm_location_object": "exact_osm_location_object_matches",
                "strict_exact_address_tuple": "strict_osm_address_matches",
                "verified_address_point_inside_mapped_polygon": "verified_point_in_polygon_matches",
            }[hypothesis["evidence"]]
            attempt_logs[hypothesis["left_candidate_id"]][counts_key] += 1
        for record in assessments:
            links = linked_registry_candidates.get(record["candidate_id"])
            if links and record["entity_type"] == "registry_project":
                record["decision"] = "review"
                record["asset_readiness"] = "review"
                record["project_link_status"] = "review"
                if len(links) > 1:
                    record["reason_codes"] = sorted({*record["reason_codes"], "ambiguous_match"})
                record["explanation_ru"] = _title(record["reason_codes"])
            record["attempt_log"] = attempt_logs[record["candidate_id"]]
        assessment_by_id = {record["candidate_id"]: record for record in assessments}
        for record in evaluations:
            record["decision"] = assessment_by_id[record["candidate_id"]]["decision"]
            record["reason_codes"] = assessment_by_id[record["candidate_id"]]["reason_codes"]
            record["attempt_log"] = attempt_logs[record["candidate_id"]]
        counts = {}
        for name, records in (("assessments", assessments), ("candidates", hypotheses),
                              ("source_evaluations", evaluations)):
            count, sha = _write_jsonl(staging_run / f"{name}.jsonl", records)
            counts[f"{name}_count"] = count
            counts[f"{name}_sha256"] = sha
        public_manifest = {"schema_version": "public-project-models/1", "run_id": run_id,
                           "public_redacted_source_ids": True,
                           "attribution": "© OpenStreetMap contributors", "license": "ODbL-1.0",
                           "model_count": geometry_counts["ready_flat"],
                           "files": sorted(files.values(), key=lambda row: row["path"])}
        save_bytes(staging_public / "public_manifest.json", canonical(public_manifest))
        manifest = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                    "registry_snapshot_id": f"building-registry-{input_hashes['estateline_projects'][:16]}",
                    "rules_version": RULES_VERSION, "checked_at": datetime.now(timezone.utc).isoformat(),
                    "input_hashes": input_hashes, "source_counts": dict(source_counts),
                    "records_total": len(assessments),
                    "decision_counts": dict(Counter(row["decision"] for row in assessments)),
                    "asset_readiness_counts": dict(Counter(row["asset_readiness"] for row in assessments)),
                    "geometry_counts": dict(geometry_counts),
                    "link_hypotheses_count": len(hypotheses),
                    "registry_rows_with_link_hypotheses": len(linked_registry_candidates),
                    "possible_osm_duplicate_pairs": sum(row["kind"] == "possible_osm_duplicate" for row in hypotheses),
                    "repeated_permit_number_groups": sum(value > 1 for value in permit_numbers.values()),
                    "public_root": public_root,
                    "public_manifest_sha256": file_sha(staging_public / "public_manifest.json"),
                    **counts}
        save_bytes(staging_run / "manifest.json", canonical(manifest))
        if orphan_public:
            existing = {path.relative_to(final_public).as_posix(): file_sha(path)
                        for path in final_public.rglob("*") if path.is_file()}
            rebuilt = {path.relative_to(staging_public).as_posix(): file_sha(path)
                       for path in staging_public.rglob("*") if path.is_file()}
            if existing != rebuilt:
                raise ValueError("Interrupted public model tree differs from deterministic retry")
        else:
            staging_public.rename(final_public)
        staging_run.rename(final_run)
        return manifest
    finally:
        shutil.rmtree(staging_run, ignore_errors=True)
        shutil.rmtree(staging_public, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    parser.add_argument("--asset-root", type=Path, default=ASSETS)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.output_root, arguments.asset_root), ensure_ascii=False,
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
