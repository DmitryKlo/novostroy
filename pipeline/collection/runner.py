"""Offline collection runner with immutable snapshots and durable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from backend.store import ROOT
from pipeline.collection.adapters import ADAPTERS, ADAPTER_VERSIONS, Batch
from pipeline.collection.http_fetch import TransientFetchError, fetch_public_file, latest_minstroy_xls

DEFAULT_CONFIG = ROOT / "pipeline/collection/jobs.local.json"
DEFAULT_STATE_DIR = ROOT / "data/runtime/collection"
MAX_RETRIES = 3
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FORBIDDEN_CONFIG_KEYS = {"token", "api_key", "apikey", "password", "secret", "credential", "authorization", "pbi"}


class RetryableCollectionError(RuntimeError):
    """A bounded transient failure; no raw response text should be attached."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, max_bytes: int) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError("source_exceeds_max_bytes")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_CONFIG_KEYS:
                raise ValueError("Credential fields must not appear in collection configuration")
            _check_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _check_secret_fields(child)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
        if parsed.username or parsed.password or query_keys & FORBIDDEN_CONFIG_KEYS:
            raise ValueError("Credential-bearing URLs are not allowed in collection configuration")


def load_config(path: Path) -> list[dict[str, Any]]:
    config = json.loads(path.read_text(encoding="utf-8"))
    _check_secret_fields(config)
    if config.get("version") != "1" or not isinstance(config.get("jobs"), list):
        raise ValueError("Collection config requires version 1 and jobs list")
    jobs = config["jobs"]
    seen: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Every job must be an object")
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not SAFE_ID.fullmatch(job_id) or job_id in seen:
            raise ValueError("Job IDs must be unique safe strings")
        seen.add(job_id)
        if job.get("kind") not in ADAPTERS and job.get("kind") not in {"gated_external", "public_http_registry"}:
            raise ValueError(f"Unsupported collection kind for {job_id}")
        if not isinstance(job.get("enabled", False), bool):
            raise ValueError("enabled must be boolean")
        if job.get("kind") != "gated_external" and not isinstance(job.get("source_id"), str):
            raise ValueError("Local jobs need source_id")
        if job.get("kind") == "public_http_registry":
            if (job.get("dataset_kind") not in {"construction_permit", "commissioning_permit"}
                    or not isinstance(job.get("page_url"), str)
                    or not isinstance(job.get("manifest_path"), str)):
                raise ValueError("Public registry HTTP jobs need a reviewed dataset page and manifest")
            page_limit = job.get("max_page_bytes", 262144)
            if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not 1 <= page_limit <= 262144:
                raise ValueError("Registry landing page cap must be at most 256 KiB")
        if job.get("import_to_postgis") and (job.get("kind") != "developer_yrl_file" or job.get("data_mode") != "real"):
            raise ValueError("PostGIS import is only available for rights-cleared real developer feeds")
        interval = job.get("min_interval_seconds", 0)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 86400:
            raise ValueError("min_interval_seconds must be 0..86400")
        limit = job.get("max_bytes", 268435456)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1073741824:
            raise ValueError("max_bytes must be 1..1073741824")
    return jobs


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Checkpoints:
    def __init__(self, state_dir: Path, apply: bool) -> None:
        self.state_dir = state_dir
        self.apply = apply
        database = state_dir / "checkpoints.sqlite3"
        if apply:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(state_dir, 0o700)
            self.connection = sqlite3.connect(database, timeout=10)
            os.chmod(database, 0o600)
        elif database.is_file():
            self.connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        else:
            self.connection = sqlite3.connect(":memory:")
        if apply or not database.is_file():
            self.connection.execute("""CREATE TABLE IF NOT EXISTS job_state (
                job_id TEXT PRIMARY KEY, fingerprint TEXT, snapshot_id TEXT, etag TEXT,
                last_modified TEXT, last_attempt_at TEXT, last_success_at TEXT,
                status TEXT NOT NULL, error_code TEXT, imported_snapshot_id TEXT
            )""")
            self.connection.commit()

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT fingerprint,snapshot_id,etag,last_modified,last_attempt_at,last_success_at,status,error_code,imported_snapshot_id FROM job_state WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(("fingerprint", "snapshot_id", "etag", "last_modified", "last_attempt_at", "last_success_at", "status", "error_code", "imported_snapshot_id"), row))

    def success(self, job_id: str, fingerprint: str, snapshot_id: str, etag: str | None,
                last_modified: str | None, attempted_at: str, imported_snapshot_id: str | None) -> None:
        if not self.apply:
            return
        with self.connection:
            self.connection.execute("""INSERT INTO job_state VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                snapshot_id=excluded.snapshot_id,etag=excluded.etag,last_modified=excluded.last_modified,
                last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                status=excluded.status,error_code=NULL,imported_snapshot_id=excluded.imported_snapshot_id""",
                (job_id, fingerprint, snapshot_id, etag, last_modified, attempted_at, utc_now(), "success", None, imported_snapshot_id))

    def failure(self, job_id: str, code: str, attempted_at: str) -> None:
        if not self.apply:
            return
        with self.connection:
            self.connection.execute("""INSERT INTO job_state(job_id,last_attempt_at,status,error_code) VALUES (?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                status=excluded.status,error_code=excluded.error_code""", (job_id, attempted_at, "failed", code))

    def close(self) -> None:
        self.connection.close()


def snapshot_valid(folder: Path) -> bool:
    manifest_path = folder / "manifest.json"
    records_path = folder / "records.json"
    if not manifest_path.is_file() or not records_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sha256_file(records_path, 1073741824) != manifest["records_sha256"]:
            return False
        if "raw_sha256" in manifest:
            raw_path = folder / "source.xls"
            return raw_path.is_file() and sha256_file(raw_path, 1073741824) == manifest["raw_sha256"]
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def write_snapshot(state_dir: Path, job_id: str, snapshot_id: str,
                   records: list[dict[str, Any]], manifest: dict[str, Any],
                   raw_bytes: bytes | None = None) -> Path:
    parent = state_dir / "snapshots" / job_id
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    destination = parent / snapshot_id
    records_bytes = canonical_json(records)
    manifest["records_sha256"] = sha256_bytes(records_bytes)
    if raw_bytes is not None:
        manifest["raw_sha256"] = sha256_bytes(raw_bytes)
    if destination.exists():
        if snapshot_valid(destination) and json.loads((destination / "manifest.json").read_text(encoding="utf-8"))["records_sha256"] == manifest["records_sha256"]:
            return destination
        raise RuntimeError("Immutable snapshot conflict")
    temporary = Path(tempfile.mkdtemp(dir=parent, prefix=".pending-"))
    try:
        _write_private(temporary / "records.json", records_bytes)
        if raw_bytes is not None:
            _write_private(temporary / "source.xls", raw_bytes)
        _write_private(temporary / "manifest.json", canonical_json(manifest))
        temporary.replace(destination)
    finally:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()
    return destination


def _failure(job_id: str, code: str, attempts: int = 0) -> dict[str, Any]:
    return {"job_id": job_id, "status": "failed" if code != "missing_access" else "missing_access",
            "code": code, "attempts": attempts}


def _fetch_with_retry(url: str, *, max_bytes: int, etag: str | None,
                      last_modified: str | None, sleep: Callable[[float], None]) -> tuple[Any, int]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_public_file(url, allowed_hosts={"minstroyrf.gov.ru"}, max_bytes=max_bytes,
                                     previous_etag=etag, previous_last_modified=last_modified), attempt
        except TransientFetchError:
            if attempt == MAX_RETRIES:
                raise RetryableCollectionError("retry_exhausted") from None
            sleep(min(0.2 * 2 ** (attempt - 1), 1.0))
    raise RetryableCollectionError("retry_exhausted")


def _run_public_registry_job(job: dict[str, Any], checkpoints: Checkpoints, *, apply: bool,
                             allow_network: bool, sleep: Callable[[float], None]) -> dict[str, Any]:
    job_id = job["job_id"]
    if not apply or not allow_network:
        return _failure(job_id, "missing_access")
    attempted_at = utc_now()
    old = checkpoints.get(job_id)
    interval = job.get("min_interval_seconds", 0)
    if old and old.get("last_attempt_at") and interval:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(old["last_attempt_at"])).total_seconds()
        if elapsed < interval:
            return {"job_id": job_id, "status": "rate_limited",
                    "retry_after_seconds": round(interval - elapsed, 2), "attempts": 0}
    old_manifest = None
    if old and old.get("snapshot_id"):
        old_folder = checkpoints.state_dir / "snapshots" / job_id / old["snapshot_id"]
        if snapshot_valid(old_folder):
            old_manifest = json.loads((old_folder / "manifest.json").read_text(encoding="utf-8"))
    attempts = 0
    try:
        reviewed = json.loads(resolve_path(job["manifest_path"]).read_text(encoding="utf-8"))
        _check_secret_fields(reviewed)
        dataset = next((entry for entry in reviewed.get("datasets", [])
                        if entry.get("source_id") == job["source_id"]), None)
        if (not isinstance(dataset, dict) or dataset.get("rights_status") != "cleared"
                or dataset.get("source_page_url") != job["page_url"]
                or reviewed.get("reuse_terms_url") != "https://minstroyrf.gov.ru/opendata/"):
            return _failure(job_id, "missing_access")
        page, used = _fetch_with_retry(job["page_url"], max_bytes=job.get("max_page_bytes", 262144),
                                       etag=None, last_modified=None, sleep=sleep)
        attempts += used
        if page.body is None:
            raise ValueError("Registry landing page has no content")
        download_url, version = latest_minstroy_xls(page.body, job["page_url"])
        same_url = old_manifest is not None and old_manifest.get("download_url") == download_url
        fetched, used = _fetch_with_retry(download_url, max_bytes=job["max_bytes"],
                                          etag=old.get("etag") if same_url else None,
                                          last_modified=old.get("last_modified") if same_url else None,
                                          sleep=sleep)
        attempts += used
        if fetched.status == "not_modified":
            if old_manifest is None or old is None:
                raise ValueError("HTTP 304 without a valid local snapshot")
            checkpoints.success(job_id, old["fingerprint"], old["snapshot_id"],
                                fetched.etag, fetched.last_modified, attempted_at, None)
            coverage = old_manifest.get("coverage", {})
            return {"job_id": job_id, "status": "no_op", "snapshot_id": old["snapshot_id"],
                    "record_count": coverage.get("record_count"), "coverage": coverage, "attempts": attempts,
                    "http_status": 304}
        if fetched.body is None or fetched.content_type != "application/vnd.ms-excel":
            raise ValueError("Registry download is not an XLS workbook")
        raw_sha = sha256_bytes(fetched.body)
        fingerprint = sha256_bytes(canonical_json({"job_id": job_id, "source_id": job["source_id"],
            "dataset_kind": job["dataset_kind"], "download_url": download_url,
            "version": version, "raw_sha256": raw_sha,
            "adapter_version": ADAPTER_VERSIONS["minstroy_registry_xls"]}))
        snapshot_id = fingerprint[:24]
        if old and old.get("fingerprint") == fingerprint and old_manifest is not None:
            checkpoints.success(job_id, fingerprint, snapshot_id, fetched.etag,
                                fetched.last_modified, attempted_at, None)
            coverage = old_manifest.get("coverage", {})
            return {"job_id": job_id, "status": "no_op", "snapshot_id": snapshot_id,
                    "record_count": coverage.get("record_count"), "coverage": coverage, "attempts": attempts,
                    "http_status": 200}
        with tempfile.NamedTemporaryFile(dir=checkpoints.state_dir, suffix=".xls", delete=False) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(fetched.body)
        try:
            latest_dataset = {**dataset, "captured_at": attempted_at, "published_version_date": version,
                              "raw_record_count": None}
            latest_manifest = {**reviewed, "datasets": [latest_dataset]}
            batch = ADAPTERS["minstroy_registry_xls"](temp_path, job, latest_manifest, None, old.get("etag") if old else None)
        finally:
            temp_path.unlink(missing_ok=True)
        snapshot_manifest = {"version": "1", "job_id": job_id, "source_id": job["source_id"],
            "adapter": "minstroy_registry_xls", "adapter_version": ADAPTER_VERSIONS["minstroy_registry_xls"],
            "data_mode": "public_registry", "snapshot_id": snapshot_id,
            "observed_at": attempted_at, "published_version_date": version,
            "download_url": download_url, "etag": fetched.etag,
            "last_modified": fetched.last_modified, "coverage": batch.coverage}
        write_snapshot(checkpoints.state_dir, job_id, snapshot_id, batch.records,
                       snapshot_manifest, raw_bytes=fetched.body)
        checkpoints.success(job_id, fingerprint, snapshot_id, fetched.etag,
                            fetched.last_modified, attempted_at, None)
        return {"job_id": job_id, "status": "imported", "snapshot_id": snapshot_id,
                "record_count": batch.coverage["record_count"], "coverage": batch.coverage,
                "attempts": attempts, "http_status": 200}
    except RetryableCollectionError:
        code = "retry_exhausted"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        code = "source_invalid"
    checkpoints.failure(job_id, code, attempted_at)
    return _failure(job_id, code, attempts)


def run_job(job: dict[str, Any], checkpoints: Checkpoints, *, apply: bool,
            allow_real: bool, allow_network: bool, adapters: Mapping[str, Callable[..., Batch]],
            sleep: Callable[[float], None]) -> dict[str, Any]:
    job_id = job["job_id"]
    if not job.get("enabled", False) or job["kind"] == "gated_external":
        return _failure(job_id, "missing_access" if job.get("requires_access", False) or job["kind"] == "gated_external" else "disabled")
    if job["kind"] == "public_http_registry":
        return _run_public_registry_job(job, checkpoints, apply=apply,
                                        allow_network=allow_network, sleep=sleep)
    if job.get("data_mode") == "real" and not allow_real:
        return _failure(job_id, "missing_access")
    if job.get("data_mode") == "real" and "rights_path" not in job:
        return _failure(job_id, "missing_access")
    attempted_at = utc_now()
    try:
        source_path = resolve_path(job["input_path"])
        max_bytes = job.get("max_bytes", 268435456)
        input_hash = sha256_file(source_path, max_bytes)
        support: dict[str, Any] = {}
        supporting_hashes: dict[str, str] = {}
        for name in ("manifest_path", "rights_path"):
            if name in job:
                path = resolve_path(job[name])
                supporting_hashes[name] = sha256_file(path, 1048576)
                support[name] = json.loads(path.read_text(encoding="utf-8"))
                _check_secret_fields(support[name])
        source_manifest = support.get("manifest_path")
        if isinstance(source_manifest, dict):
            expected_hash = (source_manifest.get("raw_sha256") if job["kind"] == "osm_poi_file"
                             else source_manifest.get("output_sha256") if job["kind"] == "city_parquet_reference"
                             else source_manifest.get("normalized_sha256") if job["kind"] == "social_enrichment_fixture" else None)
            if job["kind"] == "minstroy_registry_xls":
                dataset = next((entry for entry in source_manifest.get("datasets", [])
                                if entry.get("source_id") == job["source_id"]), None)
                if not isinstance(dataset, dict):
                    raise ValueError("Registry source is missing from manifest")
                expected_hash = dataset.get("sha256")
            if expected_hash is not None and expected_hash != input_hash:
                raise ValueError("source manifest hash mismatch")
        rights = support.get("rights_path")
        if job.get("data_mode") == "real":
            if not isinstance(rights, dict) or rights.get("rights_status") != "cleared" or rights.get("test_only") is True or rights.get("permitted_storage") is not True or rights.get("permitted_display") is not True:
                return _failure(job_id, "missing_access")
            if job.get("import_to_postgis") and not job.get("postgis_service"):
                return _failure(job_id, "missing_access")
        fingerprint = sha256_bytes(canonical_json({"job": job, "input_sha256": input_hash,
            "supporting_sha256": supporting_hashes, "adapter_version": ADAPTER_VERSIONS[job["kind"]]}))
        snapshot_id = fingerprint[:24]
        old = checkpoints.get(job_id)
        folder = checkpoints.state_dir / "snapshots" / job_id / snapshot_id
        sink_pending = bool(job.get("import_to_postgis") and old and not old.get("imported_snapshot_id"))
        if old and old["fingerprint"] == fingerprint and snapshot_valid(folder) and not sink_pending:
            saved = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            coverage = saved.get("coverage", {})
            return {"job_id": job_id, "status": "no_op", "snapshot_id": snapshot_id,
                    "record_count": coverage.get("record_count"), "coverage": coverage,
                    "attempts": 0}
        interval = job.get("min_interval_seconds", 0)
        if old and old["last_attempt_at"] and interval:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(old["last_attempt_at"])).total_seconds()
            if elapsed < interval:
                return {"job_id": job_id, "status": "rate_limited", "retry_after_seconds": round(interval - elapsed, 2), "attempts": 0}
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = "missing_access" if isinstance(exc, FileNotFoundError) and job.get("data_mode") == "real" else ("source_missing" if isinstance(exc, FileNotFoundError) else "source_invalid")
        checkpoints.failure(job_id, code, attempted_at)
        return _failure(job_id, code)

    previous_etag = old.get("etag") if old else None
    batch: Batch | None = None
    attempts = 0
    for attempts in range(1, MAX_RETRIES + 1):
        try:
            batch = adapters[job["kind"]](source_path, job, support.get("manifest_path"), rights, previous_etag)
            break
        except (RetryableCollectionError, TimeoutError):
            if attempts < MAX_RETRIES:
                sleep(min(0.2 * 2 ** (attempts - 1), 1.0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            checkpoints.failure(job_id, "adapter_invalid", attempted_at)
            return _failure(job_id, "adapter_invalid", attempts)
    if batch is None:
        checkpoints.failure(job_id, "retry_exhausted", attempted_at)
        return _failure(job_id, "retry_exhausted", attempts)

    report = {"job_id": job_id, "status": "planned" if not apply else "imported",
              "snapshot_id": snapshot_id, "record_count": batch.coverage.get("record_count", len(batch.records)),
              "coverage": batch.coverage, "attempts": attempts}
    if not apply:
        return report
    manifest = {
        "version": "1", "job_id": job_id, "source_id": job["source_id"],
        "adapter": job["kind"], "adapter_version": ADAPTER_VERSIONS[job["kind"]],
        "data_mode": job.get("data_mode", "public_sample"),
        "snapshot_id": snapshot_id, "source_sha256": input_hash,
        "supporting_sha256": supporting_hashes, "observed_at": batch.observed_at,
        "etag": batch.etag, "last_modified": batch.last_modified,
        "coverage": batch.coverage,
    }
    try:
        write_snapshot(checkpoints.state_dir, job_id, snapshot_id, batch.records, manifest)
        imported_snapshot_id = None
        if job.get("import_to_postgis"):
            if job.get("data_mode") != "real" or not allow_real:
                raise ValueError("Real PostGIS import requires explicit rights and opt-in")
            from sources.property.importer import import_records
            imported = import_records(batch.records, rights, service=job["postgis_service"], commit=True)
            imported_snapshot_id = imported["snapshot_id"]
            report["postgis_snapshot_id"] = imported_snapshot_id
        checkpoints.success(job_id, fingerprint, snapshot_id, batch.etag, batch.last_modified, attempted_at, imported_snapshot_id)
    except (OSError, ValueError, RuntimeError):
        checkpoints.failure(job_id, "commit_failed", attempted_at)
        return _failure(job_id, "commit_failed", attempts)
    return report


def run(config_path: Path = DEFAULT_CONFIG, state_dir: Path = DEFAULT_STATE_DIR, *, apply: bool = False,
        allow_real: bool = False, allow_network: bool = False,
        selected_jobs: set[str] | None = None,
        adapters: Mapping[str, Callable[..., Batch]] = ADAPTERS,
        sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    jobs = load_config(config_path)
    if selected_jobs:
        unknown = selected_jobs - {job["job_id"] for job in jobs}
        if unknown:
            raise ValueError("Unknown job selection")
        jobs = [job for job in jobs if job["job_id"] in selected_jobs]
    checkpoints = Checkpoints(state_dir, apply)
    try:
        results = [run_job(job, checkpoints, apply=apply, allow_real=allow_real,
                           allow_network=allow_network, adapters=adapters, sleep=sleep) for job in jobs]
    finally:
        checkpoints.close()
    failures = [{"job_id": item["job_id"], "code": item["code"]} for item in results if item["status"] in {"failed", "missing_access"}]
    report = {"version": "1", "mode": "apply" if apply else "dry_run", "offline": not allow_network,
              "jobs": results, "source_failures": failures,
              "counts": {status: sum(item["status"] == status for item in results)
                         for status in ("imported", "no_op", "planned", "rate_limited", "missing_access", "failed")}}
    if apply:
        report_path = state_dir / "reports" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
        atomic_json(report_path, report)
        old_reports = sorted(report_path.parent.glob("*.json"))
        for obsolete in old_reports[:-30]:
            obsolete.unlink()
    return report
