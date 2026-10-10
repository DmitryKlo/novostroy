"""Hash-checked private delivery of completed developer harvest runs.

The delivery is a projection of source assertions, not a public offers feed.
It never downloads media or promotes an asset pointer.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from sources.property.developer_harvest import Harvest, atomic_bytes, canonical_json, digest


SCHEMA_VERSION = "developer-delivery/1"
PROJECTION_RULES_VERSION = "private-spatial-candidates-v2"
MO_DISTRICT = re.compile(r"(?:^|,\s*)МО\s*$|Московская область", re.IGNORECASE)
OTHER_REGION = re.compile(r"\b(?:Калужская|Тверская|Владимирская|Тульская) область\b", re.IGNORECASE)


def _read_manifest(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "developer-harvest-run/1":
        raise ValueError("Unsupported harvest manifest")
    for item in manifest.get("files", {}).values():
        name = item.get("path")
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Unsafe harvest file name")
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Harvest file hash mismatch: {name}")
        hasher = hashlib.sha256()
        line_count = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
                line_count += chunk.count(b"\n")
        if hasher.hexdigest() != item.get("sha256"):
            raise ValueError(f"Harvest file hash mismatch: {name}")
        if name.endswith(".jsonl") and line_count != item.get("records"):
            raise ValueError(f"Harvest file record count mismatch: {name}")
    for provider, counts in manifest.get("providers", {}).items():
        if counts.get("catalog_pages_remaining") or counts.get("project_pages_remaining"):
            raise ValueError(f"Harvest still has catalog or project work: {provider}")
        if counts.get("blocked_hosts") or any(
                counts.get("pages", {}).get(state, 0) for state in ("error", "parse_error", "blocked")):
            raise ValueError(f"Harvest has unresolved errors: {provider}")
    return manifest


def _read_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Harvest observation is not an object")
                yield value


def _region_scope(row: dict[str, Any]) -> tuple[str, str]:
    fields = row.get("source_fields") or {}
    scope = fields.get("location_scope", "unknown")
    if scope == "in_scope_moscow":
        return "moscow", "source_location_scope"
    if scope != "out_of_scope":
        return "unknown", "insufficient_source_location"
    # The aggregator's msk region is a mixed Moscow/MO market bucket. Only
    # explicit district/address text is used for this more precise partition.
    district = str(fields.get("aggregator_district") or "")
    address = str(row.get("address") or "")
    if MO_DISTRICT.search(district) or MO_DISTRICT.search(address):
        return "moscow_oblast", "explicit_source_district_or_address"
    if OTHER_REGION.search(district) or OTHER_REGION.search(address):
        return "other_region", "explicit_source_district_or_address"
    return "outside_moscow_unresolved", "source_outside_without_region"


def _load_boundary(geojson_path: Path | None, manifest_path: Path | None) -> tuple[Any, str | None]:
    if geojson_path is None and manifest_path is None:
        return None, None
    if geojson_path is None or manifest_path is None:
        raise ValueError("Boundary GeoJSON and its manifest must be supplied together")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = geojson_path.read_bytes()
    source_sha = digest(raw)
    if (geojson_path.name != manifest.get("output_file")
            or source_sha != manifest.get("output_sha256")
            or manifest.get("relation_id") != 102269):
        raise ValueError("Pinned Moscow boundary hash or identity mismatch")
    from shapely.geometry import shape
    from shapely.ops import unary_union

    data = json.loads(raw)
    if data.get("type") != "FeatureCollection" or not data.get("features"):
        raise ValueError("Moscow boundary is not a FeatureCollection")
    boundary = unary_union([shape(feature["geometry"]) for feature in data["features"]])
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Moscow boundary geometry is invalid")
    return boundary, source_sha


def _geometry_status(row: dict[str, Any], boundary: Any) -> dict[str, Any]:
    result = {"geometry_inside_moscow": "unknown", "geometry_evidence_basis": None,
              "geometry_evidence_observation_id": None}
    if boundary is None:
        return result
    from shapely.geometry import Point, shape

    geometry = row.get("source_geometry")
    coordinates = row.get("coordinates")
    validation = (row.get("geometry_validation_status")
                  or row.get("source_fields", {}).get("geometry_validation_status"))
    if (row.get("record_type") == "corpus" and isinstance(geometry, dict)
            and validation == "accepted"):
        try:
            candidate = shape(geometry)
        except (TypeError, ValueError):
            return result
        basis = "aggregator_source_corpus_polygon_unverified"
    elif row.get("record_type") == "project" and isinstance(coordinates, dict):
        lon, lat = coordinates.get("longitude"), coordinates.get("latitude")
        if (not isinstance(lon, (int, float)) or isinstance(lon, bool)
                or not isinstance(lat, (int, float)) or isinstance(lat, bool)
                or not (-180 <= lon <= 180 and -90 <= lat <= 90)):
            return result
        candidate = Point(lon, lat)
        basis = "source_project_point_unverified"
    else:
        return result
    if candidate.is_empty or not candidate.is_valid:
        return result
    if boundary.covers(candidate):
        status = "inside"
    elif boundary.disjoint(candidate):
        status = "outside"
    else:
        status = "crosses_boundary"
    return {"geometry_inside_moscow": status, "geometry_evidence_basis": basis,
            "geometry_evidence_observation_id": row["observation_id"]}


def _write_rows(directory: Path, name: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    path = directory / name
    hasher = hashlib.sha256()
    byte_count = record_count = 0
    with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix=name + ".",
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for row in rows:
                line = canonical_json(row) + b"\n"
                stream.write(line)
                hasher.update(line)
                byte_count += len(line)
                record_count += 1
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    content_sha = hasher.hexdigest()
    if path.exists():
        existing = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                existing.update(chunk)
        temporary.unlink()
        if existing.hexdigest() != content_sha:
            raise ValueError(f"Immutable delivery file differs: {name}")
    else:
        os.replace(temporary, path)
    return {"path": name, "sha256": content_sha, "bytes": byte_count,
            "records": record_count}


def build_delivery(run_directories: list[Path], output_root: Path,
                   boundary_geojson: Path | None = None,
                   boundary_manifest: Path | None = None) -> dict[str, Any]:
    if not run_directories:
        raise ValueError("At least one completed harvest run is required")
    run_directories = sorted(run_directories, key=lambda path: path.name)
    source_manifests: list[dict[str, Any]] = []
    provider_coverage: dict[str, dict[str, Any]] = {}
    for directory in run_directories:
        manifest = _read_manifest(directory)
        provider_coverage.update(manifest["providers"])
        source_manifests.append({
            "run_id": manifest["run_id"],
            "manifest_sha256": digest((directory / "manifest.json").read_bytes()),
            "providers": list(manifest["providers"]),
            "directory_name": directory.name,
        })
    providers = [provider for item in source_manifests for provider in item["providers"]]
    if len(providers) != len(set(providers)):
        raise ValueError("A provider appears in more than one input run")
    boundary, boundary_sha = _load_boundary(boundary_geojson, boundary_manifest)
    identity = "developer-delivery-" + digest(canonical_json({
        "schema_version": SCHEMA_VERSION,
        "projection_rules_version": PROJECTION_RULES_VERSION,
        "inputs": [(item["run_id"], item["manifest_sha256"]) for item in source_manifests],
        "boundary_sha256": boundary_sha,
    }))[:16]
    destination = output_root / identity
    destination.mkdir(parents=True, exist_ok=True)

    observations: list[dict[str, Any]] = []
    for directory in run_directories:
        observations.extend(_read_rows(directory / "observations.jsonl"))
    observations.sort(key=lambda row: (row["provider"], row["observation_id"]))
    observation_ids = [row["observation_id"] for row in observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("Duplicate observation ID across harvest runs")

    project_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    corpus_rows: dict[tuple[str, str], list[str]] = {}
    for row in observations:
        provider, project_id = row["provider"], row.get("publisher_project_id")
        if row["record_type"] == "project" and project_id:
            project_rows.setdefault((provider, str(project_id)), []).append(row)
        if row["record_type"] == "corpus" and row.get("publisher_building_id"):
            corpus_rows.setdefault((provider, str(row["publisher_building_id"])), []).append(
                row["observation_id"])
    project_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rows in project_rows.items():
        scopes = {row.get("source_fields", {}).get("location_scope") for row in rows}
        addresses = {row.get("address") for row in rows}
        if scopes == {"in_scope_moscow"} and len(addresses) == 1 and None not in addresses:
            project_evidence[key] = rows[0]
    geometry_by_observation = {
        row["observation_id"]: _geometry_status(row, boundary)
        for row in observations if row["record_type"] in {"project", "corpus"}
    }
    project_geometry: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rows in project_rows.items():
        candidates = [geometry_by_observation[row["observation_id"]] for row in rows
                      if geometry_by_observation[row["observation_id"]]["geometry_inside_moscow"]
                      != "unknown"]
        if len(candidates) == 1:
            project_geometry[key] = candidates[0]
    corpus_geometry: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        if row["record_type"] == "corpus" and row.get("publisher_building_id"):
            value = geometry_by_observation[row["observation_id"]]
            if value["geometry_inside_moscow"] != "unknown":
                key = row["provider"], str(row["publisher_building_id"])
                if key not in corpus_geometry:
                    corpus_geometry[key] = value

    partitions: dict[str, list[dict[str, Any]]] = {
        "moscow": [], "moscow_oblast": [], "other_region": [],
        "outside_moscow_unresolved": [], "unknown": [],
        "geometry_conflict_quarantine": [],
    }
    relationships: list[dict[str, Any]] = []
    media_refs: list[dict[str, Any]] = []
    media_seen: set[tuple[str, str, str, str]] = set()
    unit_ids: set[tuple[str, str]] = set()
    units_without_publisher_id = 0
    price_bases: Counter[str] = Counter()
    media_roles: Counter[str] = Counter()
    unit_geometry_counts: Counter[str] = Counter()
    units_with_source_update = 0
    for row in observations:
        provider = row["provider"]
        observation_id = row["observation_id"]
        project_id = row.get("publisher_project_id")
        building_id = row.get("publisher_building_id")
        if row["record_type"] == "unit":
            unit_id = row.get("publisher_unit_id")
            if unit_id:
                key = provider, str(unit_id)
                if key in unit_ids:
                    raise ValueError("Duplicate publisher unit ID in selected observations")
                unit_ids.add(key)
            else:
                units_without_publisher_id += 1
            price_bases[str(row.get("price_basis") or "unknown")] += 1
            units_with_source_update += row.get("source_updated_at") is not None
            evidence = project_evidence.get((provider, str(project_id)))
            projection = Harvest._unit_projection(row, evidence)
            region, basis = _region_scope(row)
            if evidence is not None and projection["location_scope"] == "in_scope_moscow":
                region, basis = "moscow", "same_publisher_project_id"
            geo = (corpus_geometry.get((provider, str(building_id)))
                   or project_geometry.get((provider, str(project_id)))
                   or {"geometry_inside_moscow": "unknown",
                       "geometry_evidence_basis": None,
                       "geometry_evidence_observation_id": None})
            projection.update(geo)
            geo_status = geo["geometry_inside_moscow"]
            unit_geometry_counts[geo_status] += 1
            conflict = ((projection["location_scope"] == "in_scope_moscow"
                         and geo_status == "outside")
                        or (projection["location_scope"] == "out_of_scope"
                            and geo_status == "inside"))
            projection["location_geometry_conflict"] = conflict
            if conflict:
                region, basis = "geometry_conflict_quarantine", "source_scope_vs_geometry"
            projection["region_partition"] = region
            projection["region_partition_basis"] = basis
            partitions[region].append(projection)
        if project_id and row["record_type"] in {"unit", "corpus"}:
            relationships.append({
                "source_observation_id": observation_id,
                "source_record_type": row["record_type"],
                "relationship": "publisher_project_id",
                "provider": provider, "target_publisher_id": str(project_id),
                "source_identity_conflict": bool(row.get("identity_conflict")),
                "target_observation_ids": sorted(
                    target["observation_id"] for target in project_rows.get(
                        (provider, str(project_id)), [])),
                "identity_basis": "exact_same_provider_id",
            })
        if building_id and row["record_type"] == "unit":
            relationships.append({
                "source_observation_id": observation_id,
                "source_record_type": "unit",
                "relationship": "publisher_building_id",
                "provider": provider, "target_publisher_id": str(building_id),
                "source_identity_conflict": bool(row.get("identity_conflict")),
                "target_observation_ids": sorted(corpus_rows.get(
                    (provider, str(building_id)), [])),
                "identity_basis": "exact_same_provider_id",
            })
        for medium in row.get("media") or []:
            url, role, scope = medium.get("url"), medium.get("role"), medium.get("scope")
            if not isinstance(url, str):
                continue
            key = observation_id, url, str(role), str(scope)
            if key in media_seen:
                continue
            media_seen.add(key)
            media_roles[str(role or "unknown")] += 1
            media_refs.append({
                "observation_id": observation_id, "provider": provider,
                "record_type": row["record_type"], "source_page_url": row["source_page_url"],
                "observed_at": row["observed_at"], "source_updated_at": row.get("source_updated_at"),
                "url": url, "role": role, "scope": scope,
                "visual_kind": medium.get("visual_kind"),
                "source_field": medium.get("source_field"),
                "label": medium.get("label") or medium.get("caption"),
                "rights_status": "unknown", "public_display_allowed": False,
            })

    project_projections = [dict(Harvest._project_projection(row),
                                **geometry_by_observation[row["observation_id"]])
                           for row in observations if row["record_type"] in {"project", "corpus"}]
    project_geometry_counts = Counter(
        row["geometry_inside_moscow"] for row in project_projections)
    spatial_candidates = []
    for row in partitions["unknown"]:
        if (row["location_scope"] == "unknown"
                and row["geometry_inside_moscow"] == "inside"
                and row["geometry_evidence_basis"] in {
                    "aggregator_source_corpus_polygon_unverified",
                    "source_project_point_unverified",
                } and not row["location_geometry_conflict"]):
            spatial_candidates.append({
                **row, "source_location_scope": row["location_scope"],
                "moscow_boundary_sha256": boundary_sha,
                "spatial_selection_basis": row["geometry_evidence_basis"],
                "address_verified_by_spatial_test": False,
            })
    files = {"observations": _write_rows(destination, "observations.jsonl", observations)}
    files["projects_corpora"] = _write_rows(destination, "projects_corpora.jsonl",
                                           project_projections)
    for region, rows in partitions.items():
        files[f"units_{region}"] = _write_rows(destination, f"units_{region}.jsonl", rows)
    files["units_moscow_spatial_candidates"] = _write_rows(
        destination, "units_moscow_spatial_candidates.jsonl", spatial_candidates)
    files["relationships"] = _write_rows(destination, "relationships.jsonl", relationships)
    files["media_refs"] = _write_rows(destination, "media_refs.jsonl", media_refs)
    manifest = {
        "schema_version": SCHEMA_VERSION, "delivery_id": identity,
        "projection_rules_version": PROJECTION_RULES_VERSION,
        "publication_status": "private_research_only", "rights_status": "unknown",
        "all_moscow_coverage_claimed": False, "sources": source_manifests,
        "moscow_boundary_sha256": boundary_sha,
        "provider_coverage": provider_coverage,
        "files": files, "unit_partitions": {
            region: len(rows) for region, rows in partitions.items()},
        "spatial_candidates_overlap_unknown_partition": len(spatial_candidates),
        "observed_first": min((row["observed_at"] for row in observations), default=None),
        "observed_last": max((row["observed_at"] for row in observations), default=None),
        "unit_ids_unique": len(unit_ids),
        "unit_geometry_counts": dict(sorted(unit_geometry_counts.items())),
        "project_corpus_geometry_counts": dict(sorted(project_geometry_counts.items())),
        "units_without_publisher_id": units_without_publisher_id,
        "units_with_source_updated_at": units_with_source_update,
        "price_basis_counts": dict(sorted(price_bases.items())),
        "media_role_counts": dict(sorted(media_roles.items())),
        "relationship_basis": "same_provider_exact_publisher_id_only",
        "media_downloaded": 0,
        "observed_time_policy": "capture_or_public_get_time_not_source_update_time",
    }
    manifest_bytes = canonical_json(manifest) + b"\n"
    path = destination / "manifest.json"
    if path.exists() and path.read_bytes() != manifest_bytes:
        raise ValueError("Immutable delivery manifest differs")
    if not path.exists():
        atomic_bytes(path, manifest_bytes)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", type=Path, required=True,
                        help="completed private harvest run directory; repeat per provider set")
    parser.add_argument("--output-root", type=Path,
                        default=Path(".runtime/research/developer-harvest/deliveries"))
    parser.add_argument("--boundary-geojson", type=Path)
    parser.add_argument("--boundary-manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_delivery(args.run, args.output_root,
                                    args.boundary_geojson, args.boundary_manifest),
                     ensure_ascii=False,
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
