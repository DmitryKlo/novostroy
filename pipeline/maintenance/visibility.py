"""Typed project visibility decisions from dated source and official evidence.

The default for missing evidence is *visible with review context*, never
archived. A developer bankruptcy alone is not evidence that an individual
building was cancelled. This private sidecar is not a public project model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pipeline.maintenance.refresh import ROOT, _rows, atomic_bytes, exclusive_lock, read_json, sha256, write_json

RULES_VERSION = "registry-visibility-v4-age-purpose"
TERMINAL_EVENTS = {"project_terminated", "permit_revoked", "commissioned"}
REVIEW_EVENTS = {"developer_bankruptcy", "status_disputed", "construction_paused", "project_active"}
TARGET_CATEGORIES = {
    "Многоквартирные дома", "Малоэтажные дома",
    "Апартаменты, Апарт-отели, МФК", "Апартаменты, Апарт-отели и МФК",
    "Апартаменты / Апарт-отели / МФК",
}
AUDITED_MAINTENANCE = {
    "estateline:1562351": ("Ремонт фасада дома", "6c268e618cda3f90135f39acbd13d5cc5f2a0093dc3271756e9f942099ca578e", "1562351"),
    "estateline:3158763": ("Жилой дом (реставрация здания объекта культурного наследия)", "129e55801d4402cc6718a2a0789bc9d94376b90fad1d97dd0105c1496f77e494", "3158763"),
    "estateline:3754471": ("Реставрация здания объекта культурного наследия", "0767541bb076fdc63efa7dae76bc1bd272969cc4ad57ed3ed971f4c0e88441aa", "3754471"),
    "estateline:47335": ("Жилой комплекс (реставрация здания выявленного объекта культурного наследия)", "90b15e1b6f4fc7767ac119554516454738020d3c0affd1778b30bc702a2ee20e", "47335"),
}
MAINTENANCE_ONLY_TITLE = re.compile(
    r"^(?:ремонт фасада дома|реставрация (?:фасада|здания(?: (?:выявленного )?объекта культурного наследия)?))$",
    re.IGNORECASE,
)
MIXED_NEW_BUILD = re.compile(r"\b(?:новое\s+строительство|строительство\s+нового|нового\s+корпуса)\b", re.IGNORECASE)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _maintenance_purpose(row: dict[str, Any]) -> bool:
    name = " ".join(str(row.get("name") or "").split())
    if MIXED_NEW_BUILD.search(name):
        return False
    audited = AUDITED_MAINTENANCE.get(row["registry_id"])
    if audited and (name, row["provenance"]["source_report_sha256"], str(row.get("source_row_id"))) == audited:
        return True
    return MAINTENANCE_ONLY_TITLE.fullmatch(name) is not None


def decide(row: dict[str, Any], evidence: list[dict[str, Any]], *, as_of: date) -> dict[str, Any]:
    category = row["provenance"]["source_category"]
    source_date = row.get("latest_known_date")
    if not source_date:
        raise ValueError("source observation date missing")
    observed_date = date.fromisoformat(source_date[:10])
    if observed_date > as_of:
        raise ValueError("future source observation date")
    maintenance = _maintenance_purpose(row)
    if category not in TARGET_CATEGORIES:
        # Category is an explicit typed field; do not parse a vague project name.
        reasons = ["outside_new_build_category"]
        if (as_of - observed_date).days > 365:
            reasons.append("source_observation_older_than_365d")
        return {"registry_id": row["registry_id"], "visibility": "archived",
                "reason_code": reasons[0], "reason_codes": reasons,
                "source_category": category,
                "source_observed_at": source_date, "evidence_kind": "source_category",
                "evidence_url": None, "evidence_as_of": source_date,
                "status_observed_at": source_date[:10],
                "purpose_evidence": None,
                "source_snapshot_id": row["provenance"]["source_report_sha256"]}
    selected = None
    for item in evidence:
        if item["event_kind"] not in TERMINAL_EVENTS | REVIEW_EVENTS:
            raise ValueError(f"unknown event_kind: {item['event_kind']}")
        if item["registry_id"] != row["registry_id"]:
            raise ValueError("evidence registry_id mismatch")
        if not item.get("official_source_url") or not item.get("official_document_id"):
            raise ValueError("official evidence URL and document ID required")
        if item.get("identity_scope") != "exact_corpus_or_project":
            raise ValueError("exact project identity evidence required")
        observed = date.fromisoformat(item["evidence_as_of"][:10])
        if observed > as_of:
            raise ValueError("future evidence date")
        if selected is None or (item["evidence_as_of"], item["official_document_id"]) > (
                selected["evidence_as_of"], selected["official_document_id"]):
            selected = item
    # An exact, later official activity report advances the *project* status
    # observation. A separate dated OSM coordinate observation never does.
    selected_date = date.fromisoformat(selected["evidence_as_of"][:10]) if selected else None
    status_date = (max(observed_date, selected_date)
                   if selected and selected["event_kind"] == "project_active" else observed_date)
    archived_by_age = (as_of - status_date).days > 365
    archived_by_event = (selected is not None and selected["event_kind"] in TERMINAL_EVENTS
                         and selected_date >= observed_date)
    archived = archived_by_event or archived_by_age or maintenance
    reasons = (["maintenance_only"] if maintenance else []) + (
        [selected["event_kind"]] if archived_by_event else []) + (
        ["source_observation_older_than_365d"] if archived_by_age else [])
    if not reasons:
        reasons = (["terminal_status_unverified"] if selected and selected["event_kind"] in REVIEW_EVENTS | TERMINAL_EVENTS
                   else ["source_observation_only"])
    return {"registry_id": row["registry_id"], "visibility": "archived" if archived else "visible",
            "reason_code": reasons[0], "reason_codes": reasons,
            "source_category": category, "source_observed_at": source_date,
            "evidence_kind": selected["event_kind"] if selected else "source_project_purpose" if maintenance else "age_policy" if archived_by_age else None,
            "evidence_url": selected["official_source_url"] if selected else None,
            "evidence_as_of": selected["evidence_as_of"] if selected else source_date if archived_by_age or maintenance else None,
            "status_observed_at": status_date.isoformat(),
            "purpose_evidence": {"source_name": row["name"], "source_row_id": row.get("source_row_id"),
                                 "source_report_sha256": row["provenance"]["source_report_sha256"]}
                                if maintenance else None,
            "source_snapshot_id": row["provenance"]["source_report_sha256"]}


def build(root: Path = ROOT, *, evidence_path: Path | None = None,
          as_of: date | None = None, source_path: Path | None = None,
          source_manifest_path: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    as_of = date.today() if as_of is None else as_of
    source = source_path or root / "data/property/estateline/projects.jsonl"
    source_manifest = source_manifest_path or root / "data/property/estateline/source_manifest.json"
    if evidence_path is None:
        evidence_path = root / "data/property/estateline/visibility/official_events.jsonl"
    evidence_rows = _rows(evidence_path) if evidence_path.is_file() else []
    source_rows = _rows(source)
    known = {row["registry_id"] for row in source_rows}
    if len(known) != len(source_rows):
        raise ValueError("duplicate registry source identity")
    by_id: dict[str, list[dict[str, Any]]] = {}
    seen_events: set[tuple[str, str, str]] = set()
    for item in evidence_rows:
        key = (item["registry_id"], item["event_kind"], item["official_document_id"])
        if key in seen_events:
            raise ValueError("duplicate official event")
        seen_events.add(key)
        if item["registry_id"] not in known:
            raise ValueError("official event references unknown registry ID")
        by_id.setdefault(item["registry_id"], []).append(item)
    records = [decide(row, by_id.get(row["registry_id"], []), as_of=as_of) for row in source_rows]
    records.sort(key=lambda row: row["registry_id"])
    lines = b"".join(_canonical(record) for record in records)
    record_sha = hashlib.sha256(lines).hexdigest()
    source_manifest_sha = sha256(source_manifest)
    fingerprint = hashlib.sha256(_canonical({"source_sha256": sha256(source),
                                             "source_manifest_sha256": source_manifest_sha,
                                             "evidence_sha256": sha256(evidence_path) if evidence_path.is_file() else None,
                                             "rules": RULES_VERSION, "records_sha256": record_sha})).hexdigest()[:16]
    run_id = f"visibility-v1-{fingerprint}"
    output_root = root / "data/property/estateline/visibility/runs" / run_id
    future_transitions = [date.fromisoformat(record["status_observed_at"]) + timedelta(days=366)
                          for record in records if date.fromisoformat(record["status_observed_at"])
                          + timedelta(days=366) > as_of]
    manifest = {"schema_version": "registry-visibility/1", "run_id": run_id,
                "rules_version": RULES_VERSION, "source_sha256": sha256(source),
                "source_manifest_sha256": source_manifest_sha,
                "evidence_sha256": sha256(evidence_path) if evidence_path.is_file() else None,
                "records_sha256": record_sha, "records_total": len(records),
                "decisions": dict(Counter(record["visibility"] for record in records)),
                "reason_counts": dict(Counter(record["reason_code"] for record in records)),
                "reason_occurrences": dict(Counter(code for record in records for code in record["reason_codes"])),
                "as_of_date": as_of.isoformat(), "max_age_days": 365,
                "age_rule": "archive if project status observation age >365 days; no file-mtime inference",
                "next_refresh_due": min(future_transitions).isoformat() if future_transitions else None,
                "rights": "private evidence metadata; no public raw source URL projection"}
    if output_root.exists():
        if not (output_root / "manifest.json").is_file() or not (output_root / "records.jsonl").is_file():
            active_path = root / "data/property/estateline/visibility/active.json"
            if active_path.is_file() and read_json(active_path).get("run_id") == run_id:
                raise ValueError("active immutable visibility run is incomplete")
            from pipeline.maintenance.refresh import _active_generation

            generation = _active_generation(root)
            if generation and generation["bundle"]["visibility"]["run_id"] == run_id:
                raise ValueError("active immutable visibility run is incomplete")
            quarantine = output_root.with_name(f".incomplete-{run_id}")
            if quarantine.exists():
                raise ValueError("incomplete visibility run quarantine already exists")
            output_root.rename(quarantine)
        else:
            existing = read_json(output_root / "manifest.json")
            if sha256(output_root / "records.jsonl") != manifest["records_sha256"] or any(
                existing.get(key) != value for key, value in manifest.items() if key != "as_of_date"):
                raise ValueError("immutable visibility run mismatch")
            if date.fromisoformat(existing["as_of_date"]) > as_of:
                raise ValueError("visibility evaluation predates immutable run")
            return existing
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=output_root.parent))
    try:
        atomic_bytes(staged / "records.jsonl", lines)
        write_json(staged / "manifest.json", manifest)
        staged.rename(output_root)
    finally:
        if staged.exists():
            import shutil
            shutil.rmtree(staged)
    return manifest


def age_preview(root: Path = ROOT, *, as_of: date | None = None) -> dict[str, Any]:
    as_of = date.today() if as_of is None else as_of
    rows = _rows(root / "data/property/estateline/projects.jsonl")
    ages = [(as_of - date.fromisoformat(row["latest_known_date"][:10])).days for row in rows
            if row.get("latest_known_date")]
    return {"as_of": as_of.isoformat(), "source_rows": len(rows), "dated_rows": len(ages),
            "age_at_least_days": {str(days): sum(age >= days for age in ages) for days in (90, 180, 365)},
            "policy": "preview_only; age does not archive a project"}


def status(root: Path = ROOT, *, as_of: date | None = None,
           source_path: Path | None = None, source_manifest_path: Path | None = None,
           run_pointer: dict[str, Any] | None = None) -> dict[str, Any]:
    as_of = date.today() if as_of is None else as_of
    active = root / "data/property/estateline/visibility/active.json"
    if run_pointer is None and not active.is_file():
        return {"state": "stale_derived", "reason": "visibility_pointer_missing"}
    pointer = run_pointer or read_json(active)
    run = root / "data/property/estateline/visibility/runs" / pointer["run_id"]
    manifest_path = run / "manifest.json"
    if not manifest_path.is_file() or sha256(manifest_path) != pointer["manifest_sha256"]:
        return {"state": "invalid_active", "reason": "visibility_manifest_hash_mismatch"}
    manifest = read_json(manifest_path)
    if manifest.get("rules_version") != RULES_VERSION:
        return {"state": "stale_derived", "reason": "visibility_rules_changed", "run_id": pointer["run_id"]}
    source = source_path or root / "data/property/estateline/projects.jsonl"
    if sha256(source) != manifest["source_sha256"]:
        return {"state": "stale_derived", "reason": "registry_source_changed", "run_id": pointer["run_id"]}
    source_manifest = source_manifest_path or root / "data/property/estateline/source_manifest.json"
    if sha256(source_manifest) != manifest["source_manifest_sha256"]:
        return {"state": "stale_derived", "reason": "registry_manifest_changed", "run_id": pointer["run_id"]}
    evidence = root / "data/property/estateline/visibility/official_events.jsonl"
    if (sha256(evidence) if evidence.is_file() else None) != manifest["evidence_sha256"]:
        return {"state": "stale_derived", "reason": "official_evidence_changed", "run_id": pointer["run_id"]}
    due = manifest.get("next_refresh_due")
    if due and as_of >= date.fromisoformat(due):
        return {"state": "stale_derived", "reason": "age_transition_due", "run_id": pointer["run_id"]}
    if as_of < date.fromisoformat(manifest["as_of_date"]):
        return {"state": "invalid_active", "reason": "future_policy_snapshot"}
    if sha256(run / "records.jsonl") != manifest["records_sha256"]:
        return {"state": "invalid_active", "reason": "visibility_records_hash_mismatch"}
    return {"state": "current", "run_id": pointer["run_id"],
            "decisions": manifest["decisions"], "as_of_date": manifest["as_of_date"],
            "next_refresh_due": due}


def publish(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    run = root / "data/property/estateline/visibility/runs" / manifest["run_id"]
    if read_json(run / "manifest.json") != manifest:
        raise ValueError("visibility manifest changed before publication")
    if sha256(run / "records.jsonl") != manifest["records_sha256"]:
        raise ValueError("visibility records changed before publication")
    if sha256(root / "data/property/estateline/projects.jsonl") != manifest["source_sha256"]:
        raise ValueError("registry source changed before visibility publication")
    rows = _rows(run / "records.jsonl")
    source_ids = {row["registry_id"] for row in _rows(root / "data/property/estateline/projects.jsonl")}
    if len(rows) != manifest["records_total"] or {row["registry_id"] for row in rows} != source_ids:
        raise ValueError("visibility sidecar is not 1:1 with accepted registry")
    pointer = {"run_id": manifest["run_id"], "manifest_sha256": sha256(run / "manifest.json")}
    write_json(root / "data/property/estateline/visibility/active.json", pointer)
    return pointer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "age-preview", "publish"))
    args = parser.parse_args()
    if args.command == "publish":
        with exclusive_lock(ROOT):
            result = publish(ROOT, build())
    else:
        result = build() if args.command == "stage" else age_preview()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
