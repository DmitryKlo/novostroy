"""Bounded anonymous continuation of two source-linked Etalon flat catalogs.

The source page and its linked frontend bundle expose the exact API route and
pagination shape. Captures remain private research data; this tool never sends
Authorization, cookies, or media requests.
"""

from __future__ import annotations

import argparse
import email.utils
import fcntl
import json
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from sources.property.developer_harvest import (
    BLOCK_STATUSES,
    MAX_HTML_BYTES,
    append_event,
    atomic_bytes,
    canonical_json,
    checked_url,
    curl_fetch,
    digest,
    utc_now,
)
from sources.property.etalongroup_catalog_audit import (
    DEFAULT_OUTPUT as DEFAULT_BASE_OUTPUT,
    SOURCE_URL_HOSTS,
    UNIT_FIELDS,
    _deduplicate,
    _unit_row,
    _write_csv,
    _write_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
API_HOST = "newsite.etalongroup.ru"
API_BASE = f"https://{API_HOST}/api/filter/msk/flat/list/"
PROJECT_IDS = {124: "Мариин Парк", 125: "Соколин Парк"}
DEFAULT_SOURCE_CAPTURE_DIR = (
    ROOT / ".runtime/research/developer-harvest/supplemental-etalon-discovery"
)
DEFAULT_API_CAPTURE_DIR = (
    ROOT / ".runtime/research/developer-harvest/supplemental-etalon-api-completion-20261010"
)
DEFAULT_FINAL_DIR = (
    ROOT / ".runtime/research/developer-harvest/supplemental-etalon-api-final-20261010"
)
MAX_API_REQUESTS = 8
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 20


def api_url(project_id: int, pagination: dict[str, Any]) -> str:
    if project_id not in PROJECT_IDS:
        raise ValueError("API project ID is outside the two linked catalog targets")
    if pagination.get("object") != project_id:
        raise ValueError("pagination object does not match its project")
    if not isinstance(pagination.get("page"), int) or pagination["page"] < 1:
        raise ValueError("pagination page is invalid")
    if not isinstance(pagination.get("offset"), int) or pagination["offset"] < 0:
        raise ValueError("pagination offset is invalid")
    if not isinstance(pagination.get("limit"), int) or pagination["limit"] < 1:
        raise ValueError("pagination limit is invalid")
    # This field order and shape follow the source-linked frontend call.
    query = urlencode([
        ("object[]", str(project_id)),
        ("pagination", json.dumps(pagination, ensure_ascii=False, separators=(",", ":"))),
        ("getAuctionSlider", "false"),
    ])
    return checked_url(f"{API_BASE}?{query}", {API_HOST})


def parse_api_body(body: bytes, *, project_id: int, project_name: str,
                   base_row: dict[str, Any], page_url: str, observed_at: str,
                   body_sha256: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not body or len(body) > MAX_HTML_BYTES:
        raise ValueError("API JSON body is empty or exceeds the capture byte limit")
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("API response is not a successful source payload")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("itemList"), list):
        raise ValueError("API response lacks data.itemList")
    pagination = data.get("pagination")
    if not isinstance(pagination, dict) or pagination.get("object") != project_id:
        raise ValueError("API response pagination is missing or names another project")
    if not isinstance(pagination.get("haveItem"), bool):
        raise ValueError("API response pagination lacks boolean haveItem")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for flat in data["itemList"]:
        if not isinstance(flat, dict):
            raise ValueError("API itemList contains a non-object row")
        if flat.get("objectTitle") not in (None, project_name):
            raise ValueError("API row project title differs from linked project")
        unit_id = flat.get("id")
        if not isinstance(unit_id, int) or isinstance(unit_id, bool):
            raise ValueError("API unit row has no integer source ID")
        if unit_id in seen:
            raise ValueError("duplicate source unit ID within one API response")
        seen.add(unit_id)
        rows.append(_unit_row(
            flat, project_id=project_id, project_name=project_name,
            city_label=base_row.get("project_city_label"),
            address_label=base_row.get("project_address_label"),
            district_label=base_row.get("project_district_label"),
            page_url=page_url, observed_at=observed_at, body_sha256=body_sha256,
        ))
    if not rows and pagination["haveItem"]:
        raise ValueError("API returned an empty page while claiming more items")
    return rows, {
        "source_project_id": project_id,
        "project_name": project_name,
        "request_url": page_url,
        "body_sha256": body_sha256,
        "observed_at": observed_at,
        "item_rows": len(rows),
        "first_unit_id": rows[0]["source_unit_id"] if rows else None,
        "last_unit_id": rows[-1]["source_unit_id"] if rows else None,
        "returned_pagination": pagination,
    }


def _next_pagination(current: dict[str, Any], returned: dict[str, Any],
                     project_id: int) -> dict[str, Any] | None:
    if returned.get("object") != project_id:
        raise ValueError("returned pagination changed project identity")
    if not returned.get("haveItem"):
        return None
    for field in ("page", "offset", "limit"):
        if not isinstance(returned.get(field), int):
            raise ValueError(f"returned pagination lacks integer {field}")
    if returned["page"] <= current["page"] or returned["offset"] <= current["offset"]:
        raise ValueError("returned pagination does not advance")
    if returned["limit"] < 1:
        raise ValueError("returned pagination limit is invalid")
    # Preserve only the documented pagination keys; no local page-size guess.
    return {key: returned[key] for key in ("haveItem", "page", "object", "offset", "limit")}


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"date", "content-type", "etag", "last-modified", "server"}
    return {key.lower(): value[:300] for key, value in headers.items()
            if key.lower() in allowed}


def _header(headers: dict[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items()
                 if key.lower() == name.lower()), None)


def _parse_root_headers(path: Path) -> tuple[int | None, dict[str, str]]:
    status: int | None = None
    headers: dict[str, str] = {}
    for line in path.read_text(encoding="iso-8859-1").splitlines():
        if line.startswith("HTTP/"):
            parts = line.split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        elif ":" in line:
            key, value = line.split(":", 1)
            if key.lower() in {"date", "content-type", "etag", "last-modified", "server"}:
                headers[key] = value.strip()
    return status, _safe_headers(headers)


def _response_observed_at(headers: dict[str, str], fallback: str) -> str:
    value = next((v for k, v in headers.items() if k.lower() == "date"), None)
    if not value:
        return fallback
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return fallback


def _load_ledger(path: Path) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        url = event.get("url")
        if not isinstance(url, str):
            raise ValueError("API request ledger contains an invalid URL")
        if url in values and values[url] != event:
            raise ValueError("API request ledger has conflicting duplicate URL entries")
        values[url] = event
    return values


def _store_success(url: str, body: bytes, headers: dict[str, str], *,
                   event_path: Path, raw_dir: Path, source: str,
                   pagination: dict[str, Any]) -> dict[str, Any]:
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("API response exceeds capture byte limit")
    sha = digest(body)
    raw_dir.mkdir(parents=True, exist_ok=True)
    atomic_bytes(raw_dir / f"{sha}.body", body)
    event = {
        "at": utc_now(), "url": url, "status": 200, "error": None,
        "blocked": False, "body_bytes": len(body), "body_sha256": sha,
        "headers": _safe_headers(headers), "source": source,
        "request_pagination": pagination,
        "authorization_sent": False,
    }
    append_event(event_path, event)
    return event


def import_root_capture(*, capture_dir: Path, source_capture_dir: Path,
                        first_pagination: dict[str, Any]) -> dict[str, Any]:
    """Add the already-obtained anonymous project-124 response without refetching."""
    body_path = source_capture_dir / "root-api-anonymous.json"
    headers_path = source_capture_dir / "root-api-anonymous.headers"
    if not body_path.is_file() or not headers_path.is_file():
        raise ValueError("preserved anonymous response for project 124 is missing")
    status, headers = _parse_root_headers(headers_path)
    if status != 200:
        raise ValueError("preserved anonymous response is not HTTP 200")
    body = body_path.read_bytes()
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("preserved anonymous API response exceeds capture byte limit")
    url = api_url(124, first_pagination)
    raw_dir = capture_dir / "raw"
    ledger_path = capture_dir / "requests.jsonl"
    events = _load_ledger(ledger_path)
    sha = digest(body)
    prior = events.get(url)
    if prior:
        if prior.get("status") != 200 or prior.get("body_sha256") != sha:
            raise ValueError("existing project-124 page-2 capture conflicts; no refetch")
        saved = raw_dir / f"{sha}.body"
        if not saved.is_file() or digest(saved.read_bytes()) != sha:
            raise ValueError("existing project-124 API body is missing or corrupt")
        return prior
    return _store_success(
        url, body, headers, event_path=ledger_path, raw_dir=raw_dir,
        source="root-preserved-anonymous-GET; imported without refetch",
        pagination=first_pagination,
    )


def _base_inputs(base_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    manifest_path = base_dir / "manifest.json"
    rows_path = base_dir / "unit-results.jsonl"
    if not manifest_path.is_file() or not rows_path.is_file():
        raise ValueError("base Etalon partial output is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_rows = rows_path.read_bytes()
    if digest(raw_rows) != manifest.get("unit_results_jsonl_sha256"):
        raise ValueError("base Etalon unit-results hash does not match its manifest")
    rows = [json.loads(line) for line in raw_rows.decode("utf-8").splitlines() if line.strip()]
    base_by_project: dict[int, dict[str, Any]] = {}
    for row in rows:
        project_id = row.get("source_project_id")
        if project_id in PROJECT_IDS:
            base_by_project.setdefault(project_id, row)
    if set(base_by_project) != set(PROJECT_IDS):
        raise ValueError("base Etalon output lacks project 124 or 125 source rows")
    return manifest, rows, base_by_project


class PageLoader:
    def __init__(self, *, capture_dir: Path, live: bool,
                 total_attempts: int) -> None:
        self.capture_dir = capture_dir
        self.live = live
        self.events_path = capture_dir / "requests.jsonl"
        self.recovery_path = capture_dir / "recovery_requests.jsonl"
        self.raw_dir = capture_dir / "raw"
        self.events = _load_ledger(self.events_path)
        self.recovery_events = _load_ledger(self.recovery_path)
        self.total_attempts = total_attempts
        self.new_attempts = 0
        self.host_blocked = False

    def _replay(self, event: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        status = event.get("status")
        if status in BLOCK_STATUSES or event.get("blocked") or event.get("error"):
            self.host_blocked = status in BLOCK_STATUSES or bool(event.get("blocked"))
            return None, event
        if status != 200 or not event.get("body_sha256"):
            return None, event
        body_path = self.raw_dir / f"{event['body_sha256']}.body"
        if not body_path.is_file() or digest(body_path.read_bytes()) != event["body_sha256"]:
            raise ValueError("previous API response is missing or corrupt; no refetch")
        return json.loads(body_path.read_bytes()), event

    def _fetch(self, url: str, pagination: dict[str, Any], *, recovery: bool) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if self.host_blocked:
            return None, {"url": url, "status": None,
                          "error": "host_stopped_after_denial", "blocked": True}
        if self.total_attempts + self.new_attempts >= MAX_API_REQUESTS:
            return None, {"url": url, "status": None,
                          "error": "request_limit_reached", "blocked": False}
        if self.total_attempts + self.new_attempts:
            time.sleep(REQUEST_DELAY_SECONDS)
        result = curl_fetch(url, {API_HOST}, {}, REQUEST_TIMEOUT_SECONDS)
        self.new_attempts += 1
        is_blocked = result.status in BLOCK_STATUSES
        content_type = _header(dict(result.headers), "Content-Type") or ""
        response_is_json = "json" in content_type.lower()
        error = result.error
        if result.status == 200 and not response_is_json:
            error = error or "unexpected_content_type"
        if result.status == 200 and not result.body:
            error = error or "empty_body"
        event = {
            "at": utc_now(), "url": url, "final_url": result.final_url,
            "status": result.status, "error": error, "blocked": is_blocked,
            "body_bytes": len(result.body), "body_sha256": None,
            "headers": _safe_headers(dict(result.headers)),
            "request_pagination": pagination,
            "authorization_sent": False,
            "attempt_kind": "one-time-local-mime-recovery" if recovery else "ordinary-anonymous-get",
        }
        if recovery:
            event["recovery_reason"] = (
                "retry once because the prior HTTP 200 application/json body was discarded "
                "by a case-sensitive local MIME check"
            )
        if result.status == 200 and not error and result.body:
            sha = digest(result.body)
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            atomic_bytes(self.raw_dir / f"{sha}.body", result.body)
            event["body_sha256"] = sha
        destination = self.recovery_path if recovery else self.events_path
        append_event(destination, event)
        (self.recovery_events if recovery else self.events)[url] = event
        if is_blocked or error or result.status != 200:
            self.host_blocked = bool(is_blocked)
            return None, event
        return json.loads(result.body), event

    def __call__(self, url: str, pagination: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        recovery_event = self.recovery_events.get(url)
        if recovery_event:
            return self._replay(recovery_event)
        event = self.events.get(url)
        if event and event.get("status") == 200 and not event.get("body_sha256"):
            content_type = (_header(event.get("headers", {}), "Content-Type") or "").lower()
            recoverable_mime_bug = (
                event.get("error") == "unexpected_content_type"
                and event.get("body_bytes", 0) > 0
                and "json" in content_type
                and not event.get("blocked")
            )
            if recoverable_mime_bug:
                if not self.live:
                    return None, event
                # This is the only retry path: one prior successful JSON 200 was
                # discarded locally. Keep that event and log the retry separately.
                return self._fetch(url, pagination, recovery=True)
        if event:
            return self._replay(event)
        if not self.live:
            return None, {"url": url, "status": None, "error": "not_captured", "blocked": False}
        return self._fetch(url, pagination, recovery=False)


def collect_project(*, project_id: int, project_name: str,
                    start_pagination: dict[str, Any] | None,
                    seed_rows: list[dict[str, Any]], base_row: dict[str, Any],
                    source_total_count: int | None,
                    loader: Callable[[str, dict[str, Any]], tuple[dict[str, Any] | None, dict[str, Any]]],
                    capture_dir: Path, max_pages: int = MAX_API_REQUESTS) -> dict[str, Any]:
    rows = list(seed_rows)
    ids = {row["source_unit_id"] for row in rows}
    summaries: list[dict[str, Any]] = []
    pagination = start_pagination
    seen_requests: set[str] = set()
    stop_reason: str | None = None
    exhausted = False
    while pagination is not None and len(summaries) < max_pages:
        url = api_url(project_id, pagination)
        if url in seen_requests:
            raise ValueError("pagination returned a previously requested page")
        seen_requests.add(url)
        payload, event = loader(url, pagination)
        if payload is None:
            stop_reason = event.get("error") or f"http_{event.get('status')}"
            if event.get("blocked") or event.get("status") in BLOCK_STATUSES:
                stop_reason = f"blocked_http_{event.get('status')}"
            break
        body_path = capture_dir / "raw" / f"{event['body_sha256']}.body"
        body = body_path.read_bytes()
        page_rows, page_summary = parse_api_body(
            body, project_id=project_id, project_name=project_name,
            base_row=base_row, page_url=url,
            observed_at=_response_observed_at(event.get("headers", {}),
                                               event.get("at") or utc_now()),
            body_sha256=event["body_sha256"])
        rows.extend(page_rows)
        ids.update(row["source_unit_id"] for row in page_rows)
        summaries.append({**page_summary, "status": 200,
                          "request_pagination": pagination,
                          "capture_source": event.get("source", "anonymous GET")})
        next_value = page_summary["returned_pagination"]
        if source_total_count is not None and len(ids) >= source_total_count:
            exhausted = len(ids) == source_total_count
            stop_reason = "source_count_reached" if exhausted else "source_count_exceeded"
            pagination = None
            break
        pagination = _next_pagination(pagination, next_value, project_id)
        if pagination is None:
            exhausted = True
            stop_reason = "pagination_haveItem_false"
    if pagination is not None and stop_reason is None:
        stop_reason = "page_iteration_limit_reached"
    if source_total_count is not None and len(ids) == source_total_count:
        exhausted = True
    return {
        "project_id": project_id,
        "project_name": project_name,
        "source_total_count": source_total_count,
        "base_unique_ids": len({row["source_unit_id"] for row in seed_rows}),
        "combined_unique_ids": len(ids),
        "api_rows_before_dedup": len(rows) - len(seed_rows),
        "api_pages_parsed": summaries,
        "complete_by_source_count": (len(ids) == source_total_count
                                      if source_total_count is not None else exhausted),
        "pagination_exhausted": exhausted,
        "stop_reason": stop_reason or "no_pagination_state",
        "rows": rows[len(seed_rows):],
        "attempts_may_be_incomplete": bool(stop_reason and stop_reason.startswith("blocked_")),
    }


def _capture_manifest_pages(manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for page in manifest.get("project_pages", []):
        project_id = page.get("source_project_id")
        if project_id in PROJECT_IDS:
            result[project_id] = page
    if set(result) != set(PROJECT_IDS):
        raise ValueError("base manifest lacks both Etalon API continuation projects")
    return result


def run(*, base_dir: Path = DEFAULT_BASE_OUTPUT,
        source_capture_dir: Path = DEFAULT_SOURCE_CAPTURE_DIR,
        capture_dir: Path = DEFAULT_API_CAPTURE_DIR,
        final_dir: Path = DEFAULT_FINAL_DIR,
        live: bool = False, anonymous_capture: bool = False) -> dict[str, Any]:
    if live and not anonymous_capture:
        raise ValueError("live API capture requires explicit --anonymous-capture")
    base_manifest, base_rows, base_by_project = _base_inputs(base_dir)
    page_metadata = _capture_manifest_pages(base_manifest)
    capture_dir.mkdir(parents=True, exist_ok=True)
    events_path = capture_dir / "requests.jsonl"
    with (capture_dir / "run.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        events = _load_ledger(events_path)
        # Project 124 page 2 was already retrieved anonymously by root. Import
        # that immutable body and never issue the same request again.
        first_124 = page_metadata[124].get("source_ssr_pagination")
        if not isinstance(first_124, dict):
            raise ValueError("project 124 has no source-returned SSR pagination")
        imported = import_root_capture(capture_dir=capture_dir,
                                       source_capture_dir=source_capture_dir,
                                       first_pagination=first_124)
        events = _load_ledger(events_path)
        recovery_events = _load_ledger(capture_dir / "recovery_requests.jsonl")
        loader = PageLoader(capture_dir=capture_dir, live=live,
                            total_attempts=len(events) + len(recovery_events))
        project_results: list[dict[str, Any]] = []
        all_api_rows: list[dict[str, Any]] = []
        global_block = False
        # First decode the pre-captured project-124 response; its returned
        # pagination controls the next request.
        root_url = api_url(124, first_124)
        root_event = loader.events[root_url]
        root_body_path = capture_dir / "raw" / f"{root_event['body_sha256']}.body"
        root_body = root_body_path.read_bytes()
        root_rows, root_summary = parse_api_body(
            root_body, project_id=124, project_name=PROJECT_IDS[124],
            base_row=base_by_project[124], page_url=root_url,
            observed_at=_response_observed_at(root_event.get("headers", {}),
                                               root_event.get("at") or utc_now()),
            body_sha256=root_event["body_sha256"])
        root_response = json.loads(root_body)
        next_124 = _next_pagination(first_124, root_response["data"]["pagination"], 124)
        first_result = collect_project(
            project_id=124, project_name=PROJECT_IDS[124],
            start_pagination=next_124,
            seed_rows=[row for row in base_rows if row.get("source_project_id") == 124] + root_rows,
            base_row=base_by_project[124],
            source_total_count=page_metadata[124].get("source_catalog_total_count"),
            loader=loader, capture_dir=capture_dir)
        first_result["api_pages_parsed"].insert(0, {
            **root_summary, "status": 200, "request_pagination": first_124,
            "capture_source": imported.get("source"),
        })
        first_result["base_unique_ids"] = len({
            row["source_unit_id"] for row in base_rows
            if row.get("source_project_id") == 124
        })
        first_result["api_rows_before_dedup"] += len(root_rows)
        all_api_rows.extend(root_rows)
        all_api_rows.extend(first_result.pop("rows"))
        project_results.append(first_result)
        global_block = loader.host_blocked

        if not global_block:
            first_125 = page_metadata[125].get("source_ssr_pagination")
            if not isinstance(first_125, dict):
                raise ValueError("project 125 has no source-returned SSR pagination")
            second_result = collect_project(
                project_id=125, project_name=PROJECT_IDS[125],
                start_pagination=first_125,
                seed_rows=[row for row in base_rows if row.get("source_project_id") == 125],
                base_row=base_by_project[125],
                source_total_count=page_metadata[125].get("source_catalog_total_count"),
                loader=loader, capture_dir=capture_dir)
            all_api_rows.extend(second_result.pop("rows"))
            project_results.append(second_result)

        combined_rows = base_rows + all_api_rows
        deduped, conflicts = _deduplicate(combined_rows)
        final_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(final_dir / "unit-results.jsonl", deduped)
        _write_jsonl(final_dir / "unit-id-conflicts.jsonl", conflicts)
        _write_csv(final_dir / "unit-results.csv", deduped)
        fresh_events = _load_ledger(events_path)
        fresh_recovery_events = _load_ledger(capture_dir / "recovery_requests.jsonl")
        request_events = sorted(
            [*fresh_events.values(), *fresh_recovery_events.values()],
            key=lambda event: event.get("at", ""),
        )
        api_manifest = {
            "schema_version": "etalongroup-anonymous-api-completion/1",
            "generated_at": utc_now(),
            "api_endpoint": API_BASE,
            "request_policy": {
                "authorization_sent": False,
                "cookies_sent": False,
                "user_credentials_used": False,
                "max_requests_total": MAX_API_REQUESTS,
                "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
                "max_body_bytes": MAX_HTML_BYTES,
                "delay_seconds_between_new_requests": REQUEST_DELAY_SECONDS,
                "stop_http_statuses": sorted(BLOCK_STATUSES),
            },
            "base_output_dir": str(base_dir),
            "base_manifest_sha256": digest((base_dir / "manifest.json").read_bytes()),
            "base_unit_results_sha256": base_manifest["unit_results_jsonl_sha256"],
            "root_preserved_capture": {
                "body_path": str(source_capture_dir / "root-api-anonymous.json"),
                "body_sha256": digest((source_capture_dir / "root-api-anonymous.json").read_bytes()),
                "status": 200,
                "authorization_sent": False,
                "imported_without_refetch": True,
            },
            "request_count": len(request_events),
            "request_events": request_events,
            "projects": project_results,
            "raw_unit_rows_before_dedup": len(combined_rows),
            "unique_source_unit_ids_after_dedup": len(deduped),
            "unit_id_conflict_count": len(conflicts),
            "unit_results_jsonl_sha256": digest((final_dir / "unit-results.jsonl").read_bytes()),
            "unit_results_csv_sha256": digest((final_dir / "unit-results.csv").read_bytes()),
            "unit_id_conflicts_jsonl_sha256": digest((final_dir / "unit-id-conflicts.jsonl").read_bytes()),
            "price_semantics": "source fields preserved separately; no normalized current asking-price claim",
            "availability_semantics": "captured source isBooked only; not a live guarantee",
            "image_rights_status": "unknown; image URLs only, image files not fetched",
            "publication_allowed": False,
        }
        # Keep the final manifest hash-last for consumers that poll this private output.
        atomic_bytes(final_dir / "manifest.json", canonical_json(api_manifest) + b"\n")
        return api_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_OUTPUT)
    parser.add_argument("--source-capture-dir", type=Path, default=DEFAULT_SOURCE_CAPTURE_DIR)
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_API_CAPTURE_DIR)
    parser.add_argument("--final-dir", type=Path, default=DEFAULT_FINAL_DIR)
    parser.add_argument("--live", action="store_true", help="fetch source-returned pages")
    parser.add_argument("--anonymous-capture", action="store_true",
                        help="explicitly assert no Authorization or cookies; required with --live")
    args = parser.parse_args()
    for path in (args.capture_dir, args.final_dir):
        if not path.resolve().is_relative_to(ROOT / ".runtime/research/developer-harvest"):
            parser.error("output paths must remain under private developer-harvest runtime")
    try:
        result = run(base_dir=args.base_dir, source_capture_dir=args.source_capture_dir,
                     capture_dir=args.capture_dir, final_dir=args.final_dir,
                     live=args.live, anonymous_capture=args.anonymous_capture)
    except (OSError, ValueError, json.JSONDecodeError, BlockingIOError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.live and (
        any(not project.get("complete_by_source_count") for project in result["projects"])
        or result.get("unit_id_conflict_count", 0) > 0
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
