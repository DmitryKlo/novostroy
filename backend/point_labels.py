"""Bounded source assertions for the point map, joined by exact OSM identity."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend import models
from backend.store import ROOT


OSM_SOURCE = ROOT / "data/property/osm_construction/osm_moscow_residential_construction_2026-10-08.jsonl"
MAX_SOURCE_BYTES = 5_000_000
MAX_EVALUATIONS_BYTES = 10_000_000
COMPLEX_MARKER = re.compile(r"(?:\bЖК\b|\bЖилой\s+комплекс\b)", re.IGNORECASE)
UNSAFE_TEXT = re.compile(r"[<>\\\x00-\x1f]|https?://|www\.", re.IGNORECASE)
# Direct, typed and versioned member relation from the pinned source audit.
NAMED_PARENT_RELATIONS = {
    "way/1450746348/v3": 'ЖК "Зиларт-Спарк" (дом 21)',
}
PARENT_AUDIT_SOURCE_SHA256 = "46e8bb89fd189262a1f27c3c8cd8858d8450d565e8a751b00d54d4cf520b65e2"


def _source_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > 160 or UNSAFE_TEXT.search(value):
        return None
    return value


def _address(tags: dict[str, Any]) -> tuple[str | None, str | None]:
    street = _source_text(tags.get("addr:street"))
    house = _source_text(tags.get("addr:housenumber"))
    place = _source_text(tags.get("addr:place"))
    if street and house:
        return f"{street}, {house}", "osm_street_house"
    if place and house:
        return f"{place}, {house}", "osm_place_house"
    if street:
        return street, "osm_street"
    if place:
        return place, "osm_place"
    return None, None


def _complex_label(source_id: str, name: Any, tags: dict[str, Any], *,
                   parent_audit_matches: bool = True) -> tuple[str | None, str | None]:
    place = _source_text(tags.get("addr:place"))
    if place and COMPLEX_MARKER.search(place):
        return place, "osm_addr_place"
    name = _source_text(name)
    if name and COMPLEX_MARKER.search(name):
        return name, "osm_name"
    parent = NAMED_PARENT_RELATIONS.get(source_id) if parent_audit_matches else None
    if parent:
        return parent, "osm_parent_relation"
    return None, None


def _read_checked_jsonl(path: Path, expected_sha: str, max_bytes: int) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("Invalid point-label source hash")
    if path.stat().st_size > max_bytes:
        raise ValueError("Point-label source exceeds size limit")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected_sha:
        raise ValueError("Point-label source hash mismatch")
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Point-label source has a non-object row")
    return rows


@lru_cache(maxsize=4)
def _load_labels(run_id: str, source_mtime_ns: int, source_size: int,
                 evaluations_mtime_ns: int, evaluations_size: int,
                 manifest_mtime_ns: int) -> dict[str, dict[str, str | None]]:
    manifest = json.loads((models.MODELS_ROOT / run_id / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id:
        raise ValueError("Point-label run identity mismatch")
    source_sha = manifest.get("input_hashes", {}).get("osm_construction_jsonl")
    eval_sha = manifest.get("source_evaluations_sha256")
    source_rows = _read_checked_jsonl(OSM_SOURCE, source_sha, MAX_SOURCE_BYTES)
    eval_rows = _read_checked_jsonl(models.MODELS_ROOT / run_id / "source_evaluations.jsonl",
                                    eval_sha, MAX_EVALUATIONS_BYTES)
    source_by_id: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        source_id = row.get("source_record_id")
        expected_id = f"{row.get('osm_type')}/{row.get('osm_id')}/v{row.get('osm_version')}"
        if (not isinstance(source_id, str) or source_id != expected_id
                or source_id in source_by_id):
            raise ValueError("Invalid or duplicate typed OSM source identity")
        source_by_id[source_id] = row
    if len(source_by_id) != manifest.get("source_counts", {}).get("osm_construction"):
        raise ValueError("Point-label source count mismatch")
    result: dict[str, dict[str, str | None]] = {}
    used_source_ids: set[str] = set()
    for row in eval_rows:
        if row.get("source_group") != "osm_construction":
            continue
        candidate_id = row.get("candidate_id")
        source_id = row.get("source_record_id")
        source = source_by_id.get(source_id)
        if (not isinstance(candidate_id, str) or models.CANDIDATE_ID_RE.fullmatch(candidate_id) is None
                or source is None or source_id in used_source_ids or candidate_id in result
                or row.get("source_version") != source.get("source_version")):
            raise ValueError("Invalid point-label candidate/source join")
        used_source_ids.add(source_id)
        tags = json.loads(source.get("address_json", "{}"))
        if not isinstance(tags, dict):
            raise ValueError("Invalid OSM address tags")
        address, address_basis = _address(tags)
        complex_label, complex_label_basis = _complex_label(
            source_id, source.get("name"), tags,
            parent_audit_matches=source_sha == PARENT_AUDIT_SOURCE_SHA256)
        result[candidate_id] = {
            "address": address, "address_basis": address_basis,
            "complex_label": complex_label, "complex_label_basis": complex_label_basis,
        }
    if used_source_ids != set(source_by_id):
        raise ValueError("Point-label source evaluation coverage mismatch")
    return result


def load_point_labels(run: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    """Resolve only the active run's exact source identities; cache validated bytes."""
    run_id = run["model_run_id"]
    manifest_path = models.MODELS_ROOT / run_id / "manifest.json"
    evaluations_path = models.MODELS_ROOT / run_id / "source_evaluations.jsonl"
    source_stat = OSM_SOURCE.stat()
    eval_stat = evaluations_path.stat()
    labels = _load_labels(run_id, source_stat.st_mtime_ns, source_stat.st_size,
                          eval_stat.st_mtime_ns, eval_stat.st_size, manifest_path.stat().st_mtime_ns)
    mapped_ids = {item["candidate_id"] for item in run["items"]
                  if item["entity_type"] == "mapped_construction"}
    if set(labels) != mapped_ids:
        raise ValueError("Point labels do not match active mapped candidates")
    return labels
