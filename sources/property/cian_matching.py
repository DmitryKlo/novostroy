"""Offline CIAN-to-map identity review; never publishes or fetches CIAN pages.

The output is a private candidate ledger. Even an exact address is only a
review lead: a project page does not establish a specific building footprint.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".runtime/research/cian-matching"
RULES_VERSION = "cian-map-review/2"
MAX_INPUT_BYTES = 2_000_000
MAX_OBSERVATIONS = 100
MAX_LOCAL_OBJECTS = 1_000
MAX_OUTPUT_ROWS = 5_000
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,100}\Z")
MODEL_RE = re.compile(r"models-v1-[0-9a-f]{16}\Z")
POINT_RE = re.compile(r"mappt_[0-9a-f]{24}\Z")
CANDIDATE_RE = re.compile(r"cand_[0-9a-f]{24}\Z")
OSM_RECORD_RE = re.compile(r"(way|relation)/([1-9][0-9]*)/v([1-9][0-9]*)\Z")
ACCESS_METHODS = {"direct_rendered_page", "search_index", "prior_viewer_capture"}
CLAIMED_USES = {"residential_project", "commercial_building", "unknown"}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _read(path: Path, maximum: int = MAX_INPUT_BYTES) -> tuple[dict[str, Any], str]:
    if ".." in path.parts or path.is_symlink() or not path.is_file():
        raise ValueError("Unsafe or missing local input")
    size = path.stat().st_size
    if size > maximum:
        raise ValueError("Local input exceeds byte limit")
    body = path.read_bytes()
    value = json.loads(body.decode("utf-8"), object_pairs_hook=_object_pairs)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value, _sha(body)


def _time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Observation date must be explicit")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid observation date") from exc
    if parsed.tzinfo is None:
        raise ValueError("Observation date needs timezone")
    return parsed.isoformat()


def _text(value: Any, *, maximum: int = 240, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Invalid bounded text field")
    return value.strip()


def _cian_url(value: Any) -> str:
    value = _text(value, maximum=512)
    assert isinstance(value, str)
    parsed = urlsplit(value)
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid CIAN URL port") from exc
    if (parsed.scheme != "https" or host is None or
            not (host == "cian.ru" or host.endswith(".cian.ru")) or
            port is not None or parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or not parsed.path.startswith("/")):
        raise ValueError("CIAN source URL is not an exact HTTPS project/page URL")
    return value


def _source_file(root: Path, relative: Any, *, prefix: str, maximum: int) -> tuple[Path, str]:
    if (not isinstance(relative, str) or not relative.startswith(prefix)
            or relative.startswith("/") or ".." in Path(relative).parts):
        raise ValueError("Unsafe source evidence path")
    path = root
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Source evidence path contains symlink")
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Missing or oversized source evidence")
    return path, _sha(path.read_bytes())


def _identifier(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Invalid typed identity")
    return value


def _validate_observations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows = doc.get("records")
    if (doc.get("schema") != "cian-project-observations/1"
            or doc.get("schema_version", "cian-project-observations/1") != "cian-project-observations/1"
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_OBSERVATIONS):
        raise ValueError("Unsupported or oversized CIAN observations")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("CIAN observation must be an object")
        identity = _identifier(row.get("observation_id"), ID_RE)
        if identity in seen:
            raise ValueError("Duplicate CIAN observation")
        seen.add(identity)
        _cian_url(row.get("source_url"))
        _time(row.get("observed_at"))
        if row.get("source_updated_at") is not None:
            _time(row["source_updated_at"])
        if row.get("access_method") not in ACCESS_METHODS:
            raise ValueError("Unverified CIAN access method")
        if row.get("source_claimed_use") not in CLAIMED_USES:
            raise ValueError("Unverified CIAN use assertion")
        if row.get("page_scope") not in {"project", "building", "corpus"}:
            raise ValueError("Unknown CIAN page scope")
        _text(row.get("project_name"), nullable=True)
        _text(row.get("corpus_label"), nullable=True)
        address = row.get("address_components")
        if not isinstance(address, dict) or set(address) - {
                "city", "municipality", "street", "house", "corpus", "structure", "parcel"}:
            raise ValueError("Invalid typed CIAN address")
        for value in address.values():
            _text(value, maximum=120, nullable=True)
        for key in ("source_asserted_ids",):
            values = row.get(key, [])
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError("Too many source identity assertions")
            for value in values:
                _text(value, maximum=160)
        _text(row.get("evidence_notes"), maximum=1_000, nullable=True)
        _text(row.get("freshness_note"), maximum=400, nullable=True)
        lead = row.get("local_candidate")
        if not isinstance(lead, dict) or lead.get("research_lead_only") is not True:
            raise ValueError("Research target must be explicitly non-authoritative")
        _identifier(lead.get("map_point_id"), POINT_RE)
        if (not isinstance(lead.get("source_id"), str)
                or re.fullmatch(r"osm:(?:way|relation):[1-9][0-9]*", lead["source_id"]) is None
                or not isinstance(lead.get("source_version"), int)
                or lead["source_version"] < 1):
            raise ValueError("Research target lacks typed OSM identity")
        if row.get("match_assessment") not in {
                "exact_project_address_candidate_review_only",
                "project_match_corpus_ambiguous",
                "reject_candidate_pair_geographic_mismatch",
                "identity_unresolved_address_alias_unproven",
                "project_label_match_corpus_ambiguous",
                "project_exists_local_point_unlinked"}:
            raise ValueError("Unknown bounded reviewer annotation")
        for value in row.get("evidence_urls", []):
            _cian_url(value)
        for value in row.get("related_unmatched_urls", []):
            _cian_url(value)
    return rows


def _validate_local(doc: dict[str, Any], *, root: Path) -> list[dict[str, Any]]:
    rows = doc.get("records")
    if (doc.get("schema_version") != "cian-local-map-objects/1"
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_LOCAL_OBJECTS
            or doc.get("records_total") != len(rows)):
        raise ValueError("Unsupported or oversized local map object set")
    source = doc.get("source")
    active = doc.get("active_model")
    if not isinstance(source, dict) or not isinstance(active, dict):
        raise ValueError("Missing local source lineage")
    manifest_path, manifest_sha = _source_file(
        root, source.get("source_manifest_path"),
        prefix="data/property/osm_construction/", maximum=100_000)
    audit_path, audit_sha = _source_file(
        root, source.get("audit_jsonl_path"),
        prefix="data/property/osm_construction/", maximum=20_000_000)
    if (manifest_sha != source.get("source_manifest_sha256")
            or audit_sha != source.get("audit_jsonl_sha256")):
        raise ValueError("OSM source lineage hash mismatch")
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (source_manifest.get("output", {}).get("audit_sha256") != audit_sha
            or source_manifest.get("output", {}).get("audit_jsonl") !=
            source.get("audit_jsonl_path")
            or source_manifest.get("source_id") != source.get("dataset_id")):
        raise ValueError("OSM source manifest does not pin selected audit data")
    run_id = _identifier(active.get("model_run_id"), MODEL_RE)
    expected_model_path = f"data/models/{run_id}/manifest.json"
    if active.get("model_manifest_path") != expected_model_path:
        raise ValueError("Model manifest path differs from run identity")
    model_path, model_sha = _source_file(
        root, expected_model_path, prefix="data/models/", maximum=1_000_000)
    if model_sha != active.get("model_manifest_sha256"):
        raise ValueError("Model manifest hash mismatch")
    model_manifest = json.loads(model_path.read_text(encoding="utf-8"))
    if model_manifest.get("run_id") != run_id:
        raise ValueError("Model run identity mismatch")
    evaluation_path, evaluation_sha = _source_file(
        root, f"data/models/{run_id}/source_evaluations.jsonl",
        prefix="data/models/", maximum=20_000_000)
    if (evaluation_sha != active.get("source_evaluations_sha256")
            or evaluation_sha != model_manifest.get("source_evaluations_sha256")):
        raise ValueError("Model source evaluations hash mismatch")
    snapshot_path = active.get("map_api_snapshot_path")
    if snapshot_path != "/tmp/novostroy-map-points-review.json":
        raise ValueError("Map API snapshot path is outside the pinned review location")
    snapshot, snapshot_sha = _read(Path(snapshot_path), maximum=5_000_000)
    if (snapshot_sha != active.get("map_api_snapshot_sha256")
            or snapshot.get("model_run_id") != run_id
            or snapshot.get("registry_snapshot_id") != active.get("registry_snapshot_id")
            or not isinstance(snapshot.get("items"), list)):
        raise ValueError("Map API snapshot does not pin this run and eligibility")
    api_points = {item["point_id"]: item for item in snapshot["items"]
                  if isinstance(item, dict) and isinstance(item.get("point_id"), str)}
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    audit_by_id = {entry["source_record_id"]: entry for entry in audit_rows}
    evaluation_rows = [json.loads(line) for line in
                       evaluation_path.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
    mapped_evaluations = [entry for entry in evaluation_rows
                          if entry.get("source_group") == "osm_construction"]
    candidate_sources = {entry["candidate_id"]: entry["source_record_id"]
                         for entry in mapped_evaluations}
    if (len(audit_by_id) != len(audit_rows)
            or len(candidate_sources) != len(mapped_evaluations)):
        raise ValueError("Duplicate pinned source identity")
    seen_points: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid local map object")
        point_id = _identifier(row.get("map_point_id"), POINT_RE)
        candidate_id = _identifier(row.get("candidate_id"), CANDIDATE_RE)
        match = OSM_RECORD_RE.fullmatch(str(row.get("source_record_id")))
        if (point_id in seen_points or match is None
                or candidate_sources.get(candidate_id) != row["source_record_id"]
                or point_id != "mappt_" + _sha(f"mapped_construction\0{candidate_id}".encode())[:24]):
            raise ValueError("Local point identity is unverified or duplicated")
        seen_points.add(point_id)
        source_row = audit_by_id.get(row["source_record_id"])
        if (source_row is None or row.get("osm_type") != match.group(1)
                or row.get("osm_id") != int(match.group(2))
                or row.get("osm_version") != int(match.group(3))
                or row.get("geometry_sha256") != source_row.get("geometry_sha256")
                or row.get("source_name") != source_row.get("name")
                or row.get("source_updated_at") != source_row.get("source_updated_at")
                or row.get("address_tags") != json.loads(source_row.get("address_json", "{}"))):
            raise ValueError("Local point differs from pinned OSM source row")
        location = row.get("location")
        if (not isinstance(location, list) or len(location) != 2
                or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                       or not math.isfinite(v) for v in location)
                or not (36 <= location[0] <= 38 and 55 <= location[1] <= 57)):
            raise ValueError("Invalid local source-backed point")
        api_point = api_points.get(point_id)
        if (api_point is None or api_point.get("point_type") != "mapped_construction"
                or api_point.get("candidate_id") != candidate_id
                or api_point.get("location") != location):
            raise ValueError("Local point differs from pinned map API coordinate")
        source_url = row.get("source_url")
        if source_url != f"https://www.openstreetmap.org/{match.group(1)}/{match.group(2)}":
            raise ValueError("OSM source URL differs from typed identity")
    return rows


def _norm(value: str | None) -> str:
    if not value:
        return ""
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[«»\"'()]", " ", value)
    value = re.sub(r"\bул\.?\b", "улица", value)
    value = re.sub(r"\bпр-т\b", "проспект", value)
    return " ".join(re.findall(r"[а-яa-z0-9]+", value))


def _project_name(value: str | None) -> str:
    name = _norm(value)
    return re.sub(r"^(?:жк|жилой комплекс) ", "", name)


def _house_parts(value: str | None) -> tuple[str, str, str, str]:
    if not value:
        return "", "", "", ""
    text = value.casefold().replace("ё", "е").replace(" ", "")
    text = re.sub(r"^(?:дом|д\.?)", "", text)
    match = re.fullmatch(r"([0-9]+[а-я]?)(?:к(?:орпус)?([0-9а-я]+))?(?:стр?(?:оение)?([0-9а-я]+))?(?:/([0-9а-я]+))?", text)
    if match is None:
        return "", "", "", "unknown"
    return tuple(part or "" for part in match.groups())  # base, corpus, structure, slash


def _osm_url(source_record_id: str) -> str:
    match = OSM_RECORD_RE.fullmatch(source_record_id)
    if match is None:
        raise ValueError("Invalid typed OSM link")
    return f"https://www.openstreetmap.org/{match.group(1)}/{match.group(2)}"


def _compare(observation: dict[str, Any], local: dict[str, Any],
             *, address_peer_count: int) -> tuple[str, str, str]:
    address = observation["address_components"]
    tags = local["address_tags"]
    street = _norm(address.get("street"))
    local_street = _norm(tags.get("addr:street"))
    wanted_house = _house_parts(address.get("house"))
    actual_house = _house_parts(tags.get("addr:housenumber"))
    wanted_corpus = _norm(address.get("corpus")) or wanted_house[1]
    wanted_structure = _norm(address.get("structure")) or wanted_house[2]
    same_street = bool(street and local_street and street == local_street)
    same_house = bool(wanted_house[0] and actual_house[0] and
                      wanted_house[0] == actual_house[0])
    local_name = _project_name(local.get("complex_label") or local.get("source_name"))
    same_name = bool(_project_name(observation.get("project_name")) and
                     _project_name(observation.get("project_name")) == local_name)
    lead = observation["local_candidate"]
    research_lead = local["map_point_id"] == lead["map_point_id"]
    if not (same_street and same_house or same_name or research_lead):
        return "skip", "unrelated", "none"
    if research_lead and observation["match_assessment"] == "reject_candidate_pair_geographic_mismatch":
        return "rejected", "reviewed_pair_geographic_mismatch", "conflict"
    if observation["source_claimed_use"] == "commercial_building":
        return "rejected", "commercial_use_conflicts_with_residential_map_candidate", "conflict"
    source_municipality = _norm(address.get("municipality"))
    local_municipality = _norm(local.get("municipality"))
    if source_municipality and local_municipality and source_municipality != local_municipality:
        return "rejected", "municipality_conflict", "conflict"
    if same_street and same_house:
        if wanted_corpus and actual_house[1] and wanted_corpus != actual_house[1]:
            return "rejected", "corpus_conflict", "conflict"
        if wanted_structure and actual_house[2] and wanted_structure != actual_house[2]:
            return "rejected", "structure_conflict", "conflict"
        if actual_house[3] or wanted_house[3]:
            return "manual", "slash_address_semantics_unverified", "incomplete_address"
        if wanted_corpus and not actual_house[1]:
            return "manual", "local_corpus_unstated", "incomplete_address"
        if actual_house[1] and not wanted_corpus:
            return "manual", "cian_corpus_unstated", "incomplete_address"
        if wanted_structure and not actual_house[2]:
            return "manual", "local_structure_unstated", "incomplete_address"
        if actual_house[2] and not wanted_structure:
            return "manual", "cian_structure_unstated", "incomplete_address"
        if address_peer_count > 1:
            return "manual", "address_matches_multiple_local_objects", "ambiguous"
        return "review", "exact_address_project_scope_requires_building_proof", "address_agreement_only"
    if same_name and street and local_street and street != local_street:
        return "rejected", "street_conflict_despite_shared_name", "conflict"
    if same_name and wanted_house[0] and actual_house[0] and wanted_house[0] != actual_house[0]:
        return "rejected", "house_conflict_despite_shared_name", "conflict"
    if research_lead and street and local_street and street != local_street:
        return "rejected", "research_pair_street_conflict", "conflict"
    if research_lead and wanted_house[0] and actual_house[0] and wanted_house[0] != actual_house[0]:
        return "rejected", "research_pair_house_conflict", "conflict"
    if research_lead and not same_name:
        return "manual", "research_lead_without_source_identity", "insufficient"
    return "manual", "project_label_only_or_address_incomplete", "insufficient"


def _review_rows(observations: list[dict[str, Any]],
                 locals_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    by_point = {row["map_point_id"]: row for row in locals_}
    for observation in observations:
        lead = observation["local_candidate"]
        local_lead = by_point.get(lead["map_point_id"])
        if local_lead is None or lead["source_id"] != (
                f"osm:{local_lead['osm_type']}:{local_lead['osm_id']}") or (
                lead["source_version"] != local_lead["osm_version"]):
            raise ValueError("Research lead differs from pinned local object")
        address = observation["address_components"]
        street, house = _norm(address.get("street")), _house_parts(address.get("house"))[0]
        peer_count = sum(1 for local in locals_
                         if street and house and _norm(local["address_tags"].get("addr:street")) == street
                         and _house_parts(local["address_tags"].get("addr:housenumber"))[0] == house)
        before = len(results)
        for local in locals_:
            decision, reason, confidence = _compare(
                observation, local, address_peer_count=peer_count)
            if decision == "skip":
                continue
            results.append({
                "observation_id": observation["observation_id"],
                "cian_source_url": observation["source_url"],
                "cian_observed_at": observation["observed_at"],
                "cian_source_updated_at": observation.get("source_updated_at"),
                "cian_access_method": observation["access_method"],
                "cian_page_scope": observation["page_scope"],
                "cian_project_name": observation.get("project_name"),
                "cian_address": observation["address_components"],
                "source_claimed_use": observation["source_claimed_use"],
                "unmatched_source_page_use": observation.get("unmatched_source_page_use"),
                "agent_review_annotation": observation["match_assessment"],
                "review_authority": "agent_assisted_not_user_verified",
                "map_point_id": local["map_point_id"],
                "candidate_id": local["candidate_id"],
                "local_source_record_id": local["source_record_id"],
                "local_osm_url": _osm_url(local["source_record_id"]),
                "local_osm_address": local["address_tags"],
                "local_source_updated_at": local["source_updated_at"],
                "local_geometry_sha256": local["geometry_sha256"],
                "decision": decision, "reason_code": reason,
                "evidence_strength": confidence,
                "accepted_crosswalk": False,
                "coordinates_changed": False,
            })
        if len(results) == before:
            results.append({
                "observation_id": observation["observation_id"],
                "cian_source_url": observation["source_url"],
                "cian_observed_at": observation["observed_at"],
                "cian_source_updated_at": observation.get("source_updated_at"),
                "cian_access_method": observation["access_method"],
                "cian_page_scope": observation["page_scope"],
                "cian_project_name": observation.get("project_name"),
                "cian_address": observation["address_components"],
                "source_claimed_use": observation["source_claimed_use"],
                "unmatched_source_page_use": observation.get("unmatched_source_page_use"),
                "agent_review_annotation": observation["match_assessment"],
                "review_authority": "agent_assisted_not_user_verified",
                "map_point_id": None, "candidate_id": None,
                "local_source_record_id": None, "local_osm_address": None,
                "local_osm_url": None,
                "local_source_updated_at": None, "local_geometry_sha256": None,
                "decision": "manual", "reason_code": "no_comparable_local_evidence",
                "evidence_strength": "insufficient", "accepted_crosswalk": False,
                "coordinates_changed": False,
            })
    if len(results) > MAX_OUTPUT_ROWS:
        raise ValueError("Review candidate set exceeds bounded output")
    return sorted(results, key=lambda row: (row["observation_id"], row["map_point_id"] or ""))


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet formula prefixes are source-controlled, even in local review.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _artifact_bytes(rows: list[dict[str, Any]]) -> tuple[bytes, bytes, bytes]:
    jsonl = b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n").encode("utf-8") for row in rows)
    columns = ("observation_id", "decision", "reason_code", "evidence_strength",
               "map_point_id", "candidate_id", "local_source_record_id",
               "cian_source_url", "local_osm_url", "cian_observed_at", "cian_access_method",
               "cian_project_name", "source_claimed_use")
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows({key: _csv_cell(row.get(key)) for key in columns} for row in rows)
    csv_body = csv_buffer.getvalue().encode("utf-8-sig")
    labels = {"review": "Кандидат для проверки", "manual": "Недостаточно данных",
              "rejected": "Отклонена только пара"}
    explanations = {
        "exact_address_project_scope_requires_building_proof":
            "Адрес и корпус совпадают; связь с конкретным зданием требует отдельного подтверждения.",
        "local_corpus_unstated": "Корпус указан на странице проекта, но не у контура на карте.",
        "cian_corpus_unstated": "Корпус указан у контура, но не на странице проекта.",
        "slash_address_semantics_unverified":
            "Адрес через дробь нельзя автоматически приравнять к номеру строения или корпуса.",
        "reviewed_pair_geographic_mismatch":
            "Источники указывают на разные местности; отклонена эта пара, статус точки не изменён.",
        "research_lead_without_source_identity":
            "Точка выбрана для проверки, но связь с конкретным корпусом не доказана.",
        "project_label_only_or_address_incomplete":
            "Название проекта совпадает, адреса конкретного корпуса недостаточно.",
    }
    access_labels = {"search_index": "поисковый индекс",
                     "prior_viewer_capture": "предыдущее открытие страницы",
                     "direct_rendered_page": "открытая страница"}

    def link(url: str | None, label: str) -> str:
        if url is None:
            return "—"
        return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
                f'rel="noopener noreferrer">{html.escape(label)}</a>')

    def address(value: dict[str, Any] | None) -> str:
        if not value:
            return "—"
        return ", ".join(str(item) for item in value.values() if item) or "—"

    counts = Counter(row["decision"] for row in rows)
    html_rows = "\n".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in (
            html.escape(row["observation_id"]),
            link(row["cian_source_url"], "Открыть Циан"),
            html.escape(str(row.get("cian_project_name") or "—")),
            html.escape(address(row["cian_address"])),
            html.escape(f"{row['cian_observed_at']} · "
                        f"{access_labels.get(row['cian_access_method'], 'неизвестно')}; "
                        f"обновление источника: {row.get('cian_source_updated_at') or 'неизвестно'}"),
            link(row.get("local_osm_url"), row.get("local_source_record_id") or "OSM"),
            html.escape(address(row.get("local_osm_address"))),
            html.escape(labels[row["decision"]]),
            html.escape(explanations.get(row["reason_code"],
                                         "Требуется сверка исходных сведений; см. точный код в JSON/CSV.")),
        )) + "</tr>" for row in rows)
    headers = "".join(f"<th>{label}</th>" for label in
                      ("Наблюдение", "Источник", "Проект", "Адрес страницы", "Когда и как получено", "Точка карты",
                       "Адрес точки", "Итог", "Причина"))
    html_body = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                 "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
                 "<title>CIAN ↔ карта: ручная проверка</title>"
                 "<style>body{font:14px sans-serif;margin:2rem}table{border-collapse:collapse}"
                 "td,th{border:1px solid #bbb;padding:.4rem;vertical-align:top}"
                 "td{overflow-wrap:anywhere;max-width:20rem}</style>"
                 "<h1>Кандидаты связи CIAN ↔ карта</h1>"
                 "<p>Только локальная проверка. Ни одна связь не опубликована и координаты не изменены.</p>"
                 f"<p>Кандидаты: {counts['review']}; требуют уточнения: {counts['manual']}; "
                 f"отклонённые пары: {counts['rejected']}.</p>"
                 "<p>Даты обновления страниц могут быть неизвестны; наблюдение не доказывает текущий статус.</p>"
                 f"<table><thead><tr>{headers}</tr></thead><tbody>{html_rows}</tbody></table></html>"
                 ).encode("utf-8")
    return jsonl, csv_body, html_body


def review(observations_path: Path, local_path: Path,
           output_root: Path = DEFAULT_OUTPUT, *, root: Path = ROOT) -> Path:
    """Validate pinned local evidence and write an idempotent private review run."""
    observations, observations_sha = _read(observations_path)
    local, local_sha = _read(local_path)
    source_rows = _validate_observations(observations)
    local_rows = _validate_local(local, root=root)
    rows = _review_rows(source_rows, local_rows)
    implementation_sha = _sha(Path(__file__).read_bytes())
    run_hash = _sha(f"{RULES_VERSION}\0{implementation_sha}\0{observations_sha}\0{local_sha}".encode())
    run_id = f"cian-match-{run_hash[:16]}"
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output_root.is_symlink():
        raise ValueError("Unsafe review output directory")
    destination = output_root / "runs" / run_id
    if destination.exists():
        manifest, _ = _read(destination / "manifest.json")
        if (manifest.get("run_id") != run_id or
                manifest.get("input_sha256") != {"observations": observations_sha,
                                                 "local_objects": local_sha}):
            raise ValueError("Existing review run conflicts with current inputs")
        for name, metadata in manifest["files"].items():
            path = destination / name
            if not path.is_file() or _sha(path.read_bytes()) != metadata["sha256"]:
                raise ValueError("Existing review artifact hash mismatch")
        return destination
    (output_root / "runs").mkdir(mode=0o700, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".cian-match-", dir=output_root / "runs"))
    try:
        contents = dict(zip(("review.jsonl", "review.csv", "review.html"),
                            _artifact_bytes(rows), strict=True))
        for name, body in contents.items():
            path = staged / name
            path.write_bytes(body)
            path.chmod(0o600)
        counts = Counter(row["decision"] for row in rows)
        manifest = {
            "schema_version": "cian-map-matching-review/1",
            "rules_version": RULES_VERSION,
            "implementation_sha256": implementation_sha,
            "run_id": run_id,
            "input_sha256": {"observations": observations_sha,
                             "local_objects": local_sha},
            "source_lineage": {"osm_audit_sha256": local["source"]["audit_jsonl_sha256"],
                               "model_manifest_sha256": local["active_model"]["model_manifest_sha256"],
                               "model_run_id": local["active_model"]["model_run_id"]},
            "observations_total": len(source_rows),
            "local_points_checked": len(local_rows),
            "review_rows_total": len(rows),
            "decision_counts": {key: counts[key] for key in ("review", "manual", "rejected")},
            "accepted_crosswalks": 0,
            "live_cian_fetches": 0,
            "production_changes": 0,
            "publication_status": "private_review_only",
            "review_authority": "agent_assisted_not_user_verified",
            "files": {name: {"sha256": _sha(body), "bytes": len(body)}
                      for name, body in contents.items()},
        }
        path = staged / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                   indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        os.replace(staged, destination)
    except BaseException:
        for child in staged.iterdir():
            child.unlink()
        staged.rmdir()
        raise
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("local_objects", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    destination = review(args.observations, args.local_objects, args.output_root)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
