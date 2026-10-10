"""Build a source-bound media allowlist for one immutable developer menu.

This module never fetches a URL. The allowlist is private: the API exposes an
opaque media ID and serves only bytes validated against a pinned row.
"""

from __future__ import annotations

import argparse
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable
from urllib.parse import urlsplit

from backend import developer_map as map_reader
from backend import developer_units as menu_reader
from sources.property.developer_map import ROOT, _canonical, _file_sha, _fsync_dir, _rows, _sha


MENU_ROOT = ROOT / "data/property/developer_units/runs"
MAP_ROOT = ROOT / "data/property/developer_map/runs"
ALLOWED_HOSTS = frozenset({
    "cdn-dataout.trendagent.ru", "cdn.a101.ru", "cdn.fsk.ru",
})
DISPLAY_POLICY = "user_authorized_source_bound_preview/1"
PLAN_ROLES = frozenset({"apartment_plan", "furnished_apartment_plan", "floor_plan"})
MIME_BY_SUFFIX = {".svg": "image/svg+xml", ".png": "image/png",
                  ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".webp": "image/webp"}
LAKE_EVIDENCE = (ROOT / ".runtime/research/building-experience-20261010/facades/"
                 "the-lake-1")
DELIVERY_MANIFEST = (ROOT / ".runtime/research/developer-harvest/deliveries/"
                     "developer-delivery-919ca9ca87c416f4/manifest.json")


def _exact_url(raw: str, *, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> tuple[str, str]:
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("Invalid source media URL")
    value = urlsplit(raw)
    host = (value.hostname or "").lower()
    if (value.scheme != "https" or host not in allowed_hosts or value.username
            or value.password or value.port or value.query or value.fragment):
        raise ValueError("Source media URL violates exact host/HTTPS policy")
    if not value.path.startswith("/") or ".." in Path(value.path).parts:
        raise ValueError("Unsafe source media path")
    return host, MIME_BY_SUFFIX.get(Path(value.path).suffix.lower(), "application/octet-stream")


def _rows_by_id(path: Path, key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _rows(path):
        identifier = row.get(key)
        if not isinstance(identifier, str) or identifier in result:
            raise ValueError(f"Missing or duplicate {key}")
        result[identifier] = row
    return result


def plan_media_rows(map_run_id: str, unit_run_id: str) -> Iterable[dict[str, Any]]:
    """Yield one registry row per exact plan descriptor in a validated menu."""
    map_run = map_reader.load_staged(map_run_id)
    menu_reader.load_staged(unit_run_id, map_run)
    map_dir = MAP_ROOT / map_run_id
    menu_dir = MENU_ROOT / unit_run_id
    map_manifest = json.loads((map_dir / "manifest.json").read_text(encoding="utf-8"))
    menu_manifest = json.loads((menu_dir / "manifest.json").read_text(encoding="utf-8"))
    if (map_manifest.get("run_id") != map_run_id or
            menu_manifest.get("run_id") != unit_run_id or
            menu_manifest.get("map_manifest_sha256") != _file_sha(map_dir / "manifest.json")):
        raise ValueError("Media allowlist must pin the exact map/menu generation")
    points = _rows_by_id(map_dir / "points.jsonl", "registry_id")
    units = _rows_by_id(menu_dir / "units.jsonl", "unit_id")
    floors = _rows_by_id(menu_dir / "floors.jsonl", "floor_id")
    capture_path = (ROOT / ".runtime/research/building-experience-20261010/facades/"
                    "the-lake-1/unit_media_manifest.jsonl")
    captured_floor: dict[str, dict[str, Any]] = {}
    if menu_manifest.get("captures_sha256"):
        if not capture_path.is_file() or _file_sha(capture_path) != menu_manifest["captures_sha256"]:
            raise ValueError("Pinned source capture manifest is missing or changed")
        captured_floor = {row["source_url"]: row for row in _rows(capture_path)
                          if row.get("role") == "floor_plan"}
    seen: set[str] = set()
    for plan in _rows(menu_dir / "plans.jsonl"):
        role, unit_id, floor_id = plan.get("role"), plan.get("unit_id"), plan.get("floor_id")
        if role not in PLAN_ROLES or bool(unit_id) == bool(floor_id):
            raise ValueError("Plan lacks an exact supported unit/floor binding")
        registry_id = plan.get("registry_id")
        if registry_id not in points:
            raise ValueError("Plan references an absent building")
        if unit_id:
            target = units.get(unit_id)
            observed_at = target and target.get("observed_at")
            scope = "unit_plan"
        else:
            target = floors.get(floor_id)
            capture = captured_floor.get(plan.get("source_url"))
            if not capture or capture.get("registry_id") != registry_id:
                raise ValueError("Floor image lacks its exact pinned capture")
            observed_at = capture.get("observed_at")
            scope = "floor_plan"
        if target is None or target.get("registry_id") != registry_id or not observed_at:
            raise ValueError("Plan crosses building or lacks source observation date")
        source_url = plan.get("source_url")
        host, expected_mime = _exact_url(source_url)
        plan_id = plan.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.startswith("plan_"):
            raise ValueError("Invalid source plan ID")
        media_id = "media_" + _sha(f"{plan_id}\0{source_url}".encode("utf-8"))[:24]
        if media_id in seen:
            raise ValueError("Duplicate source media ID")
        seen.add(media_id)
        asset_sha = plan.get("asset_sha256")
        if asset_sha is not None:
            asset = menu_dir / "assets" / f"{asset_sha}.png"
            if plan.get("asset_mime") != "image/png" or not asset.is_file() or _file_sha(asset) != asset_sha:
                raise ValueError("Prepared menu asset is not hash-validated PNG")
        yield {"media_id": media_id, "registry_id": registry_id,
               "unit_id": unit_id, "floor_id": floor_id,
               "scope": scope, "role": role,
               "source_role": plan.get("source_role"),
               "source_scope": plan.get("source_scope"),
               "source_field": plan.get("source_field"),
               "source_url": source_url, "allowed_host": host,
               "expected_mime": expected_mime,
               "observed_at": observed_at,
               "source_evidence_sha256": plan["source_evidence_sha256"],
               "rights_status": plan.get("rights_status", "unknown"),
               "display_policy": DISPLAY_POLICY,
               "prepared_sha256": asset_sha,
               "prepared_mime": "image/png" if asset_sha else None,
               "prepared_bytes": asset.stat().st_size if asset_sha else None,
               "prepared_source": "menu_asset" if asset_sha else None}


def lake_gallery_rows(map_run_id: str) -> Iterable[dict[str, Any]]:
    """Bind the reviewed one-corpus Lake gallery, keeping its project scope."""
    map_dir = MAP_ROOT / map_run_id
    points = _rows_by_id(map_dir / "points.jsonl", "registry_id")
    source_manifest = json.loads((LAKE_EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    evidence_path = LAKE_EVIDENCE / "facade_candidates.jsonl"
    if (_file_sha(evidence_path) != next((item["sha256"] for item in
          source_manifest["files"] if item["path"] == evidence_path.name), None)):
        raise ValueError("Lake gallery evidence SHA mismatch")
    registry_id = source_manifest["registry_id"]
    point = points.get(registry_id)
    if (point is None or point.get("source_group") != "fsk" or
            point.get("publisher_building_id") != source_manifest["source_building_id"] or
            point.get("publisher_project_id") != source_manifest["project_id"]):
        raise ValueError("Lake gallery cannot bind to the active exact corpus")
    for source in _rows(evidence_path):
        if source.get("role") not in {"facade_photo", "render"}:
            continue
        if (source.get("registry_id") != registry_id or
                source.get("source_building_id") != point["publisher_building_id"] or
                source.get("project_id") != point["publisher_project_id"] or
                source.get("source_page_url") != "https://fsk.ru/the-lake" or
                (source.get("identity_evidence") or {}).get("source_scope") != "project_gallery"):
            raise ValueError("Gallery lacks reviewed single-corpus project provenance")
        source_url = source["source_url"]
        host, expected_mime = _exact_url(source_url)
        file_path = LAKE_EVIDENCE / source["local_file"]
        if (not file_path.is_file() or _file_sha(file_path) != source["sha256"] or
                file_path.stat().st_size > 10_000_000):
            raise ValueError("Lake gallery capture SHA/size mismatch")
        role = "facade_photo" if source["role"] == "facade_photo" else "project_rendering"
        yield {"media_id": "media_" + _sha(
                   f"{registry_id}\0{role}\0{source_url}".encode("utf-8"))[:24],
               "registry_id": registry_id, "unit_id": None, "floor_id": None,
               "scope": "project_gallery_single_corpus", "role": role,
               "source_role": source["role"], "source_scope": "project_gallery",
               "source_field": "official_fsk_project_gallery",
               "source_url": source_url, "allowed_host": host,
               "expected_mime": expected_mime,
               "observed_at": source["source_page_observed_at"],
               "source_evidence_sha256": _sha(_canonical(source)),
               "rights_status": "unknown", "display_policy": DISPLAY_POLICY,
               "project_gallery_label": source.get("source_label"),
               "identity_basis": "official_one_corpus_exact_address_project_gallery",
               "prepared_sha256": None, "prepared_mime": None,
               "prepared_bytes": None, "prepared_source": None,
               "known_capture_sha256": source["sha256"]}


def project_gallery_rows(map_run_id: str, unit_run_id: str) -> Iterable[dict[str, Any]]:
    """Offer one publisher project rendering per house, never as a corpus photo."""
    map_dir = MAP_ROOT / map_run_id
    menu_manifest = json.loads((MENU_ROOT / unit_run_id / "manifest.json").read_text(encoding="utf-8"))
    if _file_sha(DELIVERY_MANIFEST) != menu_manifest.get("delivery_manifest_sha256"):
        raise ValueError("Project media source delivery changed")
    delivery = json.loads(DELIVERY_MANIFEST.read_text(encoding="utf-8"))
    entry = delivery["files"]["projects_corpora"]
    projects_path = DELIVERY_MANIFEST.parent / entry["path"]
    if _file_sha(projects_path) != entry["sha256"]:
        raise ValueError("Project media rows changed")
    points = _rows_by_id(map_dir / "points.jsonl", "registry_id")
    wanted = {(point["source_group"], point["publisher_project_id"])
              for point in points.values()}
    project_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _rows(projects_path):
        key = row.get("source_group"), row.get("publisher_project_id")
        if row.get("record_type") != "project" or key not in wanted:
            continue
        if key in project_rows:
            raise ValueError("Multiple project media rows for one publisher project ID")
        project_rows[key] = row
    for point in points.values():
        row = project_rows.get((point["source_group"], point["publisher_project_id"]))
        if row is None:
            continue
        renderings = [item for item in row.get("media") or []
                      if item.get("scope") == "project" and
                      item.get("role") == "project_rendering"]
        if not renderings:
            continue
        source = renderings[0]
        source_url = source["url"]
        host, expected_mime = _exact_url(source_url)
        registry_id = point["registry_id"]
        yield {"media_id": "media_" + _sha(
                   f"{registry_id}\0project_rendering\0{source_url}".encode("utf-8"))[:24],
               "registry_id": registry_id, "unit_id": None, "floor_id": None,
               "scope": "project_gallery", "role": "project_rendering",
               "source_role": source["role"], "source_scope": "project",
               "source_field": source.get("source_field"),
               "source_url": source_url, "allowed_host": host,
               "expected_mime": expected_mime,
               "observed_at": row["observed_at"],
               "source_evidence_sha256": _sha(_canonical({"project": row["observation_id"],
                                                           "media": source})),
               "rights_status": source.get("rights_status", "unknown"),
               "display_policy": DISPLAY_POLICY,
               "project_gallery_label": "Визуализация ЖК",
               "identity_basis": "same_publisher_project_id_project_scope",
               "prepared_sha256": None, "prepared_mime": None,
               "prepared_bytes": None, "prepared_source": None}


def stage_plan_media(map_run_id: str, unit_run_id: str, output: Path) -> dict[str, Any]:
    """Write a private candidate JSONL and its reproducible source manifest."""
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.is_symlink() or output.exists():
        raise ValueError("Candidate output already exists")
    manifest_path = output.with_suffix(".manifest.json")
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ValueError("Candidate manifest already exists")
    menu_path = MENU_ROOT / unit_run_id / "manifest.json"
    map_path = MAP_ROOT / map_run_id / "manifest.json"
    digest = hashlib.sha256()
    count = 0
    gallery_count = 0
    project_gallery_count = 0
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".media-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            for row in chain(plan_media_rows(map_run_id, unit_run_id),
                             lake_gallery_rows(map_run_id),
                             project_gallery_rows(map_run_id, unit_run_id)):
                line = _canonical(row) + b"\n"
                stream.write(line)
                digest.update(line)
                count += 1
                gallery_count += row["scope"] == "project_gallery_single_corpus"
                project_gallery_count += row["scope"] == "project_gallery"
            if gallery_count != 3:
                raise ValueError("Lake gallery evidence does not contain exactly 3 source images")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
        _fsync_dir(output.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    manifest = {"schema_version": "developer-media-allowlist/1",
                "map_run_id": map_run_id, "map_manifest_sha256": _file_sha(map_path),
                "unit_run_id": unit_run_id, "unit_manifest_sha256": _file_sha(menu_path),
                "media_file": output.name, "media_sha256": digest.hexdigest(),
                "media_total": count, "allowed_hosts": sorted(ALLOWED_HOSTS),
                "plan_media_total": count - gallery_count - project_gallery_count,
                "gallery_media_total": gallery_count,
                "project_gallery_media_total": project_gallery_count,
                "display_policy": DISPLAY_POLICY}
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
    parser.add_argument("--map-run-id", required=True)
    parser.add_argument("--unit-run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(stage_plan_media(args.map_run_id, args.unit_run_id, args.output),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
