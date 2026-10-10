"""Build a private, versioned map of houses linked to captured apartment IDs.

The source delivery is evidence, not a live offer feed.  Project markers are
never reused as building coordinates, and this module never fetches a URL.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from shapely.geometry import Point, Polygon, shape

from sources.property.developer_delivery import _load_boundary


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "developer-map-run/1"
RULES = "developer-buildings-v1"
UNIT_PARTITIONS = ("units_moscow", "units_unknown", "units_moscow_oblast",
                   "units_other_region", "units_outside_moscow_unresolved")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
HOUSE_RE = re.compile(r"(?:^|[\s,])(?:д\.?|дом|вл\.?)\s*\d", re.IGNORECASE)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"Non-object JSONL row in {path.name}")
                yield value


def _checked_file(directory: Path, files: dict[str, Any], key: str) -> Path:
    item = files.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"Missing input file entry: {key}")
    name, digest, count = item.get("path"), item.get("sha256"), item.get("records")
    if (not isinstance(name, str) or Path(name).name != name
            or not isinstance(digest, str) or not HASH_RE.fullmatch(digest)
            or type(count) is not int or count < 0):
        raise ValueError(f"Unsafe input file entry: {key}")
    path = directory / name
    if path.is_symlink() or not path.is_file() or _file_sha(path) != digest:
        raise ValueError(f"Input file SHA mismatch: {key}")
    if sum(1 for _ in path.open("rb")) != count:
        raise ValueError(f"Input row count mismatch: {key}")
    return path


def _checked_delivery(path: Path) -> tuple[dict[str, Any], dict[str, Path], str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Delivery manifest is missing or a symlink")
    manifest_bytes = path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != "developer-delivery/1":
        raise ValueError("Unsupported developer delivery")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Delivery file list is missing")
    needed = ("projects_corpora", *UNIT_PARTITIONS)
    checked = {key: _checked_file(path.parent, files, key) for key in needed}
    return manifest, checked, _sha(manifest_bytes)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _point_id(source_group: str, project_id: str, building_key: str) -> tuple[str, str]:
    identity = (f"{source_group}\x1f{building_key}" if building_key.startswith("id:") else
                f"{source_group}\x1f{project_id}\x1f{building_key}")
    digest = _sha(identity.encode("utf-8"))
    return "registry-" + digest[:24], "mappt_" + digest[:24]


def _building_key(row: dict[str, Any]) -> tuple[str | None, str | None]:
    building = row.get("building") or {}
    publisher_id = building.get("publisher_building_id")
    if isinstance(publisher_id, (str, int)) and str(publisher_id).strip():
        return "id:" + str(publisher_id).strip(), _text(building.get("corpus"))
    corpus = _text(building.get("corpus"))
    if corpus and corpus.casefold() not in {"?", "-", "не указан", "нет"}:
        return "corpus:" + corpus.casefold(), corpus
    return None, None


def _same_location_scope(rows: set[str], polygon_inside: bool) -> bool:
    if "out_of_scope" in rows:
        return False
    return "in_scope_moscow" in rows or polygon_inside


def _build_indices(path: Path) -> tuple[dict[tuple[str, str], dict[str, Any]],
                                        dict[tuple[str, str, str], dict[str, Any]]]:
    projects: dict[tuple[str, str], dict[str, Any]] = {}
    corpora: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _rows(path):
        source, project_id = _text(row.get("source_group")), _text(row.get("publisher_project_id"))
        if not source or not project_id:
            continue
        if row.get("record_type") == "project":
            key = (source, project_id)
            if key not in projects or row.get("observed_at", "") > projects[key].get("observed_at", ""):
                projects[key] = row
        elif row.get("record_type") == "corpus":
            building_id = row.get("publisher_building_id")
            corpus = _text(row.get("corpus"))
            # A label may repeat within one development. Never alias a precise
            # publisher building ID through an ambiguous display label.
            keys = (["id:" + str(building_id)] if building_id is not None else
                    ["corpus:" + corpus.casefold()] if corpus else [])
            for key in keys:
                index_key = (source, project_id, key)
                previous = corpora.get(index_key)
                if previous and previous.get("source_geometry") != row.get("source_geometry"):
                    raise ValueError(f"Conflicting source corpus geometry: {index_key}")
                corpora[index_key] = row
    return projects, corpora


def _source_polygon_location(corpus: dict[str, Any] | None, boundary: Any) -> list[float] | None:
    if not corpus or corpus.get("geometry_basis") != "aggregator_unverified":
        return None
    geometry = corpus.get("source_geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        polygon = shape(geometry)
    except (TypeError, ValueError):
        return None
    if (polygon.geom_type not in {"Polygon", "MultiPolygon"} or polygon.is_empty
            or not polygon.is_valid or not boundary.covers(polygon)):
        return None
    point = polygon.representative_point()
    return [round(point.x, 9), round(point.y, 9)]


def _checked_geocode(manifest_path: Path, delivery_sha: str,
                     boundary: Any) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[str, str]]:
    """Accept a bounded exact-building point, keeping reviews as provenance only."""
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Geocode manifest is missing or a symlink")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "geocode-evidence-manifest/1":
        raise ValueError("Unsupported geocode evidence manifest")
    parent = manifest.get("parent_delivery")
    if parent and parent.get("manifest_sha256") != delivery_sha:
        raise ValueError("Geocode evidence references another delivery")
    result_name = manifest.get("result_path")
    if not isinstance(result_name, str) or not result_name.endswith(".jsonl"):
        raise ValueError("Invalid geocode result path")
    result_path = manifest_path.parent / Path(result_name).name
    if result_path.is_symlink() or _file_sha(result_path) != manifest.get("result_sha256"):
        raise ValueError("Geocode result SHA mismatch")
    rows = list(_rows(result_path))
    expected_count = manifest.get("record_count", (manifest.get("counts") or {}).get("total"))
    if len(rows) != expected_count:
        raise ValueError("Geocode result count mismatch")
    verified: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("schema_version") != "geocode-evidence/1":
            raise ValueError("Invalid geocode result schema")
        if row.get("status") != "verified_point":
            continue
        source = row.get("source_group")
        project_id = row.get("publisher_project_id")
        building_id = row.get("publisher_building_id")
        target = f"geocode:{source}:{building_id}"
        resolved = row.get("resolved_buildings")
        if (source != "fsk" or not project_id or not building_id
                or row.get("target_ids") != [target]
                or not isinstance(resolved, list) or len(resolved) != 1
                or resolved[0].get("publisher_project_id") != project_id
                or resolved[0].get("publisher_building_id") != building_id
                or row.get("point_type") != "building_footprint_centroid"
                or resolved[0].get("point_type") != row["point_type"]):
            raise ValueError("Geocode building identity is not exact")
        coord = row.get("coordinates") or {}
        lon, lat = coord.get("longitude"), coord.get("latitude")
        if (coord.get("crs") != "EPSG:4326" or coord.get("order") != "lon_lat"
                or not all(type(v) in (float, int) and math.isfinite(v) for v in (lon, lat))
                or resolved[0].get("coordinates") != [lon, lat]
                or not boundary.covers(Point(lon, lat))):
            raise ValueError("Geocode point is outside the verified Moscow boundary")
        consulted = row.get("pages_consulted") or {}
        if (consulted.get("count") != len(set(consulted.get("urls") or []))
                or consulted.get("count", 99) > 5):
            raise ValueError("Geocode page budget is invalid")
        evidence = row.get("evidence") or []
        if (len(evidence) < 2 or not all(item.get("exact_address_match") is True
                                        for item in evidence)):
            raise ValueError("Geocode exact-address evidence is incomplete")
        source_capture = manifest.get("source_capture") or {}
        snapshot_name = source_capture.get("retained_derived_snapshot")
        if not isinstance(snapshot_name, str):
            raise ValueError("Geocode geometry snapshot is missing")
        snapshot_path = (manifest_path.parent / Path(snapshot_name).parent.name /
                         Path(snapshot_name).name)
        if (snapshot_path.is_symlink() or _file_sha(snapshot_path) !=
                source_capture.get("derived_snapshot_sha256")):
            raise ValueError("Geocode geometry snapshot SHA mismatch")
        snapshot = json.loads(snapshot_path.read_bytes())
        osm = snapshot.get("osm_object") or {}
        index = (row.get("local_crosswalk") or {}).get("index_entry") or {}
        if (osm.get("osm_type") != "way" or osm.get("osm_id") != row.get("osm_id")
                or osm.get("osm_version") != row.get("osm_version")
                or index.get("osm_id") != osm["osm_id"]
                or index.get("candidate_count") != 1
                or index.get("ambiguous_index_match") is not False
                or snapshot.get("source_response_sha256") != source_capture.get("response_sha256")):
            raise ValueError("Geocode OSM crosswalk is not unique")
        ring = osm.get("closed_ring_nodes") or []
        if len(ring) < 4:
            raise ValueError("Geocode footprint has no closed ring")
        polygon = Polygon([(node["longitude"], node["latitude"]) for node in ring])
        if (not polygon.is_valid or polygon.is_empty or not boundary.covers(polygon)
                or not polygon.covers(Point(lon, lat))
                or polygon.centroid.distance(Point(lon, lat)) > 0.00005):
            raise ValueError("Geocode coordinate is not the building footprint centroid")
        normalized = _text(row.get("normalized_address"))
        if (not normalized or "москва" not in normalized.casefold()
                or not (HOUSE_RE.search(normalized)
                        or re.search(r",\s*\d+[а-я]?(?:/\d+)?\s*\Z", normalized,
                                     re.IGNORECASE))):
            # A full civic address must be explicit before it is advertised.
            raise ValueError("Geocode lacks a complete building address")
        key = (source, str(project_id), "id:" + str(building_id))
        if key in verified:
            raise ValueError("Duplicate geocoded building")
        verified[key] = {"location": [lon, lat], "address": normalized,
                         "point_type": row["point_type"],
                         "evidence_sha256": _sha(_canonical(row)),
                         "source_response_sha256": source_capture["response_sha256"]}
    return verified, {"role": "geocode", "manifest_sha256": _sha(raw),
                      "results_sha256": _file_sha(result_path)}


def _new_candidate(source: str, project_id: str, building_key: str) -> dict[str, Any]:
    return {"source_group": source, "publisher_project_id": project_id,
            "building_key": building_key, "unit_ids": set(), "scope": set(),
            "names": set(), "addresses": set(), "corpus_labels": set(),
            "observed_at": "", "source_hashes": set()}


def _collect_core_units(paths: dict[str, Path]) -> tuple[dict[tuple[str, str, str], dict[str, Any]],
                                                       list[dict[str, Any]], dict[str, int]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing: dict[tuple[str, str, str], dict[str, Any]] = {}
    unit_counts: dict[str, int] = defaultdict(int)
    for partition in UNIT_PARTITIONS:
        for row in _rows(paths[partition]):
            source = _text(row.get("source_group"))
            project = row.get("project") or {}
            unit = row.get("unit") or {}
            project_id = _text(project.get("publisher_project_id"))
            unit_id = _text(str(unit.get("publisher_unit_id"))) if unit.get("publisher_unit_id") is not None else None
            if not source or not unit_id:
                raise ValueError("Delivery unit has no source identity")
            unit_counts[source] += 1
            building_key, corpus_label = _building_key(row)
            if not project_id or not building_key:
                reason = "missing_project_identity" if not project_id else "missing_building_identity"
                key = (source, project_id or "", reason)
                aggregate = missing.setdefault(key, {"source_group": source,
                    "publisher_project_id": project_id, "reason_codes": [reason], "unit_ids": set()})
                aggregate["unit_ids"].add(unit_id)
                continue
            key = (source, project_id, building_key)
            aggregate = groups.setdefault(key, _new_candidate(*key))
            aggregate["unit_ids"].add(unit_id)
            aggregate["scope"].add(row.get("location_scope") or "unknown")
            for field, value in (("names", _text(project.get("name"))),
                                 ("addresses", _text((row.get("building") or {}).get("full_address"))
                                  or _text(project.get("address"))),
                                 ("corpus_labels", corpus_label)):
                if value:
                    aggregate[field].add(value)
            aggregate["observed_at"] = max(aggregate["observed_at"], row.get("observed_at") or "")
            evidence = row.get("source_evidence_observation_id") or row.get("observation_id")
            if evidence:
                aggregate["source_hashes"].add(str(evidence))
    rejected = [{**{k: v for k, v in item.items() if k != "unit_ids"},
                 "unit_count": len(item["unit_ids"])} for item in missing.values()]
    # A101 sometimes omits the numeric building ID on one catalog page but
    # supplies it for another unit in the *same exact project+corpus*. Merge
    # only if there is precisely one upstream ID; a conflicting pair leaves
    # the label-only units in review, without conflating buildings.
    for (source, project_id, key), label_group in list(groups.items()):
        if source != "a101" or not key.startswith("corpus:"):
            continue
        upstream = [(candidate_key, candidate) for candidate_key, candidate in groups.items()
                    if candidate_key[:2] == (source, project_id)
                    and candidate_key[2].startswith("id:")
                    and key[7:] in {label.casefold() for label in candidate["corpus_labels"]}]
        if not upstream:
            continue
        groups.pop((source, project_id, key))
        if len(upstream) != 1:
            rejected.append({"source_group": source, "publisher_project_id": project_id,
                             "building_key": key, "unit_count": len(label_group["unit_ids"]),
                             "reason_codes": ["ambiguous_building_id_for_corpus_label"]})
            continue
        target = upstream[0][1]
        for field in ("unit_ids", "scope", "names", "addresses", "corpus_labels", "source_hashes"):
            target[field].update(label_group[field])
        target["observed_at"] = max(target["observed_at"], label_group["observed_at"])
    return groups, rejected, dict(unit_counts)


def _supplemental_rejections(level_manifest: Path | None,
                             etalon_manifest: Path | None,
                             afi_manifest: Path | None) -> tuple[list[dict[str, Any]],
                                                                    list[dict[str, str]], dict[str, int]]:
    rejected: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    if level_manifest is not None:
        raw = level_manifest.read_bytes()
        manifest = json.loads(raw)
        if manifest.get("schema_version") != "developer-harvest-run/1":
            raise ValueError("Unsupported Level run manifest")
        source = _checked_file(level_manifest.parent, manifest["files"], "observations")
        grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in _rows(source):
            if row.get("record_type") != "unit" or row.get("source_group") != "level":
                continue
            project_id, building_id, unit_id = (row.get(key) for key in
                ("publisher_project_id", "publisher_building_id", "publisher_unit_id"))
            if unit_id is None:
                raise ValueError("Level source unit identity is missing")
            grouped[(str(project_id or ""), str(building_id or ""))].add(str(unit_id))
        for (project_id, building_id), unit_ids in sorted(grouped.items()):
            rejected.append({"source_group": "level", "publisher_project_id": project_id,
                             "building_key": "id:" + building_id if building_id else None,
                             "unit_count": len(unit_ids),
                             "reason_codes": ["no_building_address_or_location_evidence"]})
        counts["level"] = len(set().union(*grouped.values())) if grouped else 0
        inputs.append({"role": "level", "manifest_sha256": _sha(raw)})
    if etalon_manifest is not None:
        raw = etalon_manifest.read_bytes()
        manifest = json.loads(raw)
        if manifest.get("schema_version") != "etalongroup-anonymous-api-completion/1":
            raise ValueError("Unsupported Etalon final manifest")
        source = etalon_manifest.parent / "unit-results.jsonl"
        if source.is_symlink() or _file_sha(source) != manifest.get("unit_results_jsonl_sha256"):
            raise ValueError("Etalon source JSONL hash mismatch")
        grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in _rows(source):
            project_id, unit_id = row.get("source_project_id"), row.get("source_unit_id")
            if project_id is None or unit_id is None:
                raise ValueError("Etalon source identity is missing")
            building = str(row.get("source_corpus_number_from_label") or "")
            scope = str(row.get("municipality_scope") or "unknown")
            grouped[(str(project_id), building, scope)].add(str(unit_id))
        for (project_id, building, scope), unit_ids in sorted(grouped.items()):
            reason = ("source_municipality_conflict" if scope.startswith("conflicting") else
                      "no_building_address_or_location_evidence")
            rejected.append({"source_group": "etalon", "publisher_project_id": project_id,
                             "building_key": "corpus:" + building if building else None,
                             "unit_count": len(unit_ids), "reason_codes": [reason]})
        counts["etalon"] = len({(key[0], unit_id) for key, unit_ids in grouped.items()
                                for unit_id in unit_ids})
        if counts["etalon"] != manifest.get("unique_source_unit_ids_after_dedup"):
            raise ValueError("Etalon source count mismatch")
        inputs.append({"role": "etalon", "manifest_sha256": _sha(raw)})
    if afi_manifest is not None:
        raw = afi_manifest.read_bytes()
        manifest = json.loads(raw)
        if (manifest.get("schema_version") != "afi-homepage-card-capture/1"
                or (manifest.get("artifacts") or {}).get("unit_cards") != "unit_cards.jsonl"):
            raise ValueError("Unsupported AFI card manifest")
        source = afi_manifest.parent / "unit_cards.jsonl"
        if source.is_symlink() or not source.is_file():
            raise ValueError("AFI source JSONL is missing")
        grouped: dict[str, set[str]] = defaultdict(set)
        all_ids: set[str] = set()
        for row in _rows(source):
            unit_id = _text(row.get("unit_source_id"))
            project_path = _text(row.get("project_source_path"))
            if (not unit_id or not project_path
                    or row.get("source_body_sha256") != manifest.get("source_body_sha256")):
                raise ValueError("AFI card identity or source hash mismatch")
            if unit_id in all_ids:
                raise ValueError("Duplicate AFI unit card ID")
            all_ids.add(unit_id)
            grouped[project_path].add(unit_id)
        if len(all_ids) != manifest.get("counts", {}).get("product_cards"):
            raise ValueError("AFI card count mismatch")
        for project_path, unit_ids in sorted(grouped.items()):
            rejected.append({"source_group": "afi", "publisher_project_id": project_path,
                             "building_key": None, "unit_count": len(unit_ids),
                             "reason_codes": ["no_building_address_or_location_evidence",
                                              "moscow_scope_unverified"]})
        counts["afi"] = len(all_ids)
        inputs.append({"role": "afi", "manifest_sha256": _sha(raw),
                       "unit_cards_sha256": _file_sha(source)})
    return rejected, inputs, counts


def _candidate_result(candidate: dict[str, Any], project: dict[str, Any] | None,
                      corpus: dict[str, Any] | None, boundary: Any) -> tuple[dict[str, Any] | None,
                                                                           dict[str, Any] | None]:
    source = candidate["source_group"]
    project_id = candidate["publisher_project_id"]
    building_key = candidate["building_key"]
    scope = candidate["scope"]
    location = _source_polygon_location(corpus, boundary)
    reason: list[str] = []
    if "out_of_scope" in scope:
        reason.append("source_outside_moscow_or_conflict")
    elif not _same_location_scope(scope, location is not None):
        reason.append("moscow_scope_unverified")
    names = candidate["names"] | ({_text(project.get("project_name"))} if project else set())
    names.discard(None)
    # Publisher project ID is the identity. Differences like "ЖК «X»" vs "X"
    # are display aliases, not a reason to lose correctly identified homes.
    project_name = (_text(project.get("project_name")) if project else None) or (
        sorted(names)[0] if names else None)
    labels = set(candidate["corpus_labels"])
    if corpus and _text(corpus.get("corpus")):
        labels.add(corpus["corpus"].strip())
    if len(labels) > 1:
        reason.append("conflicting_corpus_labels")
    corpus_label = sorted(labels)[0] if labels else None
    addresses = set(candidate["addresses"])
    if corpus and _text(corpus.get("address")):
        addresses.add(corpus["address"].strip())
    if project and _text(project.get("address")):
        addresses.add(project["address"].strip())
    # A corpus-specific address wins over a project address. Different unit
    # addresses within one building identity are a conflict, not a tie-breaker.
    corpus_address = _text(corpus.get("address")) if corpus else None
    if corpus_address:
        address, address_basis = corpus_address, "source_corpus_address"
    elif len(candidate["addresses"]) == 1:
        address, address_basis = next(iter(candidate["addresses"])), "unit_project_address"
    elif project and _text(project.get("address")):
        address, address_basis = project["address"].strip(), "source_project_address"
    else:
        address, address_basis = None, None
    if len(candidate["addresses"]) > 1 and not corpus_address:
        reason.append("conflicting_unit_addresses")
    if not address:
        reason.append("no_source_address")
    if not project_name or not corpus_label:
        reason.append("missing_project_or_corpus_label")
    if (location is None and not building_key.startswith("id:")
            and source != "a101"):
        reason.append("unlocated_corpus_label_without_building_id")
    if reason:
        return None, {"source_group": source, "publisher_project_id": project_id,
                      "building_key": building_key, "unit_count": len(candidate["unit_ids"]),
                      "reason_codes": sorted(set(reason))}
    registry_id, point_id = _point_id(source, project_id, building_key)
    evidence = {"source_group": source, "publisher_project_id": project_id,
                "building_key": building_key, "unit_ids": sorted(candidate["unit_ids"]),
                "observation_ids": sorted(candidate["source_hashes"]),
                "corpus_observation_id": corpus.get("observation_id") if corpus else None}
    developer_name = _text(project.get("developer_name")) if project else None
    if not developer_name and project:
        developer_name = _text(project.get("builder_name"))
    point = {"schema_version": "developer-map-building/1", "registry_id": registry_id,
             "point_id": point_id, "point_type": "developer_building",
             "name": f"{project_name} · корпус {corpus_label}",
             "complex_label": project_name, "project_name": project_name,
             "developer_name": developer_name, "address": address,
             "address_complete": bool(address_basis == "source_corpus_address"
                                      and HOUSE_RE.search(address)),
             "address_basis": address_basis, "location": location,
             "location_quality": "verified" if location else None,
             "location_basis": "source_corpus_polygon_representative_point" if location else None,
             "map_eligible": location is not None, "unit_count": len(candidate["unit_ids"]),
             "observed_at": candidate["observed_at"], "source_group": source,
             "publisher_project_id": project_id,
             "publisher_building_id": building_key[3:] if building_key.startswith("id:") else None,
             "identity_basis": ("publisher_building_id" if building_key.startswith("id:")
                                else "source_project_corpus_label"),
             "source_corpus_label": corpus_label,
             "source_project_name_aliases": sorted(names),
             "source_evidence_sha256": _sha(_canonical(evidence))}
    return point, None


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical(row) + b"\n" for row in rows)


def _write_private(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", prefix=".pointer-", suffix=".tmp",
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(_canonical(payload) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _build_groups(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        if point.get("publisher_project_id"):
            grouped[(point["source_group"], point["publisher_project_id"])].append(point)
    groups = []
    for (source, project_id), members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        registry_ids = sorted(member["registry_id"] for member in members)
        point_ids = sorted(member["point_id"] for member in members if member["map_eligible"])
        label_values = {member["project_name"] for member in members if member["project_name"]}
        if len(label_values) != 1:
            continue
        group_id = "group_" + _sha(f"{source}\x1f{project_id}".encode("utf-8"))[:24]
        evidence = _sha(_canonical({"source_group": source, "publisher_project_id": project_id,
                                    "member_registry_ids": registry_ids}))
        groups.append({"schema_version": "developer-map-group/1", "group_id": group_id,
                       "group_label": next(iter(label_values)),
                       "proof_basis": "same_publisher_project_id",
                       "member_registry_ids": registry_ids, "member_point_ids": point_ids,
                       "source_group": source, "publisher_project_id": project_id,
                       "evidence_sha256": evidence})
    return groups


def _apply_reviewed_crosswalk(points: list[dict[str, Any]],
                              corpora: dict[tuple[str, str, str], dict[str, Any]],
                              crosswalk_manifest: Path, delivery_sha: str,
                              geocode_inputs: list[dict[str, str]]) -> dict[str, str]:
    """Merge only a reviewed exact-building pair with overlapping source outlines."""
    raw = crosswalk_manifest.read_bytes()
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != "developer-map-crosswalk-manifest/1"
            or manifest.get("source_delivery_manifest_sha256") != delivery_sha
            or manifest.get("geocode_manifest_sha256") not in
            {item["manifest_sha256"] for item in geocode_inputs}):
        raise ValueError("Crosswalk inputs do not match this map run")
    entries_path = crosswalk_manifest.parent / "crosswalks.jsonl"
    if (entries_path.is_symlink() or _file_sha(entries_path) != manifest.get("crosswalks_sha256")):
        raise ValueError("Crosswalk evidence SHA mismatch")
    entries = list(_rows(entries_path))
    if len(entries) != manifest.get("records_total") or len(entries) != 1:
        raise ValueError("Crosswalk count or review scope is invalid")
    row = entries[0]
    if (row.get("schema_version") != "developer-map-crosswalk/1"
            or row.get("decision") != "same_physical_building"
            or row.get("official_single_corpus") is not True):
        raise ValueError("Crosswalk has no reviewed single-building decision")
    fsk_key = ("fsk", row.get("fsk_project_id"), "id:" + str(row.get("fsk_building_id")))
    pro_key = ("proplanirovki", row.get("proplan_project_id"),
               "id:" + str(row.get("proplan_building_id")))
    by_identity = {(point["source_group"], point["publisher_project_id"],
                    "id:" + point["publisher_building_id"]): point for point in points
                   if point["publisher_building_id"] is not None}
    fsk, pro = by_identity.get(fsk_key), by_identity.get(pro_key)
    corpus = corpora.get(pro_key)
    if (not fsk or not pro or not corpus or not fsk["map_eligible"]
            or not pro["map_eligible"] or not fsk["address_complete"]
            or fsk["location_basis"] != "exact_address_osm_building_footprint_centroid"
            or fsk["source_corpus_label"] != pro["source_corpus_label"]
            or pro["project_name"].casefold() not in fsk["project_name"].casefold()
            or "снежн" not in (pro["address"] or "").casefold()
            or "снежн" not in (fsk["address"] or "").casefold()
            or sum(item["source_group"] == "fsk" and
                   item["publisher_project_id"] == fsk["publisher_project_id"]
                   for item in points) != 1):
        raise ValueError("Crosswalk project, corpus or civic address is ambiguous")
    snapshot_path = crosswalk_manifest.parent / "osm-footprint.json"
    if snapshot_path.is_symlink() or _file_sha(snapshot_path) != manifest.get("osm_footprint_sha256"):
        raise ValueError("Crosswalk OSM footprint SHA mismatch")
    snapshot = json.loads(snapshot_path.read_bytes())
    osm = snapshot["osm_object"]
    if (osm.get("osm_type") != "way" or osm.get("osm_id") != row.get("osm_id")
            or osm.get("osm_version") != row.get("osm_version")):
        raise ValueError("Crosswalk refers to another OSM building")
    osm_polygon = Polygon([(node["longitude"], node["latitude"])
                           for node in osm["closed_ring_nodes"]])
    pro_polygon = shape(corpus["source_geometry"])
    if not osm_polygon.is_valid or not pro_polygon.is_valid:
        raise ValueError("Crosswalk outlines are invalid")
    overlap = osm_polygon.intersection(pro_polygon).area
    jaccard = overlap / osm_polygon.union(pro_polygon).area
    pro_covered = overlap / pro_polygon.area
    if jaccard < 0.25 or pro_covered < 0.60:
        raise ValueError("Crosswalk outlines do not sufficiently overlap")
    # Check uniqueness against every retained source corpus polygon, not only
    # a nearby centroid or a similarly named project.
    intersecting = []
    for key, candidate in corpora.items():
        if key[0] != "proplanirovki" or not candidate.get("source_geometry"):
            continue
        candidate_polygon = shape(candidate["source_geometry"])
        if candidate_polygon.is_valid and osm_polygon.intersection(candidate_polygon).area > 0:
            intersecting.append(key)
    if intersecting != [pro_key]:
        raise ValueError("Crosswalk OSM building intersects other corpus outlines")
    fsk["source_observed_unit_counts"] = {"fsk": fsk["unit_count"],
                                            "proplanirovki": pro["unit_count"]}
    fsk["unit_count_basis"] = "fsk_source_observations_only"
    fsk["cross_source_identity"] = {"proplanirovki": {
        "publisher_project_id": pro["publisher_project_id"],
        "publisher_building_id": pro["publisher_building_id"],
        "observed_unit_count": pro["unit_count"],
        "source_evidence_sha256": pro["source_evidence_sha256"]},
        "overlap_jaccard": round(jaccard, 6),
        "proplan_polygon_covered": round(pro_covered, 6),
        "crosswalk_evidence_sha256": _sha(_canonical(row))}
    fsk["source_evidence_sha256"] = _sha(_canonical({
        "fsk": fsk["source_evidence_sha256"],
        "proplanirovki": pro["source_evidence_sha256"],
        "crosswalk": _sha(_canonical(row))}))
    points.remove(pro)
    return {"role": "reviewed_crosswalk", "manifest_sha256": _sha(raw),
            "crosswalks_sha256": _file_sha(entries_path)}


def build(delivery_manifest: Path, output_root: Path, boundary_geojson: Path,
          boundary_manifest: Path, *, level_manifest: Path | None = None,
          etalon_manifest: Path | None = None,
          afi_manifest: Path | None = None,
          geocode_manifests: tuple[Path, ...] = (),
          crosswalk_manifest: Path | None = None) -> dict[str, Any]:
    delivery, paths, delivery_sha = _checked_delivery(delivery_manifest)
    boundary, boundary_sha = _load_boundary(boundary_geojson, boundary_manifest)
    if boundary is None or boundary_sha != delivery.get("moscow_boundary_sha256"):
        raise ValueError("Delivery and pinned Moscow boundary differ")
    projects, corpora = _build_indices(paths["projects_corpora"])
    candidates, rejections, source_unit_counts = _collect_core_units(paths)
    geocodes: dict[tuple[str, str, str], dict[str, Any]] = {}
    geocode_inputs = []
    for evidence_manifest in geocode_manifests:
        verified, input_ref = _checked_geocode(evidence_manifest, delivery_sha, boundary)
        if geocodes.keys() & verified.keys():
            raise ValueError("Duplicate geocode candidate across evidence manifests")
        geocodes.update(verified)
        geocode_inputs.append(input_ref)
    points = []
    for (source, project_id, building_key), candidate in sorted(candidates.items()):
        project = projects.get((source, project_id))
        corpus = corpora.get((source, project_id, building_key))
        point, rejection = _candidate_result(candidate, project, corpus, boundary)
        if point:
            geocode = geocodes.pop((source, project_id, building_key), None)
            if geocode:
                if point["location"] is not None or point["identity_basis"] != "publisher_building_id":
                    raise ValueError("Geocode point would overwrite building geometry or label-only identity")
                point.update({"location": geocode["location"], "location_quality": "verified",
                              "location_basis": "exact_address_osm_building_footprint_centroid",
                              "map_eligible": True, "address": geocode["address"],
                              "address_complete": True,
                              "address_basis": "official_single_corpus_address_exact_osm",
                              "location_evidence_sha256": geocode["evidence_sha256"]})
                point["source_evidence_sha256"] = _sha(_canonical({
                    "unit_evidence_sha256": point["source_evidence_sha256"],
                    "geocode_evidence_sha256": geocode["evidence_sha256"]}))
            points.append(point)
        else:
            rejections.append(rejection)
    if geocodes:
        raise ValueError("Geocode evidence did not match one admitted building")
    crosswalk_input = (_apply_reviewed_crosswalk(points, corpora, crosswalk_manifest,
                       delivery_sha, geocode_inputs) if crosswalk_manifest else None)
    supplemental, extra_inputs, supplement_counts = _supplemental_rejections(
        level_manifest, etalon_manifest, afi_manifest)
    rejections.extend(supplemental)
    points.sort(key=lambda item: item["registry_id"])
    rejections.sort(key=lambda item: (item["source_group"],
                                     item.get("publisher_project_id") or "",
                                     item.get("building_key") or "",
                                     ",".join(item["reason_codes"])))
    groups = _build_groups(points)
    coverage = {}
    for source, total in sorted({**source_unit_counts, **supplement_counts}.items()):
        admitted = sum(point.get("source_observed_unit_counts", {}).get(
            source, point["unit_count"] if point["source_group"] == source else 0)
            for point in points)
        excluded = sum(row["unit_count"] for row in rejections if row["source_group"] == source)
        if admitted + excluded != total:
            raise ValueError(f"Publisher unit coverage mismatch: {source}")
        reason_units: dict[str, int] = defaultdict(int)
        for row in rejections:
            if row["source_group"] == source:
                for code in row["reason_codes"]:
                    reason_units[code] += row["unit_count"]
        coverage[source] = {"source_unit_ids": total, "admitted_unit_ids": admitted,
                            "excluded_unit_ids": excluded,
                            "exclusion_reason_unit_counts": dict(sorted(reason_units.items()))}
    if len({point["registry_id"] for point in points}) != len(points):
        raise ValueError("Duplicate opaque building identity")
    if len({point["point_id"] for point in points}) != len(points):
        raise ValueError("Duplicate opaque point identity")
    outputs = {"points": _jsonl_bytes(points), "groups": _jsonl_bytes(groups),
               "rejections": _jsonl_bytes(rejections)}
    output_shas = {key: _sha(value) for key, value in outputs.items()}
    inputs = [{"role": "delivery", "manifest_sha256": delivery_sha},
              {"role": "moscow_boundary", "manifest_sha256": _file_sha(boundary_manifest),
               "data_sha256": boundary_sha}, *extra_inputs, *geocode_inputs,
              *([crosswalk_input] if crosswalk_input else [])]
    fingerprint = _sha(_canonical({"rules": RULES, "inputs": inputs,
                                   "outputs": output_shas}))
    run_id = "developer-map-v1-" + fingerprint[:16]
    snapshot_id = "developer-map-snapshot-" + _sha(_canonical(output_shas))[:16]
    manifest = {"schema_version": "developer-map-snapshot/1", "rules_version": RULES,
                "run_id": run_id, "snapshot_id": snapshot_id,
                "buildings_total": len(points), "points_total": len(points),
                "map_points_total": sum(bool(point["map_eligible"]) for point in points),
                "groups_total": len(groups), "rejections_total": len(rejections),
                **{key + "_sha256": value for key, value in output_shas.items()},
                "source_delivery_manifest_sha256": delivery_sha,
                "inputs": inputs,
                "observed_at": max((point["observed_at"] for point in points), default=None),
                "rights_status": "source_metadata_rights_unverified",
                "source_unit_counts": source_unit_counts,
                "supplemental_unit_counts": supplement_counts,
                "coverage": coverage,
                "publication_scope": "building_metadata_only_no_prices_or_media"}
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs = output_root / "runs"
    runs.mkdir(exist_ok=True, mode=0o700)
    target = runs / run_id
    manifest_bytes = _canonical(manifest) + b"\n"
    if target.exists():
        if target.is_symlink() or any((target / f"{key}.jsonl").read_bytes() != value
                                      for key, value in outputs.items()) or (
                target / "manifest.json").read_bytes() != manifest_bytes:
            raise ValueError("Immutable run ID collision")
        return manifest
    staging = Path(tempfile.mkdtemp(prefix=".stage-developer-map-", dir=runs))
    os.chmod(staging, 0o700)
    try:
        for key, value in outputs.items():
            _write_private(staging / f"{key}.jsonl", value)
        _write_private(staging / "manifest.json", manifest_bytes)
        _fsync_dir(staging)
        os.replace(staging, target)
        _fsync_dir(runs)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    return manifest


def validate_run(output_root: Path, run_id: str) -> dict[str, Any]:
    if re.fullmatch(r"developer-map-v1-[0-9a-f]{16}", run_id) is None:
        raise ValueError("Invalid developer map run ID")
    directory = output_root / "runs" / run_id
    if directory.is_symlink():
        raise ValueError("Run is a symlink")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest.get("schema_version") != "developer-map-snapshot/1"
            or manifest.get("run_id") != run_id):
        raise ValueError("Invalid developer map manifest")
    for key in ("points", "groups", "rejections"):
        path = directory / f"{key}.jsonl"
        if path.is_symlink() or _file_sha(path) != manifest.get(f"{key}_sha256"):
            raise ValueError(f"Developer map {key} hash mismatch")
        if sum(1 for _ in path.open("rb")) != manifest.get(f"{key}_total"):
            raise ValueError(f"Developer map {key} count mismatch")
    if manifest["points_total"] != manifest["buildings_total"]:
        raise ValueError("Developer map building count mismatch")
    return manifest


def publish(output_root: Path, run_id: str, *, expected_active_sha: str | None = None) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = output_root / "publish.lock"
    with lock_path.open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = validate_run(output_root, run_id)
        pointer_path = output_root / "active.json"
        current = pointer_path.read_bytes() if pointer_path.exists() else None
        if expected_active_sha is not None and _sha(current or b"") != expected_active_sha:
            raise ValueError("Active developer map pointer changed")
        pointer = {"schema_version": "developer-map-pointer/1", "run_id": run_id,
                   "manifest_sha256": _file_sha(output_root / "runs" / run_id / "manifest.json")}
        if current != _canonical(pointer) + b"\n":
            _atomic_private_json(pointer_path, pointer)
        return pointer


def rollback_legacy(output_root: Path, *, expected_active_sha: str) -> dict[str, Any]:
    """Atomically restore the legacy view while retaining every immutable run."""
    if not HASH_RE.fullmatch(expected_active_sha):
        raise ValueError("Rollback requires the exact current pointer SHA")
    lock_path = output_root / "publish.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pointer_path = output_root / "active.json"
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise ValueError("Active developer map pointer is missing")
        current = pointer_path.read_bytes()
        if _sha(current) != expected_active_sha:
            raise ValueError("Active developer map pointer changed")
        pointer = json.loads(current)
        validate_run(output_root, pointer["run_id"])
        history = output_root / "pointer_history"
        history.mkdir(mode=0o700, exist_ok=True)
        archived = history / f"{expected_active_sha}.json"
        if archived.exists():
            if archived.read_bytes() != current:
                raise ValueError("Rollback pointer history collision")
        else:
            _write_private(archived, current)
            _fsync_dir(history)
        pointer_path.unlink()
        _fsync_dir(output_root)
        return {"rolled_back_to": "legacy", "retained_run_id": pointer["run_id"],
                "pointer_history_sha256": expected_active_sha}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path,
                        default=ROOT / "data/property/developer_map")
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build", help="Stage an immutable verified-building snapshot")
    build_cmd.add_argument("--delivery-manifest", required=True, type=Path)
    build_cmd.add_argument("--boundary-geojson", type=Path,
                           default=ROOT / "data/city/osm_moscow_city_boundary.geojson")
    build_cmd.add_argument("--boundary-manifest", type=Path,
                           default=ROOT / "data/city/osm_moscow_city_boundary.manifest.json")
    build_cmd.add_argument("--level-manifest", type=Path)
    build_cmd.add_argument("--etalon-manifest", type=Path)
    build_cmd.add_argument("--afi-manifest", type=Path)
    build_cmd.add_argument("--geocode-manifest", type=Path, action="append", default=[])
    build_cmd.add_argument("--crosswalk-manifest", type=Path)
    validate_cmd = commands.add_parser("validate", help="Validate an immutable staged run")
    validate_cmd.add_argument("run_id")
    publish_cmd = commands.add_parser("publish", help="Atomically activate a validated run")
    publish_cmd.add_argument("run_id")
    publish_cmd.add_argument("--expected-active-sha")
    rollback_cmd = commands.add_parser("rollback-legacy", help="CAS restore the legacy view")
    rollback_cmd.add_argument("--expected-active-sha", required=True)
    commands.add_parser("status", help="Read the current pointer without changing it")
    arguments = parser.parse_args()
    if arguments.command == "build":
        result = build(arguments.delivery_manifest, arguments.output_root,
                       arguments.boundary_geojson, arguments.boundary_manifest,
                       level_manifest=arguments.level_manifest,
                       etalon_manifest=arguments.etalon_manifest,
                       afi_manifest=arguments.afi_manifest,
                       geocode_manifests=tuple(arguments.geocode_manifest),
                       crosswalk_manifest=arguments.crosswalk_manifest)
    elif arguments.command == "validate":
        result = validate_run(arguments.output_root, arguments.run_id)
    elif arguments.command == "publish":
        result = publish(arguments.output_root, arguments.run_id,
                         expected_active_sha=arguments.expected_active_sha)
    elif arguments.command == "rollback-legacy":
        result = rollback_legacy(arguments.output_root,
                                 expected_active_sha=arguments.expected_active_sha)
    else:
        pointer_path = arguments.output_root / "active.json"
        result = json.loads(pointer_path.read_text()) if pointer_path.exists() else {"active": None}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
