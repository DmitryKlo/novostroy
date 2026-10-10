"""Validated internal visibility policy for the building registry.

Archived source records remain in the immutable import and review evidence.
Only the ordinary catalogue projection excludes them. Raw source identities,
evidence URLs, and local paths never enter public DTOs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.store import ROOT


VISIBILITY_ROOT = ROOT / "data/property/estateline/visibility"
ACTIVE_PATH = VISIBILITY_ROOT / "active.json"
RUN_RE = re.compile(r"visibility-v1-[0-9a-f]{16}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@lru_cache(maxsize=4)
def _load_run(run_id: str, manifest_hash: str,
              manifest_mtime_ns: int, manifest_size: int,
              records_mtime_ns: int, records_size: int) -> dict[str, Any]:
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise ValueError("Visibility pointer run ID is invalid")
    if not isinstance(manifest_hash, str) or not HASH_RE.fullmatch(manifest_hash):
        raise ValueError("Visibility pointer manifest hash is invalid")
    manifest_path = VISIBILITY_ROOT / "runs" / run_id / "manifest.json"
    records_path = VISIBILITY_ROOT / "runs" / run_id / "records.jsonl"
    manifest_bytes = manifest_path.read_bytes()
    if _sha256(manifest_bytes) != manifest_hash:
        raise ValueError("Visibility manifest differs from active pointer")
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != "registry-visibility/1"
            or manifest.get("run_id") != run_id
            or manifest.get("rules_version") not in {
                "registry-visibility-v3-age-gt365", "registry-visibility-v4-age-purpose"}):
        raise ValueError("Unsupported visibility policy")
    if manifest.get("max_age_days") != 365:
        raise ValueError("Visibility age threshold is invalid")
    valid_from = date.fromisoformat(manifest["as_of_date"])
    refresh_due = date.fromisoformat(manifest["next_refresh_due"])
    today = datetime.now(MOSCOW_TZ).date()
    if not valid_from <= today < refresh_due:
        raise RuntimeError("Visibility decisions need a refreshed policy run")
    payload = records_path.read_bytes()
    if _sha256(payload) != manifest.get("records_sha256"):
        raise ValueError("Visibility records differ from manifest")
    records = [json.loads(line) for line in payload.splitlines() if line.strip()]
    if len(records) != manifest.get("records_total"):
        raise ValueError("Visibility record count differs from manifest")
    by_source: dict[str, dict[str, Any]] = {}
    for row in records:
        source_id = row.get("registry_id") if isinstance(row, dict) else None
        if (not isinstance(source_id, str) or not source_id
                or source_id in by_source or row.get("visibility") not in {"visible", "archived"}):
            raise ValueError("Visibility source identity or decision is invalid")
        by_source[source_id] = row
    counts = {decision: sum(row["visibility"] == decision for row in records)
              for decision in ("visible", "archived")}
    if counts != manifest.get("decisions"):
        raise ValueError("Visibility decision counts differ from manifest")
    return {"manifest": manifest, "manifest_sha256": manifest_hash,
            "records": by_source, "counts": counts}


def load_visibility(source_hash: str, source_manifest_hash: str,
                    source_dates: dict[str, str], *,
                    pinned: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if pinned is None and not ACTIVE_PATH.is_file():
        return None
    pointer = pinned if pinned is not None else json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    run_id = pointer.get("run_id") if isinstance(pointer, dict) else None
    manifest_hash = pointer.get("manifest_sha256") if isinstance(pointer, dict) else None
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise ValueError("Visibility pointer run ID is invalid")
    if not isinstance(manifest_hash, str) or not HASH_RE.fullmatch(manifest_hash):
        raise ValueError("Visibility pointer manifest hash is invalid")
    manifest_path = VISIBILITY_ROOT / "runs" / run_id / "manifest.json"
    records_path = VISIBILITY_ROOT / "runs" / run_id / "records.jsonl"
    manifest_stat, records_stat = manifest_path.stat(), records_path.stat()
    loaded = _load_run(run_id, manifest_hash, manifest_stat.st_mtime_ns,
                       manifest_stat.st_size, records_stat.st_mtime_ns, records_stat.st_size)
    manifest = loaded["manifest"]
    if not date.fromisoformat(manifest["as_of_date"]) <= datetime.now(MOSCOW_TZ).date() < date.fromisoformat(manifest["next_refresh_due"]):
        raise RuntimeError("Visibility decisions need a refreshed policy run")
    if manifest.get("source_sha256") != source_hash or manifest.get("source_manifest_sha256") != source_manifest_hash:
        raise ValueError("Visibility run targets another registry import")
    if set(loaded["records"]) != set(source_dates):
        raise ValueError("Visibility run does not cover the complete registry")
    if any(row.get("source_observed_at") != source_dates[source_id]
           for source_id, row in loaded["records"].items()):
        raise ValueError("Visibility evidence date differs from registry import")
    return loaded
