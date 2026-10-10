"""Build a local, apartment-free project registry from EstateLine XLSX reports.

The XLSX files are user-supplied snapshots. This importer reads only the
project-level fields needed for a directory and drops contact, price,
apartment, and unit-level data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

DEFAULT_SOURCE_DIR = Path("/home/pirojban/Загрузки/1_xls_estateline")
DEFAULT_OUTPUT_DIR = Path("data/property/estateline")
RIGHTS_STATUS = "user_supplied_internal_use"
SCHEMA_VERSION = "estateline-projects-v1"
SS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

FIELD_ALLOWLIST = {
    "ID", "Наименование", "Вид объекта", "Состояние", "Рынок",
    "Этажность", "Местоположение", "Срок ввода", "Характеристика",
}
FIELD_NAMES = {
    "ID": "id", "Наименование": "name", "Вид объекта": "source_type",
    "Состояние": "status", "Рынок": "market", "Этажность": "floors",
    "Местоположение": "address", "Срок ввода": "handover",
    "Характеристика": "characteristic",
}
REPORT_PERIOD_RE = re.compile(
    r"в период\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+"
    r"(\d{2}\.\d{2}\.\d{4}).*?по региону\s*[«\"]([^»\"]+)[»\"]",
    re.I | re.S,
)
PAGE_ID_RE = re.compile(r"/baza-stroyashchikhsya-obektov/(\d+)(?:[/?#]|$)")
OBJECT_START_RE = re.compile(r"^Информация о строительном объекте\s*:", re.I)
RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–—]\s*(\d+)\s*(?:этаж(?:ей|а|ность)?)?\s*$", re.I)
INTEGER_RE = re.compile(r"^\s*(\d+)\s*(?:этаж(?:ей|а|ность)?)?\s*$", re.I)
HANDOVER_RE = re.compile(r"^\s*(?:(I{1,3}|IV)\s*квартал\s*)?(\d{4})\s*$", re.I)
APARTMENT_TERM_RE = re.compile(
    r"(?<!\w)(?:апартамент(?:ы|а|ов|ам|ами|ах|е|у)?|"
    r"апарт\s*[-‑–—]?\s*(?:отел(?:ь|я|и|ей|ям|ями|ях)?|"
    r"комплекс(?:а|ы|ов|ам|ами|ах)?))(?!\w)",
    re.I,
)
RESIDENTIAL_TERM_RE = re.compile(
    r"(?<!\w)(?:жил(?:ой|ая|ое|ые|ого|ую|ых|ым|ыми)|"
    r"квартир(?:а|ы|е|у|ой|ам|ами|ах)?)(?!\w)",
    re.I,
)
NEGATED_RESIDENTIAL_TERM_RE = re.compile(
    r"(?<!\w)не\s+(?:жил(?:ой|ая|ое|ые|ого|ую|ых|ым|ыми)|"
    r"квартир(?:а|ы|е|у|ой|ам|ами|ах)?)(?!\w)",
    re.I,
)
RESIDENTIAL_COMPLEX_CONTEXT_RE = re.compile(
    r"(?<!\w)(?:(?:многофункциональн\w+)\s+)?"
    r"жил(?:ой|ого|ом|ым|ые|ых)\s+комплекс\w*"
    r"(?:\s+[«\"][^»\"]{1,100}[»\"])?"
    r"(?:\s+с\s+апартамент\w*)?",
    re.I,
)
OFFICE_OBJECT_RE = re.compile(
    r"(?<!\w)(?:офисн\w+|бизнес[- ]?центр\w*|делов\w+\s+центр\w*|"
    r"административн\w+\s+(?:центр|комплекс|здание)\w*|"
    r"многофункциональн\w+\s+делов\w+\s+центр\w*)(?!\w)",
    re.I,
)
MOSCOW_RE = re.compile(r"(?:^|[\s,;(])г\.?\s*Москва\b|\bМосква\b", re.I)
MOSCOW_REGION_RE = re.compile(r"Московск(?:ая|ой)\s+област", re.I)
KAZAN_RE = re.compile(r"Казан(?:ь|и|ью|е|ская|ский)\b", re.I)
EXPLICIT_CITY_RE = re.compile(r"(?:^|[,;\s])г(?:\.|\s)\s*([^,;()]+)", re.I)
TARGET_CATEGORIES = {
    "многоквартирные дома",
    "малоэтажные дома",
    "апартаменты, апарт-отели, мфк",
    "апартаменты, апарт-отели и мфк",
    "апартаменты / апарт-отели / мфк",
}


def is_target_category(category: str) -> bool:
    return normalize_for_compare(category) in TARGET_CATEGORIES


@dataclass(frozen=True)
class SourceFile:
    path: Path
    sha256: str
    bytes: int
    period_start: date
    period_end: date
    region: str
    report_text: str


@dataclass
class SourceRecord:
    page_id: str
    source_row_id: str | None
    source_url: str
    source_file: SourceFile
    sheet_name: str
    category: str
    fields: dict[str, str]
    row_start: int
    row_end: int

    @property
    def address(self) -> str | None:
        return nonempty(self.fields.get("address"))


def nonempty(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{SS_NS}}}t"))
        for item in root.findall(f"{{{SS_NS}}}si")
    ]


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{SS_NS}}}t"))
    value_node = cell.find(f"{{{SS_NS}}}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        if "Id" in rel.attrib and "Target" in rel.attrib
    }
    result = []
    for sheet in workbook.findall(f".//{{{SS_NS}}}sheet"):
        rel_id = sheet.attrib.get(f"{{{REL_NS}}}id")
        if rel_id not in targets:
            continue
        target = targets[rel_id]
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        result.append((sheet.attrib.get("name", ""), path))
    return result


def _hyperlinks(archive: zipfile.ZipFile, sheet_path: str) -> dict[int, str]:
    stem, filename = posixpath.split(sheet_path)
    rels_path = posixpath.join(stem, "_rels", f"{filename}.rels")
    try:
        rels_root = ET.fromstring(archive.read(rels_path))
        targets = {
            item.attrib["Id"]: item.attrib.get("Target", "")
            for item in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        sheet_root = ET.fromstring(archive.read(sheet_path))
    except KeyError:
        return {}
    output = {}
    for link in sheet_root.findall(f".//{{{SS_NS}}}hyperlink"):
        ref = link.attrib.get("ref", "")
        match = re.fullmatch(r"A(\d+)", ref)
        rel_id = link.attrib.get(f"{{{REL_NS}}}id")
        if match and rel_id and rel_id in targets:
            output[int(match.group(1))] = targets[rel_id]
    return output


def _row_cells(archive: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> list[tuple[int, str, str]]:
    root = ET.fromstring(archive.read(sheet_path))
    data = root.find(f"{{{SS_NS}}}sheetData")
    if data is None:
        return []
    rows = []
    for row in data.findall(f"{{{SS_NS}}}row"):
        number = int(row.attrib.get("r", "0"))
        columns: dict[str, str] = {}
        for cell in row.findall(f"{{{SS_NS}}}c"):
            match = re.match(r"([A-Z]+)", cell.attrib.get("r", ""))
            if match and match.group(1) in {"A", "B"}:
                columns[match.group(1)] = _cell_value(cell, shared)
        rows.append((number, columns.get("A", ""), columns.get("B", "")))
    return rows


def parse_period_header(text: str) -> tuple[date, date, str]:
    match = REPORT_PERIOD_RE.search(text)
    if not match:
        raise ValueError(f"unrecognized EstateLine report title: {text[:180]!r}")
    return (
        datetime.strptime(match.group(1), "%d.%m.%Y").date(),
        datetime.strptime(match.group(2), "%d.%m.%Y").date(),
        match.group(3).strip(),
    )


def parse_source_file(path: Path, file_hash: str | None = None) -> tuple[SourceFile, list[SourceRecord], dict[str, int]]:
    """Read one XLSX while retaining only allowlisted project facts."""
    digest = file_hash or sha256_file(path)
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet_data = []
        report_title = ""
        for sheet_name, sheet_path in _sheet_paths(archive):
            rows = _row_cells(archive, sheet_path, shared)
            if not report_title and rows:
                report_title = rows[0][1]
            sheet_data.append((sheet_name, rows, _hyperlinks(archive, sheet_path)))
    period_start, period_end, region = parse_period_header(report_title)
    source_file = SourceFile(path, digest, path.stat().st_size, period_start, period_end, region, report_title)
    records: list[SourceRecord] = []
    for sheet_name, rows, links in sheet_data:
        category = rows[1][1].strip().lstrip("-–— ").strip() if len(rows) > 1 else ""
        starts = [i for i, (_row, label, _value) in enumerate(rows) if OBJECT_START_RE.match(label.strip())]
        for position, index in enumerate(starts):
            next_index = starts[position + 1] if position + 1 < len(starts) else len(rows)
            row_start = rows[index][0]
            url = links.get(row_start, "")
            page_match = PAGE_ID_RE.search(url)
            if not page_match:
                continue
            allowed: dict[str, str] = {}
            row_end = row_start
            for row_num, label, value in rows[index:next_index]:
                row_end = row_num
                label = label.strip()
                if label == "РАБОТЫ ПО ОБЪЕКТУ":
                    break
                if label in FIELD_ALLOWLIST and label not in allowed:
                    allowed[label] = value.strip()
            fields = {FIELD_NAMES[label]: value for label, value in allowed.items()}
            records.append(SourceRecord(
                page_id=page_match.group(1),
                source_row_id=nonempty(fields.get("id")),
                source_url=url,
                source_file=source_file,
                sheet_name=sheet_name,
                category=category,
                fields=fields,
                row_start=row_start,
                row_end=row_end,
            ))
    return source_file, records, dict(Counter(record.category for record in records))


def normalize_for_compare(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _explicit_city_conflict(record: SourceRecord) -> list[str]:
    address = record.address or ""
    reasons: list[str] = []
    if MOSCOW_REGION_RE.search(address):
        reasons.append("address_explicit_moscow_region")
    if KAZAN_RE.search(address):
        reasons.append("address_explicit_kazan")
    if not MOSCOW_RE.search(address):
        for match in EXPLICIT_CITY_RE.finditer(address):
            value = match.group(1).strip().casefold()
            if value.startswith("москва"):
                continue
            if value.startswith("казан"):
                reasons.append("address_explicit_kazan")
            elif "област" in value or value in {"мо", "московская область"}:
                reasons.append("address_explicit_moscow_region")
            elif value and not re.fullmatch(r"\d{5,6}", value):
                reasons.append("address_explicit_other_city")
    return sorted(set(reasons))


def classify_record(record: SourceRecord) -> tuple[str | None, list[str]]:
    """Return accepted registry kind or review reasons for one source record."""
    category = normalize_for_compare(record.category)
    reasons: list[str] = []
    if category in {"многоквартирные дома", "многоквартирный дом"}:
        kind: str | None = "multifamily"
    elif category in {"малоэтажные дома", "малоэтажные жилые дома"}:
        kind = "low_rise"
    elif category in {
        "апартаменты, апарт-отели, мфк",
        "апартаменты, апарт-отели и мфк",
        "апартаменты / апарт-отели / мфк",
    }:
        name_and_type = " ".join((
            record.fields.get("name", ""),
            record.fields.get("source_type", ""),
        ))
        characteristic = record.fields.get("characteristic", "")
        description_without_context = RESIDENTIAL_COMPLEX_CONTEXT_RE.sub(" ", characteristic)
        has_office_subject = bool(OFFICE_OBJECT_RE.search(name_and_type))
        has_apartment = bool(
            APARTMENT_TERM_RE.search(name_and_type)
            or APARTMENT_TERM_RE.search(description_without_context)
        )
        evidence_without_negated_use = NEGATED_RESIDENTIAL_TERM_RE.sub(
            " ", " ".join((name_and_type, description_without_context))
        )
        has_residential = bool(RESIDENTIAL_TERM_RE.search(evidence_without_negated_use))
        has_contextual_residential_signal = bool(
            APARTMENT_TERM_RE.search(characteristic)
            or RESIDENTIAL_TERM_RE.search(NEGATED_RESIDENTIAL_TERM_RE.sub(" ", characteristic))
        )
        if has_office_subject and not has_apartment and not has_residential and has_contextual_residential_signal:
            kind = None
            reasons.append("residential_signal_only_describes_neighboring_complex")
        elif has_apartment:
            # Explicit apartment use stays a separate kind even if prose also
            # describes the complex as residential.
            kind = "apartment_complex"
        elif has_residential:
            kind = "multifamily"
        else:
            kind = None
            reasons.append("commercial_group_without_explicit_residential_or_apartment_use")
    else:
        kind = None
        reasons.append("unrecognized_source_category")
    conflicts = _explicit_city_conflict(record)
    reasons.extend(conflicts)
    if conflicts:
        kind = None
    if not record.fields.get("name"):
        reasons.append("missing_project_name")
        kind = None
    return kind, sorted(set(reasons))


def parse_floors(value: str | None) -> int | str | None:
    if not value:
        return None
    raw = " ".join(value.split())
    match = RANGE_RE.fullmatch(raw)
    if match:
        low, high = map(int, match.groups())
        return str(low) if low == high else f"{low}–{high}"
    match = INTEGER_RE.fullmatch(raw)
    if match:
        return int(match.group(1))
    return raw


def normalize_handover(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value:
        return None, None, None
    raw = " ".join(value.split())
    match = HANDOVER_RE.fullmatch(raw)
    if not match:
        return raw, None, None
    quarter, year = match.groups()
    if quarter:
        q = {"I": 1, "II": 2, "III": 3, "IV": 4}[quarter.upper()]
        return f"{year}-Q{q}", year, str(q)
    return year, year, None


def _facts_equal(a: SourceRecord, b: SourceRecord) -> bool:
    return (
        a.source_row_id == b.source_row_id
        and a.category == b.category
        and a.sheet_name == b.sheet_name
        and normalize_for_compare(a.source_file.region) == normalize_for_compare(b.source_file.region)
        and {
            key: normalize_for_compare(value)
            for key, value in a.fields.items()
        } == {
            key: normalize_for_compare(value)
            for key, value in b.fields.items()
        }
    )


def _sort_key(record: SourceRecord) -> tuple[str, str, int, str]:
    return record.source_file.path.name, record.sheet_name, record.row_start, record.source_row_id or ""


def _latest_by_page_id(records: Iterable[SourceRecord]) -> dict[str, list[SourceRecord]]:
    """Select latest versions by canonical linked page ID before any filtering."""
    by_id: dict[str, list[SourceRecord]] = defaultdict(list)
    for record in records:
        by_id[record.page_id].append(record)
    latest_by_id: dict[str, list[SourceRecord]] = {}
    for page_id, versions in by_id.items():
        latest_date = max(record.source_file.period_end for record in versions)
        latest_by_id[page_id] = sorted(
            (record for record in versions if record.source_file.period_end == latest_date),
            key=_sort_key,
        )
    return latest_by_id


def _latest_records_conflict(records: list[SourceRecord]) -> bool:
    return len(records) > 1 and any(
        not _facts_equal(records[0], item)
        for item in records[1:]
    )


def _load_osm_index(index_dir: Path) -> tuple[dict[tuple[str | None, ...], dict[str, Any]], dict[str, Any]]:
    addresses_path = index_dir / "addresses.jsonl"
    manifest_path = index_dir / "manifest.json"
    if not addresses_path.exists() or not manifest_path.exists():
        return {}, {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output: dict[tuple[str | None, ...], dict[str, Any]] = {}
    collisions: set[tuple[str | None, ...]] = set()
    with addresses_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            raw_key = item.get("address_key")
            key = (
                tuple(raw_key)
                if isinstance(raw_key, list) and len(raw_key) == 5
                else tuple(item.get(field) for field in (
                    "street_key", "housenumber_key", "block_key", "building_key", "conscriptionnumber_key"
                ))
            )
            if key in output:
                output.pop(key, None)
                collisions.add(key)
                continue
            if key not in collisions:
                output[key] = item
    return output, manifest


def _parse_source_address(address: str) -> tuple[tuple[str | None, ...] | None, str | None]:
    """Parse a complete, specific street/house address without dropping tails."""
    try:
        from sources.city.local_address_index import address_key_tuple
    except ImportError:
        return None, "address_normalizer_unavailable"
    unsafe_context = re.compile(
        r"(?<!\w)(?:владение|вл\.?|участ\w*|земельн\w*\s+участ\w*|"
        r"з\s*/\s*у|кадастров\w*|снос\w*|демонтаж\w*|вблизи|рядом|около|"
        r"недалеко)(?!\w)|(?<!\w)у\s+(?:д\.?|дом)\s*\d",
        re.I,
    )
    if unsafe_context.search(address):
        return None, "address_has_holding_plot_demolition_or_nearby_context"
    unmapped_component = re.compile(
        r"(?<!\w)(?:корпус|корп\.?|строение|стр\.?|литера|лит\.?)(?!\w)",
        re.I,
    )
    if unmapped_component.search(address):
        return None, "address_contains_unmapped_building_component"
    # Parenthetical notes may be removed only when they contain no second
    # address/component. Plot/demolition/nearby terms were rejected above.
    parentheses = re.findall(r"\(([^()]*)\)", address)
    for note in parentheses:
        if re.search(
            r"(?<!\w)(?:г\.|ул\.?|шоссе|проспект|дом|д\.?|корп\.?|стр\.?)(?!\w)",
            note,
            re.I,
        ):
            return None, "address_has_parenthetical_address_component"
    cleaned = re.sub(r"\s*\([^()]*\)", "", address)
    parts = [part.strip() for part in cleaned.replace("\n", ", ").split(",") if part.strip()]
    useful: list[str] = []
    for part in parts:
        if re.fullmatch(r"(?:г\.?\s*)?Москва", part, re.I) or re.fullmatch(r"\d{5,6}", part):
            continue
        part = re.sub(r"^(?:г\.?\s*)?Москва\b\s*[,]?\s*", "", part, flags=re.I).strip()
        if part:
            useful.append(part)
    street_type = re.compile(
        r"(?:^|\s)(?:улица|ул\.?|шоссе|проспект|просп\.?|пр-т\.?|переулок|пер\.?|проезд|"
        r"набережная|наб\.?|бульвар|б-р|аллея|площадь|пл\.?|тупик|линия|дорога)\b",
        re.I,
    )
    house = re.compile(
        r"^(?:(?:дом|д\.)\s*)?\d+[\wА-Яа-яЁё./-]*$",
        re.I,
    )
    candidates = [
        (i, useful[i], useful[i + 1])
        for i in range(len(useful) - 1)
        if street_type.search(useful[i]) and house.fullmatch(useful[i + 1])
    ]
    if len(candidates) != 1:
        return None, "address_not_unambiguously_parseable"
    street_index, street, house_number = candidates[0]
    if street_index + 2 != len(useful):
        return None, "address_contains_unmapped_trailing_components"
    # An explicit house prefix is syntax in the source string, not a separate
    # OSM component. Normalize only this documented prefix before exact lookup.
    house_number = re.sub(r"^(?:дом|д\.)\s*", "", house_number, flags=re.I)
    # Optional OSM tags have no confirmed semantic equivalent in workbook
    # address wording, so all optional components remain exact nulls.
    key = address_key_tuple(street, house_number)
    if key is None:
        return None, "address_missing_street_or_housenumber"
    return key, None


def _source_address_tuple(address: str) -> tuple[str | None, ...] | None:
    return _parse_source_address(address)[0]


def _location(address: str | None, index: dict[tuple[str | None, ...], dict[str, Any]]) -> tuple[list[float] | None, str, str | None, str | None, dict[str, Any] | None]:
    if not address:
        return None, "unknown", None, "missing_source_address", None
    key, parse_reason = _parse_source_address(address)
    if key is None:
        return None, "unknown", None, parse_reason or "address_not_unambiguously_parseable", None
    item = index.get(key)
    if item is None:
        return None, "unknown", "strict_osm_address_tuple", "no_unique_exact_osm_match", None
    try:
        lon, lat = float(item["lon"]), float(item["lat"])
    except (KeyError, TypeError, ValueError):
        return None, "unknown", "strict_osm_address_tuple", "matched_record_missing_coordinates", None
    identity = {
        key: item.get(key)
        for key in ("osm_type", "osm_id", "osm_version", "osm_timestamp", "members", "representative_point_method")
        if item.get(key) is not None
    }
    identity["source_attribution"] = item.get("source_attribution", "© OpenStreetMap contributors")
    return [lon, lat], "verified", "strict_osm_address_tuple", None, identity


def _make_project(record: SourceRecord, kind: str, index: dict[tuple[str | None, ...], dict[str, Any]]) -> dict[str, Any]:
    handover, handover_year, handover_quarter = normalize_handover(record.fields.get("handover"))
    location, quality, method, reason, location_source = _location(record.address, index)
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_id": f"estateline:{record.page_id}",
        "source_row_id": record.source_row_id,
        "source_url_page_id": record.page_id,
        "city": "Москва",
        "record_kind": kind,
        "name": nonempty(record.fields.get("name")),
        "address": record.address,
        "building_type": nonempty(record.fields.get("source_type")) or record.category,
        "floors": parse_floors(record.fields.get("floors")),
        "status": nonempty(record.fields.get("status")),
        "handover": handover,
        "handover_source_text": nonempty(record.fields.get("handover")),
        "handover_year": handover_year,
        "handover_quarter": handover_quarter,
        "latest_known_date": record.source_file.period_end.isoformat(),
        "location": location,
        "location_quality": quality,
        "location_method": method,
        "location_reason": reason,
        "provenance": {
            "source": "EstateLine user-supplied XLSX report",
            "rights_status": "user_supplied_internal_use",
            "source_report": record.source_file.path.name,
            "source_report_sha256": record.source_file.sha256,
            "report_period_start": record.source_file.period_start.isoformat(),
            "report_period_end": record.source_file.period_end.isoformat(),
            "source_sheet": record.sheet_name,
            "source_category": record.category,
            "source_row_start": record.row_start,
            "source_row_end": record.row_end,
            "source_url": record.source_url,
            "date_semantics": "report_period_end; last observed snapshot, not current status",
            "location_source": location_source,
        },
    }


def _make_review(record: SourceRecord, reasons: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_id": f"estateline:{record.page_id}",
        "source_row_id": record.source_row_id,
        "source_url_page_id": record.page_id,
        "name": nonempty(record.fields.get("name")),
        "source_report_period_end": record.source_file.period_end.isoformat(),
        "source_report_region": record.source_file.region,
        "source_category": record.category,
        "address": record.address,
        "review_reasons": sorted(set(reasons)),
        "provenance": {
            "source": "EstateLine user-supplied XLSX report",
            "rights_status": RIGHTS_STATUS,
            "source_report": record.source_file.path.name,
            "source_report_sha256": record.source_file.sha256,
            "report_period_start": record.source_file.period_start.isoformat(),
            "report_period_end": record.source_file.period_end.isoformat(),
            "source_sheet": record.sheet_name,
            "source_row_start": record.row_start,
            "source_row_end": record.row_end,
            "source_url": record.source_url,
        },
    }


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def schema_summary() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "file": "projects.jsonl",
        "description": "Moscow project/building registry from latest EstateLine report snapshots; no apartment offers or unit prices.",
        "required_fields": ["registry_id", "city", "record_kind", "name", "latest_known_date"],
        "fields": {
            "registry_id": "estateline:<linked EstateLine page ID>; canonical identity, unique per output row",
            "source_row_id": "EstateLine workbook ID; preserved independently and not used as canonical identity",
            "source_url_page_id": "linked EstateLine page ID as seen in workbook hyperlink",
            "city": "Москва for accepted rows only",
            "record_kind": ["multifamily", "apartment_complex", "low_rise"],
            "name": "project/building name",
            "address": "source project-level address text",
            "building_type": "source Вид объекта, falling back to worksheet category",
            "floors": "integer for one exact integer; otherwise source range/text retained",
            "status": "latest observed source status, not asserted current",
            "handover": "normalized YYYY or YYYY-Qn when source supports it; original source text retained separately",
            "latest_known_date": "source report period end, not a live update timestamp",
            "location": "[longitude, latitude] only for one exact OSM address-index key; otherwise null",
            "location_quality": ["verified", "unknown"],
            "location_method": "strict_osm_address_tuple for exact index matches",
            "location_reason": "null for a verified match; otherwise why no marker was assigned",
            "provenance": "server-only source/report/file hash, row and hyperlink audit data",
        },
        "source_date_semantics": "The report period end is the latest known source observation. Status and handover are historical source assertions and may be stale.",
        "rights_status": RIGHTS_STATUS,
        "excluded_data": ["apartment/unit rows", "apartment prices", "contact names", "emails", "phone numbers", "developer contact details"],
    }


def import_corpus(source_dir: Path, output_dir: Path, address_index_dir: Path | None = None) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(
        [*source_dir.glob("moskva-*.xlsx"), *source_dir.glob("moskovskaya-obl-*.xlsx")],
        key=lambda path: path.name,
    )
    if not paths:
        raise FileNotFoundError(f"no Moscow/Moscow-region XLSX reports in {source_dir}")
    parsed_files: list[SourceFile] = []
    all_records: list[SourceRecord] = []
    file_hashes: dict[Path, str] = {}
    file_report: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    parse_errors: list[dict[str, str]] = []
    for path in paths:
        digest = sha256_file(path)
        file_hashes[path] = digest
        try:
            source_file, records, counts = parse_source_file(path, digest)
        except Exception as exc:
            parse_errors.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        parsed_files.append(source_file)
        all_records.extend(records)
        category_counts.update(counts)
        file_report.append({
            "path": path.name, "sha256": digest, "bytes": source_file.bytes,
            "report_period_start": source_file.period_start.isoformat(),
            "report_period_end": source_file.period_end.isoformat(),
            "report_region": source_file.region, "records_total": len(records),
            "record_categories": counts,
        })

    ignored = Counter()
    latest_by_id = _latest_by_page_id(all_records)
    history_target_ids = {
        record.page_id for record in all_records if is_target_category(record.category)
    }
    latest_target_ids = {
        page_id
        for page_id, tied_latest in latest_by_id.items()
        if any(is_target_category(record.category) for record in tied_latest)
    }
    latest_non_target_supersedes = {
        page_id for page_id in history_target_ids if page_id not in latest_target_ids
    }
    excluded_non_moscow = 0
    superseded_categories = Counter(
        latest_by_id[page_id][-1].category
        for page_id in latest_non_target_supersedes
    )
    ignored.update({
        f"superseded_by_latest_{category}": count
        for category, count in superseded_categories.items()
    })
    regions_by_id: dict[str, set[str]] = defaultdict(set)
    for record in all_records:
        regions_by_id[record.page_id].add(normalize_for_compare(record.source_file.region))

    index: dict[tuple[str | None, ...], dict[str, Any]] = {}
    osm_manifest = None
    if address_index_dir is not None:
        try:
            from sources.city.local_address_index import address_key_tuple as _address_key_tuple  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "OSM address matching requires the project geodata environment; "
                "run with .runtime/geodata-env/bin/python"
            ) from exc
        index, osm_manifest = _load_osm_index(address_index_dir)
    projects: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    reasons_count = Counter()
    review_primary_count = Counter()
    kind_count = Counter()
    tie_count = sum(1 for versions in latest_by_id.values() if len(versions) > 1)
    conflicting_target_ties = 0
    moscow_names = {"москва", "г. москва"}
    for page_id in sorted(latest_target_ids):
        tied_latest = latest_by_id[page_id]
        latest_target_records = [record for record in tied_latest if is_target_category(record.category)]
        selected = (latest_target_records or tied_latest)[-1]
        kind, reasons = classify_record(selected)
        if _latest_records_conflict(tied_latest):
            kind = None
            reasons.append("conflicting_records_at_latest_report_date")
            conflicting_target_ties += 1
        regions = regions_by_id[page_id]
        if regions.intersection(moscow_names) and any(region not in moscow_names for region in regions):
            kind = None
            reasons.append("source_report_region_conflict_moscow_and_moscow_region")
        region = normalize_for_compare(selected.source_file.region)
        if region not in moscow_names:
            if not MOSCOW_RE.search(selected.address or "") and not regions.intersection(moscow_names):
                excluded_non_moscow += 1
                ignored[f"latest_target_outside_moscow:{selected.category}"] += 1
                continue
            kind = None
            reasons.append("source_report_region_is_moscow_region")
        if kind is None:
            if not reasons:
                reasons.append("not_eligible_for_moscow_project_registry")
            item = _make_review(selected, reasons)
            review.append(item)
            reasons_count.update(item["review_reasons"])
            primary_order = (
                "conflicting_records_at_latest_report_date",
                "source_report_region_conflict_moscow_and_moscow_region",
                "source_report_region_is_moscow_region",
                "address_explicit_moscow_region",
                "address_explicit_kazan",
                "address_explicit_other_city",
                "residential_signal_only_describes_neighboring_complex",
                "address_contains_unmapped_building_component",
                "address_has_holding_plot_demolition_or_nearby_context",
                "address_contains_unmapped_trailing_components",
                "address_has_parenthetical_address_component",
                "commercial_group_without_explicit_residential_or_apartment_use",
                "missing_project_name",
                "not_eligible_for_moscow_project_registry",
            )
            primary = next((reason for reason in primary_order if reason in item["review_reasons"]), item["review_reasons"][0])
            review_primary_count[primary] += 1
            continue
        projects.append(_make_project(selected, kind, index))
        kind_count[kind] += 1

    projects.sort(key=lambda item: item["registry_id"])
    review.sort(key=lambda item: (item["source_report_period_end"], item["registry_id"], item["source_report_region"]))
    projects_path = output_dir / "projects.jsonl"
    review_path = output_dir / "review_required.jsonl"
    schema_path = output_dir / "schema.json"
    manifest_path = output_dir / "source_manifest.json"
    _write_jsonl(projects_path, projects)
    _write_jsonl(review_path, review)
    schema_temp = schema_path.with_name(schema_path.name + ".tmp")
    schema_temp.write_text(json.dumps(schema_summary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    schema_temp.replace(schema_path)
    inventory = [
        {"path": path.name, "sha256": file_hashes[path], "bytes": path.stat().st_size}
        for path in paths
    ]
    valid_periods = [item.period_end for item in parsed_files]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_name": "EstateLine",
        "rights_status": RIGHTS_STATUS,
        "rights_note": "User-supplied local source authorized for internal use in this local project; no open-license claim or public redistribution permission asserted.",
        "source_directory_label": source_dir.name,
        "source_files_total": len(paths),
        "source_files_parsed": len(parsed_files),
        "source_files_by_report_region": dict(Counter(item.region for item in parsed_files)),
        "source_files": inventory,
        "reports": file_report,
        "records_parsed": len(all_records),
        "records_by_source_category": dict(sorted(category_counts.items())),
        "all_latest_page_ids": len(latest_by_id),
        "eligible_page_ids_latest": len(latest_target_ids),
        "latest_target_ids_by_source_category": dict(sorted(Counter(
            next(record.category for record in latest_by_id[page_id] if is_target_category(record.category))
            for page_id in latest_target_ids
        ).items())),
        "superseded_target_ids_by_latest_non_target_total": len(latest_non_target_supersedes),
        "superseded_target_ids_by_latest_non_target_category": dict(sorted(superseded_categories.items())),
        "excluded_non_moscow_latest_target_total": excluded_non_moscow,
        "latest_date_range": {
            "from": min(valid_periods).isoformat() if valid_periods else None,
            "to": max(valid_periods).isoformat() if valid_periods else None,
        },
        "latest_date_tie_page_ids": tie_count,
        "latest_date_conflicting_target_tie_page_ids": conflicting_target_ties,
        "projects_sha256": sha256_file(projects_path),
        "records_total": len(projects),
        "projects_by_record_kind": dict(sorted(kind_count.items())),
        "review_required_sha256": sha256_file(review_path),
        "review_required_total": len(review),
        "review_reasons": dict(sorted(reasons_count.items())),
        "review_primary_buckets": dict(sorted(review_primary_count.items())),
        "disjoint_final_buckets": {
            **{f"project:{key}": value for key, value in sorted(kind_count.items())},
            **{f"review:{key}": value for key, value in sorted(review_primary_count.items())},
        },
        "selection_exclusions_by_latest_category": dict(sorted(ignored.items())),
        "parse_errors": parse_errors,
        "locations": {
            "verified": sum(1 for item in projects if item["location_quality"] == "verified"),
            "null": sum(1 for item in projects if item["location"] is None),
            "osm_index_manifest": osm_manifest,
        },
        "outputs": {
            "projects": {"path": projects_path.name, "sha256": sha256_file(projects_path), "records": len(projects)},
            "review_required": {"path": review_path.name, "sha256": sha256_file(review_path), "records": len(review)},
            "schema": {"path": schema_path.name, "sha256": sha256_file(schema_path)},
        },
        "snapshot_at": max(valid_periods).isoformat() if valid_periods else None,
    }
    manifest_temp = manifest_path.with_name(manifest_path.name + ".tmp")
    manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_temp.replace(manifest_path)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--address-index", type=Path)
    args = parser.parse_args(argv)
    manifest = import_corpus(args.source_dir, args.output_dir, args.address_index)
    print(json.dumps({
        "projects": manifest["records_total"],
        "review_required": manifest["review_required_total"],
        "projects_sha256": manifest["projects_sha256"],
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
