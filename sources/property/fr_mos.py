"""Private FR renovation-map collector and source-backed CSV exporter.

This feed contains renovation-program map buildings, not individual apartment
prices or offers. Exported candidates are for address cross-checking only and
are never inserted into the product apartment map.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / ".runtime"
SNAPSHOT_ROOT = RUNTIME_ROOT / "research" / "fr-mos-20261010"
DEFAULT_OUTPUT_ROOT = SNAPSHOT_ROOT / "runs"
DEFAULT_MAP_JSON = SNAPSHOT_ROOT / "map-data-64m.body"
DEFAULT_FILTER_JSON = SNAPSHOT_ROOT / "map-filters.body"
DEFAULT_MAP_HEADERS = SNAPSHOT_ROOT / "map-data-64m.headers"
DEFAULT_FILTER_HEADERS = SNAPSHOT_ROOT / "map-filters.headers"

SOURCE_HOST = "https://fr.mos.ru"
MAP_URL = (
    "https://fr.mos.ru/pokupka-nedvizhimosti-dlya-vseh/ajax.php"
    "?category[]=ALL&map=ren"
)
FILTER_URL = (
    "https://fr.mos.ru/pokupka-nedvizhimosti-dlya-vseh/ajax.php?cmd=filters"
)
SERVER_IP = "212.11.155.166"
VERIFIED_INTERFACE = "enp10s0"
VERIFIED_SOURCE_IP = "192.168.1.111"
VERIFIED_GATEWAY = "192.168.1.1"
PROJECT_DETAIL_PREFIX = "/uchastnikam-programmy/karta-renovatsii/"
NEW_BUILD_STATUSES = {"PROCESSING", "FINISHED"}
STATUS_LABELS = {
    "OLD": "Дом, включённый в Программу реновации",
    "CONFIRMED": "Стартовая площадка",
    "START": "Стартовая площадка",
    "KVARTAL": "Объекты квартальной застройки",
    "PROCESSING": "Строится",
    "FINISHED": "Введён в эксплуатацию",
}
UNKNOWN_DEVELOPER_MARKERS = {"", "-", "—"}
DATASET_SCOPE = "renovation-map registry, not unit prices"
MAX_BODY_BYTES = 64 * 1024 * 1024
CSV_FIELDS = (
    "dataset_scope",
    "source_record_id",
    "source_project_code",
    "address_raw",
    "develop_address_raw",
    "latitude",
    "longitude",
    "source_status_code",
    "source_status_label",
    "source_flats_raw",
    "developer_raw",
    "county_id",
    "county_label",
    "district_id",
    "district_label",
    "source_map_url",
    "source_detail_url",
    "image_url",
)


class CollectionError(RuntimeError):
    """A source or transport check failed; no fallback is attempted."""

    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response


class SourceDenied(CollectionError):
    """The source returned an access-denial status; stop without retrying."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".tmp-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def parse_headers(path: Path) -> dict[str, str | None]:
    """Extract only safe provenance fields; ignore cookies and auth headers."""
    status: str | None = None
    response_date: str | None = None
    content_type: str | None = None
    content_length: str | None = None
    for line in path.read_text(encoding="latin-1").splitlines():
        match = re.match(r"^HTTP/\S+\s+(\d{3})", line.strip(), flags=re.I)
        if match:
            status = match.group(1)
        elif ":" in line:
            key, value = line.split(":", 1)
            key = key.lower()
            if key == "date":
                response_date = value.strip()
            elif key == "content-type":
                content_type = value.strip()
            elif key == "content-length":
                content_length = value.strip()
    return {
        "http_status": status,
        "http_response_date": response_date,
        "content_type": content_type,
        "content_length": content_length,
    }


def validate_json_capture(path: Path, headers_path: Path, *, label: str) -> dict[str, Any]:
    metadata = parse_headers(headers_path)
    if metadata["http_status"] != "200":
        status = metadata["http_status"]
        if status in {"401", "403", "429"}:
            raise SourceDenied(f"{label} source returned HTTP {status}; stopping without retry")
        raise CollectionError(f"{label} response status is {status!r}, expected 200")
    content_type = metadata["content_type"] or ""
    if "json" not in content_type.lower():
        raise CollectionError(f"{label} response content type is not JSON")
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"{label} response is not complete valid JSON") from exc
    return {"payload": payload, **metadata}


def parse_positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def parse_coordinates(value: Any) -> tuple[float | None, float | None]:
    """The captured source JS maps coords[0] to latitude and coords[1] to longitude."""
    if not isinstance(value, list) or len(value) != 2:
        return None, None
    try:
        latitude, longitude = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None, None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None
    return latitude, longitude


def build_filter_labels(filters: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    counties = filters.get("county", {})
    county_labels: dict[str, str] = {}
    district_labels: dict[str, str] = {}
    if not isinstance(counties, dict):
        return county_labels, district_labels
    for county_id, county in counties.items():
        if not isinstance(county, dict):
            continue
        if county.get("name"):
            county_labels[str(county_id)] = str(county["name"])
        districts = county.get("district", {})
        if isinstance(districts, dict):
            for district_id, district in districts.items():
                if isinstance(district, dict) and district.get("name"):
                    district_labels[f"{county_id}/{district_id}"] = str(district["name"])
    return county_labels, district_labels


def project_item(
    item: dict[str, Any],
    *,
    county_labels: dict[str, str],
    district_labels: dict[str, str],
    response_date: str | None,
) -> dict[str, Any]:
    status = str(item.get("status_code") or "").upper()
    source_flats = item.get("flats")
    flat_count = parse_positive_integer(source_flats)
    latitude, longitude = parse_coordinates(item.get("coords"))
    source_id = str(item.get("id")) if item.get("id") is not None else None
    project_code = str(item.get("code")) if item.get("code") else None
    raw_developer = item.get("developer")
    developer_text = str(raw_developer).strip() if raw_developer is not None else ""
    developer_claim = (
        developer_text
        if developer_text.casefold() not in UNKNOWN_DEVELOPER_MARKERS
        else None
    )
    county_id = str(item.get("county")) if item.get("county") is not None else None
    district_id = str(item.get("district")) if item.get("district") is not None else None
    is_candidate = status in NEW_BUILD_STATUSES and flat_count is not None

    detail_url = None
    if project_code and status not in {"OLD", "CONFIRMED", "START", "KVARTAL"}:
        # Captured homepage globals were pageType=PT and ajaxType=for_sell.
        detail_url = (
            f"{SOURCE_HOST}{PROJECT_DETAIL_PREFIX}{quote(project_code, safe='')}/"
            f"?open_sale=0&object={quote(source_id or '', safe='')}&type=for_sell"
        )

    image_path = item.get("img")
    image_url = (
        f"{SOURCE_HOST}{image_path}"
        if isinstance(image_path, str) and image_path.startswith("/")
        else None
    )
    return {
        "provider": "fr_mos_renovation_map",
        "publisher_host": "fr.mos.ru",
        "dataset_scope": DATASET_SCOPE,
        "source_record_kind": "renovation_map_building",
        "source_record_id": source_id,
        "source_project_code": project_code,
        "source_map_url": MAP_URL,
        "source_detail_url": detail_url,
        "address_raw": item.get("name"),
        "develop_address_raw": item.get("develop_address"),
        "source_status_raw": item.get("status_code"),
        "source_status_code": status or None,
        "source_status_label": STATUS_LABELS.get(status),
        "source_flats_raw": source_flats,
        "source_positive_flats_count": flat_count,
        "address_crosscheck_candidate": is_candidate,
        "candidate_basis": (
            "PROCESSING/FINISHED status plus positive source flats field"
            if is_candidate
            else None
        ),
        "source_coords_raw": item.get("coords"),
        "latitude": latitude,
        "longitude": longitude,
        "county_id": county_id,
        "county_label": county_labels.get(county_id or ""),
        "district_id": district_id,
        "district_label": district_labels.get(f"{county_id}/{district_id}"),
        "developer_raw": raw_developer,
        "developer_source_claim": developer_claim,
        "developer_assessment": "source_assertion_unverified" if developer_claim else "unknown",
        "corpus_id": None,
        "corpus_assessment": "not_present_as_a_field_in_map_item",
        "floors_raw": item.get("floors"),
        "commissioning_text_raw": item.get("vvod"),
        "image_path_raw": image_path,
        "image_url": image_url,
        "image_role": "map_card_preview" if image_url else None,
        "source_geometry_raw": item.get("polygon"),
        "source_geometry_assessment": (
            "source_map_polygon_unverified_footprint" if item.get("polygon") else "not_present"
        ),
        "price": None,
        "availability": None,
        "unit_listing_present_in_map_payload": False,
        "http_response_date": response_date,
        "source_updated_at": None,
        "source_fields": item,
    }


def project_payloads(
    map_payload: dict[str, Any],
    filter_payload: dict[str, Any],
    *,
    response_date: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    objects = map_payload.get("objects")
    items = objects.get("items") if isinstance(objects, dict) else None
    filters = filter_payload.get("filters")
    if not isinstance(items, list) or not isinstance(filters, dict):
        raise ValueError("expected map objects.items and filter filters objects")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("map items must all be objects")
    source_ids = [str(item.get("id")) for item in items if item.get("id") is not None]
    if len(source_ids) != len(items):
        raise ValueError("source map item is missing its source id")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("duplicate source map IDs")

    county_labels, district_labels = build_filter_labels(filters)
    records = [
        project_item(
            item,
            county_labels=county_labels,
            district_labels=district_labels,
            response_date=response_date,
        )
        for item in items
    ]
    candidates = [record for record in records if record["address_crosscheck_candidate"]]
    statuses = Counter(record["source_status_code"] or "UNKNOWN" for record in records)
    candidate_statuses = Counter(record["source_status_code"] or "UNKNOWN" for record in candidates)
    manifest = {
        "schema_version": "fr-mos-renovation-map/1",
        "provider": "fr_mos_renovation_map",
        "dataset_scope": DATASET_SCOPE,
        "source_role": (
            "public renovation-program map cross-check; not apartment unit inventory"
        ),
        "source_map_url": MAP_URL,
        "source_filter_url": FILTER_URL,
        "source_record_count": len(records),
        "source_status_counts": dict(sorted(statuses.items())),
        "crosscheck_candidate_count": len(candidates),
        "candidate_status_counts": dict(sorted(candidate_statuses.items())),
        "candidate_rule": (
            "source status in PROCESSING/FINISHED and positive integer source flats field"
        ),
        "unit_prices_or_availability_present": False,
        "corpus_field_present": False,
        "item_category_field_present": any(
            "type" in item or "category" in item for item in items
        ),
        "developer_values_are_unverified_source_assertions": True,
        "source_updated_at_field_present": False,
        "http_response_date": response_date,
        "publisher_category_enum": filters.get("type"),
        "warning": (
            "Map registry rows are not unit prices/offers and are not automatically "
            "added to the apartment map. The source item has no corpus or "
            "type/category field; developer and map geometry are unverified source claims."
        ),
    }
    return records, manifest


def csv_cell(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        if len(value) > 32000:
            raise ValueError("CSV value exceeds safe cell length")
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json(row) + b"\n" for row in rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
            writer.writerow(CSV_FIELDS)
            for row in rows:
                writer.writerow([csv_cell(row.get(key)) for key in CSV_FIELDS])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    raw = path.read_bytes()
    return {"path": path.name, "records": len(rows), "bytes": len(raw), "sha256": sha256_bytes(raw)}


def write_processed(
    processed_dir: Path,
    source_items: list[dict[str, Any]],
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    processed_dir.mkdir(parents=True, exist_ok=False)
    candidates = [record for record in records if record["address_crosscheck_candidate"]]
    outputs = {
        "source_rows": atomic_write_and_hash(
            processed_dir / "source-rows.jsonl", jsonl_bytes(source_items)
        ),
        "source_records": atomic_write_and_hash(
            processed_dir / "source-records.jsonl", jsonl_bytes(records)
        ),
        "candidate_records": atomic_write_and_hash(
            processed_dir / "crosscheck-candidates.jsonl", jsonl_bytes(candidates)
        ),
        "source_csv": write_csv(processed_dir / "source-records.csv", records),
        "candidate_csv": write_csv(processed_dir / "crosscheck-candidates.csv", candidates),
    }
    result = {
        **manifest,
        "outputs": outputs,
        "csv_format": "UTF-8 BOM, semicolon delimiter, CRLF, spreadsheet formula escaped",
        "raw_source_rows_file": "source-rows.jsonl",
    }
    atomic_write(processed_dir / "manifest.json", canonical_json(result) + b"\n")
    return result


def atomic_write_and_hash(path: Path, raw: bytes) -> dict[str, Any]:
    atomic_write(path, raw)
    return {
        "path": path.name,
        "records": raw.count(b"\n"),
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
    }


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def ensure_runtime_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    runtime = RUNTIME_ROOT.resolve()
    if not resolved.is_relative_to(runtime):
        raise ValueError("output paths must remain under the repository .runtime directory")
    return resolved


def make_run_dir(output_root: Path) -> tuple[str, Path]:
    root = ensure_runtime_path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        run_id = new_run_id()
        run_dir = root / run_id
        try:
            run_dir.mkdir()
            return run_id, run_dir
        except FileExistsError:
            continue
    raise CollectionError("could not allocate a unique versioned run directory")


def runtime_manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def read_capture_pair(
    map_path: Path,
    filter_path: Path,
    map_headers: Path,
    filter_headers: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    map_capture = validate_json_capture(map_path, map_headers, label="map")
    filter_capture = validate_json_capture(filter_path, filter_headers, label="filters")
    if (
        not isinstance(map_capture["payload"], dict)
        or not isinstance(filter_capture["payload"], dict)
    ):
        raise CollectionError("FR responses must be JSON objects")
    source_meta = {
        "map": capture_file_metadata(map_path, map_headers, map_capture, url=MAP_URL),
        "filters": capture_file_metadata(
            filter_path, filter_headers, filter_capture, url=FILTER_URL
        ),
    }
    return map_capture["payload"], filter_capture["payload"], source_meta


def capture_file_metadata(
    body_path: Path,
    headers_path: Path,
    capture: dict[str, Any],
    *,
    url: str,
) -> dict[str, Any]:
    return {
        "url": url,
        "body_path": str(body_path.resolve()),
        "body_bytes": body_path.stat().st_size,
        "body_sha256": sha256_file(body_path),
        "headers_path": str(headers_path.resolve()),
        "headers_sha256": sha256_file(headers_path),
        "http_status": capture["http_status"],
        "http_response_date": capture["http_response_date"],
        "content_type": capture["content_type"],
        "source_updated_at": None,
    }


def versioned_offline_run(
    *,
    map_path: Path,
    filter_path: Path,
    map_headers: Path,
    filter_headers: Path,
    output_root: Path,
) -> dict[str, Any]:
    map_payload, filter_payload, source_meta = read_capture_pair(
        map_path, filter_path, map_headers, filter_headers
    )
    map_date = source_meta["map"]["http_response_date"]
    records, projection_manifest = project_payloads(
        map_payload, filter_payload, response_date=map_date
    )
    run_id, run_dir = make_run_dir(output_root)
    run_dir_manifest = {
        "run_id": run_id,
        "collection_mode": "offline_reparse",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_captures": source_meta,
        "network_requests_made": False,
        **projection_manifest,
    }
    processed = write_processed(
        run_dir / "processed",
        map_payload["objects"]["items"],
        records,
        run_dir_manifest,
    )
    run_manifest = {
        "run_id": run_id,
        "schema_version": "fr-mos-run/1",
        "collection_mode": "offline_reparse",
        "created_at": run_dir_manifest["created_at"],
        "network_requests_made": False,
        "processed_manifest_path": str(run_dir / "processed" / "manifest.json"),
        "processed_manifest_sha256": sha256_file(run_dir / "processed" / "manifest.json"),
        "source_captures": source_meta,
        "source_record_count": processed["source_record_count"],
        "crosscheck_candidate_count": processed["crosscheck_candidate_count"],
    }
    atomic_write(runtime_manifest_path(run_dir), canonical_json(run_manifest) + b"\n")
    return run_manifest


def route_fields(output: str) -> dict[str, str | None]:
    first_line = output.splitlines()[0] if output.splitlines() else ""

    def token(name: str) -> str | None:
        match = re.search(rf"\b{name}\s+(\S+)", first_line)
        return match.group(1) if match else None

    return {
        "device": token("dev"),
        "source_ip": token("src"),
        "gateway": token("via"),
        "route_line": first_line,
    }


def build_route_probe_command(interface: str) -> list[str]:
    if interface != VERIFIED_INTERFACE:
        raise CollectionError(f"live collection is pinned to {VERIFIED_INTERFACE}")
    return ["ip", "-4", "route", "get", SERVER_IP, "oif", interface]


def validate_direct_route(interface: str, output: str) -> dict[str, str | None]:
    route = route_fields(output)
    expected = {
        "device": VERIFIED_INTERFACE,
        "source_ip": VERIFIED_SOURCE_IP,
        "gateway": VERIFIED_GATEWAY,
    }
    if interface != VERIFIED_INTERFACE or any(
        route.get(key) != value for key, value in expected.items()
    ):
        raise CollectionError(
            "direct-route proof did not match the verified physical path; no request was made"
        )
    return {
        **route,
        "probe_command": f"ip -4 route get {SERVER_IP} oif {interface}",
        "target_ip": SERVER_IP,
        "verified": "true",
    }


def verify_direct_route(interface: str) -> dict[str, str | None]:
    if interface != VERIFIED_INTERFACE:
        raise CollectionError(f"live collection is pinned to {VERIFIED_INTERFACE}")
    result = subprocess.run(
        build_route_probe_command(interface),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CollectionError("could not verify the direct physical route; no request was made")
    return validate_direct_route(interface, result.stdout)


def redact_response_headers(raw: bytes) -> bytes:
    sensitive = {"set-cookie", "set-cookie2", "authorization", "proxy-authorization"}
    kept: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        key, separator, _value = line.partition(b":")
        if separator and key.decode("latin-1").strip().lower() in sensitive:
            continue
        kept.append(line)
    return b"".join(kept)


def build_curl_command(
    url: str,
    *,
    interface: str,
    headers_path: Path,
    body_path: Path,
) -> list[str]:
    if interface != VERIFIED_INTERFACE:
        raise CollectionError(f"live collection is pinned to {VERIFIED_INTERFACE}")
    temporary_headers = headers_path.with_suffix(headers_path.suffix + ".tmp-raw")
    writeout = (
        "http_code=%{http_code}\nsize_download=%{size_download}\n"
        "remote_ip=%{remote_ip}\nlocal_ip=%{local_ip}\n"
        "url_effective=%{url_effective}\n"
    )
    return [
        "/usr/bin/curl",
        "--ipv4",
        "--interface",
        f"if!{interface}",
        "--noproxy",
        "*",
        "--resolve",
        f"fr.mos.ru:443:{SERVER_IP}",
        "--connect-timeout",
        "12",
        "--max-time",
        "120",
        "--max-filesize",
        str(MAX_BODY_BYTES),
        "--globoff",
        "--silent",
        "--show-error",
        "--dump-header",
        str(temporary_headers),
        "--output",
        str(body_path),
        "--write-out",
        writeout,
        url,
    ]


def parse_curl_output(output: str) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return {
        "http_status": values.get("http_code"),
        "size_download": values.get("size_download"),
        "remote_ip": values.get("remote_ip"),
        "local_ip": values.get("local_ip"),
        "url_effective": values.get("url_effective"),
    }


def fetch_endpoint(
    url: str,
    *,
    interface: str,
    noproxy: bool,
    raw_dir: Path,
    request_name: str,
    route_evidence: dict[str, str | None],
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if not noproxy:
        raise CollectionError("live collection requires --noproxy; no request was made")
    if (
        route_evidence.get("verified") != "true"
        or route_evidence.get("device") != VERIFIED_INTERFACE
        or route_evidence.get("source_ip") != VERIFIED_SOURCE_IP
        or route_evidence.get("gateway") != VERIFIED_GATEWAY
        or route_evidence.get("target_ip") != SERVER_IP
    ):
        raise CollectionError("verified physical route evidence is required; no request was made")
    raw_dir.mkdir(parents=True, exist_ok=True)
    body_path = raw_dir / f"{request_name}.body"
    headers_path = raw_dir / f"{request_name}.headers"
    command = build_curl_command(
        url,
        interface=interface,
        headers_path=headers_path,
        body_path=body_path,
    )
    temporary_headers = headers_path.with_suffix(headers_path.suffix + ".tmp-raw")
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=125,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError(f"request failed without retry: {exc.__class__.__name__}") from exc
    finally:
        if temporary_headers.exists():
            atomic_write(headers_path, redact_response_headers(temporary_headers.read_bytes()))
            temporary_headers.unlink(missing_ok=True)

    response = parse_curl_output(result.stdout or "")
    response.update(
        {
            "url": url,
            "request_name": request_name,
            "curl_exit_code": result.returncode,
            "body_path": str(body_path),
            "body_bytes": body_path.stat().st_size if body_path.exists() else 0,
            "body_sha256": sha256_file(body_path) if body_path.exists() else None,
            "headers_path": str(headers_path),
            "headers_sha256": sha256_file(headers_path) if headers_path.exists() else None,
            "headers": parse_headers(headers_path) if headers_path.exists() else {},
        }
    )
    status = response.get("http_status")
    if status in {"401", "403", "429"}:
        raise SourceDenied(
            f"source returned HTTP {status}; stopped without retry or route change",
            response,
        )
    if result.returncode != 0:
        raise CollectionError(
            f"curl failed with exit {result.returncode}; no retry was attempted",
            response,
        )
    if status != "200":
        raise CollectionError(f"source returned HTTP {status!r}, expected 200", response)
    if response.get("remote_ip") != SERVER_IP:
        raise CollectionError("source resolved to a different IP; stopped without retry", response)
    if response.get("local_ip") != VERIFIED_SOURCE_IP:
        raise CollectionError("curl used a different local source IP; stopped without retry", response)
    if response.get("url_effective") != url:
        raise CollectionError("source redirected; redirects are not followed", response)
    content_type = (response.get("headers") or {}).get("content_type") or ""
    if "json" not in content_type.lower():
        raise CollectionError("source response was not JSON; stopped without retry", response)
    return response


def write_run_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write(run_dir / "manifest.json", canonical_json(manifest) + b"\n")


def versioned_live_run(
    *,
    interface: str,
    noproxy: bool,
    output_root: Path,
) -> dict[str, Any]:
    run_id, run_dir = make_run_dir(output_root)
    raw_dir = run_dir / "raw"
    started = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "schema_version": "fr-mos-run/1",
        "collection_mode": "live_direct_physical",
        "collection_status": "started",
        "created_at": started,
        "network_requests_made": False,
        "source_map_url": MAP_URL,
        "source_filter_url": FILTER_URL,
        "safety": {
            "interface_requested": interface,
            "noproxy_required": True,
            "proxy_bypassed": True,
            "pinned_server_ip": SERVER_IP,
            "redirects_followed": False,
            "retries": 0,
            "denial_policy": (
                "stop on 401/403/429 without retry, proxy, alternate IP, or route change"
            ),
            "cookie_request_header_sent": False,
            "set_cookie_response_headers_saved": False,
        },
        "requests": [],
    }
    write_run_manifest(run_dir, manifest)
    try:
        route = verify_direct_route(interface)
    except CollectionError as exc:
        manifest["collection_status"] = "stopped_route_unverified"
        manifest["route_error"] = str(exc)
        write_run_manifest(run_dir, manifest)
        raise
    manifest["route_evidence"] = route
    manifest["network_requests_made"] = True
    write_run_manifest(run_dir, manifest)

    responses: dict[str, dict[str, Any]] = {}
    for request_name, url in (("filters", FILTER_URL), ("map", MAP_URL)):
        try:
            response = fetch_endpoint(
                url,
                interface=interface,
                noproxy=noproxy,
                raw_dir=raw_dir,
                request_name=request_name,
                route_evidence=route,
            )
        except CollectionError as exc:
            response = getattr(exc, "response", None)
            if response:
                manifest["requests"].append(response)
            manifest["collection_status"] = (
                "stopped_source_denial" if isinstance(exc, SourceDenied) else "incomplete_transport"
            )
            manifest["failure"] = str(exc)
            write_run_manifest(run_dir, manifest)
            raise
        responses[request_name] = response
        manifest["requests"].append(response)
        write_run_manifest(run_dir, manifest)

    map_path = Path(responses["map"]["body_path"])
    filters_path = Path(responses["filters"]["body_path"])
    map_payload = read_json(map_path)
    filter_payload = read_json(filters_path)
    records, projection = project_payloads(
        map_payload,
        filter_payload,
        response_date=(responses["map"].get("headers") or {}).get("http_response_date"),
    )
    processed = write_processed(
        run_dir / "processed",
        map_payload["objects"]["items"],
        records,
        {
            **projection,
            "run_id": run_id,
            "collection_mode": "live_direct_physical",
            "created_at": started,
            "source_captures": responses,
            "route_evidence": route,
            "network_requests_made": True,
        },
    )
    manifest["collection_status"] = "complete"
    manifest["source_record_count"] = processed["source_record_count"]
    manifest["crosscheck_candidate_count"] = processed["crosscheck_candidate_count"]
    manifest["processed_manifest_path"] = str(run_dir / "processed" / "manifest.json")
    manifest["processed_manifest_sha256"] = sha256_file(
        run_dir / "processed" / "manifest.json"
    )
    write_run_manifest(run_dir, manifest)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="fetch only the two confirmed public endpoints"
    )
    parser.add_argument(
        "--interface", default=None, help="live mode only; must be the verified enp10s0"
    )
    parser.add_argument(
        "--noproxy", action="store_true", help="required in live mode; disable all proxies"
    )
    parser.add_argument("--map-json", type=Path, default=DEFAULT_MAP_JSON)
    parser.add_argument("--filter-json", type=Path, default=DEFAULT_FILTER_JSON)
    parser.add_argument("--map-headers", type=Path, default=DEFAULT_MAP_HEADERS)
    parser.add_argument("--filter-headers", type=Path, default=DEFAULT_FILTER_HEADERS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)
    try:
        ensure_runtime_path(args.output_root)
    except ValueError as exc:
        parser.error(str(exc))
    if args.live and (args.interface is None or not args.noproxy):
        parser.error("--live requires both --interface enp10s0 and --noproxy")
    if args.live and args.interface != VERIFIED_INTERFACE:
        parser.error(f"--live is pinned to --interface {VERIFIED_INTERFACE}")
    if not args.live and (args.interface is not None or args.noproxy):
        parser.error("--interface and --noproxy are only valid with --live")
    if not args.live:
        try:
            for path in (args.map_json, args.filter_json, args.map_headers, args.filter_headers):
                ensure_runtime_path(path)
        except ValueError as exc:
            parser.error(f"offline capture inputs must remain under .runtime: {exc}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.live:
            result = versioned_live_run(
                interface=args.interface,
                noproxy=args.noproxy,
                output_root=args.output_root,
            )
        else:
            result = versioned_offline_run(
                map_path=args.map_json,
                filter_path=args.filter_json,
                map_headers=args.map_headers,
                filter_headers=args.filter_headers,
                output_root=args.output_root,
            )
    except (CollectionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"fr_mos: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
