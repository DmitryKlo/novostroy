"""Normalize locally stored Minstroy open-data permit workbooks.

This adapter reads only local XLS files. It emits clearly separate registry
events, never apartment offers, and conservatively selects only rows whose
object name begins with an explicit residential-building/quarter description.
The optional XLS reader dependency is ``xlrd>=2.0``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "property-registry/minstroy/1"
MINISTRY_ROOT = "https://minstroyrf.gov.ru"
OPEN_DATA_ROOT = f"{MINISTRY_ROOT}/opendata"
REUSE_TERMS_URL = f"{OPEN_DATA_ROOT}/"

DATASETS: dict[str, dict[str, str]] = {
    "construction_permit": {
        "source_id": "minstroy_construction_permits",
        "dataset_id": "7707780887-reestrstroit",
        "name": "Реестр выданных разрешений на строительство",
        "page_url": f"{OPEN_DATA_ROOT}/7707780887-reestrstroit/",
        "download_url": (
            f"{MINISTRY_ROOT}/upload/iblock/f74/"
            "m77bajhxyc1fwqooo37kdfbeb6jtnvo5/"
            "data_20260529t0000_structure_20161117t1330.xls"
        ),
        "filename": "permits_2026-05-29.xls",
        "http_observation": {
            "status_code": 200,
            "redirects": 0,
            "content_type": "application/vnd.ms-excel",
            "content_length": 2706432,
            "last_modified": "Mon, 01 Jun 2026 22:21:07 GMT",
            "etag": '"6a1e05d3-294c00"',
            "accept_ranges": "bytes",
            "if_none_match_status": 304,
            "if_modified_since_status": 304,
            "http_checked_at": "2026-10-09T10:17:11Z",
        },
        "event_type": "construction_permit_issued",
    },
    "commissioning_permit": {
        "source_id": "minstroy_commissioning_permits",
        "dataset_id": "7707780887-puttingintooperation",
        "name": "Реестр выданных разрешений на ввод объектов в эксплуатацию",
        "page_url": f"{OPEN_DATA_ROOT}/7707780887-puttingintooperation/",
        "download_url": (
            f"{MINISTRY_ROOT}/upload/iblock/62d/"
            "cqy3hwwozsegpkp139axe52tiujdt5b0/"
            "data_20260529t0000_structure_20161117t1330.xls"
        ),
        "filename": "commissioning_2026-05-29.xls",
        "http_observation": {
            "status_code": 200,
            "redirects": 0,
            "content_type": "application/vnd.ms-excel",
            "content_length": 1443840,
            "last_modified": "Mon, 01 Jun 2026 22:17:26 GMT",
            "etag": '"6a1e04f6-160800"',
            "accept_ranges": "bytes",
            "if_none_match_status": 304,
            "if_modified_since_status": 304,
            "http_checked_at": "2026-10-09T10:17:22Z",
        },
        "event_type": "commissioning_permit_issued",
    },
}

RELEASE_DATE = "2026-05-29"
PUBLISHED_LABEL = "29.05.2026"
ATTRIBUTION = "Минстрой России, открытые данные; дата версии набора 29.05.2026"
LICENSE_TERMS = (
    "Минстрой России разрешает свободное, бесплатное и бессрочное повторное "
    "использование, копирование, публикацию, распространение, изменение, "
    "объединение и коммерческое использование открытых данных при условии "
    "законного использования, отсутствия искажения и сохранения ссылки на источник."
)

RESIDENTIAL_PREFIX = re.compile(
    r"^(?:многоквартирн[а-яё]*\s+жил[а-яё]*\s+дом[а-яё]*|"
    r"жил[а-яё]*\s+квартал[а-яё]*|жил[а-яё]*\s+дом[а-яё]*)\b",
    re.IGNORECASE,
)
MOSCOW_CITY = re.compile(r"(?:г\s*\.?\s*москва|город\s+москва)", re.IGNORECASE)
MOSCOW_HOUSING_WORD = re.compile(
    r"многоквартир|жил[а-яё]*\s+(?:дом|квартал|микрорайон|здан)", re.IGNORECASE
)
LEADING_QUOTES = " \t\r\n«»\"'“”„‟()[]{}"

UNKNOWN_PROJECT_FIELDS = (
    "address",
    "construction_start_date",
    "completion_date",
    "floor_count",
    "area_sqm",
)

CSV_FIELDS = (
    "record_type",
    "data_mode",
    "record_mode",
    "source_id",
    "source_record_id",
    "dataset_id",
    "dataset_release_date",
    "event_type",
    "serial_number",
    "permit_number",
    "egrul_number",
    "event_date",
    "project_name",
    "applicant",
    "address",
    "construction_start_date",
    "completion_date",
    "floor_count",
    "area_sqm",
    "price_rub",
    "unknown_fields",
    "source_fields_json",
)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    cleaned = " ".join(str(value).replace("\xa0", " ").split())
    if not cleaned or cleaned.casefold() in {"nan", "none"}:
        return None
    return cleaned


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def is_explicit_residential_name(value: Any) -> bool:
    """Return true only for residential object titles, not incidental mentions."""
    name = _clean(value)
    if name is None:
        return False
    return bool(RESIDENTIAL_PREFIX.search(name.lstrip(LEADING_QUOTES)))


def _field(headers: Sequence[str], *prefixes: str) -> str | None:
    normalized = [(header, " ".join(header.casefold().split())) for header in headers]
    for prefix in prefixes:
        for original, folded in normalized:
            if folded.startswith(prefix.casefold()):
                return original
    return None


def _make_fact(
    value: Any,
    *,
    unit: str | None,
    scope: str,
    observed_at: str,
    source_id: str,
    source_record_id: str,
    source_field: str | None,
    rights_status: str = "cleared",
) -> dict[str, Any]:
    status = "observed" if value is not None else "unknown"
    return {
        "value": value,
        "unit": unit,
        "status": status,
        "scope": scope,
        "observed_at": observed_at,
        "provenance": {
            "source_id": source_id,
            "source_record_id": source_record_id,
            "source_field": source_field,
            "source_updated_at": None,
            "rights_status": rights_status,
            "method": "direct field mapping; no address or amenity inference",
            "source_version": RELEASE_DATE,
        },
    }


def normalize_registry_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    dataset_kind: str,
    observed_at: str,
    residential_only: bool = True,
) -> list[dict[str, Any]]:
    """Normalize workbook rows; callable with simple mappings for unit tests."""
    if dataset_kind not in DATASETS:
        raise ValueError(f"unsupported Minstroy dataset kind: {dataset_kind}")
    try:
        parsed_observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be ISO-8601 with timezone") from exc
    if parsed_observed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")

    config = DATASETS[dataset_kind]
    records: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for row_index, raw_row in enumerate(rows, start=1):
        source_fields = {str(key).strip(): _clean(value) for key, value in raw_row.items()}
        headers = list(source_fields)
        row_number_field = _field(headers, "№ п/п", "номер п/п")
        object_field = _field(headers, "наименование объекта")
        applicant_field = _field(headers, "наименование застройщика", "наименование заявителя")
        event_date_field = _field(headers, "дата выдачи разрешения", "дата выданного разрешения")
        permit_number_field = _field(headers, "номер выданного разрешения")
        egrul_field = _field(headers, "номер из ерул")

        project_name = source_fields.get(object_field) if object_field else None
        if residential_only and not is_explicit_residential_name(project_name):
            continue
        row_number = source_fields.get(row_number_field) if row_number_field else None
        source_record_token = row_number or str(row_index)
        source_record_id = f"{config['dataset_id']}:{source_record_token}"
        if source_record_id in seen_source_ids:
            raise ValueError(f"duplicate source record identifier: {source_record_id}")
        seen_source_ids.add(source_record_id)

        raw_event_date = source_fields.get(event_date_field) if event_date_field else None
        parsed_event_date = _parse_date(raw_event_date)
        permit_number = source_fields.get(permit_number_field) if permit_number_field else None
        egrul_number = source_fields.get(egrul_field) if egrul_field else None
        applicant = source_fields.get(applicant_field) if applicant_field else None
        source = {
            "name": config["name"],
            "url": config["page_url"],
            "quality": "unknown",
            "record_mode": "registry_status",
            "observed_at": parsed_observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_updated_at": None,
            "rights_status": "cleared",
            "attribution": ATTRIBUTION,
            "source_id": config["source_id"],
            "source_record_id": source_record_id,
            "source_fields": source_fields,
            "source_rights": {
                "rights_status": "cleared",
                "access_basis": "Minstroy official open-data reuse terms",
                "permitted_storage": True,
                "permitted_display": True,
                "attribution_required": True,
                "no_distortion_required": True,
                "terms_url": REUSE_TERMS_URL,
            },
        }
        project_facts = {
            "project.name": _make_fact(
                project_name,
                unit=None,
                scope="project",
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=object_field,
            ),
            "project.applicant": _make_fact(
                applicant,
                unit=None,
                scope="project",
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=applicant_field,
            ),
            "registry.permit_number": _make_fact(
                permit_number,
                unit=None,
                scope="project",
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=permit_number_field,
            ),
            "registry.egrul_number": _make_fact(
                egrul_number,
                unit=None,
                scope="project",
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=egrul_field,
            ),
            "registry.permit_issue_date": _make_fact(
                parsed_event_date,
                unit="date",
                scope="project",
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=event_date_field,
            ),
        }
        for key, scope, unit in (
            ("project.address", "project", None),
            ("project.construction_start_date", "project", "date"),
            ("project.completion_date", "project", "date"),
            ("building.floor_count", "building", "floor"),
            ("building.area_sqm", "building", "m²"),
        ):
            project_facts[key] = _make_fact(
                None,
                unit=unit,
                scope=scope,
                observed_at=source["observed_at"],
                source_id=config["source_id"],
                source_record_id=source_record_id,
                source_field=None,
            )

        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "registry_event",
                "data_mode": "real",
                "source": source,
                "dataset_id": config["dataset_id"],
                "dataset_release_date": RELEASE_DATE,
                "record_subtype": config["event_type"],
                "registry_event": {
                    "serial_number": row_number,
                    "permit_number": permit_number,
                    "egrul_number": egrul_number,
                    "event_date": parsed_event_date,
                    "event_date_raw": raw_event_date,
                },
                "project_reference": {
                    "name": project_name,
                    "applicant": applicant,
                    "address": None,
                    "construction_start_date": None,
                    "completion_date": None,
                    "floor_count": None,
                    "area_sqm": None,
                    "price_rub": None,
                    "field_status": {
                        "address": "unknown",
                        "construction_start_date": "unknown",
                        "completion_date": "unknown",
                        "floor_count": "unknown",
                        "area_sqm": "unknown",
                        "price_rub": "unknown",
                    },
                },
                "facts": project_facts,
                "classification": {
                    "kind": "residential_name_candidate",
                    "method": "object name starts with explicit residential-building or residential-quarter wording",
                    "confidence": "source_text_only",
                    "current_project_status": "unknown",
                },
            }
        )
    return records


def find_moscow_housing_mentions(
    rows: Iterable[Mapping[str, Any]],
    *,
    dataset_kind: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Return audit-only source rows mentioning Moscow and housing terms.

    These are broad text matches, not residential projects. Callers must review
    the object's primary description before any project classification.
    """
    if dataset_kind not in DATASETS:
        raise ValueError(f"unsupported Minstroy dataset kind: {dataset_kind}")
    try:
        parsed_observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be ISO-8601 with timezone") from exc
    if parsed_observed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    config = DATASETS[dataset_kind]
    row_list = list(rows)
    headers = list(row_list[0]) if row_list else []
    object_field = _field(headers, "наименование объекта") if headers else None
    applicant_field = _field(
        headers, "наименование застройщика", "наименование заявителя"
    ) if headers else None
    row_number_field = _field(headers, "№ п/п", "номер п/п") if headers else None
    permit_field = _field(headers, "номер выданного разрешения") if headers else None
    event_date_field = _field(headers, "дата выдачи разрешения", "дата выданного разрешения") if headers else None
    matches: list[dict[str, Any]] = []
    for index, raw_row in enumerate(row_list, start=1):
        fields = {str(key).strip(): _clean(value) for key, value in raw_row.items()}
        object_name = fields.get(object_field) if object_field else None
        if not object_name or not MOSCOW_CITY.search(object_name) or not MOSCOW_HOUSING_WORD.search(object_name):
            continue
        row_number = fields.get(row_number_field) if row_number_field else None
        source_record_id = f"{config['dataset_id']}:{row_number or index}"
        is_residential_title = is_explicit_residential_name(object_name)
        matches.append(
            {
                "record_type": "registry_audit_text_match",
                "data_mode": "real",
                "source": {
                    "name": config["name"],
                    "url": config["page_url"],
                    "quality": "unknown",
                    "record_mode": "registry_status",
                    "observed_at": parsed_observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "source_updated_at": None,
                    "rights_status": "cleared",
                    "attribution": ATTRIBUTION,
                    "source_id": config["source_id"],
                    "source_record_id": source_record_id,
                    "source_fields": fields,
                    "source_rights": {
                        "rights_status": "cleared",
                        "access_basis": "Minstroy official open-data reuse terms",
                        "permitted_storage": True,
                        "permitted_display": True,
                        "attribution_required": True,
                        "no_distortion_required": True,
                        "terms_url": REUSE_TERMS_URL,
                    },
                },
                "dataset_id": config["dataset_id"],
                "record_subtype": config["event_type"],
                "dataset_release_date": RELEASE_DATE,
                "source_row_number": row_number,
                "project_name_source_text": object_name,
                "applicant_source_text": fields.get(applicant_field) if applicant_field else None,
                "permit_number_source_text": fields.get(permit_field) if permit_field else None,
                "event_date_source_text": fields.get(event_date_field) if event_date_field else None,
                "is_explicit_residential_title": is_residential_title,
                "classification": (
                    "residential_title_candidate"
                    if is_residential_title
                    else "exclude_non_residential_object_with_incidental_housing_reference"
                ),
                "classification_reason": (
                    "housing words occur in object-name prose; this record is not a residential-title match"
                    if not is_residential_title
                    else "audit hit requires manual project validation"
                ),
            }
        )
    return matches


def _workbook_rows(path: Path) -> list[dict[str, str | None]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise RuntimeError("reading Minstroy XLS requires optional dependency xlrd>=2.0") from exc
    workbook = xlrd.open_workbook(path, on_demand=True)
    sheet = workbook.sheet_by_index(0)
    header_row = None
    for index in range(sheet.nrows):
        values = [_clean(sheet.cell_value(index, col)) for col in range(sheet.ncols)]
        if any(value and value.casefold().startswith("наименование объекта") for value in values):
            header_row = index
            break
    if header_row is None:
        raise ValueError(f"could not locate object-name header in {path}")
    headers = [_clean(sheet.cell_value(header_row, col)) or "" for col in range(sheet.ncols)]
    if len([header for header in headers if header]) != len(set(header for header in headers if header)):
        raise ValueError(f"duplicate non-empty headers in {path}")
    output: list[dict[str, str | None]] = []
    for row_index in range(header_row + 1, sheet.nrows):
        row: dict[str, str | None] = {}
        has_value = False
        for col, header in enumerate(headers):
            if not header:
                continue
            value = _clean(sheet.cell_value(row_index, col))
            if value is not None:
                has_value = True
            row[header] = value
        if has_value:
            output.append(row)
    workbook.release_resources()
    return output


def parse_registry_workbook(
    path: Path | str,
    *,
    dataset_kind: str,
    observed_at: str,
    residential_only: bool = True,
) -> list[dict[str, Any]]:
    """Parse a local public XLS workbook into standalone registry-event records."""
    workbook_path = Path(path)
    return normalize_registry_rows(
        _workbook_rows(workbook_path),
        dataset_kind=dataset_kind,
        observed_at=observed_at,
        residential_only=residential_only,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dump_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _csv_row(record: Mapping[str, Any]) -> dict[str, str]:
    event = record["registry_event"]
    project = record["project_reference"]
    return {
        "record_type": str(record["record_type"]),
        "data_mode": str(record["data_mode"]),
        "record_mode": str(record["source"]["record_mode"]),
        "source_id": str(record["source"]["source_id"]),
        "source_record_id": str(record["source"]["source_record_id"]),
        "dataset_id": str(record["dataset_id"]),
        "dataset_release_date": str(record["dataset_release_date"]),
        "event_type": str(record["record_subtype"]),
        "serial_number": event["serial_number"] or "",
        "permit_number": event["permit_number"] or "",
        "egrul_number": event["egrul_number"] or "",
        "event_date": event["event_date"] or "",
        "project_name": project["name"] or "",
        "applicant": project["applicant"] or "",
        "address": "",
        "construction_start_date": "",
        "completion_date": "",
        "floor_count": "",
        "area_sqm": "",
        "price_rub": "",
        "unknown_fields": ",".join(UNKNOWN_PROJECT_FIELDS + ("price_rub",)),
        "source_fields_json": json.dumps(record["source"]["source_fields"], ensure_ascii=False, sort_keys=True),
    }


def _write_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        for record in records:
            writer.writerow(_csv_row(record))


def build_layer(
    *,
    permits_path: Path,
    commissioning_path: Path,
    output_dir: Path,
    observed_at: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = {
        "construction_permit": (permits_path, DATASETS["construction_permit"]),
        "commissioning_permit": (commissioning_path, DATASETS["commissioning_permit"]),
    }
    normalized: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    audit_datasets: dict[str, Any] = {}
    moscow_housing_mentions: list[dict[str, Any]] = []
    for kind, (path, config) in inputs.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        raw_rows = _workbook_rows(path)
        dataset_records = normalize_registry_rows(raw_rows, dataset_kind=kind, observed_at=observed_at)
        dataset_moscow_mentions = find_moscow_housing_mentions(
            raw_rows,
            dataset_kind=kind,
            observed_at=observed_at,
        )
        moscow_housing_mentions.extend(dataset_moscow_mentions)
        normalized.extend(dataset_records)
        residential_moscow = [
            record for record in dataset_records
            if MOSCOW_CITY.search(record["project_reference"]["name"] or "")
        ]
        covered_headers = sorted({key for row in raw_rows for key in row})
        selected_fields = {
            "serial_number": _field(covered_headers, "№ п/п", "номер п/п"),
            "project_name": _field(covered_headers, "наименование объекта"),
            "applicant": _field(covered_headers, "наименование застройщика", "наименование заявителя"),
            "event_date": _field(covered_headers, "дата выдачи разрешения", "дата выданного разрешения"),
            "permit_number": _field(covered_headers, "номер выданного разрешения"),
            "egrul_number": _field(covered_headers, "номер из ерул"),
        }
        field_nonempty_counts: dict[str, int] = {}
        for normalized_field, source_field in selected_fields.items():
            field_nonempty_counts[normalized_field] = sum(
                1 for row in raw_rows if source_field and _clean(row.get(source_field)) is not None
            )
        residential_source_rows = [record["source"]["source_fields"] for record in dataset_records]
        residential_nonempty_counts = {
            normalized_field: sum(
                1 for row in residential_source_rows
                if source_field and _clean(row.get(source_field)) is not None
            )
            for normalized_field, source_field in selected_fields.items()
        }
        audit_datasets[kind] = {
            "dataset_id": config["dataset_id"],
            "source_id": config["source_id"],
            "source_page_url": config["page_url"],
            "published_version_date": RELEASE_DATE,
            "published_label": PUBLISHED_LABEL,
            "source_download_url": config["download_url"],
            "http_observation": config["http_observation"],
            "raw_record_count": len(raw_rows),
            "strict_residential_name_record_count": len(dataset_records),
            "moscow_housing_term_name_mention_count": len(dataset_moscow_mentions),
            "moscow_housing_term_mentions_excluded_as_non_residential_title": sum(
                1 for record in dataset_moscow_mentions
                if not record["is_explicit_residential_title"]
            ),
            "residential_records_with_explicit_moscow_city_mention": len(residential_moscow),
            "residential_records_with_explicit_sochi_city_mention": sum(
                1 for record in dataset_records
                if "сочи" in (record["project_reference"]["name"] or "").casefold()
            ),
            "fields_in_workbook": covered_headers,
            "mapped_fields": selected_fields,
            "nonempty_record_counts_for_mapped_fields": field_nonempty_counts,
            "residential_subset_nonempty_record_counts_for_mapped_fields": residential_nonempty_counts,
        }
        manifests.append(
            {
                "dataset_id": config["dataset_id"],
                "source_id": config["source_id"],
                "name": config["name"],
                "source_page_url": config["page_url"],
                "download_url": config["download_url"],
                "local_raw_path": str(path.as_posix()),
                "published_version_date": RELEASE_DATE,
                "published_label": PUBLISHED_LABEL,
                "captured_at": observed_at,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "http_observation": config["http_observation"],
                "rights_status": "cleared",
                "attribution": ATTRIBUTION,
                "reuse_terms_url": REUSE_TERMS_URL,
                "reuse_terms_summary": LICENSE_TERMS,
            }
        )

    normalized.sort(key=lambda record: (record["dataset_id"], record["registry_event"]["serial_number"] or ""))
    jsonl_path = output_dir / "residential_registry.jsonl"
    csv_path = output_dir / "residential_registry.csv"
    _dump_jsonl(jsonl_path, normalized)
    _write_csv(csv_path, normalized)
    moscow_mentions_path = output_dir / "moscow_housing_mentions_audit.json"
    moscow_mentions_audit = {
        "schema_version": "minstroy-moscow-housing-text-audit/1",
        "captured_at": observed_at,
        "purpose": "Audit broad Moscow + housing-word matches; this file is not a residential registry or offer feed.",
        "records": moscow_housing_mentions,
    }
    moscow_mentions_path.write_text(
        json.dumps(moscow_mentions_audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    moscow_exclusion_count = sum(
        1 for record in moscow_housing_mentions
        if not record["is_explicit_residential_title"]
    )

    total_residential = len(normalized)
    moscow_residential = sum(
        1 for record in normalized
        if MOSCOW_CITY.search(record["project_reference"]["name"] or "")
    )
    audit = {
        "schema_version": "minstroy-open-registry-audit/1",
        "captured_at": observed_at,
        "published_version_date": RELEASE_DATE,
        "source_terms_url": REUSE_TERMS_URL,
        "rights_status": "cleared_with_required_attribution",
        "classification_rule": (
            "strict title-prefix match for многоквартирные жилые дома, жилой дом, "
            "жилые дома, or жилой квартал; incidental mention in infrastructure description is excluded"
        ),
        "counts": {
            "construction_permit_rows": audit_datasets["construction_permit"]["raw_record_count"],
            "commissioning_permit_rows": audit_datasets["commissioning_permit"]["raw_record_count"],
            "construction_residential_name_records": audit_datasets["construction_permit"]["strict_residential_name_record_count"],
            "commissioning_residential_name_records": audit_datasets["commissioning_permit"]["strict_residential_name_record_count"],
            "residential_name_records_total_not_unique_projects": total_residential,
            "moscow_residential_name_records_with_explicit_city_text": moscow_residential,
            "moscow_housing_term_name_mentions": len(moscow_housing_mentions),
            "moscow_non_residential_title_mentions_excluded": moscow_exclusion_count,
        },
        "interpretation": (
            "Rows are registry events, not unique buildings or live construction status. "
            "The strict residential-title scan found no Moscow residential record. "
            "The two broad Moscow-plus-housing-word hits are construction/commissioning events for a gas-pipeline relocation; "
            "their narrative mentions a future South Butovo residential microdistrict, so they are audit-only and excluded. "
            "The residential-name rows are historical Sochi housing records; no offers, prices, "
            "coordinates, addresses, floor counts, floor areas, starts, or completion dates are inferred."
        ),
        "datasets": audit_datasets,
        "normalized_outputs": {
            "jsonl": jsonl_path.name,
            "csv": csv_path.name,
            "record_count": total_residential,
            "moscow_housing_mentions_audit": moscow_mentions_path.name,
            "moscow_housing_mentions_audit_record_count": len(moscow_housing_mentions),
        },
        "raw_source_manifest": "source_manifest.json",
    }
    manifest = {
        "schema_version": "minstroy-open-source-manifest/1",
        "captured_at": observed_at,
        "reuse_terms_url": REUSE_TERMS_URL,
        "reuse_terms_summary": LICENSE_TERMS,
        "required_attribution": ATTRIBUTION,
        "landing_page_http_observation": {
            "checked_at": "2026-10-09T10:17:37Z",
            "construction_permit_page": {
                "url": DATASETS["construction_permit"]["page_url"],
                "status_code": 200,
                "redirects": 0,
                "host": "minstroyrf.gov.ru",
                "content_type": "text/html; charset=UTF-8",
                "content_length": 45796,
                "cache_control": "no-store, no-cache, must-revalidate",
                "etag": None,
                "last_modified": None,
            },
            "commissioning_permit_page": {
                "url": DATASETS["commissioning_permit"]["page_url"],
                "status_code": 200,
                "redirects": 0,
                "host": "minstroyrf.gov.ru",
                "content_type": "text/html; charset=UTF-8",
                "content_length": 45875,
                "cache_control": "no-store, no-cache, must-revalidate",
                "etag": None,
                "last_modified": None,
            },
            "suggested_scheduled_fetch": {
                "frequency": "monthly; source updates are monthly in the captured page history",
                "landing_page_max_bytes": 262144,
                "construction_xls_max_bytes": 4194304,
                "commissioning_xls_max_bytes": 2097152,
                "allow_redirects": False,
                "allowlist_host": "minstroyrf.gov.ru",
                "require_https": True,
            },
        },
        "datasets": manifests,
        "normalized_outputs": {
            "jsonl": {
                "path": jsonl_path.name,
                "bytes": jsonl_path.stat().st_size,
                "sha256": _sha256(jsonl_path),
            },
            "csv": {
                "path": csv_path.name,
                "bytes": csv_path.stat().st_size,
                "sha256": _sha256(csv_path),
            },
            "audit": {
                "path": "audit.json",
                "bytes": (output_dir / "audit.json").stat().st_size,
                "sha256": _sha256(output_dir / "audit.json"),
            },
            "moscow_housing_mentions_audit": {
                "path": moscow_mentions_path.name,
                "bytes": moscow_mentions_path.stat().st_size,
                "sha256": _sha256(moscow_mentions_path),
            },
        },
    }
    (output_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permits", type=Path, default=Path("data/property/minstroy/raw/permits_2026-05-29.xls"))
    parser.add_argument("--commissioning", type=Path, default=Path("data/property/minstroy/raw/commissioning_2026-05-29.xls"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/property/minstroy"))
    parser.add_argument("--observed-at", required=True, help="ISO-8601 capture timestamp with timezone")
    args = parser.parse_args(argv)
    try:
        audit = build_layer(
            permits_path=args.permits,
            commissioning_path=args.commissioning,
            output_dir=args.output_dir,
            observed_at=args.observed_at,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(audit["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
