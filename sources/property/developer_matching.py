"""Offline, evidence-limited developer-unit and mapped-building identity review.

This module never fetches pages or writes to the product catalogue. A publisher
project ID establishes membership of its own units. A mapped building joins that
project only through a unique, typed building address or building-scoped ID.
"""

from __future__ import annotations

import csv
import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

RULES_VERSION = "developer-map-review/16"
MAX_ROWS = 250_000
MAX_FILE_BYTES = 100_000_000
MAX_SEARCH_PAGES_PER_ADDRESS = 5
SEARCH_FIRST_BATCH = 30
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGETS = ROOT / ".runtime/research/developer-harvest/targets.jsonl"
DEFAULT_OUTPUT = ROOT / ".runtime/research/developer-harvest/matching"
DEFAULT_CLAIMS = ROOT / ".runtime/research/developer-harvest/search_claims.json"
DEFAULT_MANUAL_LINKS = DEFAULT_OUTPUT / "manual_project_links.jsonl"
BASE_SEARCH_WORKERS = ("report_sources", "luna_cache", "luna_clinics")
SEARCH_WORKERS = (*BASE_SEARCH_WORKERS, "address_tail_a", "address_tail_b",
                  "luna_coverage")
OFFICIAL_SOURCE_GROUPS = frozenset({"a101", "fsk", "granel", "brusnika", "etalon"})
OFFICIAL_EVIDENCE_HOSTS = {
    "a101": frozenset({"a101.ru", "www.a101.ru", "cdn.a101.ru"}),
    "fsk": frozenset({"fsk.ru", "www.fsk.ru", "cdn.fsk.ru"}),
    "granel": frozenset({"granelle.ru", "www.granelle.ru"}),
    "brusnika": frozenset({"moskva.brusnika.ru"}),
    "etalon": frozenset({"etalongroup.ru", "www.etalongroup.ru"}),
}
OFFICIAL_PUBLIC_RECORD_HOSTS = frozenset({
    "mos.ru", "www.mos.ru", "xn--80az8a.xn--d1aqf.xn--p1ai",
})
REVIEWED_PROJECT_EVIDENCE_HOSTS = frozenset({
    "vestnikmoscow.mos.ru", "uznai.mos.ru", "partner.samolet.ru",
    "strana.com", "www.sminex.com", "hals-development.ru",
    "gk-osnova.ru", "plus.dev", "krost.ru", "metrofond.ru",
    "kutuzov-city.ru", "rodinapark.ru", "www.1-ng.ru",
    "www.lsrgroup.ru", "www.mosvodokanal.ru", "erzrf.ru",
    "sibakademstroy.brusnika.ru",
}) | OFFICIAL_PUBLIC_RECORD_HOSTS | frozenset().union(*OFFICIAL_EVIDENCE_HOSTS.values())
_CADASTRE = re.compile(r"77:[0-9]{2}:[0-9]{7}:[0-9]+\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_LOCAL_TARGET = re.compile(r"(?:estateline:[0-9]+|osm_construction:(?:way|relation)/[0-9]+/v[0-9]+)\Z")


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _value(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Identity/address value is not bounded text")
    text = str(value).strip()
    if len(text) > 240 or any(ord(char) < 32 for char in text):
        raise ValueError("Identity/address value is not bounded text")
    return text


def _identity(value: Any) -> str:
    text = _value(value)
    if not text or _ID.fullmatch(text) is None:
        raise ValueError("Invalid source-owned identity")
    return text


def _norm(value: Any) -> str:
    text = _value(value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[а-яa-z0-9]+", text))


def _street(value: Any) -> str:
    text = _norm(value)
    if not text:
        return ""
    aliases = {"ул": "улица", "пркт": "проспект", "просп": "проспект",
               "пер": "переулок", "наб": "набережная",
               "ш": "шоссе", "бульв": "бульвар"}
    parts = [aliases.get(part, part) for part in text.split()]
    kinds = {"улица", "проспект", "проезд", "переулок", "набережная",
             "шоссе", "бульвар"}
    if parts and parts[0] in kinds:
        parts = parts[1:] + parts[:1]
    return " ".join(parts)


def _house(value: Any) -> str:
    text = _value(value).casefold().replace("ё", "е").strip()
    text = re.sub(r"^(?:дом|д\.?)(?=\s*\d)\s*", "", text)
    # Preserve / and -: 16/1 must never become 161 or an asserted corpus.
    return re.sub(r"\s+", "", text).replace("№", "")


@dataclass(frozen=True)
class Address:
    city: str
    municipality: str
    street: str
    house: str
    corpus: str
    structure: str
    plot: str
    quality: str

    @classmethod
    def from_source(cls, value: Mapping[str, Any] | None,
                    quality: str | None = None) -> Address:
        if value is None:
            return cls("", "", "", "", "", "", "", "missing")
        if not isinstance(value, Mapping):
            raise ValueError("Typed address must be an object")
        quality = _value(quality or value.get("parse_quality") or value.get("quality") or "partial")
        if quality not in {"exact", "exact_street_house_only",
                           "exact_source_tag_presence", "partial", "ambiguous", "missing"}:
            raise ValueError("Unsupported address quality")
        return cls(_norm(value.get("city")), _norm(value.get("municipality")),
                   _street(value.get("street")), _house(value.get("house")),
                   _house(value.get("corpus")), _house(value.get("structure")),
                   _house(value.get("plot")), quality)

    @property
    def stem(self) -> tuple[str, str, str]:
        return self.city, self.street, self.house

    @property
    def full(self) -> tuple[str, str, str, str, str, str]:
        return (self.city, self.municipality, self.street, self.house,
                self.corpus, self.structure)


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason_code: str
    evidence_basis: str


def _building_ids(row: Mapping[str, Any]) -> set[tuple[str, str]]:
    values = row.get("official_ids") or []
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError("Official IDs must be a bounded list")
    result: set[tuple[str, str]] = set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Invalid official ID assertion")
        if item.get("scope") == "building":
            result.add((_identity(item.get("namespace")), _identity(item.get("value"))))
    return result


def compare_building_target(building: Mapping[str, Any],
                            target: Mapping[str, Any]) -> Verdict:
    """Compare one publisher building to one local target; uniqueness is external."""
    components = building.get("address_components")
    source_quality = building.get("address_parse_quality")
    if (not source_quality and isinstance(components, Mapping)
            and not components.get("parse_quality") and not components.get("quality")
            and all(components.get(key) for key in ("city", "street", "house"))):
        # These components were copied from explicit publisher fields. The
        # unparsed full_address string does not receive this status.
        source_quality = "exact"
    source = Address.from_source(components, source_quality)
    local = Address.from_source(target.get("address_components"),
                                target.get("address_parse_quality"))
    source_ids, target_ids = _building_ids(building), _building_ids(target)
    if source.city and local.city and source.city != local.city:
        return Verdict("rejected", "city_conflict", "typed_address")
    if (source.municipality and local.municipality
            and source.municipality != local.municipality):
        return Verdict("rejected", "municipality_conflict", "typed_address")
    if source_ids & target_ids:
        if local.city != "москва" or source.city not in {"", "москва"}:
            return Verdict("review", "city_scope_needs_review", "official_building_id")
        if target.get("entity_scope") not in {"mapped_building", "building"}:
            return Verdict("review", "local_scope_unverified", "official_building_id")
        return Verdict("accepted", "official_building_id_exact", "official_building_id")
    if source.city != "москва" or local.city != "москва":
        return Verdict("review", "city_scope_needs_review", "typed_address")
    if source.quality != "exact" or local.quality not in {
            "exact", "exact_source_tag_presence"}:
        return Verdict("review", "address_not_exact", "typed_address")
    if not source.city or not local.city or not source.street or not local.street:
        return Verdict("review", "address_incomplete", "typed_address")
    if source.street != local.street:
        return Verdict("unmatched", "street_differs", "typed_address")
    if source.plot or local.plot:
        return Verdict("review", "plot_vs_building_address", "typed_address")
    if source.house.startswith(("вл", "уч", "з/у")) or local.house.startswith(("вл", "уч", "з/у")):
        return Verdict("review", "holding_not_finished_house", "typed_address")
    if not source.house or not local.house:
        return Verdict("review", "house_missing", "typed_address")
    if source.house != local.house:
        combined_source = source.house + ("к" + source.corpus if source.corpus else "")
        combined_local = local.house + ("к" + local.corpus if local.corpus else "")
        if combined_source == combined_local:
            return Verdict("review", "combined_house_corpus_not_verified", "typed_address")
        combined_source = source.house + ("с" + source.structure if source.structure else "")
        combined_local = local.house + ("с" + local.structure if local.structure else "")
        if combined_source == combined_local:
            return Verdict("review", "combined_house_structure_not_verified", "typed_address")
        return Verdict("rejected", "house_conflict", "typed_address")
    if source.corpus and local.corpus and source.corpus != local.corpus:
        return Verdict("rejected", "corpus_conflict", "typed_address")
    if source.structure and local.structure and source.structure != local.structure:
        return Verdict("rejected", "structure_conflict", "typed_address")
    if source.corpus != local.corpus or source.structure != local.structure:
        return Verdict("review", "corpus_or_structure_missing", "typed_address")
    if source.municipality != local.municipality:
        return Verdict("review", "municipality_missing_one_side", "typed_address")
    if not source.corpus and not source.structure:
        return Verdict("review", "house_only_without_building_key", "typed_address")
    if target.get("entity_scope") not in {"mapped_building", "building"}:
        return Verdict("review", "local_scope_unverified", "typed_address")
    return Verdict("accepted", "full_building_address_exact", "typed_address")


def compare_project_target(project: Mapping[str, Any],
                           target: Mapping[str, Any], *, official: bool) -> Verdict:
    """Join a source project to an address only; never assert a building footprint."""
    components = project.get("address_components")
    source_quality = project.get("address_parse_quality")
    if (not source_quality and isinstance(components, Mapping)
            and not components.get("parse_quality") and not components.get("quality")
            and all(components.get(key) for key in ("city", "street", "house"))):
        source_quality = "exact"
    source = Address.from_source(components, source_quality)
    local = Address.from_source(target.get("address_components"),
                                target.get("address_parse_quality"))
    if source.city and local.city and source.city != local.city:
        return Verdict("rejected", "city_conflict", "project_address")
    if (source.municipality and local.municipality
            and source.municipality != local.municipality):
        return Verdict("rejected", "municipality_conflict", "project_address")
    if source.city != "москва" or local.city != "москва":
        return Verdict("review", "city_scope_needs_review", "project_address")
    if source.quality != "exact" or local.quality not in {
            "exact", "exact_street_house_only", "exact_source_tag_presence"}:
        return Verdict("review", "address_not_exact", "project_address")
    if not source.street or not local.street or not source.house or not local.house:
        return Verdict("review", "address_incomplete", "project_address")
    if source.street != local.street:
        return Verdict("unmatched", "street_differs", "project_address")
    if source.plot or local.plot or source.house.startswith(("вл", "уч", "з/у")) \
            or local.house.startswith(("вл", "уч", "з/у")):
        return Verdict("review", "plot_vs_building_address", "project_address")
    if source.house != local.house:
        return Verdict("rejected", "house_conflict", "project_address")
    if (source.corpus != local.corpus or source.structure != local.structure):
        if ((source.corpus and local.corpus and source.corpus != local.corpus)
                or (source.structure and local.structure and source.structure != local.structure)):
            return Verdict("rejected", "corpus_conflict", "project_address")
        return Verdict("review", "corpus_or_structure_missing", "project_address")
    if source.municipality != local.municipality:
        return Verdict("review", "municipality_missing_one_side", "project_address")
    if not official:
        return Verdict("review", "third_party_project_address", "project_address")
    return Verdict("accepted", "unique_full_project_address", "project_address")


def _site_words(value: Any, *, max_length: int = 1200) -> list[str]:
    if (not isinstance(value, str) or not value or len(value) > max_length
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Invalid project site address text")
    return re.findall(r"[а-яa-z0-9]+", value.casefold().replace("ё", "е"))


def _holding_site_key(fragment: Any) -> tuple[str, str]:
    """Require an explicit street and holding number; this is project scope only."""
    words = _site_words(fragment, max_length=160)
    if words.count("вл") != 1:
        raise ValueError("Project site lacks one explicit holding marker")
    at = words.index("вл")
    if at < 1 or at + 2 != len(words) or re.fullmatch(r"[0-9]+[а-я]?", words[-1]) is None:
        raise ValueError("Project site lacks a bounded holding address")
    street = [word for word in words[:at]
              if word not in {"ул", "улица", "пр", "проезд", "пер", "переулок"}]
    if not street or not any(re.search(r"[а-я]", word) for word in street):
        raise ValueError("Project site lacks a street name")
    return " ".join(street), words[-1]


def group_id(source_group: str, publisher_project_id: str) -> str:
    scope = f"{_identity(source_group)}\0{_identity(publisher_project_id)}"
    return "devgroup_" + _digest(scope.encode("utf-8"))[:24]


def _source_url(value: Any, allowed_hosts: Iterable[str]) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing source URL")
    text = value.strip()
    if not text or len(text) > 1000 or any(ord(char) < 32 for char in text):
        raise ValueError("Missing or oversized source URL")
    url = urlsplit(text)
    try:
        port = url.port
    except ValueError as exc:
        raise ValueError("Invalid source URL port") from exc
    if (url.scheme != "https" or url.hostname not in set(allowed_hosts)
            or port is not None or url.username is not None or url.password is not None
            or not url.path.startswith("/") or url.fragment):
        raise ValueError("Source URL is outside provider allowlist")
    return text


def _target_id(target: Mapping[str, Any]) -> str:
    for key in ("target_id", "registry_id", "candidate_id", "map_point_id"):
        if target.get(key):
            if isinstance(target[key], str) and _LOCAL_TARGET.fullmatch(target[key]):
                return target[key]
            return _identity(target[key])
    raise ValueError("Local target lacks typed identity")


def _building_ref(source_group: str, project_id: str, building: Mapping[str, Any],
                  observation_id: str) -> str:
    building_id = _value(building.get("publisher_building_id"))
    address = Address.from_source(building.get("address_components"))
    # A repeated exact source address is one building assertion within its
    # publisher project, but it is not a fabricated publisher_building_id.
    basis = building_id or ("source-address:" + "|".join(address.full)
                            if address.street and address.house else observation_id)
    return "devbld_" + _digest(f"{source_group}\0{project_id}\0{basis}".encode())[:24]


def _scope(row: Mapping[str, Any]) -> str:
    return row.get("location_scope") or (row.get("source_fields") or {}).get("location_scope") or "unknown"


def _project(row: Mapping[str, Any]) -> Mapping[str, Any]:
    project = row.get("project")
    if isinstance(project, Mapping):
        return project
    return {"publisher_project_id": row.get("publisher_project_id"),
            "name": row.get("project_name"), "address": row.get("address"),
            "address_components": row.get("address_components"),
            "address_parse_quality": row.get("address_parse_quality")}


def _building(row: Mapping[str, Any]) -> Mapping[str, Any]:
    building = row.get("building")
    if isinstance(building, Mapping):
        return building
    return {"publisher_building_id": row.get("publisher_building_id"),
            "corpus": row.get("corpus"), "structure": row.get("structure"),
            "full_address": row.get("address"),
            "address_components": row.get("address_components")}


def review_matches(units: list[dict[str, Any]], projects: list[dict[str, Any]],
                   targets: list[dict[str, Any]],
                   providers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Build publisher groups and conservative local-building match verdicts."""
    if any(len(rows) > MAX_ROWS for rows in (units, projects, targets)):
        raise ValueError("Review input exceeds row limit")
    publisher_projects: dict[tuple[str, str], dict[str, Any]] = {}
    group_members: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_urls: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_scopes: dict[tuple[str, str], set[str]] = defaultdict(set)
    projects_out: list[dict[str, Any]] = []
    units_out: list[dict[str, Any]] = []
    seen_units: set[tuple[str, str]] = set()

    def allowed(source_group: str) -> set[str]:
        value = providers.get(source_group)
        if not isinstance(value, Mapping) or not isinstance(value.get("allowed_hosts"), list):
            raise ValueError("Provider host allowlist missing")
        hosts = value["allowed_hosts"]
        if not hosts or len(hosts) > 20 or any(not isinstance(h, str) for h in hosts):
            raise ValueError("Invalid provider host allowlist")
        return set(hosts)

    for row in projects:
        source_group = _identity(row.get("source_group"))
        project = _project(row)
        project_id = _value(project.get("publisher_project_id"))
        url = _source_url(row.get("source_url"), allowed(source_group))
        observation_id = _identity(row.get("observation_id"))
        if row.get("identity_conflict") is True or row.get("identity_quality") != "publisher_asserted":
            projects_out.append({"observation_id": observation_id, "source_group": source_group,
                                 "source_url": url, "decision": "review",
                                 "reason_code": "publisher_identity_conflict" if row.get("identity_conflict")
                                 else "publisher_project_identity_unverified", "group_id": None})
            continue
        if not project_id:
            projects_out.append({"observation_id": observation_id, "source_group": source_group,
                                 "source_url": url, "decision": "review",
                                 "reason_code": "publisher_project_id_missing", "group_id": None})
            continue
        key = source_group, _identity(project_id)
        source_scope = _scope(row)
        publisher_projects.setdefault(key, row)
        group_members.setdefault(key, set())
        group_urls[key].add(url)
        group_scopes[key].add(source_scope)
        if project.get("name"):
            group_names[key].add(_value(project["name"]))
        if source_scope != "in_scope_moscow":
            projects_out.append({"observation_id": observation_id, "source_group": source_group,
                                 "source_url": url, "publisher_project_id": project_id,
                                 "decision": "review", "reason_code": "moscow_scope_unverified",
                                 "group_id": group_id(*key),
                                 "source_location_scope": source_scope})
            continue
        projects_out.append({"observation_id": observation_id, "source_group": source_group,
                             "source_url": url, "publisher_project_id": project_id,
                             "decision": "accepted", "reason_code": "publisher_project_id_asserted",
                             "group_id": group_id(*key),
                             "source_location_scope": source_scope})

    for row in units:
        source_group = _identity(row.get("source_group"))
        url = _source_url(row.get("source_url"), allowed(source_group))
        observation_id = _identity(row.get("observation_id"))
        project = _project(row)
        unit = row.get("unit") or {}
        project_id = _value(project.get("publisher_project_id"))
        unit_id = _value(unit.get("publisher_unit_id"))
        scope = _scope(row)
        if scope != "in_scope_moscow":
            units_out.append({"observation_id": observation_id, "source_group": source_group,
                              "source_url": url, "decision": "review",
                              "reason_code": "moscow_scope_unverified", "group_id": None})
            continue
        if row.get("identity_conflict") is True:
            units_out.append({"observation_id": observation_id, "source_group": source_group,
                              "source_url": url, "decision": "review",
                              "reason_code": "publisher_identity_conflict", "group_id": None})
            continue
        if row.get("identity_quality") != "publisher_asserted":
            units_out.append({"observation_id": observation_id, "source_group": source_group,
                              "source_url": url, "decision": "review",
                              "reason_code": "publisher_unit_identity_unverified", "group_id": None})
            continue
        if not unit_id:
            units_out.append({"observation_id": observation_id, "source_group": source_group,
                              "source_url": url, "decision": "review",
                              "reason_code": "publisher_unit_id_missing", "group_id": None})
            continue
        identity = source_group, _identity(unit_id)
        if identity in seen_units:
            raise ValueError("Duplicate publisher unit ID in source group")
        seen_units.add(identity)
        if not project_id:
            units_out.append({"observation_id": observation_id, "source_group": source_group,
                              "source_url": url, "publisher_unit_id": unit_id,
                              "decision": "review", "reason_code": "publisher_project_id_missing",
                              "group_id": None})
            continue
        project_id = _identity(project_id)
        key = source_group, project_id
        group_members[key].add(unit_id)
        group_urls[key].add(url)
        group_scopes[key].add(scope)
        if project.get("name"):
            group_names[key].add(_value(project["name"]))
        units_out.append({"observation_id": observation_id, "source_group": source_group,
                          "source_url": url, "publisher_unit_id": unit_id,
                          "publisher_project_id": project_id,
                          "decision": "accepted", "reason_code": "publisher_project_id_asserted",
                          "group_id": group_id(*key), "map_target_id": None})

    target_by_id = {_target_id(row): row for row in targets}
    if len(target_by_id) != len(targets):
        raise ValueError("Duplicate local target ID")
    buildings: dict[str, dict[str, Any]] = {}
    conflicted_buildings: set[str] = set()
    for row in [*projects, *units]:
        if (_scope(row) != "in_scope_moscow"
                or row.get("identity_conflict") is True):
            continue
        source_group = _identity(row.get("source_group"))
        project = _project(row)
        project_id = _value(project.get("publisher_project_id"))
        building = _building(row)
        if not project_id or not isinstance(building, Mapping):
            continue
        if row.get("record_type") == "project" and not building.get("publisher_building_id"):
            continue
        # A project-only page does not claim a particular building or footprint.
        if not building.get("publisher_building_id") and not building.get("address_components"):
            continue
        observation_id = _identity(row.get("observation_id"))
        ref = _building_ref(source_group, project_id, building, observation_id)
        if ref in buildings and buildings[ref]["building"] != building:
            conflicted_buildings.add(ref)
            continue
        buildings[ref] = {"source_group": source_group, "publisher_project_id": project_id,
                          "building": building, "source_url": row["source_url"],
                          "observation_id": observation_id,
                          "observed_at": row.get("observed_at"),
                          "source_updated_at": row.get("source_updated_at"),
                          "source_rights_status": row.get("source_rights_status"),
                          "building_ref": ref, "group_id": group_id(source_group, project_id)}

    # Evaluate only typed same-street leads or exact building-scoped official IDs.
    by_street: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_official: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for target in targets:
        address = Address.from_source(target.get("address_components"),
                                      target.get("address_parse_quality"))
        if address.street:
            by_street[address.street].append(target)
        for identity in _building_ids(target):
            by_official[identity].append(target)
    project_assertions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in projects:
        if (_scope(row) != "out_of_scope" and row.get("identity_conflict") is not True
                and row.get("identity_quality") == "publisher_asserted"):
            source_group = _identity(row.get("source_group"))
            project = _project(row)
            if project.get("publisher_project_id"):
                project_assertions[(source_group,
                                    _identity(project["publisher_project_id"]))].append(row)
    project_matches: list[dict[str, Any]] = []
    for (source_group, project_id), assertions in sorted(project_assertions.items()):
        addresses = set()
        for row in assertions:
            components = _project(row).get("address_components")
            parsed = Address.from_source(components)
            if parsed.street and parsed.house:
                addresses.add((*parsed.full, parsed.plot))
        conflicting_addresses = len(addresses) > 1
        seen: set[tuple[str, ...]] = set()
        for row in sorted(assertions, key=lambda item: item["source_url"]):
            project = _project(row)
            components = project.get("address_components")
            parsed = Address.from_source(components)
            address_key = (*parsed.full, parsed.plot)
            if address_key in seen:
                continue
            seen.add(address_key)
            base = {"source_group": source_group, "publisher_project_id": project_id,
                    "project_name": project.get("name"), "source_url": row["source_url"],
                    "observation_id": row["observation_id"],
                    "observed_at": row.get("observed_at"),
                    "source_updated_at": row.get("source_updated_at"),
                    "source_rights_status": row.get("source_rights_status"),
                    "project_address": components, "association_scope": "project_address_only",
                    "geometry_claimed": False, "source_role": (
                        "developer_site" if source_group in OFFICIAL_SOURCE_GROUPS
                        else "third_party_aggregator" if source_group == "proplanirovki"
                        else "source_role_unverified")}
            if not parsed.street or not parsed.house:
                project_matches.append({**base, "target_id": None, "target_source_url": None,
                                        "decision": "review", "reason_code": "address_not_exact",
                                        "evidence_basis": "project_address"})
                continue
            evaluations = []
            for target in by_street.get(parsed.street, []):
                verdict = compare_project_target(project, target,
                                                 official=source_group in OFFICIAL_SOURCE_GROUPS)
                if verdict.decision in {"unmatched", "rejected"}:
                    continue
                evaluations.append({**base, "target_id": _target_id(target),
                                    "target_source_url": target.get("source_url"),
                                    "target_address": target.get("address_components"),
                                    "target_visibility": target.get("visibility"),
                                    "decision": verdict.decision,
                                    "reason_code": verdict.reason_code,
                                    "evidence_basis": verdict.evidence_basis})
            if not evaluations:
                project_matches.append({**base, "target_id": None, "target_source_url": None,
                                        "decision": "unmatched",
                                        "reason_code": "no_exact_typed_candidate",
                                        "evidence_basis": "project_address"})
            else:
                if conflicting_addresses:
                    for item in evaluations:
                        item["decision"] = "review"
                        item["reason_code"] = "publisher_project_address_conflict"
                accepted = [item for item in evaluations if item["decision"] == "accepted"]
                if len(accepted) != 1:
                    for item in accepted:
                        item["decision"] = "review"
                        item["reason_code"] = "multiple_local_targets"
                project_matches.extend(evaluations)
    accepted_by_project_target = Counter(item["target_id"] for item in project_matches
                                         if item["decision"] == "accepted")
    for item in project_matches:
        if item["decision"] == "accepted" and accepted_by_project_target[item["target_id"]] != 1:
            item["decision"], item["reason_code"] = "review", "multiple_publisher_projects"
    candidate_rows: list[dict[str, Any]] = []
    for ref, value in sorted(buildings.items()):
        building = value["building"]
        address = Address.from_source(building.get("address_components"))
        candidate_targets = {_target_id(row): row for row in by_street.get(address.street, [])}
        for official in _building_ids(building):
            candidate_targets.update({_target_id(row): row for row in by_official.get(official, [])})
        evaluations: list[dict[str, Any]] = []
        for target_id, target in sorted(candidate_targets.items()):
            verdict = compare_building_target(building, target)
            if verdict.decision == "unmatched":
                continue
            if verdict.decision == "accepted" and value["source_group"] not in OFFICIAL_SOURCE_GROUPS:
                verdict = Verdict("review", "third_party_building_address",
                                  verdict.evidence_basis)
            evaluations.append({**value, "target_id": target_id,
                                "map_point_id": target.get("map_point_id")
                                if target.get("map_candidate_available") else None,
                                "target_source_url": target.get("source_url"),
                                "target_address": target.get("address_components"),
                                "decision": verdict.decision, "reason_code": verdict.reason_code,
                                "evidence_basis": verdict.evidence_basis})
        if not evaluations:
            candidate_rows.append({**value, "target_id": None, "target_source_url": None,
                                   "target_address": None, "decision": "unmatched",
                                   "reason_code": "no_exact_typed_candidate",
                                   "evidence_basis": "none"})
        else:
            if ref in conflicted_buildings:
                for item in evaluations:
                    item["decision"] = "review"
                    item["reason_code"] = "publisher_building_assertion_conflict"
            accepted = [row for row in evaluations if row["decision"] == "accepted"]
            if len(accepted) != 1:
                for item in accepted:
                    item["decision"] = "review"
                    item["reason_code"] = "multiple_local_targets"
            candidate_rows.extend(evaluations)
    # A local target cannot automatically belong to two publisher buildings.
    accepted_by_target = Counter(row["target_id"] for row in candidate_rows
                                 if row["decision"] == "accepted")
    for row in candidate_rows:
        if row["decision"] == "accepted" and accepted_by_target[row["target_id"]] != 1:
            row["decision"], row["reason_code"] = "review", "multiple_publisher_buildings"

    project_links: dict[tuple[str, str], set[str]] = defaultdict(set)
    building_links: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in project_matches:
        if item["decision"] == "accepted":
            project_links[(item["source_group"], item["publisher_project_id"])].add(
                item["target_id"])
    for item in candidate_rows:
        if item["decision"] == "accepted":
            building_links[(item["source_group"], item["publisher_project_id"])].add(
                item["target_id"])
    unit_inputs = {_identity(row.get("observation_id")): row for row in units}
    for item in units_out:
        source = unit_inputs[item["observation_id"]]
        building = _building(source)
        project = _project(source)
        unit_value = source.get("unit") or {}
        item.update({"publisher_unit_id": item.get("publisher_unit_id") or
                     unit_value.get("publisher_unit_id"),
                     "project_name": project.get("name"),
                     "project_address": project.get("address"),
                     "building_address": building.get("full_address"),
                     "unit_number": unit_value.get("number"),
                     "area_m2": unit_value.get("area_m2"),
                     "rooms": unit_value.get("rooms"),
                     "floor": unit_value.get("floor"),
                     "publisher_project_id": item.get("publisher_project_id") or
                     project.get("publisher_project_id"),
                     "publisher_building_id": building.get("publisher_building_id"),
                     "corpus": building.get("corpus"),
                     "structure": building.get("structure"),
                     "observed_at": source.get("observed_at"),
                     "source_updated_at": source.get("source_updated_at"),
                     "price_rub": source.get("price_rub"),
                     "price_basis": source.get("price_basis"),
                     "source_rights_status": source.get("source_rights_status")})
        key = (item["source_group"], item.get("publisher_project_id"))
        item["accepted_project_target_ids"] = sorted(project_links.get(key, set()))
        item["accepted_building_target_ids"] = sorted(building_links.get(key, set()))

    groups = []
    for key, members in sorted(group_members.items()):
        attached = sorted({row["target_id"] for row in candidate_rows
                           if row["decision"] == "accepted"
                           and (row["source_group"], row["publisher_project_id"]) == key})
        groups.append({"group_id": group_id(*key), "source_group": key[0],
                       "publisher_project_id": key[1],
                       "source_role": ("developer_site" if key[0] in OFFICIAL_SOURCE_GROUPS
                                       else "third_party_aggregator" if key[0] == "proplanirovki"
                                       else "source_role_unverified"),
                       "name_assertions": sorted(group_names[key]),
                       "source_location_scopes": sorted(group_scopes[key]),
                       "publisher_unit_ids": sorted(members),
                       "accepted_local_target_ids": attached,
                       "accepted_project_target_ids": sorted(project_links.get(key, set())),
                       "source_urls": sorted(group_urls[key]),
                       "group_basis": "source_project_id_exact",
                       "map_link_status": ("building_verified" if attached else
                                           "project_address_only" if project_links.get(key)
                                           else "unlinked"),
                       "publication_allowed": False})
    return {"units": units_out, "projects": projects_out,
            "project_matches": project_matches,
            "building_matches": candidate_rows, "groups": groups}


def apply_reviewed_project_links(result: dict[str, Any], links: list[dict[str, Any]],
                                 projects: list[dict[str, Any]],
                                 targets: list[dict[str, Any]],
                                 research_root: Path) -> None:
    """Accept a project identity only when pinned official cadastral evidence verifies it."""
    project_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in projects:
        if (row.get("identity_quality") == "publisher_asserted"
                and _scope(row) != "out_of_scope"):
            project = _project(row)
            if project.get("publisher_project_id"):
                project_rows[(row["source_group"], str(project["publisher_project_id"]))].append(row)
    target_rows = {_target_id(row): row for row in targets}
    seen_pairs: set[tuple[str, str, str]] = set()
    for link in links:
        if link.get("schema_version") != "developer-project-adjudication/1":
            raise ValueError("Unsupported manual project adjudication")
        if link.get("reviewer_kind") != "agent":
            raise ValueError("Project adjudication must identify agent review")
        group = _identity(link.get("source_group"))
        project_id = _identity(link.get("publisher_project_id"))
        target_id = link.get("target_id")
        if (group not in OFFICIAL_SOURCE_GROUPS or not isinstance(target_id, str)
                or target_id not in target_rows):
            raise ValueError("Adjudication source/target is not eligible")
        key = group, project_id, target_id
        if key in seen_pairs:
            raise ValueError("Duplicate project adjudication")
        seen_pairs.add(key)
        source_rows = project_rows.get((group, project_id), [])
        project_names = {_norm(_project(row).get("name")) for row in source_rows
                         if _project(row).get("name")}
        project_name = _value(link.get("project_name"))
        same_name_ids = {_identity(_project(row)["publisher_project_id"])
                         for row in projects if row.get("source_group") == group
                         and _project(row).get("publisher_project_id")
                         and _norm(_project(row).get("name")) == _norm(project_name)}
        if (not source_rows or project_names != {_norm(project_name)}
                or same_name_ids != {project_id}):
            raise ValueError("Adjudication project ID/name is not unique in harvest")
        match_basis = link.get("match_basis", "official_cadastre_project_identity")
        if match_basis not in {"official_cadastre_project_identity",
                               "official_full_project_site_address"}:
            raise ValueError("Unsupported project adjudication basis")
        cadastre = _value(link.get("target_cadastre"))
        source_fragment = link.get("source_address_fragment")
        target_fragment = link.get("target_address_fragment")
        if match_basis == "official_cadastre_project_identity":
            if _CADASTRE.fullmatch(cadastre) is None or cadastre not in str(
                    target_rows[target_id].get("address") or ""):
                raise ValueError("Adjudication cadastral token is absent from target")
        else:
            source_key = _holding_site_key(source_fragment)
            if source_key != _holding_site_key(target_fragment):
                raise ValueError("Project site addresses differ")
            source_words = " ".join(_site_words(source_fragment))
            target_words = " ".join(_site_words(target_fragment))
            if not any(source_words in " ".join(_site_words(
                    _project(row).get("address")))
                    for row in source_rows if _project(row).get("address")):
                raise ValueError("Project site address is absent from source")
            if target_words not in " ".join(_site_words(str(
                    target_rows[target_id].get("address") or ""))):
                raise ValueError("Project site address is absent from target")
            street, holding = source_key
            for row in projects:
                other_project = _project(row)
                if (row.get("source_group") != group
                        or other_project.get("publisher_project_id") == project_id
                        or not other_project.get("address")):
                    continue
                other = " ".join(_site_words(other_project["address"]))
                if street in other and f"вл {holding}" in other:
                    raise ValueError("Another publisher project asserts the same site")
        research_name = link.get("research_file")
        if (not isinstance(research_name, str)
                or re.fullmatch(r"[a-z0-9_-]+\.jsonl", research_name) is None):
            raise ValueError("Unsafe adjudication research file")
        body = _read_local(research_root / research_name)
        if _digest(body) != link.get("research_sha256"):
            raise ValueError("Adjudication research hash mismatch")
        matching_research = [row for row in _read_jsonl(body)
                             if row.get("observation_id") == link.get("research_observation_id")]
        if len(matching_research) != 1:
            raise ValueError("Adjudication research observation is not unique")
        observation = matching_research[0]
        observed_targets = observation.get("target_ids") or [
            item.get("target_id") for item in observation.get("targets", [])]
        if target_id not in observed_targets:
            raise ValueError("Adjudication target is absent from research observation")
        reviewed_targets = [item for item in observation.get("targets", [])
                            if item.get("target_id") == target_id]
        reviewed_positive = any(str(item.get("assessment", "")).startswith(
            "accept_candidate_project") for item in reviewed_targets)
        reviewed_positive |= observation.get("project_match_status") == "resolved"
        reviewed_positive |= any(
            item.get("project_match_status") == "resolved"
            and target_id in item.get("target_ids", [])
            for item in observation.get("assessments", []))
        if not reviewed_positive:
            raise ValueError("Adjudication research did not endorse project identity")
        evidence_url = link.get("evidence_url")
        if not isinstance(evidence_url, str):
            raise ValueError("Adjudication evidence URL is missing")
        parsed_url = urlsplit(evidence_url)
        if (parsed_url.scheme != "https" or parsed_url.hostname not in
                OFFICIAL_EVIDENCE_HOSTS[group] | OFFICIAL_PUBLIC_RECORD_HOSTS
                or parsed_url.username or parsed_url.password
                or parsed_url.fragment or parsed_url.port is not None):
            raise ValueError("Adjudication evidence is not on official host")
        evidence = [entry for entry in observation.get("evidence", [])
                    if entry.get("source_url") == evidence_url]
        claims = " ".join(str(text) for entry in evidence
                          for text in entry.get("claims", []))
        if len(claims) > 20_000:
            raise ValueError("Adjudication official claim text is oversized")
        claim_words = " ".join(re.findall(r"[а-яa-z0-9]+", claims.casefold().replace("ё", "е")))
        if not evidence or _norm(project_name) not in claim_words:
            raise ValueError("Adjudication lacks official project assertion")
        if match_basis == "official_cadastre_project_identity":
            if cadastre not in claims:
                raise ValueError("Adjudication lacks exact official cadastre assertion")
        elif " ".join(_site_words(source_fragment)) not in claim_words:
            raise ValueError("Adjudication lacks exact official site address assertion")
        source = sorted(source_rows, key=lambda row: row["source_url"])[0]
        result["project_matches"].append({
            "source_group": group, "publisher_project_id": project_id,
            "project_name": project_name, "source_url": source["source_url"],
            "observation_id": source["observation_id"],
            "observed_at": source.get("observed_at"),
            "source_updated_at": source.get("source_updated_at"),
            "source_rights_status": source.get("source_rights_status"),
            "project_address": _project(source).get("address_components"),
            "target_id": target_id,
            "target_source_url": target_rows[target_id].get("source_url"),
            "target_address": target_rows[target_id].get("address_components"),
            "target_visibility": target_rows[target_id].get("visibility"),
            "decision": "accepted",
            "reason_code": ("official_cadastre_project_identity_reviewed"
                            if match_basis == "official_cadastre_project_identity"
                            else "official_full_project_site_address_reviewed"),
            "evidence_basis": ("official_cadastre_and_project_name"
                               if match_basis == "official_cadastre_project_identity"
                               else "official_site_address_and_project_name"),
            "evidence_url": evidence_url,
            "evidence_role": ("official_public_record" if parsed_url.hostname in
                              OFFICIAL_PUBLIC_RECORD_HOSTS else "developer_document"),
            "reviewer_kind": "agent",
            "research_observation_id": observation["observation_id"],
            "association_scope": "project_address_only", "geometry_claimed": False,
            "source_role": "developer_site"})
    accepted_by_target = Counter(item["target_id"] for item in result["project_matches"]
                                 if item["decision"] == "accepted")
    accepted_project_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in result["project_matches"]:
        if item["decision"] == "accepted" and accepted_by_target[item["target_id"]] != 1:
            item["decision"], item["reason_code"] = "review", "multiple_publisher_projects"
        if item["decision"] == "accepted":
            accepted_project_targets[(item["source_group"], item["publisher_project_id"])].add(
                item["target_id"])
    for group in result["groups"]:
        ids = sorted(accepted_project_targets.get((group["source_group"],
                                                   group["publisher_project_id"]), set()))
        group["accepted_project_target_ids"] = ids
        if ids and group["map_link_status"] == "unlinked":
            group["map_link_status"] = "project_address_only"
    for unit in result["units"]:
        unit["accepted_project_target_ids"] = sorted(accepted_project_targets.get(
            (unit["source_group"], unit.get("publisher_project_id")), set()))


def _project_title_key(value: Any) -> str:
    words = _norm(value).split()
    while words and words[0] in {"жк", "жилой", "комплекс", "квартал"}:
        words.pop(0)
    return " ".join(words)


def apply_agent_site_adjudications(result: dict[str, Any],
                                   adjudications: list[tuple[dict[str, Any], str]],
                                   projects: list[dict[str, Any]],
                                   targets: list[dict[str, Any]]) -> None:
    """Attach pinned, agent-reviewed project sites; never assign a corpus or footprint."""
    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for project in projects:
        if (project.get("source_group") == "proplanirovki"
                and project.get("record_type") == "project"
                and project.get("identity_quality") == "publisher_asserted"
                and _scope(project) != "out_of_scope"
                and project.get("publisher_project_id")):
            source_rows[str(project["publisher_project_id"])].append(project)
    target_rows = {_target_id(target): target for target in targets}
    seen_pairs: set[tuple[str, str]] = set()
    aliases: list[dict[str, Any]] = []
    for row, source_sha in adjudications:
        if row.get("schema_version") not in {
                "aggregator-project-adjudication/1", "developer-project-adjudication-result/1"}:
            raise ValueError("Unsupported agent site adjudication schema")
        if row.get("reviewer_kind") != "agent":
            raise ValueError("Site adjudication must identify agent review")
        if row.get("decision") != "accepted_project":
            continue
        if row.get("corpus_match_status", row.get("corpus_link_status")) != "unresolved":
            raise ValueError("Project adjudication cannot claim an individual corpus")
        if row.get("geometry_match_status", row.get("geometry_status", "unresolved")) != "unresolved":
            raise ValueError("Project adjudication cannot claim geometry")
        provenance = row.get("publisher_provenance") or {}
        if not isinstance(provenance, Mapping):
            raise ValueError("Invalid source project provenance")
        source_group = row.get("source_group", provenance.get("source_group"))
        project_id = row.get("publisher_project_id")
        target_id = row.get("target_id")
        if (source_group != "proplanirovki" or not isinstance(project_id, str)
                or not isinstance(target_id, str) or target_id not in target_rows):
            raise ValueError("Agent site project or target is unavailable")
        key = project_id, target_id
        if key in seen_pairs:
            raise ValueError("Duplicate site adjudication pair")
        seen_pairs.add(key)
        candidates = source_rows.get(project_id, [])
        project_name = row.get("publisher_project_name", provenance.get("project_name"))
        source_url = row.get("publisher_source_url", provenance.get("project_record_url"))
        if (not candidates or not project_name or not source_url
                or { _norm(item.get("project_name")) for item in candidates }
                   != {_norm(project_name)}
                or source_url not in {item.get("source_url") for item in candidates}):
            raise ValueError("Adjudicated source project ID/name/URL disagrees with harvest")
        if (row.get("source_observation_id") and row["source_observation_id"] not in
                {item.get("observation_id") for item in candidates}):
            raise ValueError("Adjudicated source observation differs from harvest")
        if row.get("target_address") and row["target_address"] != target_rows[target_id].get("address"):
            raise ValueError("Adjudicated target address differs from pinned source")
        reason = row.get("reason_ru")
        evidence_urls = row.get("evidence_urls")
        evidence_rows = row.get("sources", row.get("source_access"))
        if (not isinstance(reason, str) or not 30 <= len(reason) <= 4000
                or not isinstance(evidence_urls, list) or not 1 <= len(evidence_urls) <= 5
                or not isinstance(evidence_rows, list)):
            raise ValueError("Agent site evidence is incomplete")
        recorded_urls = {entry.get("url") for entry in evidence_rows
                         if isinstance(entry, Mapping)}
        primary_urls = []
        for url in evidence_urls:
            if not isinstance(url, str) or url not in recorded_urls:
                raise ValueError("Agent site URL is not pinned in evidence")
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.port is not None or parsed.fragment):
                raise ValueError("Unsafe agent site evidence URL")
            if parsed.hostname in REVIEWED_PROJECT_EVIDENCE_HOSTS:
                primary_urls.append(url)
        insufficient_primary = not primary_urls
        checked_at = row.get("checked_at")
        try:
            parsed_time = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("Invalid adjudication observation time") from exc
        if parsed_time.tzinfo is None:
            raise ValueError("Adjudication observation time lacks timezone")
        previous = [match for match in result["project_matches"]
                    if match["decision"] == "accepted" and match["target_id"] == target_id]
        shared_primary_url = next((url for url in primary_urls if any(
            url == old.get("evidence_url") or url in old.get("evidence_urls", [])
            for old in previous)), None)
        same_project = bool(previous) and all(
            _project_title_key(match["project_name"]) == _project_title_key(project_name)
            for match in previous)
        duplicate_target = bool(previous) and not (shared_primary_url and same_project)
        source = sorted(candidates, key=lambda item: item["source_url"])[0]
        record = {
            "source_group": source_group, "publisher_project_id": project_id,
            "project_name": project_name, "source_url": source_url,
            "observation_id": source["observation_id"], "observed_at": source.get("observed_at"),
            "source_updated_at": source.get("source_updated_at"),
            "source_rights_status": source.get("source_rights_status"),
            "project_address": source.get("address_components"),
            "target_id": target_id, "target_source_url": target_rows[target_id].get("source_url"),
            "target_address": target_rows[target_id].get("address_components"),
            "target_visibility": target_rows[target_id].get("visibility"),
            "decision": "review" if duplicate_target or insufficient_primary else "accepted",
            "reason_code": ("multiple_publisher_projects" if duplicate_target
                            else "agent_site_primary_evidence_missing" if insufficient_primary
                            else "agent_reviewed_project_site"),
            "evidence_basis": "pinned_agent_project_site_evidence",
            "evidence_url": primary_urls[0] if primary_urls else evidence_urls[0],
            "evidence_urls": evidence_urls,
            "reviewer_kind": "agent", "research_observation_id": target_id,
            "research_sha256": source_sha,
            "association_scope": "project_address_only", "geometry_claimed": False,
            "source_role": "third_party_aggregator",
        }
        result["project_matches"].append(record)
        if duplicate_target or insufficient_primary:
            continue
        for group in result["groups"]:
            if (group["source_group"], group["publisher_project_id"]) == (source_group, project_id):
                group["accepted_project_target_ids"] = sorted(set(
                    group["accepted_project_target_ids"]) | {target_id})
                if group["map_link_status"] == "unlinked":
                    group["map_link_status"] = "project_address_only"
        for unit in result["units"]:
            if (unit["source_group"], unit.get("publisher_project_id")) == (source_group, project_id):
                unit["accepted_project_target_ids"] = sorted(set(
                    unit["accepted_project_target_ids"]) | {target_id})
        if previous:
            for old in previous:
                aliases.append({"target_id": target_id,
                                "source_group_a": old["source_group"],
                                "publisher_project_id_a": old["publisher_project_id"],
                                "source_group_b": source_group,
                                "publisher_project_id_b": project_id,
                                "shared_evidence_url": shared_primary_url,
                                "identity_scope": "same_reviewed_project_only"})
    result["verified_project_aliases"] = aliases


def confirmed_local_complex_groups(groups: list[dict[str, Any]],
                                   project_matches: list[dict[str, Any]],
                                   targets: list[dict[str, Any]],
                                   aliases: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Project identity evidence for local targets, without a corpus or geometry claim."""
    target_rows = {_target_id(target): target for target in targets}
    accepted: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for match in project_matches:
        if match["decision"] == "accepted":
            accepted[(match["source_group"], match["publisher_project_id"])].append(match)
    parent: dict[tuple[str, str], tuple[str, str]] = {key: key for key in accepted}
    def root(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            key = parent[key]
        return key
    for alias in aliases or []:
        left = alias["source_group_a"], alias["publisher_project_id_a"]
        right = alias["source_group_b"], alias["publisher_project_id_b"]
        if left not in parent or right not in parent or left == right:
            raise ValueError("Verified project alias references missing groups")
        parent[root(right)] = root(left)
    by_root: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for group in groups:
        key = group["source_group"], group["publisher_project_id"]
        matches = accepted.get(key, [])
        if not matches:
            continue
        if sorted(item["target_id"] for item in matches) != group["accepted_project_target_ids"]:
            raise ValueError("Confirmed group and project links disagree")
        by_root[root(key)].append(group)
    output = []
    for cluster in by_root.values():
        ordered = sorted(cluster, key=lambda group: (
            group["source_role"] != "developer_site", group["source_group"],
            group["publisher_project_id"]))
        canonical = ordered[0]
        cluster_keys = {(group["source_group"], group["publisher_project_id"])
                        for group in ordered}
        matches = sorted((item for key in cluster_keys for item in accepted[key]),
                         key=lambda item: (item["target_id"], item["source_group"]))
        members_by_target: dict[str, dict[str, Any]] = {}
        for match in matches:
            target = target_rows[match["target_id"]]
            member = members_by_target.setdefault(match["target_id"], {
                "target_id": match["target_id"], "target_name": target.get("name"),
                "target_address": target.get("address"),
                "target_visibility": target.get("visibility"),
                "target_source_url": target.get("source_url"),
                "project_identity_evidence": [], "corpus_geometry_status": "unresolved"})
            member["project_identity_evidence"].append({
                "source_group": match["source_group"],
                "publisher_project_id": match["publisher_project_id"],
                "evidence_basis": match["evidence_basis"],
                "evidence_url": match["evidence_url"],
                "reviewer_kind": match["reviewer_kind"],
                "research_observation_id": match["research_observation_id"]})
        alias_rows = sorted((alias for alias in aliases or []
                             if (alias["source_group_a"], alias["publisher_project_id_a"]) in cluster_keys
                             and (alias["source_group_b"], alias["publisher_project_id_b"]) in cluster_keys),
                            key=lambda alias: (alias["target_id"], alias["source_group_b"]))
        output.append({"group_id": canonical["group_id"],
                       "source_group": canonical["source_group"],
                       "publisher_project_id": canonical["publisher_project_id"],
                       "project_name": next(item["project_name"] for item in matches
                                            if item["source_group"] == canonical["source_group"]
                                            and item["publisher_project_id"] == canonical["publisher_project_id"]),
                       "source_role": canonical["source_role"],
                       "source_urls": sorted({url for group in ordered
                                              for url in group["source_urls"]}),
                       "publisher_aliases": [{"source_group": group["source_group"],
                                              "publisher_project_id": group["publisher_project_id"],
                                              "source_group_id": group["group_id"],
                                              "source_role": group["source_role"]}
                                             for group in ordered],
                       "verified_alias_evidence": alias_rows,
                       "local_target_ids": sorted(members_by_target),
                       "local_members": [members_by_target[key]
                                         for key in sorted(members_by_target)],
                       "identity_scope": "project_only",
                       "cross_provider_union_verified": bool(alias_rows),
                       "unit_identity_deduplicated": False,
                       "publication_status": "private_review_only"})
    return sorted(output, key=lambda group: group["group_id"])


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


REASON_RU = {
    "official_building_id_exact": "Совпал явный идентификатор корпуса в обоих источниках.",
    "full_building_address_exact": "Совпал полный адрес корпуса; другой такой точки не найдено.",
    "publisher_project_id_asserted": "Издатель указал идентификатор ЖК для этой квартиры.",
    "publisher_unit_id_missing": "Нет явного идентификатора квартиры издателя.",
    "publisher_unit_identity_unverified": "Идентификатор квартиры основан только на странице.",
    "publisher_project_id_missing": "Нет явного идентификатора ЖК издателя.",
    "publisher_project_identity_unverified": "Идентификатор ЖК не подтверждён самим источником.",
    "unique_full_project_address": "Единственный локальный объект с тем же полным типизированным адресом проекта.",
    "official_cadastre_project_identity_reviewed": "Проверенное официальное утверждение связывает название проекта с точным кадастровым участком локальной записи; корпус остаётся неизвестным.",
    "official_full_project_site_address_reviewed": "Официальный источник подтверждает единственный проект по полному адресу площадки; отдельный корпус и контур не установлены.",
    "agent_reviewed_project_site": "Агент проверил проект и площадку по закреплённым источникам; корпус и контур не установлены.",
    "agent_site_primary_evidence_missing": "Для принятия проектной связи не хватает первичного подтверждения площадки.",
    "third_party_project_address": "Адрес совпадает, но утверждение получено из стороннего каталога.",
    "third_party_building_address": "Адрес корпуса совпадает в стороннем каталоге; нужна независимая проверка.",
    "publisher_project_address_conflict": "Один ID проекта сопровождается разными адресными утверждениями.",
    "multiple_publisher_projects": "На один локальный адрес претендуют несколько проектов источника.",
    "publisher_identity_conflict": "Наблюдения издателя расходятся по идентичности объекта.",
    "moscow_scope_unverified": "Московская область применения записи не подтверждена.",
    "city_conflict": "Города в адресах различаются.",
    "municipality_conflict": "Населённые пункты или муниципальные части различаются.",
    "city_scope_needs_review": "Географическая область требует ручной проверки.",
    "local_scope_unverified": "Локальная запись не подтверждает контур отдельного корпуса.",
    "address_not_exact": "Адресные компоненты неполны или извлечены неоднозначно.",
    "address_incomplete": "Не хватает улицы или города в одном из адресов.",
    "plot_vs_building_address": "Адрес участка или владения не равен адресу корпуса.",
    "holding_not_finished_house": "Номер владения или участка не подтверждает дом.",
    "house_missing": "Номер дома не указан.",
    "house_conflict": "Номера домов различаются.",
    "combined_house_corpus_not_verified": "Дом и корпус записаны слитно в одном источнике; эквивалентность требует проверки.",
    "combined_house_structure_not_verified": "Дом и строение записаны слитно в одном источнике; эквивалентность требует проверки.",
    "corpus_conflict": "Номера корпусов различаются.",
    "structure_conflict": "Номера строений различаются.",
    "corpus_or_structure_missing": "Корпус или строение указаны только в одном источнике.",
    "municipality_missing_one_side": "Муниципальная часть указана только в одном источнике.",
    "house_only_without_building_key": "Совпадает только дом без отдельного номера корпуса.",
    "multiple_local_targets": "По тем же признакам найдено несколько локальных объектов.",
    "multiple_publisher_buildings": "На один локальный объект претендуют несколько корпусов издателя.",
    "no_exact_typed_candidate": "Подходящий объект по типизированному адресу не найден.",
}


def _safe_review_link(value: Any) -> str:
    """Only these reviewed evidence hosts may become clickable in local HTML."""
    if not isinstance(value, str) or len(value) > 1000:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "www.openstreetmap.org", "openstreetmap.org", "estateline.ru",
            "www.estateline.ru", "etalongroup.ru", "www.etalongroup.ru",
            "a101.ru", "www.a101.ru", "cdn.a101.ru", "fsk.ru", "www.fsk.ru",
            "granelle.ru", "www.granelle.ru", "moskva.brusnika.ru",
            "proplanirovki.ru", "www.proplanirovki.ru"}
            or parsed.username or parsed.password or parsed.port is not None
            or parsed.fragment):
        return ""
    return value


def review_html(rows: list[dict[str, Any]], *, summary: Mapping[str, int] | None = None,
                units: list[dict[str, Any]] | None = None,
                project_matches: list[dict[str, Any]] | None = None,
                local_complex_groups: list[dict[str, Any]] | None = None,
                manual_rows: list[dict[str, Any]] | None = None,
                searched_tasks: int | None = None,
                queued_tasks: int | None = None) -> str:
    """One offline HTML file; at most 100 DOM rows per table at any moment."""
    labels = {"accepted": "Подтверждена по источникам",
              "review": "Требует проверки", "rejected": "Отклонена пара",
              "unmatched": "Совпадение не найдено"}
    manual_labels = {"resolved": "Подтверждён проект или участок",
                     "accepted": "Связь принята по источникам",
                     "candidate": "Кандидат для проверки",
                     "rejected": "Пара отклонена",
                     "unresolved": "Не установлено"}
    def reason(row: Mapping[str, Any]) -> str:
        return REASON_RU.get(row.get("reason_code"), "Требуется дополнительная проверка.")

    def val(value: Any) -> str:
        return "" if value is None else str(value)[:1_000]

    def rubles(value: Any) -> str:
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0):
            return f"{value:,.0f}".replace(",", " ") + " ₽"
        return ""

    price_basis_ru = {
        "unknown": "Условие не установлено",
        "asking_total": "Сумма предложения",
        "cash_100_percent": "При 100% оплате",
        "discounted price at 100% payment": "Со скидкой при 100% оплате",
        "from; 100% payment": "От; при 100% оплате",
        "from; payment conditions not shown on catalog card": "От; условия не указаны",
        "marketing price; applicability flags in source_fields": "Рекламная сумма; условия уточнять",
        "source base asking price": "Базовая сумма источника",
        "displayed asking price; payment conditions not stated on unit page":
            "На странице; условия не указаны",
        "aggregator-displayed asking price; payment conditions not stated":
            "Агрегатор; условия не указаны",
    }

    def evidence_link(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        try:
            parsed = urlsplit(value)
            if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                    and not parsed.password and parsed.port is None and not parsed.fragment):
                return value[:1_000]
        except ValueError:
            pass
        return ""

    max_index = 20_000
    datasets = {
        "buildings": [[val(row.get("source_group")), val(row.get("publisher_project_id")),
                       val(row.get("building_ref")), val(row.get("target_id")),
                       labels[row["decision"]], reason(row),
                       _safe_review_link(row.get("source_url")),
                       _safe_review_link(row.get("target_source_url"))]
                      for row in rows[:max_index]],
        "projects": [[val(row.get("source_group")), val(row.get("publisher_project_id")),
                      val(row.get("project_name")), val(row.get("target_id")),
                      labels[row["decision"]], reason(row),
                      _safe_review_link(row.get("source_url")),
                      _safe_review_link(row.get("target_source_url")),
                      _safe_review_link(row.get("evidence_url"))]
                     for row in (project_matches or [])[:max_index]],
        "units": [[val(row.get("project_name") or "Проект не указан"),
                   rubles(row.get("price_rub")),
                   price_basis_ru.get(row.get("price_basis"), "Условие не установлено"),
                   val(row.get("area_m2")), val(row.get("rooms")), val(row.get("floor")),
                   val(row.get("project_address")), val(row.get("corpus")),
                   val(row.get("unit_number")), val(row.get("source_group")),
                   labels[row["decision"]], reason(row),
                   val(row.get("publisher_project_id")),
                   val(row.get("publisher_building_id")),
                   val(row.get("publisher_unit_id")), val(row.get("observation_id")),
                   _safe_review_link(row.get("source_url"))]
                  for row in (units or [])[:max_index]],
        "manual": [[val(row.get("target_id")),
                    manual_labels.get(row.get("project_match_status"),
                                      val(row.get("project_match_status"))),
                    manual_labels.get(row.get("corpus_match_status"),
                                      val(row.get("corpus_match_status"))),
                    val(row.get("note")),
                    *[evidence_link(url) for url in row.get("source_urls", [])[:5]]]
                   for row in (manual_rows or [])[:max_index]],
        "groups": [[val(row.get("project_name")),
                    str(len(row.get("local_target_ids", []))),
                    val(", ".join(row.get("local_target_ids", []))),
                    val(", ".join(f"{item['source_group']}:{item['publisher_project_id']}"
                                  for item in row.get("publisher_aliases", []))),
                    "Да" if row.get("cross_provider_union_verified") else "Нет",
                    _safe_review_link(next((member.get("project_identity_evidence", [{}])[0]
                                             .get("evidence_url") for member in row.get("local_members", [])
                                             if member.get("project_identity_evidence")), None))]
                   for row in (local_complex_groups or [])[:max_index]],
    }
    headings = {
        "buildings": ("Корпус и локальный объект", "Источник", "ID ЖК издателя", "Корпус",
                      "Локальный ID", "Решение", "Причина", "Страница издателя", "Локальный источник"),
        "projects": ("Связь проекта с адресом", "Источник", "ID ЖК издателя", "Название проекта",
                     "Локальный ID", "Решение", "Причина", "Страница проекта",
                     "Локальный источник", "Официальное основание"),
        "units": ("Квартиры издателей", "ЖК / проект", "Наблюдаемая сумма", "Условие суммы",
                  "Площадь, м²", "Комнаты", "Этаж", "Адрес проекта", "Корпус",
                  "Квартира №", "Источник", "Решение", "Причина", "ID ЖК",
                  "ID корпуса", "ID квартиры", "Наблюдение", "Страница источника"),
        "manual": ("Ручная проверка адресов", "Локальный ID", "Проект/участок",
                   "Корпус", "Основание и ограничение", "Источник 1", "Источник 2",
                   "Источник 3", "Источник 4", "Источник 5"),
        "groups": ("Подтверждённые группы ЖК", "Название ЖК", "Локальных записей",
                   "Локальные ID", "ID проектов источников", "Алиас между источниками",
                   "Основание"),
    }
    total = {"buildings": len(rows), "projects": len(project_matches or []),
             "units": len(units or []), "manual": len(manual_rows or []),
             "groups": len(local_complex_groups or [])}
    counts = summary or Counter(row["decision"] for row in rows)
    count_text = ", ".join(f"{labels[key]}: {int(counts.get(key, 0))}"
                           for key in ("accepted", "review", "rejected", "unmatched"))
    sections = []
    for key, columns in headings.items():
        if key == "manual" and manual_rows is None:
            continue
        if key == "groups" and local_complex_groups is None:
            continue
        title, *headers = columns
        sections.append(
            f'<section data-table="{key}"><h2>{html.escape(title)}</h2>'
            f'<p>Индексировано {len(datasets[key])} из {total[key]}; полный набор в JSONL/TSV. '
            'На странице показывается не более 100 строк.</p>'
            '<label>Поиск по таблице <input type="search" autocomplete="off"></label> '
            '<button type="button" data-prev>Назад</button> '
            '<span data-count></span> '
            '<button type="button" data-next>Далее</button>'
            '<table><thead><tr>' + "".join(f"<th>{html.escape(name)}</th>" for name in headers)
            + '</tr></thead><tbody></tbody></table></section>')
    payload = json.dumps(datasets, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    script = """const data=JSON.parse(document.getElementById('review-data').textContent);
for(const section of document.querySelectorAll('[data-table]')){
 const key=section.dataset.table, rows=data[key], box=section.querySelector('input');
 const body=section.querySelector('tbody'), count=section.querySelector('[data-count]');
 const size=window.innerWidth<=600?25:100;
 let page=0;
 function render(){
  const q=box.value.trim().toLocaleLowerCase();
  const filtered=q?rows.filter(r=>r.some(v=>String(v).toLocaleLowerCase().includes(q))):rows;
  const pages=Math.max(1,Math.ceil(filtered.length/size)); page=Math.min(page,pages-1);
  body.replaceChildren();
  for(const row of filtered.slice(page*size,(page+1)*size)){
   const tr=document.createElement('tr');
   row.forEach((value,index)=>{
    const td=document.createElement('td');
    const link=(key==='manual'&&index>=4)||(key==='buildings'&&index>=6)||
      (key==='projects'&&index>=6)||(key==='units'&&index>=16)||
      (key==='groups'&&index===5);
    if(link&&value){const a=document.createElement('a');a.href=value;a.textContent='Открыть';
      a.rel='noopener noreferrer';a.target='_blank';td.append(a)}
    else td.textContent=value||'—'; tr.append(td);
   }); body.append(tr);
  }
  count.textContent=`${filtered.length} записей · страница ${page+1}/${pages}`;
  section.querySelector('[data-prev]').disabled=page===0;
  section.querySelector('[data-next]').disabled=page>=pages-1;
 }
 box.addEventListener('input',()=>{page=0;render()});
 section.querySelector('[data-prev]').addEventListener('click',()=>{page--;render()});
 section.querySelector('[data-next]').addEventListener('click',()=>{page++;render()});
 render();
}"""
    manual_text = (f" Проверено {searched_tasks} из {queued_tasks} активных адресных задач."
                   if searched_tasks is not None and queued_tasks is not None else "")
    return ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">"
            "<title>Сверка застройщиков и карты</title>"
            "<style>body{font:14px system-ui;margin:1rem;color:#222}section{margin:2rem 0}"
            "table{border-collapse:collapse;display:block;overflow:auto;max-width:100%}"
            "td,th{padding:.35rem .55rem;border:1px solid #ddd;text-align:left;min-width:5rem}"
            "input{max-width:80vw}button{padding:.25rem .5rem}</style>"
            "<h1>Закрытая сверка</h1>"
            "<p>Совпадение названия или близости не устанавливает принадлежность ЖК. "
            "Адрес проекта не подтверждает контур, этажность или текущий статус корпуса.</p>"
            f"<p>{html.escape(count_text)}. Это локальный review, без публикации в каталоге."
            f"{html.escape(manual_text)}</p>" + "".join(sections)
            + f'<script type="application/json" id="review-data">{payload}</script>'
            + f"<script>{script}</script></html>")


def search_queue(targets: list[dict[str, Any]], accepted_target_ids: set[str],
                 *, first_batch: int = SEARCH_FIRST_BATCH,
                 claims: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Prepare queries, never perform them or infer identity from their text."""
    if not 0 <= first_batch <= 300:
        raise ValueError("Unbounded first search batch")
    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    insufficient: list[dict[str, str]] = []
    for target in targets:
        target_id = _target_id(target)
        if target_id in accepted_target_ids:
            continue
        if target.get("record_kind") not in {
                "multifamily", "apartment_complex", "low_rise",
                "mapped_construction_candidate"}:
            insufficient.append({"target_id": target_id, "reason_code": "not_residential_target"})
            continue
        raw_name = target.get("source_jk_label") or target.get("name")
        name = _value(re.sub(r"\s+", " ", raw_name).strip()[:240]
                      if isinstance(raw_name, str) else raw_name)
        address = Address.from_source(target.get("address_components"),
                                      target.get("address_parse_quality"))
        if not name:
            insufficient.append({"target_id": target_id, "reason_code": "name_missing"})
            continue
        if any(token in _norm(name) for token in (
                "детский сад", "школа", "поликлиника", "подстанция",
                "котельная", "гараж", "паркинг", "автодорога")):
            insufficient.append({"target_id": target_id, "reason_code": "nonresidential_name"})
            continue
        city = address.city or _norm(target.get("city"))
        if not city:
            insufficient.append({"target_id": target_id, "reason_code": "locality_missing"})
            continue
        raw_address = target.get("address")
        raw_first = ""
        if isinstance(raw_address, str):
            raw_first = raw_address.split("(", 1)[0].split("\n", 1)[0]
            raw_first = re.sub(r"\b[0-9]{6}\b", "", raw_first)
            raw_first = re.sub(r"^(?:г\.?\s*)?Москва\s*,?\s*", "", raw_first,
                               flags=re.IGNORECASE).strip(" ,.;")
            raw_first = re.sub(r"\s+", " ", raw_first).strip()[:240]
        if address.street and address.house:
            key = ("typed", *address.full)
            query_address = " ".join(part for part in
                                     (address.street, address.house, address.corpus,
                                      address.structure) if part)
            basis = "typed_address_search_only"
        elif raw_first and re.search(
                r"(?:ул\.?|улиц|просп|проезд|шосс|набер|переул|бульвар|"
                r"вл\.?|владен|деревн|посел|квартал|мкр\.?|микрорайон|район|"
                r"площад|земельн|участ)", raw_first.casefold()):
            key = ("raw", city, _norm(raw_first))
            query_address = raw_first
            basis = "raw_source_address_search_only"
        elif (target.get("source_jk_label") or
              not re.fullmatch(r"(?:жилой дом|жилой комплекс|корпус\s*\d+|к\s*\d+)",
                               _norm(name))):
            key = ("name", city, _norm(name))
            query_address = ""
            basis = "source_name_locality_search_only"
        else:
            insufficient.append({"target_id": target_id, "reason_code": "usable_address_or_name_missing"})
            continue
        item = buckets.setdefault(key, {
            "address_key": "address_" + _digest("\0".join(key).encode())[:24],
            "city": city, "municipality": address.municipality or None,
            "street": address.street or None, "house": address.house or None,
            "corpus": address.corpus or None, "structure": address.structure or None,
            "query_address": query_address, "query_basis": basis,
            "target_ids": [], "names": [], "consulted_pages": [],
            "max_consulted_pages": MAX_SEARCH_PAGES_PER_ADDRESS,
            "remaining_pages": MAX_SEARCH_PAGES_PER_ADDRESS,
            "status": "deferred_archived", "query": "", "priority_score": 0,
            "active_target_ids": [], "archived_target_ids": [],
        })
        item["target_ids"].append(target_id)
        item["names"].append(name)
        active = target.get("visibility") in {"visible", "mapped_candidate"}
        item["active_target_ids" if active else "archived_target_ids"].append(target_id)
        if active:
            item["status"] = "unsearched"
        name_norm = _norm(name)
        priority = (4 if active else 0)
        priority += 3 if target.get("source_jk_label") else 0
        generic = re.match(
            r"^(?:(?:многофункциональный|административно|клубный|многоквартирный)\s+)*"
            r"(?:жилой\s+(?:комплекс|дом|квартал)|жилые\s+дома|"
            r"многофункциональный\s+комплекс|реконструкция\b)",
            name_norm) is not None
        priority += 4 if not generic and not re.match(r"^(?:корпус|к)\s*\d+\b", name_norm) else 0
        priority += 2 if basis == "typed_address_search_only" else 1 if query_address else 0
        priority += 1 if len(name_norm.split()) >= 2 else 0
        item["priority_score"] = max(item["priority_score"], priority)
    tasks = []
    for item in buckets.values():
        item["target_ids"] = sorted(set(item["target_ids"]))
        item["names"] = sorted(set(item["names"]))
        item["active_target_ids"] = sorted(set(item["active_target_ids"]))
        item["archived_target_ids"] = sorted(set(item["archived_target_ids"]))
        # Query is a lead only; no result URL or asserted match follows from it.
        item["query"] = " ".join(part for part in
                                  (item["names"][0], "Москва", item["query_address"],
                                   "новостройка") if part)
        tasks.append(item)
    tasks.sort(key=lambda row: (-row["priority_score"], -len(row["target_ids"]),
                                row["address_key"]))
    for rank, item in enumerate(tasks, 1):
        item["rank"] = rank
    claims = claims or {}
    if any(worker not in SEARCH_WORKERS for worker in claims.values()):
        raise ValueError("Unknown research worker in claims")
    shards: dict[str, list[dict[str, Any]]] = {worker: [] for worker in SEARCH_WORKERS}
    for item in tasks:
        if item["status"] != "unsearched":
            item["assigned_research_lane"] = None
            continue
        owners = {claims[target_id] for target_id in item["target_ids"] if target_id in claims}
        if len(owners) > 1:
            raise ValueError("Conflicting search claims for one address")
        owner = next(iter(owners)) if owners else min(
            BASE_SEARCH_WORKERS, key=lambda worker: (len(shards[worker]),
                                                     BASE_SEARCH_WORKERS.index(worker)))
        item["assigned_research_lane"] = owner
        item["claim_basis"] = "prior_claim" if owners else "deterministic_balance"
        shards[owner].append(item)
    active_tasks = [row for row in tasks if row["status"] == "unsearched"]
    first_batches = {worker: rows[:first_batch] for worker, rows in shards.items()}
    batch = sorted((row for rows in first_batches.values() for row in rows),
                   key=lambda item: item["rank"])
    return {"schema_version": "developer-address-search/1", "tasks": tasks,
            "first_batch": batch, "first_batches": first_batches,
            "shards": shards, "insufficient_input": insufficient,
            "counts": {"address_tasks": len(tasks), "first_batch": len(batch),
                       "active_address_tasks": len(active_tasks),
                       "deferred_archived_tasks": len(tasks) - len(active_tasks),
                       "insufficient_input": len(insufficient)}}


def _read_local(path: Path, *, maximum: int = MAX_FILE_BYTES) -> bytes:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Missing or oversized review input")
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("Symlink in review input path")
        current = current.parent
    return path.read_bytes()


def _read_jsonl(body: bytes, *, expected: int | None = None) -> list[dict[str, Any]]:
    if len(body) > MAX_FILE_BYTES:
        raise ValueError("Review input exceeds byte limit")
    rows = []
    for line in body.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("Review JSONL row is not an object")
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError("Review input exceeds row limit")
    if expected is not None and len(rows) != expected:
        raise ValueError("Review row count differs from manifest")
    return rows


def _harvest_file(run_dir: Path, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = metadata.get("path")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z_]+\.jsonl", name):
        raise ValueError("Harvest projection path must be a flat JSONL basename")
    body = _read_local(run_dir / name)
    if _digest(body) != metadata.get("sha256"):
        raise ValueError("Harvest projection hash mismatch")
    if metadata.get("bytes") is not None and len(body) != metadata["bytes"]:
        raise ValueError("Harvest projection byte count mismatch")
    return _read_jsonl(body, expected=metadata.get("records"))


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")) + "\n").encode("utf-8") for row in rows)


def _csv_bytes(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return output.getvalue().encode("utf-8-sig")


def _tsv_bytes(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return output.getvalue().encode("utf-8-sig")


def write_review(harvest_manifest: Path, targets_path: Path = DEFAULT_TARGETS,
                 output_root: Path = DEFAULT_OUTPUT,
                 claims_path: Path | None = None,
                 manual_links_path: Path | None = None,
                 site_adjudication_paths: list[Path] | None = None) -> Path:
    """Validate pinned offline files and write one immutable private review run."""
    manifest_bytes = _read_local(harvest_manifest, maximum=2_000_000)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("Harvest manifest lacks pinned projections")
    delivery = manifest.get("schema_version") == "developer-delivery/1"
    providers = manifest.get("provider_coverage") if delivery else manifest.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("Harvest manifest lacks provider host allowlists")
    run_dir = harvest_manifest.parent
    if delivery:
        if (manifest.get("publication_status") != "private_research_only"
                or manifest.get("all_moscow_coverage_claimed") is not False):
            raise ValueError("Invalid private delivery status or coverage claim")
        source_root = run_dir.parent.parent
        for source in manifest.get("sources", []):
            source_name = source.get("directory_name")
            if (not isinstance(source_name, str)
                    or re.fullmatch(r"developer-harvest-[a-f0-9]{16}", source_name) is None):
                raise ValueError("Unsafe delivery source directory")
            source_body = _read_local(source_root / source_name / "manifest.json",
                                      maximum=2_000_000)
            if _digest(source_body) != source.get("manifest_sha256"):
                raise ValueError("Delivery source manifest hash mismatch")
        if len(manifest.get("sources", [])) != 2:
            raise ValueError("Delivery must pin both source manifests")
        unit_file, project_file = "units_moscow", "projects_corpora"
    elif manifest.get("schema_version") == "developer-harvest-run/1":
        unit_file, project_file = "developer_units", "developer_projects"
    else:
        raise ValueError("Unsupported harvest manifest schema")
    units = _harvest_file(run_dir, manifest["files"][unit_file])
    projects = _harvest_file(run_dir, manifest["files"][project_file])
    target_bytes = _read_local(targets_path)
    targets = _read_jsonl(target_bytes)
    claims_bytes = _read_local(claims_path, maximum=1_000_000) if claims_path else b""
    claims_doc = json.loads(claims_bytes) if claims_bytes else {"claims": {}}
    if (not isinstance(claims_doc, dict) or
            claims_doc.get("schema_version", "developer-search-claims/1") !=
            "developer-search-claims/1" or not isinstance(claims_doc.get("claims"), dict)):
        raise ValueError("Invalid search claims document")
    result = review_matches(units, projects, targets, providers)
    manual_bytes = _read_local(manual_links_path, maximum=1_000_000) if manual_links_path else b""
    if manual_bytes:
        apply_reviewed_project_links(result, _read_jsonl(manual_bytes), projects, targets,
                                     ROOT / ".runtime/research/developer-harvest")
    adjudication_sources: dict[str, str] = {}
    adjudications: list[tuple[dict[str, Any], str]] = []
    for path in site_adjudication_paths or []:
        resolved = path.resolve()
        research_root = ROOT / ".runtime/research/developer-harvest"
        if not resolved.is_relative_to(research_root.resolve()):
            raise ValueError("Site adjudication must remain in private research root")
        body = _read_local(path, maximum=2_000_000)
        sha = _digest(body)
        adjudication_sources[str(resolved.relative_to(research_root.resolve()))] = sha
        adjudications.extend((row, sha) for row in _read_jsonl(body))
    if len(adjudication_sources) > 3:
        raise ValueError("Too many site adjudication inputs")
    if adjudications:
        apply_agent_site_adjudications(result, adjudications, projects, targets)
    matches = result["building_matches"]
    project_matches = result["project_matches"]
    local_complex_groups = confirmed_local_complex_groups(
        result["groups"], project_matches, targets,
        result.get("verified_project_aliases", []))
    accepted_ids = {row["target_id"] for row in (*matches, *project_matches)
                    if row["decision"] == "accepted"}
    queue = search_queue(targets, accepted_ids, claims=claims_doc["claims"])
    evidence_scope = search_queue(targets, set(), first_batch=0,
                                  claims=claims_doc["claims"])
    counts = {kind: dict(Counter(row["decision"] for row in result[kind]))
              for kind in ("units", "projects", "project_matches", "building_matches")}
    counts.update({"groups": len(result["groups"]), "harvest_units": len(units),
                   "harvest_projects": len(projects), "local_targets": len(targets),
                   "confirmed_local_complex_groups": len(local_complex_groups),
                   "cross_provider_verified_groups": sum(
                       group["cross_provider_union_verified"] for group in local_complex_groups),
                   "accepted_local_project_targets": len({target_id
                       for group in local_complex_groups for target_id in group["local_target_ids"]}),
                   "confirmed_multi_target_groups": sum(
                       len(group["local_target_ids"]) > 1 for group in local_complex_groups),
                   "address_tasks": queue["counts"]["address_tasks"]})
    third_party_groups = sum(group["source_role"] == "third_party_aggregator"
                             for group in result["groups"])
    linked_project_groups = sum(bool(group["accepted_project_target_ids"])
                                for group in result["groups"])
    linked_unit_rows = sum(bool(unit["accepted_project_target_ids"])
                           for unit in result["units"])
    capture_times = []
    for row in (*units, *projects):
        observed = row.get("observed_at")
        if isinstance(observed, str):
            try:
                parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                capture_times.append(parsed.astimezone(timezone.utc))
    observed_first = min(capture_times).isoformat() if capture_times else "не указано"
    observed_last = max(capture_times).isoformat() if capture_times else "не указано"
    moscow_by_provider = Counter(row["source_group"] for row in units)
    coverage_lines = []
    for provider, status in sorted(providers.items()):
        if not isinstance(status, Mapping):
            raise ValueError("Invalid provider coverage summary")
        coverage_lines.append(
            f"- {provider}: в исходном каталоге замечено "
            f"{int(status.get('unit_observations') or 0)} строк квартир; "
            f"в строгом московском срезе {moscow_by_provider[provider]}; "
            f"страниц проектов {int(status.get('project_pages_fetched') or 0)}/"
            f"{int(status.get('project_pages_total') or 0)}; "
            f"полнота проектной очереди "
            f"{'подтверждена' if status.get('project_inventory_exhausted') is True else 'не подтверждена'}; "
            f"полнота квартирной очереди "
            f"{'подтверждена' if status.get('catalog_exhausted') is True else 'не подтверждена'}, "
            f"ожидают карточки {int(status.get('listing_detail_pending') or 0)}.")
    report = (
        "# Закрытая сверка предложений и карты\n\n"
        f"Run: `{manifest.get('delivery_id', manifest.get('run_id', 'unknown'))}`. "
        f"Данные локально наблюдались {observed_first} — {observed_last}. "
        "Это время локального наблюдения, а не дата обновления объявления.\n\n"
        f"Квартирных строк в проверенном harvest: {len(units)}; "
        f"проектных/корпусных строк: {len(projects)}; локальных целей: {len(targets)}. "
        f"Групп по явным ID проектов внутри источников: {len(result['groups'])} "
        f"(из них {third_party_groups} в стороннем агрегаторе); "
        f"с локальными проектными записями связаны {linked_project_groups} групп источников, "
        f"объединённых по проверенным алиасам в {len(local_complex_groups)} групп ЖК "
        f"({counts['cross_provider_verified_groups']} межисточниковых, "
        f"{counts['confirmed_multi_target_groups']} объединяют несколько локальных записей); "
        f"и {linked_unit_rows} строк квартир внутри них; "
        f"принятых связей проект→адрес: "
        f"{counts['project_matches'].get('accepted', 0)}; "
        f"принятых связей с локальным зданием: "
        f"{counts['building_matches'].get('accepted', 0)}. "
        "Принятая издательская группа не означает подтверждённый контур корпуса.\n\n"
        f"Поиск: {queue['counts']['active_address_tasks']} активных адресных задач, "
        f"{queue['counts']['deferred_archived_tasks']} архивных отложено, "
        f"{queue['counts']['insufficient_input']} записей не дают пригодного запроса. "
        "Каждая задача допускает не более пяти просмотренных страниц; "
        "пустой consulted_pages означает, что поиск ещё не выполнен.\n\n"
        "Охват источников по исходному манифесту:\n" + "\n".join(coverage_lines) +
        "\n\nНулевой строгий московский срез источника не означает отсутствие "
        "его объявлений: координаты или адрес могли остаться непроверенными. "
        "475 проектных страниц стороннего каталога относятся к региональному "
        "API, включающему Московскую область, а не ко всем московским новостройкам. "
        "\n\nПрава на публичное размещение цен и изображений не установлены. "
        "Этот вывод — частный review: он не изменяет каталог, карту, цены, "
        "идентичность ЖК или файлы продукта.\n")
    run_key = _digest(b"\0".join((RULES_VERSION.encode(), manifest_bytes, target_bytes,
                                  claims_bytes, manual_bytes, *(
                                      f"{name}:{sha}".encode()
                                      for name, sha in sorted(adjudication_sources.items())))))[:16]
    run_id = "developer-review-" + run_key
    artifacts = {
        "units.jsonl": _jsonl_bytes(result["units"]),
        "projects.jsonl": _jsonl_bytes(result["projects"]),
        "project_matches.jsonl": _jsonl_bytes(project_matches),
        "verified_project_aliases.jsonl": _jsonl_bytes(result.get("verified_project_aliases", [])),
        "building_matches.jsonl": _jsonl_bytes(matches),
        "groups.jsonl": _jsonl_bytes(result["groups"]),
        "confirmed_local_complex_groups.jsonl": _jsonl_bytes(local_complex_groups),
        "confirmed_local_complex_groups.tsv": _tsv_bytes(
            [{"group_id": group["group_id"], "source_group": group["source_group"],
              "publisher_project_id": group["publisher_project_id"],
              "project_name": group["project_name"],
              "publisher_aliases": ", ".join(
                  f"{alias['source_group']}:{alias['publisher_project_id']}"
                  for alias in group["publisher_aliases"]),
              "local_target_ids": ", ".join(group["local_target_ids"]),
              "evidence_urls": ", ".join(sorted({evidence["evidence_url"]
                                                   for member in group["local_members"]
                                                   for evidence in member["project_identity_evidence"]})),
              "identity_scope": group["identity_scope"]}
             for group in local_complex_groups],
            ("group_id", "source_group", "publisher_project_id", "project_name",
             "publisher_aliases",
             "local_target_ids", "evidence_urls", "identity_scope")),
        "project_matches.tsv": _tsv_bytes(project_matches, (
            "source_group", "publisher_project_id", "project_name", "target_id",
            "association_scope", "decision", "reason_code", "source_url",
            "target_source_url", "observed_at", "source_updated_at",
            "source_rights_status", "evidence_url", "research_observation_id")),
        "building_matches.tsv": _tsv_bytes(matches, (
            "source_group", "publisher_project_id", "building_ref", "target_id",
            "decision", "reason_code", "evidence_basis", "source_url",
            "target_source_url", "observed_at", "source_updated_at",
            "source_rights_status")),
        "units.tsv": _tsv_bytes(result["units"], (
            "project_name", "project_address", "building_address", "corpus",
            "unit_number", "area_m2", "rooms", "floor", "price_rub", "price_basis",
            "source_group", "publisher_project_id", "publisher_building_id",
            "publisher_unit_id", "observation_id", "decision", "reason_code",
            "group_id", "source_url", "observed_at", "source_updated_at",
            "source_rights_status")),
        "building_matches.csv": _csv_bytes(matches, (
            "source_group", "publisher_project_id", "building_ref", "target_id",
            "decision", "reason_code", "evidence_basis", "source_url")),
        "units.csv": _csv_bytes(result["units"], (
            "project_name", "project_address", "area_m2", "rooms", "floor",
            "price_rub", "price_basis", "source_group", "publisher_project_id",
            "publisher_unit_id", "observation_id", "decision", "reason_code",
            "group_id", "source_url")),
        "report.html": review_html(matches, summary=counts["building_matches"],
                                   units=result["units"],
                                   project_matches=project_matches,
                                   local_complex_groups=local_complex_groups).encode("utf-8"),
        "REPORT.md": report.encode("utf-8"),
        "search_tasks.jsonl": _jsonl_bytes(queue["tasks"]),
        "search_tasks_evidence_scope.jsonl": _jsonl_bytes(evidence_scope["tasks"]),
        "search_first_batch.jsonl": _jsonl_bytes(queue["first_batch"]),
        "search_insufficient_input.jsonl": _jsonl_bytes(queue["insufficient_input"]),
    }
    for worker in SEARCH_WORKERS:
        artifacts[f"search_shard_{worker}.jsonl"] = _jsonl_bytes(queue["shards"][worker])
        artifacts[f"search_first30_{worker}.jsonl"] = _jsonl_bytes(
            queue["first_batches"][worker])
    receipt = {"schema_version": RULES_VERSION, "run_id": run_id,
               "publication_status": "private_review_only",
               "harvest_manifest_sha256": _digest(manifest_bytes),
               "targets_sha256": _digest(target_bytes),
               "claims_sha256": _digest(claims_bytes) if claims_bytes else None,
               "manual_project_links_sha256": _digest(manual_bytes) if manual_bytes else None,
               "site_adjudication_sources_sha256": adjudication_sources,
               "counts": counts,
               "search": queue["counts"],
               "search_shards": {worker: len(queue["shards"][worker])
                                 for worker in SEARCH_WORKERS},
               "files": {name: {"sha256": _digest(body), "bytes": len(body)}
                         for name, body in artifacts.items()}}
    artifacts["manifest.json"] = (json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                             indent=2) + "\n").encode("utf-8")
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output_root / run_id
    if destination.exists():
        if not destination.is_dir() or any(_read_local(destination / name) != body
                                            for name, body in artifacts.items()):
            raise ValueError("Immutable review run already exists with different bytes")
        return destination
    with tempfile.TemporaryDirectory(prefix=".developer-review-", dir=output_root) as raw_temp:
        temporary = Path(raw_temp)
        for name, body in artifacts.items():
            path = temporary / name
            path.write_bytes(body)
            path.chmod(0o600)
        os.replace(temporary, destination)
    return destination


def _split_evidence_rows(rows: list[dict[str, Any]],
                         by_id: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Keep one research observation per original address task, even if a source grouped targets."""
    split: list[dict[str, Any]] = []
    for raw in rows:
        target_ids = raw.get("target_ids")
        if target_ids is None and isinstance(raw.get("targets"), list):
            target_ids = [item.get("target_id") for item in raw["targets"]]
        if target_ids is None and isinstance(raw.get("target"), dict):
            target_ids = [raw["target"].get("target_id")]
        if (not isinstance(target_ids, list) or not target_ids
                or not all(isinstance(item, str) for item in target_ids)
                or len(set(target_ids)) != len(target_ids)):
            raise ValueError("Search result lacks distinct typed target IDs")
        grouped: dict[str, list[str]] = defaultdict(list)
        for target_id in target_ids:
            tasks = by_id.get(target_id, [])
            if len(tasks) != 1:
                raise ValueError("Search target does not map to one queued address")
            grouped[tasks[0]["address_key"]].append(target_id)
        for ids in grouped.values():
            row = dict(raw)
            row["target_ids"] = ids
            if isinstance(raw.get("targets"), list):
                row["targets"] = [item for item in raw["targets"]
                                  if item.get("target_id") in ids]
            if isinstance(raw.get("assessments"), list):
                assessments = []
                for item in raw["assessments"]:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid source assessment")
                    claimed = item.get("target_ids") or [item.get("target_id")]
                    matched = [target_id for target_id in claimed if target_id in ids]
                    if matched:
                        assessments.append({**item, "target_ids": matched})
                row["assessments"] = assessments
            split.append(row)
    return split


def write_search_evidence(review_dir: Path, result_paths: list[Path],
                          output_root: Path = DEFAULT_OUTPUT,
                          research_root: Path = ROOT / ".runtime/research/developer-harvest") -> Path:
    """Pin bounded human research separately; never promote it to a map join."""
    if not 1 <= len(result_paths) <= 20:
        raise ValueError("Expected a bounded number of search result files")
    review_manifest = _read_local(review_dir / "manifest.json", maximum=2_000_000)
    tasks = _read_jsonl(_read_local(review_dir / "search_tasks.jsonl"))
    scope_path = review_dir / "search_tasks_evidence_scope.jsonl"
    scope_tasks = _read_jsonl(_read_local(scope_path)) if scope_path.exists() else tasks
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in scope_tasks:
        for target_id in task["target_ids"]:
            by_id[target_id].append(task)
    pinned: dict[str, str] = {}
    indexed: list[dict[str, Any]] = []
    manual_rows: list[dict[str, Any]] = []
    consulted_per_task: Counter[str] = Counter()
    consulted_source_sets: dict[str, set[frozenset[str]]] = defaultdict(set)
    root = research_root
    for path in result_paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError("Search evidence must remain in private research root")
        relative = str(resolved.relative_to(root.resolve()))
        body = _read_local(path)
        pinned[relative] = _digest(body)
        for row in _split_evidence_rows(_read_jsonl(body), by_id):
            target_ids = row.get("target_ids")
            if target_ids is None and isinstance(row.get("targets"), list):
                target_ids = [target.get("target_id") for target in row["targets"]]
            if target_ids is None and isinstance(row.get("target"), dict):
                target_ids = [row["target"].get("target_id")]
            if (not isinstance(target_ids, list) or not target_ids
                    or not all(isinstance(item, str) for item in target_ids)):
                raise ValueError("Search result lacks typed target IDs")
            matching_tasks = {item["address_key"]: item for item in by_id[target_ids[0]]}
            matching_tasks = {key: task for key, task in matching_tasks.items()
                              if set(target_ids).issubset(set(task["target_ids"]))}
            if len(matching_tasks) != 1:
                raise ValueError("Search result does not map to one queued address")
            task = next(iter(matching_tasks.values()))
            if task["status"] != "unsearched":
                continue
            sources = row.get("sources", row.get("evidence"))
            if not isinstance(sources, list) or len(sources) > MAX_SEARCH_PAGES_PER_ADDRESS:
                raise ValueError("Search result exceeds per-address page cap")
            consulted = row.get("consulted_pages")
            if isinstance(consulted, list):
                if (len(consulted) > MAX_SEARCH_PAGES_PER_ADDRESS
                        or len(consulted) != len(set(consulted))
                        or not all(isinstance(url, str) for url in consulted)):
                    raise ValueError("Invalid consulted-page ledger")
                consulted_count = len(consulted)
            else:
                consulted_count = row.get("pages_consulted", len(sources))
                if (not isinstance(consulted_count, int) or isinstance(consulted_count, bool)
                        or not 0 <= consulted_count <= len(sources)):
                    raise ValueError("Consulted page count exceeds source evidence")
            for source in sources:
                if not isinstance(source, dict) or not isinstance(source.get("url", source.get("source_url")), str):
                    raise ValueError("Search source lacks URL")
                url = urlsplit(source.get("url", source.get("source_url")))
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.port is not None or url.fragment):
                    raise ValueError("Unsafe search evidence URL")
            if isinstance(consulted, list) and not set(consulted).issubset({
                    source.get("url", source.get("source_url")) for source in sources}):
                raise ValueError("Consulted URL lacks source evidence")
            timestamp = row.get("checked_at", row.get("observed_at"))
            if not isinstance(timestamp, str):
                raise ValueError("Search evidence lacks observation time")
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Invalid search observation time") from exc
            if parsed.tzinfo is None:
                raise ValueError("Search observation time lacks timezone")
            source_set = frozenset(source.get("url", source.get("source_url"))
                                   for source in sources)
            if source_set not in consulted_source_sets[task["address_key"]]:
                consulted_per_task[task["address_key"]] += consulted_count
                consulted_source_sets[task["address_key"]].add(source_set)
            if consulted_per_task[task["address_key"]] > MAX_SEARCH_PAGES_PER_ADDRESS:
                raise ValueError("Search evidence exceeds shared per-address page cap: "
                                 f"{task['address_key']}={consulted_per_task[task['address_key']]}")
            indexed.append({"address_key": task["address_key"],
                            "reported_address_key": row.get("address_key"),
                            "target_ids": sorted(set(target_ids)),
                            "source_file": relative,
                            "observed_at": timestamp,
                            "consulted_pages": consulted_count,
                            "reported_status": row.get("status", row.get("assessment")),
                            "publication_status": "review_only"})
            claims = row.get("assessments")
            if not isinstance(claims, list) or not claims:
                if isinstance(row.get("targets"), list):
                    claims = row["targets"]
                else:
                    claims = [row]
            source_urls = [source.get("url", source.get("source_url")) for source in sources]
            for claim in claims:
                if not isinstance(claim, dict):
                    raise ValueError("Invalid target-level research assessment")
                claim_ids = claim.get("target_ids")
                if claim_ids is None and claim.get("target_id"):
                    claim_ids = [claim["target_id"]]
                if claim_ids is None:
                    claim_ids = target_ids
                if (not isinstance(claim_ids, list) or not claim_ids
                        or not set(claim_ids).issubset(set(target_ids))):
                    raise ValueError("Research assessment target IDs disagree with address")
                assessment = claim.get("assessment", row.get("assessment"))
                if isinstance(assessment, Mapping):
                    note = assessment.get("reason") or assessment.get("project_identity")
                else:
                    note = assessment
                identity = row.get("identity")
                if not note and isinstance(identity, Mapping):
                    note = identity.get("assessment")
                note = note or claim.get("reason") or row.get("reason") or ""
                limitations = row.get("limitations")
                if isinstance(limitations, list) and limitations:
                    note = f"{note} {' '.join(str(value) for value in limitations[:2])}".strip()
                note = str(note)[:1_000]
                project_status = claim.get("project_match_status", row.get("project_match_status"))
                corpus_status = claim.get("corpus_match_status", row.get("corpus_match_status"))
                if project_status is None and isinstance(assessment, str):
                    project_status = ("candidate" if assessment.startswith("accept_candidate")
                                      else "unresolved")
                for target_id in sorted(set(claim_ids)):
                    manual_rows.append({"address_key": task["address_key"],
                                        "target_id": target_id,
                                        "source_file": relative,
                                        "source_urls": source_urls,
                                        "observed_at": timestamp,
                                        "project_match_status": project_status or "unresolved",
                                        "corpus_match_status": corpus_status or "unresolved",
                                        "note": note,
                                        "publication_status": "review_only"})
    indexed.sort(key=lambda item: (item["address_key"], item["source_file"]))
    manual_rows.sort(key=lambda item: (item["address_key"], item["target_id"],
                                       item["source_file"]))
    digest_input = b"\0".join((b"developer-search-evidence/3", review_manifest, *(
        f"{name}:{sha}".encode() for name, sha in sorted(pinned.items()))))
    run_id = "search-evidence-" + _digest(digest_input)[:16]
    content = _jsonl_bytes(indexed)
    manual_content = _jsonl_bytes(manual_rows)
    review_doc = json.loads(review_manifest)
    review_files = {}
    for name in ("building_matches.jsonl", "project_matches.jsonl", "units.jsonl",
                 "confirmed_local_complex_groups.jsonl"):
        if name not in review_doc.get("files", {}):
            review_files[name] = []
            continue
        body = _read_local(review_dir / name)
        if _digest(body) != review_doc["files"][name]["sha256"]:
            raise ValueError("Review artifact hash differs from manifest")
        review_files[name] = _read_jsonl(body)
    combined_html = review_html(
        review_files["building_matches.jsonl"],
        units=review_files["units.jsonl"],
        project_matches=review_files["project_matches.jsonl"],
        local_complex_groups=review_files["confirmed_local_complex_groups.jsonl"],
        manual_rows=manual_rows,
        searched_tasks=len(consulted_per_task),
        queued_tasks=sum(task["status"] == "unsearched" for task in scope_tasks))
    coverage = {"schema_version": "developer-search-evidence/3", "run_id": run_id,
                "review_run_id": json.loads(review_manifest)["run_id"],
                "publication_status": "private_review_only",
                "searched_address_tasks": len(consulted_per_task),
                "queued_address_tasks": sum(task["status"] == "unsearched" for task in tasks),
                "search_scope_address_tasks": sum(task["status"] == "unsearched"
                                                  for task in scope_tasks),
                "consulted_pages": sum(consulted_per_task.values()),
                "manual_target_assessments": len(manual_rows),
                "source_files_sha256": pinned,
                "index_sha256": _digest(content),
                "manual_review_sha256": _digest(manual_content)}
    files = {"index.jsonl": content,
             "manual_review.jsonl": manual_content,
             "manual_review.tsv": _tsv_bytes(manual_rows, (
                 "address_key", "target_id", "project_match_status", "corpus_match_status",
                 "note", "observed_at", "source_file")),
             "report.html": combined_html.encode("utf-8"),
             "REPORT.md": (f"# Сверка каталогов и адресов\n\nПроверено "
                           f"{len(consulted_per_task)} из "
                           f"{coverage['search_scope_address_tasks']} исходных активных адресных задач; "
                           f"после принятых связей в очереди осталось "
                           f"{coverage['queued_address_tasks']}; "
                           f"{coverage['consulted_pages']} просмотренных страниц; "
                           f"{len(manual_rows)} target-level наблюдений. "
                           "Ручные наблюдения не публикуют адресную/корпусную связь.\n").encode(),
             "manifest.json": (json.dumps(coverage, ensure_ascii=False, sort_keys=True,
                                           indent=2) + "\n").encode()}
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output_root / run_id
    if destination.exists():
        if any(_read_local(destination / name) != body for name, body in files.items()):
            raise ValueError("Immutable search evidence run differs from existing files")
        return destination
    with tempfile.TemporaryDirectory(prefix=".search-evidence-", dir=output_root) as raw_temp:
        temporary = Path(raw_temp)
        for name, body in files.items():
            (temporary / name).write_bytes(body)
            (temporary / name).chmod(0o600)
        os.replace(temporary, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline developer-to-map identity review")
    parser.add_argument("--harvest-manifest", type=Path, required=True)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--manual-project-links", type=Path, default=DEFAULT_MANUAL_LINKS)
    parser.add_argument("--site-adjudication", type=Path, action="append", default=[])
    parser.add_argument("--search-results", type=Path, action="append", default=[])
    args = parser.parse_args()
    path = write_review(args.harvest_manifest, args.targets,
                        claims_path=args.claims if args.claims.exists() else None,
                        manual_links_path=args.manual_project_links
                        if args.manual_project_links.exists() else None,
                        site_adjudication_paths=args.site_adjudication)
    print(path)
    if args.search_results:
        print(write_search_evidence(path, args.search_results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
