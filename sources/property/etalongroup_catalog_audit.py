"""Bounded private capture and extraction of the seven Etalon apartment catalogs.

Only the six non-Shagal, source-linked ``/flat/`` URLs are fetched. Shagal's
successful capture is an immutable local input. The parser accepts both the
render-group SSR catalog and the paginated flat-list SSR slice, and marks page
coverage explicitly. This module writes private research artifacts only; it
does not publish offers or media.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import html
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sources.property.developer_harvest import (
    BLOCK_STATUSES,
    CHALLENGE_TITLE,
    MAX_HTML_BYTES,
    append_event,
    atomic_bytes,
    canonical_json,
    checked_url,
    curl_fetch,
    digest,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[2]
HOST = "www.etalongroup.ru"
SOURCE_URL_HOSTS = {HOST, "etalongroup.ru", "imgs.etalongroup.ru"}
EXPECTED_PROJECTS = {
    "shagal": "Шагал",
    "voxhall": "Воксхолл",
    "mariinn-park": "Мариин Парк",
    "sokolinn-park": "Соколин Парк",
    "nagatino-i-land": "Нагатино Ай-Лэнд",
    "desyatka": "Десятка",
    "izumrudnye-kholmy": "Изумрудные Холмы",
}
SHAGAL_BODY_SHA256 = "def0fd7b7bce3f5c76f826864994a2e78ea6a306ebf23ef64f05e84a2a2b0056"
CATALOG_JSONL_SHA256 = "2852250648d78d6ee2c4f951059c9796720251c2eff4d24a507d3436df76de7d"
DEFAULT_CATALOG = ROOT / ".runtime/research/developer-harvest/supplemental-etalon/projects.jsonl"
DEFAULT_SHAGAL_BODY = ROOT / ".runtime/research/developer-harvest/supplemental-etalon-discovery/root-flat.html"
DEFAULT_OUTPUT = ROOT / ".runtime/research/developer-harvest/supplemental-etalon-all-pages-20261010"
UNIT_FIELDS = (
    "schema_version", "source_project_id", "project_name", "project_city_label",
    "municipality_scope", "project_address_label", "project_district_label",
    "source_render_id", "source_render_label", "source_corpus_number_from_label",
    "source_section_id", "source_section_label", "source_floor_group_key",
    "source_unit_id", "source_unit_number", "source_kv_number", "source_floor",
    "source_area_m2", "source_room_id", "source_title", "source_price_rub",
    "source_price_total_rub", "source_price_special_rub", "source_delivery_label",
    "source_is_booked", "source_unit_detail_url", "source_unit_image_url",
    "source_capture_url", "source_capture_observed_at", "source_capture_body_sha256",
)
CONFLICT_FIELDS = (
    "source_project_id", "project_name", "project_city_label", "project_address_label",
    "source_render_id", "source_render_label", "source_section_id", "source_section_label",
    "source_floor_group_key", "source_unit_number", "source_kv_number", "source_floor",
    "source_area_m2", "source_room_id", "source_title", "source_price_rub",
    "source_price_total_rub", "source_price_special_rub", "source_delivery_label",
    "source_is_booked", "source_unit_detail_url", "source_unit_image_url",
)


class ScriptPayloadParser(HTMLParser):
    """Collect only the Nuxt payload needed for source-data extraction."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._script_target = False
        self._script_chunks: list[str] = []
        self.nuxt_json: str | None = None
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            values = dict(attrs)
            self._script_target = values.get("id") == "__NUXT_DATA__"
            if self._script_target:
                self._script_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_target:
            self.nuxt_json = "".join(self._script_chunks)
            self._script_target = False

    def handle_data(self, data: str) -> None:
        if self._script_target:
            self._script_chunks.append(data)
        else:
            self.text_chunks.append(data)


def _unwrap(value: Any) -> Any:
    while (isinstance(value, list) and len(value) == 2 and
           isinstance(value[0], str) and value[0] in {"ShallowReactive", "Reactive", "Ref"}):
        value = value[1]
    return value


def _resolve_pool(pool: list[Any], ref: int, depth: int = 0,
                  active: frozenset[int] = frozenset()) -> Any:
    if depth > 100 or ref in active:
        return "<cycle>"
    if ref < 0 or ref >= len(pool):
        return None
    active = active | {ref}
    node = pool[ref]
    if isinstance(node, dict):
        return {
            key: (_resolve_pool(pool, value, depth + 1, active)
                  if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                  else value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [(_resolve_pool(pool, value, depth + 1, active)
                 if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                 else value) for value in node]
    return node


def _find_object_data(root: Any) -> dict[str, Any]:
    """Find the project catalog payload without assuming query array positions."""
    candidates: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        value = _unwrap(value)
        if isinstance(value, dict):
            candidate = value.get("objectData")
            candidate = _unwrap(candidate)
            if (isinstance(candidate, dict) and isinstance(candidate.get("id"), int)
                    and isinstance(candidate.get("name"), str)
                    and "renderDataList" in candidate):
                candidates.append(candidate)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(root)
    unique = {int(value["id"]): value for value in candidates
              if isinstance(value.get("id"), int)}
    if len(unique) != 1:
        raise ValueError(f"expected one project objectData, found {len(unique)}")
    return next(iter(unique.values()))


def _municipality_scope(city_label: Any, address_label: Any = None,
                        district_label: Any = None) -> str:
    if not isinstance(city_label, str) or not city_label.strip():
        return "unknown"
    label = city_label.strip()
    locality_labels = [value.strip() for value in (address_label, district_label)
                       if isinstance(value, str) and value.strip()]
    outside_moscow = re.compile(
        r"(?i)(Красногор|Раменск|Московск(?:ая|ой)\s+обл(?:асть)?|г\.\s*Видное)")
    conflicts = [value for value in locality_labels if outside_moscow.search(value)]
    if label in {"Москва", "г. Москва"} and conflicts:
        return "conflicting source locality labels: " + "; ".join(
            [f"city={label}", *(f"other={value}" for value in conflicts)])
    if label in {"Москва", "г. Москва"}:
        return "Moscow city per source city label"
    return f"source city label only: {label}"


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _flat_query_data(root: Any, project_id: int) -> tuple[dict[str, Any] | None, int | None]:
    """Find this project's SSR flat-list slice and independent total-count query."""
    list_candidates: list[dict[str, Any]] = []
    total_count: int | None = None
    for node in _walk_dicts(root):
        query_key = node.get("queryKey")
        if not isinstance(query_key, list) or len(query_key) < 3:
            continue
        if query_key[1] != "msk" or query_key[2] != project_id:
            continue
        state = node.get("state")
        state_data = state.get("data") if isinstance(state, dict) else None
        payload = state_data.get("data") if isinstance(state_data, dict) else None
        if not isinstance(payload, dict):
            continue
        if query_key[0] == "flat-by-object-list":
            if isinstance(payload.get("itemList"), list):
                list_candidates.append(payload)
        elif query_key[0] == "filter-flat-by-object":
            count = payload.get("count")
            if isinstance(count, int) and not isinstance(count, bool):
                total_count = count
    if len(list_candidates) > 1:
        signatures = {canonical_json(value.get("pagination")) for value in list_candidates}
        if len(signatures) > 1:
            raise ValueError("multiple conflicting SSR flat-list slices for project")
        list_candidates = [max(list_candidates, key=lambda value: len(value.get("itemList", [])))]
    return (list_candidates[0] if list_candidates else None), total_count


def _unit_row(flat: dict[str, Any], *, project_id: int, project_name: str,
              city_label: Any, address_label: Any, district_label: Any,
              page_url: str, observed_at: str, body_sha256: str,
              render_id: int | None = None, render_label: Any = None,
              corpus_number: str | None = None, section_id: int | None = None,
              section_label: Any = None, floor_group_key: str | None = None) -> dict[str, Any]:
    unit_id = flat.get("id")
    if not isinstance(unit_id, int) or isinstance(unit_id, bool):
        raise ValueError("SSR unit row has no integer source ID")
    unit_url = flat.get("link")
    if not isinstance(unit_url, str):
        raise ValueError(f"unit {unit_id} lacks source detail URL")
    checked_url(unit_url, SOURCE_URL_HOSTS)
    path_parts = urlsplit(unit_url).path.rstrip("/").split("/")
    if not path_parts or path_parts[-1] != str(unit_id):
        raise ValueError(f"unit {unit_id} ID does not match its source URL")
    unit_image_url = flat.get("img")
    if unit_image_url is not None:
        unit_image_url = checked_url(unit_image_url, SOURCE_URL_HOSTS)
    title_data = flat.get("titleDataList")
    source_number = flat.get("number")
    if source_number is None and isinstance(title_data, dict):
        source_number = title_data.get("number")
    return {
        "schema_version": "etalongroup-apartment-result/2",
        "source_project_id": project_id,
        "project_name": project_name,
        "project_city_label": city_label,
        "municipality_scope": _municipality_scope(city_label, address_label, district_label),
        "project_address_label": address_label,
        "project_district_label": district_label,
        "source_render_id": render_id,
        "source_render_label": render_label,
        "source_corpus_number_from_label": corpus_number,
        "source_section_id": section_id,
        "source_section_label": section_label,
        "source_floor_group_key": floor_group_key,
        "source_unit_id": unit_id,
        "source_unit_number": source_number,
        "source_kv_number": flat.get("kvNumber"),
        "source_floor": flat.get("floor"),
        "source_area_m2": flat.get("area"),
        "source_room_id": flat.get("roomId"),
        "source_title": flat.get("title"),
        "source_price_rub": flat.get("price"),
        "source_price_total_rub": flat.get("priceTotal"),
        "source_price_special_rub": flat.get("priceSpecial"),
        "source_delivery_label": flat.get("deliveryName"),
        "source_is_booked": flat.get("isBooked"),
        "source_unit_detail_url": unit_url,
        "source_unit_image_url": unit_image_url,
        "source_capture_url": page_url,
        "source_capture_observed_at": observed_at,
        "source_capture_body_sha256": body_sha256,
    }


def parse_catalog_page(body: bytes, *, target: dict[str, Any], page_url: str,
                       observed_at: str, body_sha256: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not body or len(body) > MAX_HTML_BYTES:
        raise ValueError("HTML body is empty or exceeds capture byte limit")
    parser = ScriptPayloadParser()
    parser.feed(body.decode("utf-8", "replace"))
    if parser.nuxt_json is None:
        raise ValueError("__NUXT_DATA__ payload missing")
    pool = json.loads(parser.nuxt_json)
    if not isinstance(pool, list) or len(pool) < 2 or not isinstance(pool[1], dict):
        raise ValueError("unexpected Nuxt payload shape")
    root = _resolve_pool(pool, 1)
    object_data = _find_object_data(root)
    if object_data.get("name") != target["project_name"]:
        raise ValueError("source page project name does not match linked catalog target")
    if object_data.get("id") != target["developer_project_id"]:
        raise ValueError("source page project ID does not match linked listing record")

    project_id = object_data.get("id")
    project_name = object_data.get("name")
    city_label = object_data.get("city")
    address_label = object_data.get("address")
    district_label = object_data.get("district")
    units: list[dict[str, Any]] = []
    render_groups = object_data["renderDataList"]
    if not isinstance(render_groups, dict):
        page_list, listed_total = _flat_query_data(root, project_id)
        pagination = page_list.get("pagination", {}) if page_list else {}
        items = page_list.get("itemList", []) if page_list else []
        if not isinstance(items, list):
            items = []
        for flat in items:
            if isinstance(flat, dict):
                units.append(_unit_row(
                    flat, project_id=project_id, project_name=project_name,
                    city_label=city_label, address_label=address_label,
                    district_label=district_label, page_url=page_url,
                    observed_at=observed_at, body_sha256=body_sha256))
        unit_ids = [row["source_unit_id"] for row in units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("duplicate unit IDs within one captured project page")
        metadata = {
            "source_project_id": project_id,
            "project_name": project_name,
            "project_city_label": city_label,
            "project_address_label": address_label,
            "project_district_label": district_label,
            "render_groups": 0,
            "sections": 0,
            "unit_rows": len(units),
            "unique_unit_ids": len(set(unit_ids)),
            "nuxt_pool_items": len(pool),
            "nuxt_payload_chars": len(parser.nuxt_json),
            "render_type_code": object_data.get("renderTypeCode"),
            "catalog_data_mode": "flat-by-object-list SSR page slice",
            "catalog_parse_state": (
                "ssr_page_slice" if units else
                "no_inventory_rows" if listed_total == 0 else
                "no_ssr_unit_rows_with_nonzero_or_unknown_catalog_count"),
            "source_catalog_total_count": listed_total,
            "source_ssr_item_count": len(items),
            "source_ssr_pagination": pagination,
            "complete_catalog_in_this_capture": (
                len(items) == listed_total if isinstance(listed_total, int) else False),
        }
        return object_data, units, metadata

    section_count = 0
    for render_key, render_value in render_groups.items():
        render = _unwrap(render_value)
        if not isinstance(render, dict):
            continue
        render_id = render.get("id")
        if not isinstance(render_id, int):
            try:
                render_id = int(render_key)
            except (TypeError, ValueError):
                render_id = None
        render_label = render.get("name")
        section_list = render.get("sectionList", {})
        if not isinstance(section_list, dict):
            continue
        for section_key, section_value in section_list.items():
            section = _unwrap(section_value)
            if not isinstance(section, dict):
                continue
            section_count += 1
            section_id = section.get("id")
            if not isinstance(section_id, int):
                try:
                    section_id = int(section_key)
                except (TypeError, ValueError):
                    section_id = None
            for floor_key, floor_value in section.get("floorList", {}).items():
                floor_node = _unwrap(floor_value)
                if not isinstance(floor_node, dict):
                    continue
                flat_list = floor_node.get("flatList", {})
                values = (flat_list.values() if isinstance(flat_list, dict)
                          else flat_list if isinstance(flat_list, list) else [])
                for flat_value in values:
                    flat = _unwrap(flat_value)
                    if not isinstance(flat, dict) or not isinstance(flat.get("id"), int):
                        continue
                    unit_id = flat["id"]
                    corpus_match = re.search(r"\bкорпус\s+(\S+)", render_label or "", re.I)
                    units.append(_unit_row(
                        flat, project_id=project_id, project_name=project_name,
                        city_label=city_label, address_label=address_label,
                        district_label=district_label, page_url=page_url,
                        observed_at=observed_at, body_sha256=body_sha256,
                        render_id=render_id, render_label=render_label,
                        corpus_number=(corpus_match.group(1).rstrip(",.;")
                                       if corpus_match else None),
                        section_id=section_id, section_label=section.get("name"),
                        floor_group_key=str(floor_key)))
    unit_ids = [row["source_unit_id"] for row in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("duplicate unit IDs within one captured project page")
    metadata = {
        "source_project_id": project_id,
        "project_name": project_name,
        "project_city_label": city_label,
        "project_address_label": address_label,
        "project_district_label": district_label,
        "render_groups": len(render_groups),
        "sections": section_count,
        "unit_rows": len(units),
        "unique_unit_ids": len(set(unit_ids)),
        "nuxt_pool_items": len(pool),
        "nuxt_payload_chars": len(parser.nuxt_json),
        "render_type_code": object_data.get("renderTypeCode"),
        "catalog_data_mode": "renderDataList",
        "catalog_parse_state": "render_groups_parsed" if units else "no_inventory_rows",
        "source_catalog_total_count": None,
        "source_ssr_item_count": len(units),
        "source_ssr_pagination": None,
        "complete_catalog_in_this_capture": True,
    }
    return object_data, units, metadata


def load_targets(catalog_path: Path) -> dict[str, dict[str, Any]]:
    raw = catalog_path.read_bytes()
    if digest(raw) != CATALOG_JSONL_SHA256:
        raise ValueError("frozen Etalon listing JSONL hash changed")
    targets: dict[str, dict[str, Any]] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # The frozen project observations intentionally contain the literal
        # project URL, not a separately normalized slug field. Derive the
        # allowlisted slug from that URL rather than editing the frozen input.
        project_url = checked_url(row.get("project_url"), {HOST})
        project_path = urlsplit(project_url).path.rstrip("/")
        slug = project_path.rsplit("/", 1)[-1]
        name = row.get("project_name")
        url = row.get("apartment_catalog_url")
        if slug not in EXPECTED_PROJECTS or name != EXPECTED_PROJECTS[slug]:
            raise ValueError("unexpected project in frozen Etalon listing")
        if project_path != f"/msk/object/{slug}":
            raise ValueError(f"project URL is not the exact source-linked route for {slug}")
        checked = checked_url(url, {HOST})
        parsed = urlsplit(checked)
        if parsed.path != f"/msk/object/{slug}/flat/" or parsed.query:
            raise ValueError(f"catalog URL is not the exact source-linked route for {slug}")
        if slug in targets:
            raise ValueError(f"duplicate project slug in frozen listing: {slug}")
        targets[slug] = {
            "slug": slug,
            "project_name": name,
            "project_url": row.get("project_url"),
            "catalog_url": checked,
            "developer_project_id": row.get("developer_project_id"),
        }
    if set(targets) != set(EXPECTED_PROJECTS):
        raise ValueError("frozen listing must contain exactly the seven expected cards")
    return targets


def _safe_headers(headers: dict[str, str] | Any) -> dict[str, str | None]:
    allow = {"date", "content-type", "etag", "last-modified", "server", "x-powered-by"}
    result: dict[str, str | None] = {}
    for name, value in headers.items():
        if name.lower() in allow:
            result[name.lower()] = value[:300] if isinstance(value, str) else None
    return result


def capture_six(targets: dict[str, dict[str, Any]], capture_dir: Path, *, live: bool) -> list[dict[str, Any]]:
    capture_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = capture_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ledger = capture_dir / "requests.jsonl"
    target_list = [targets[slug] for slug in EXPECTED_PROJECTS if slug != "shagal"]
    if len(target_list) != 6:
        raise ValueError("capture target bound is not exactly six pages")
    with (capture_dir / "capture.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing: dict[str, dict[str, Any]] = {}
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = json.loads(line)
                    existing[event["url"]] = event
        if live:
            for index, target in enumerate(target_list):
                url = target["catalog_url"]
                prior = existing.get(url)
                if prior:
                    if prior.get("status") == 200 and not prior.get("error") and prior.get("body_sha256"):
                        body_path = raw_dir / f"{prior['body_sha256']}.body"
                        if not body_path.is_file() or digest(body_path.read_bytes()) != prior["body_sha256"]:
                            raise ValueError("previous successful capture is missing or has bad hash; no refetch")
                        continue
                    raise ValueError("prior URL attempt is non-success; no retry or next same-host request")
                if index:
                    time.sleep(1.0)
                result = curl_fetch(url, {HOST}, {}, 20)
                blocked = result.status in BLOCK_STATUSES or (
                    result.status == 200 and bool(CHALLENGE_TITLE.search(
                        result.body[:100_000].decode("utf-8", "replace"))))
                error = result.error or (
                    "challenge_interstitial" if blocked and result.status == 200 else None)
                if result.status == 200 and not result.body and not error:
                    error = "empty_body"
                event: dict[str, Any] = {
                    "at": utc_now(), "url": url, "final_url": result.final_url,
                    "status": result.status, "error": error, "blocked": blocked,
                    "body_bytes": len(result.body), "body_sha256": None,
                    "headers": _safe_headers(dict(result.headers)),
                }
                if result.status == 200 and not error and result.body:
                    body_hash = digest(result.body)
                    atomic_bytes(raw_dir / f"{body_hash}.body", result.body)
                    event["body_sha256"] = body_hash
                append_event(ledger, event)
                existing[url] = event
                if blocked or error or result.status != 200:
                    break
        return [existing[url] for url in (target["catalog_url"] for target in target_list)
                if url in existing]


def _read_unit_rows(body_path: Path, target: dict[str, Any], page_url: str,
                    observed_at: str, expected_hash: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    body = body_path.read_bytes()
    actual_hash = digest(body)
    if actual_hash != expected_hash:
        raise ValueError(f"capture hash mismatch: {body_path}")
    _obj, rows, metadata = parse_catalog_page(
        body, target=target, page_url=page_url, observed_at=observed_at,
        body_sha256=actual_hash)
    return rows, metadata


def _deduplicate(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["source_unit_id"]].append(row)
    accepted: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for unit_id in sorted(grouped):
        variants = grouped[unit_id]
        signatures = {canonical_json({key: row.get(key) for key in CONFLICT_FIELDS})
                      for row in variants}
        if len(signatures) != 1:
            differing = [key for key in CONFLICT_FIELDS
                         if len({canonical_json(row.get(key)) for row in variants}) > 1]
            conflicts.append({
                "source_unit_id": unit_id,
                "conflict_fields": differing,
                "variants": [{key: row.get(key) for key in UNIT_FIELDS} for row in variants],
            })
            continue
        first = dict(variants[0])
        observations = {
            canonical_json({
                "url": row["source_capture_url"],
                "observed_at": row["source_capture_observed_at"],
                "body_sha256": row["source_capture_body_sha256"],
            }): {
                "url": row["source_capture_url"],
                "observed_at": row["source_capture_observed_at"],
                "body_sha256": row["source_capture_body_sha256"],
            }
            for row in variants
        }
        first["source_page_observations"] = [observations[key] for key in sorted(observations)]
        accepted.append(first)
    return accepted, conflicts


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    data = b"".join(canonical_json(row) + b"\n" for row in rows)
    atomic_bytes(path, data)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import io

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(UNIT_FIELDS) + ["source_page_observations"],
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        cooked = dict(row)
        if "source_page_observations" in cooked:
            cooked["source_page_observations"] = json.dumps(
                cooked["source_page_observations"], ensure_ascii=False)
        writer.writerow(cooked)
    atomic_bytes(path, stream.getvalue().encode("utf-8"))


def _run_locked(*, catalog_path: Path, shagal_body_path: Path,
                output_dir: Path, live: bool) -> dict[str, Any]:
    targets = load_targets(catalog_path)
    shagal_bytes = shagal_body_path.read_bytes()
    shagal_sha = digest(shagal_bytes)
    if shagal_sha != SHAGAL_BODY_SHA256:
        raise ValueError("preserved Shagal capture hash changed")
    output_dir.mkdir(parents=True, exist_ok=True)
    captures = capture_six(targets, output_dir / "captures", live=live)

    rows: list[dict[str, Any]] = []
    page_metadata: list[dict[str, Any]] = []
    shagal_target = targets["shagal"]
    _shagal_object, shagal_rows, shagal_meta = parse_catalog_page(
        shagal_bytes, target=shagal_target, page_url=shagal_target["catalog_url"],
        observed_at="2026-10-10T17:00:29+00:00", body_sha256=shagal_sha)
    rows.extend(shagal_rows)
    page_metadata.append({
        **shagal_meta, "slug": "shagal", "url": shagal_target["catalog_url"],
        "status": 200, "body_bytes": len(shagal_bytes), "body_sha256": shagal_sha,
        "observed_at": "2026-10-10T17:00:29+00:00", "capture_input": str(shagal_body_path),
    })
    event_by_url = {event["url"]: event for event in captures}
    for slug in EXPECTED_PROJECTS:
        if slug == "shagal":
            continue
        target = targets[slug]
        event = event_by_url.get(target["catalog_url"])
        if not event or event.get("status") != 200 or event.get("error") or not event.get("body_sha256"):
            page_metadata.append({
                "slug": slug, "url": target["catalog_url"], "status": event.get("status") if event else None,
                "error": event.get("error") if event else "not_attempted_or_not_captured",
                "blocked": event.get("blocked", False) if event else False,
            })
            continue
        body_path = output_dir / "captures" / "raw" / f"{event['body_sha256']}.body"
        parsed_rows, metadata = _read_unit_rows(
            body_path, target, event["final_url"], event["at"], event["body_sha256"])
        rows.extend(parsed_rows)
        page_metadata.append({
            **metadata, "slug": slug, "url": target["catalog_url"], "final_url": event["final_url"],
            "status": event["status"], "body_bytes": event["body_bytes"],
            "body_sha256": event["body_sha256"], "observed_at": event["at"],
            "capture_headers": event.get("headers", {}),
        })
    deduped, conflicts = _deduplicate(rows)
    _write_jsonl(output_dir / "unit-results.jsonl", deduped)
    _write_jsonl(output_dir / "unit-id-conflicts.jsonl", conflicts)
    _write_csv(output_dir / "unit-results.csv", deduped)
    manifest = {
        "schema_version": "etalongroup-multi-project-ssr-catalog/1",
        "generated_at": utc_now(),
        "input_project_listing_jsonl": str(catalog_path),
        "input_project_listing_sha256": digest(catalog_path.read_bytes()),
        "preserved_shagal_capture": {
            "path": str(shagal_body_path), "sha256": shagal_sha,
            "bytes": len(shagal_bytes), "observed_at": "2026-10-10T17:00:29+00:00",
        },
        "requested_catalog_pages": 7,
        "live_target_pages": 6,
        "successful_project_pages_parsed": sum(page.get("status") == 200 and "unit_rows" in page
                                                for page in page_metadata),
        "project_pages": page_metadata,
        "raw_unit_rows_before_dedup": len(rows),
        "unique_source_unit_ids_after_dedup": len(deduped),
        "unit_id_conflict_count": len(conflicts),
        "unit_results_jsonl_sha256": digest((output_dir / "unit-results.jsonl").read_bytes()),
        "unit_results_csv_sha256": digest((output_dir / "unit-results.csv").read_bytes()),
        "unit_id_conflicts_jsonl_sha256": digest((output_dir / "unit-id-conflicts.jsonl").read_bytes()),
        "price_semantics": "raw price, priceTotal, and priceSpecial fields are preserved separately; no universal semantic normalization",
        "availability_semantics": "captured isBooked field only; not a live guarantee",
        "image_rights_status": "unknown; source image URLs retained privately and image files not fetched",
        "publication_allowed": False,
    }
    atomic_bytes(output_dir / "manifest.json", canonical_json(manifest) + b"\n")
    return manifest


def run(*, catalog_path: Path = DEFAULT_CATALOG, shagal_body_path: Path = DEFAULT_SHAGAL_BODY,
        output_dir: Path = DEFAULT_OUTPUT, live: bool = False) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "run.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_locked(catalog_path=catalog_path, shagal_body_path=shagal_body_path,
                           output_dir=output_dir, live=live)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-jsonl", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--shagal-html", type=Path, default=DEFAULT_SHAGAL_BODY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true",
                        help="fetch only the six exact non-Shagal source-linked /flat/ URLs")
    args = parser.parse_args()
    if not args.output_dir.resolve().is_relative_to(ROOT / ".runtime/research/developer-harvest"):
        parser.error("output-dir must remain under private developer-harvest runtime")
    try:
        result = run(catalog_path=args.catalog_jsonl, shagal_body_path=args.shagal_html,
                     output_dir=args.output_dir, live=args.live)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
