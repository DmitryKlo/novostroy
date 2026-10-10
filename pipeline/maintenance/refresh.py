"""Validate local snapshots and refresh only changed, approved dependencies.

This module never fetches a source. A source which is missing, older, or only
partly imported leaves the last validated product selected. The CLI uses one
non-blocking process lock for refresh and retention operations.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
ESTATE_FILES = ("projects.jsonl", "review_required.jsonl", "schema.json", "source_manifest.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


@contextlib.contextmanager
def exclusive_lock(root: Path) -> Iterator[None]:
    path = root / "data/runtime/refresh/refresh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another refresh or retention operation is running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _identity(rows: list[dict[str, Any]]) -> set[str]:
    identifiers = [row["registry_id"] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate registry_id in normalized snapshot")
    return set(identifiers)


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _estate_inventory(root: Path) -> dict[str, Any]:
    normalized = _snapshot_for_active(root)
    manifest_path = normalized / "source_manifest.json"
    if not manifest_path.is_file():
        return {"state": "input_unavailable", "reason": "normalized_manifest_missing"}
    generation = _active_generation(root)
    if generation and sha256(manifest_path) != generation["bundle"]["normalized_snapshot"]["source_manifest_sha256"]:
        return {"state": "invalid_active", "reason": "generation_source_manifest_hash_mismatch"}
    manifest = read_json(manifest_path)
    raw = root / "../Загрузки/1_xls_estateline"
    expected = {row["path"]: row for row in manifest["source_files"]}
    present = {path.name: path for pattern in ("moskva-*.xlsx", "moskovskaya-obl-*.xlsx")
               for path in raw.glob(pattern)}
    missing = sorted(expected.keys() - present.keys())
    if missing:
        return {"state": "input_unavailable", "reason": "source_files_missing",
                "missing_count": len(missing), "sample": missing[:3],
                "snapshot_at": manifest.get("snapshot_at")}
    observed = {name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                for name, path in sorted(present.items())}
    changed = sorted(name for name, item in observed.items()
                     if name not in expected or item["bytes"] != expected[name]["bytes"]
                     or item["sha256"] != expected[name]["sha256"])
    inventory_hash = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
    outputs = manifest["outputs"]
    output_errors = sorted(key for key in ("projects", "review_required", "schema")
                           if not (normalized / outputs[key]["path"]).is_file()
                           or sha256(normalized / outputs[key]["path"]) != outputs[key]["sha256"])
    return {"state": "invalid_active" if output_errors else "changed" if changed else "current",
            "snapshot_at": manifest.get("snapshot_at"), "raw_files": len(present),
            "raw_inventory_sha256": inventory_hash,
            "changed_count": len(changed), "changed_sample": changed[:5],
            "output_errors": output_errors, "rights_status": manifest.get("rights_status")}


def _model_status(root: Path) -> dict[str, Any]:
    from sources.models import build as model_build

    generation = _active_generation(root)
    pointer = root / "data/models/active.json"
    if generation is None and not pointer.is_file():
        return {"state": "input_unavailable", "reason": "active_pointer_missing"}
    active = generation["bundle"]["model"] if generation else read_json(pointer)
    manifest_path = root / "data/models" / active["run_id"] / "manifest.json"
    if not manifest_path.is_file() or sha256(manifest_path) != active["manifest_sha256"]:
        return {"state": "invalid_active", "reason": "active_manifest_hash_mismatch"}
    manifest = read_json(manifest_path)
    public_manifest = root / "assets" / manifest["public_root"] / "public_manifest.json"
    if not public_manifest.is_file() or sha256(public_manifest) != manifest["public_manifest_sha256"]:
        return {"state": "invalid_active", "reason": "public_model_manifest_hash_mismatch"}
    snapshot = _snapshot_for_active(root)
    selected_inputs = {key: (snapshot / "projects.jsonl" if key == "estateline_projects" else
                             snapshot / "review_required.jsonl" if key == "estateline_review" else
                             root / path.relative_to(model_build.ROOT))
                       for key, path in model_build.INPUTS.items()}
    missing = [key for key, path in selected_inputs.items() if not path.is_file()]
    if missing:
        return {"state": "input_unavailable", "missing": missing, "run_id": active["run_id"]}
    changes = []
    for key, selected in selected_inputs.items():
        if sha256(selected) != manifest["input_hashes"].get(key):
            changes.append(key)
    return {"state": "stale_derived" if changes else "current", "run_id": active["run_id"],
            "input_changed": changes, "checked_at": manifest.get("checked_at"),
            "records_total": manifest.get("records_total")}


def _nearby_status(root: Path) -> dict[str, Any]:
    social = root / "data/social"
    poi = social / "osm_moscow_social_pois_261008.parquet"
    poi_manifest = social / "osm_moscow_social_pois_261008.manifest.json"
    graph = social / "valhalla_moscow/build_manifest.json"
    graph_config = social / "valhalla_moscow/valhalla_pedestrian.json"
    if not all(path.is_file() for path in (poi, poi_manifest, graph, graph_config)):
        return {"state": "input_unavailable", "reason": "poi_or_graph_missing"}
    doc = read_json(poi_manifest)
    poi_valid = sha256(poi) == doc["output"]["sha256"]
    evidence = social / "oms_registry_records_2026.json"
    oms_manifest = social / "oms_verified_clinics_2026.manifest.json"
    oms = social / "oms_verified_clinics_2026.json"
    if not poi_valid:
        return {"state": "invalid_active", "reason": "poi_hash_mismatch"}
    if not evidence.is_file() or not oms_manifest.is_file() or not oms.is_file():
        return {"state": "input_unavailable", "reason": "oms_evidence_or_sidecar_missing"}
    sidecar = read_json(oms_manifest)
    stale = (sidecar["source_manifest_sha256"] != sha256(poi_manifest)
             or sidecar["evidence_sha256"] != sha256(evidence))
    if sidecar["output_sha256"] != sha256(oms):
        return {"state": "invalid_active", "reason": "oms_hash_mismatch"}
    return {"state": "stale_derived" if stale else "current", "poi_rows": doc["output"].get("row_count"),
            "oms_rows": sidecar["records_total"], "oms_refresh_configured": True,
            "graph_state": "present_not_rebuilt_by_refresh"}


def _pinned_sources(root: Path) -> dict[str, dict[str, Any]]:
    """Check source and output hashes without pretending a new feed exists."""
    result: dict[str, dict[str, Any]] = {}
    osm_path = root / "data/property/osm_construction/source_manifest.json"
    if not osm_path.is_file():
        result["osm_construction"] = {"state": "input_unavailable"}
    else:
        osm = read_json(osm_path)
        outputs = ((osm["output"]["geoparquet"], osm["output"]["geoparquet_sha256"]),
                   (osm["output"]["audit_jsonl"], osm["output"]["audit_sha256"]))
        invalid = [name for name, digest in outputs if not (root / name).is_file() or sha256(root / name) != digest]
        raw = root / osm["input"]["file"]
        raw_changed = raw.is_file() and sha256(raw) != osm["input"]["sha256"]
        result["osm_construction"] = {
            "state": "invalid_active" if invalid else "input_changed_needs_versioned_import" if raw_changed
                     else "input_unavailable" if not raw.is_file() else "current",
            "source_snapshot_at": osm["source_snapshot_at"],
            "rows": osm["output"]["geoparquet_row_count"], "output_errors": invalid,
            "refresh_hook": "not_configured_versioned_import"}
    min_path = root / "data/property/minstroy/source_manifest.json"
    if not min_path.is_file():
        result["minstroy_events"] = {"state": "input_unavailable"}
    else:
        min_doc = read_json(min_path)
        # The current Minstroy builder records audit.json's previous hash before
        # rewriting audit.json with its own output inventory. A self-hash is not
        # representable; validate the actual model input and other outputs.
        outputs = [(root / "data/property/minstroy" / row["path"], row["sha256"])
                   for key, row in min_doc["normalized_outputs"].items() if key != "audit"]
        invalid = [str(path.relative_to(root)) for path, digest in outputs
                   if not path.is_file() or sha256(path) != digest]
        raw_changed = []
        missing = []
        for dataset in min_doc["datasets"]:
            path = root / dataset["local_raw_path"]
            if not path.is_file():
                missing.append(dataset["source_id"])
            elif sha256(path) != dataset["sha256"]:
                raw_changed.append(dataset["source_id"])
        result["minstroy_events"] = {
            "state": "invalid_active" if invalid else "input_unavailable" if missing
                     else "input_changed_needs_published_version" if raw_changed else "current",
            "published_version_dates": [row["published_version_date"] for row in min_doc["datasets"]],
            "output_errors": invalid, "raw_changed": raw_changed, "missing": missing,
            "audit_metadata": "self_hash_excluded_from_gate",
            "refresh_hook": "reimport_requires_version_metadata"}
    return result


def status(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    from pipeline.maintenance.visibility import status as visibility_status

    estate = _estate_inventory(root)
    if estate.get("state") == "changed" and estate.get("raw_inventory_sha256"):
        pending = root / "data/runtime/refresh/pending" / f"estate-{estate['raw_inventory_sha256'][:16]}.json"
        if pending.is_file():
            estate["pending_generation"] = pending.relative_to(root).as_posix()
    models = _model_status(root) if root == ROOT else {"state": "not_configured", "reason": "fixture_root"}
    nearby = _nearby_status(root) if root == ROOT else {"state": "not_configured", "reason": "fixture_root"}
    pinned = _pinned_sources(root) if root == ROOT else {}
    generation = _active_generation(root)
    visibility = visibility_status(root,
        source_path=_snapshot_for_active(root) / "projects.jsonl",
        source_manifest_path=_snapshot_for_active(root) / "source_manifest.json",
        run_pointer=generation["bundle"]["visibility"] if generation else None
    ) if (_snapshot_for_active(root) / "projects.jsonl").is_file() else {
        "state": "input_unavailable", "reason": "registry_projects_missing"}
    assets = read_json(root / "assets/manifest.json") if (root / "assets/manifest.json").is_file() else {}
    return {"schema_version": 1, "mode": "offline_local_approved_sources", "root": str(root),
            "dependencies": {
                "registry": estate,
                "registry_visibility": visibility,
                "model_assessments": models,
                "nearby_poi_graph_oms": nearby,
                **pinned,
                "city_ground_roads": {"state": "pinned_snapshot", "refresh_hook": "not_configured_heavy_asset_build"},
                "market_feed": {"state": "input_unavailable", "reason": "authorized_feed_not_configured"},
                "collection": {"state": "external_checkpointed_runner", "network": "not_invoked"},
            },
            "active_assets": {key: assets.get(key) for key in
                              ("city_tileset_url", "ground_tileset_url", "roads_tileset_url", "project_models_root")},
            "actions": (["stage_registry_reimport", "validate_registry", "stage_model_and_visibility",
                         "commit_generation_pointer"]
                        if estate["state"] == "changed" and not estate.get("pending_generation") else
                        ["pending_generation_retained"] if estate.get("pending_generation") else
                        ["build_audit_publish_models"] if models["state"] == "stale_derived" else [])
                       + (["refresh_visibility"] if visibility["state"] == "stale_derived" and estate["state"] != "changed" else [])
                       + (["refresh_oms_sidecar"] if nearby["state"] == "stale_derived" else []),
            "blocking_states": [key for key, value in (("registry", estate), ("model_assessments", models),
                                                      ("nearby", nearby),
                                                      ("osm_construction", pinned.get("osm_construction", {})),
                                                      ("minstroy_events", pinned.get("minstroy_events", {})))
                                if value["state"] in {"invalid_active", "input_unavailable"}]}


def _validate_estate(stage: Path, active: Path) -> dict[str, Any]:
    new, old = read_json(stage / "source_manifest.json"), read_json(active / "source_manifest.json")
    if new["source_files_parsed"] != new["source_files_total"] or new["parse_errors"]:
        raise ValueError("partial source parse; old snapshot retained")
    if new["rights_status"] != old["rights_status"]:
        raise ValueError("source rights changed; manual review required")
    if _date(new["snapshot_at"]) < _date(old["snapshot_at"]):
        raise ValueError("source snapshot moved backwards")
    new_rows = _rows(stage / "projects.jsonl")
    new_review = _rows(stage / "review_required.jsonl")
    old_rows = _rows(active / "projects.jsonl")
    old_review = _rows(active / "review_required.jsonl")
    old_ids = _identity(old_rows) | _identity(old_review)
    new_ids = _identity(new_rows) | _identity(new_review)
    if len(new_rows) + len(new_review) != len(new_ids):
        raise ValueError("duplicate registry_id across buckets")
    missing = sorted(old_ids - new_ids)
    if missing:
        write_json(stage / "stale_records.json", {"source_snapshot_at": new["snapshot_at"],
                                                   "unresolved_prior_ids": missing})
        raise ValueError(f"{len(missing)} prior source IDs disappeared; review stale_records.json")
    for name in ESTATE_FILES[:-1]:
        if sha256(stage / name) != new["outputs"]["review_required" if name == "review_required.jsonl"
                                             else "projects" if name == "projects.jsonl"
                                             else "schema"]["sha256"]:
            raise ValueError(f"staged checksum mismatch: {name}")
    return {"projects": len(new_rows), "review": len(new_review), "new_ids": len(new_ids - old_ids),
            "snapshot_at": new["snapshot_at"]}


def _restore(journal: Path, root: Path) -> None:
    meta = read_json(journal / "journal.json")
    for item in meta["files"]:
        target = root / item["path"]
        backup = journal / item["backup"]
        if item["existed"]:
            atomic_bytes(target, backup.read_bytes())
        else:
            target.unlink(missing_ok=True)
    write_json(journal / "state.json", {"status": "rolled_back"})


def _transaction(root: Path, staged: dict[Path, Path], *, model: dict[str, Any] | None = None,
                 visibility: dict[str, Any] | None = None) -> None:
    from sources.models.publish import publish

    runtime = root / "data/runtime/refresh"
    runtime.mkdir(parents=True, exist_ok=True)
    journal = Path(tempfile.mkdtemp(prefix="journal-", dir=runtime))
    items = []
    for i, target in enumerate(sorted(staged, key=str)):
        relative = target.relative_to(root).as_posix()
        backup = f"backup-{i}"
        existed = target.exists()
        if existed:
            (journal / backup).write_bytes(target.read_bytes())
        items.append({"path": relative, "backup": backup, "existed": existed})
    if model is not None:
        for target in (root / "assets/manifest.json", root / "data/models/active.json"):
            backup = f"backup-{len(items)}"
            (journal / backup).write_bytes(target.read_bytes())
            items.append({"path": target.relative_to(root).as_posix(), "backup": backup, "existed": True})
    if visibility is not None:
        target = root / "data/property/estateline/visibility/active.json"
        backup = f"backup-{len(items)}"
        existed = target.exists()
        if existed:
            (journal / backup).write_bytes(target.read_bytes())
        items.append({"path": target.relative_to(root).as_posix(), "backup": backup, "existed": existed})
    write_json(journal / "journal.json", {"files": items, "created_at": datetime.now(timezone.utc).isoformat()})
    write_json(journal / "state.json", {"status": "applying"})
    try:
        for target, source in staged.items():
            atomic_bytes(target, source.read_bytes())
        if model is not None:
            publish(model["run_id"], model["manifest_sha256"], root / "data/models", root / "assets")
        if visibility is not None:
            from pipeline.maintenance.visibility import publish as publish_visibility

            publish_visibility(root, visibility)
        write_json(journal / "state.json", {"status": "committed",
                                               "after_sha256": {item["path"]: sha256(root / item["path"])
                                                                for item in items}})
    except BaseException:
        _restore(journal, root)
        raise


def recover_incomplete(root: Path) -> int:
    count = 0
    for journal in (root / "data/runtime/refresh").glob("journal-*"):
        if (journal / "journal.json").is_file() and read_json(journal / "state.json").get("status") == "applying":
            _restore(journal, root)
            count += 1
    return count


def rollback_last(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    with exclusive_lock(root):
        journals = sorted((path for path in (root / "data/runtime/refresh").glob("journal-*")
                           if (path / "state.json").is_file()
                           and read_json(path / "state.json").get("status") == "committed"),
                          key=lambda path: path.stat().st_mtime, reverse=True)
        if not journals:
            raise ValueError("no committed refresh transaction available for rollback")
        journal = journals[0]
        state = read_json(journal / "state.json")
        for rel, expected in state["after_sha256"].items():
            target = root / rel
            if not target.is_file() or sha256(target) != expected:
                raise ValueError(f"current file changed since transaction: {rel}")
        _restore(journal, root)
        return {"rolled_back": journal.name, "restored_files": len(state["after_sha256"])}


def _record_published(root: Path, paths: list[Path]) -> None:
    """Track only generations this refresh command itself published."""
    ledger = root / "data/runtime/refresh/published.jsonl"
    existing = ledger.read_bytes() if ledger.is_file() else b""
    known = {json.loads(line)["path"] for line in existing.splitlines() if line.strip()}
    now = datetime.now(timezone.utc).timestamp()
    additions = b"".join((json.dumps({"path": path.relative_to(root).as_posix(),
                                      "published_epoch": now}, sort_keys=True) + "\n").encode()
                         for path in paths if path.relative_to(root).as_posix() not in known)
    if additions:
        atomic_bytes(ledger, existing + additions)


def _verify_pending(root: Path, path: Path, inventory_hash: str) -> dict[str, Any]:
    pending = read_json(path)
    if pending.get("raw_inventory_sha256") != inventory_hash:
        raise ValueError("pending registry generation has a different raw inventory")
    snapshot = root / pending["normalized_snapshot"]
    model = root / "data/models" / pending["model_run_id"] / "manifest.json"
    visibility = root / "data/property/estateline/visibility/runs" / pending["visibility_run_id"] / "manifest.json"
    for item, digest in ((snapshot / "source_manifest.json", pending["normalized_manifest_sha256"]),
                         (model, pending["model_manifest_sha256"]),
                         (visibility, pending["visibility_manifest_sha256"]),
                         (root / pending["bundle_path"], pending["bundle_sha256"])):
        if not item.is_file() or sha256(item) != digest:
            raise ValueError(f"pending generation changed: {item}")
    active = _active_generation(root)
    active_source = _snapshot_for_active(root) / "projects.jsonl"
    if sha256(active_source) != pending["active_registry_source_sha256"]:
        raise ValueError("active registry changed since pending generation was built")
    if (active["bundle_sha256"] if active else None) != pending.get("expected_previous_bundle_sha256"):
        raise ValueError("active generation changed since pending generation was built")
    return pending


def build_generation_bundle(root: Path, snapshot: Path, model_run_id: str,
                            visibility_run_id: str) -> dict[str, Any]:
    """Pin one already-audited source/model/visibility set without publishing."""
    normalized_root = root / "data/property/estateline/normalized"
    if (snapshot.parent != normalized_root / "snapshots"
            or not re.fullmatch(r"estate-[a-f0-9]{16}", snapshot.name)
            or snapshot.is_symlink()):
        raise ValueError("normalized snapshot must be an immutable local estate snapshot")
    if not re.fullmatch(r"models-v1-[a-f0-9]{16}", model_run_id):
        raise ValueError("invalid model run ID")
    if not re.fullmatch(r"visibility-v1-[a-f0-9]{16}", visibility_run_id):
        raise ValueError("invalid visibility run ID")
    model_path = root / "data/models" / model_run_id / "manifest.json"
    visibility_path = root / "data/property/estateline/visibility/runs" / visibility_run_id / "manifest.json"
    model = read_json(model_path)
    visibility = read_json(visibility_path)
    source_hash = sha256(snapshot / "projects.jsonl")
    manifest_hash = sha256(snapshot / "source_manifest.json")
    source_manifest = read_json(snapshot / "source_manifest.json")
    for key, filename in (("projects", "projects.jsonl"), ("review_required", "review_required.jsonl"),
                          ("schema", "schema.json")):
        if sha256(snapshot / filename) != source_manifest["outputs"][key]["sha256"]:
            raise ValueError(f"normalized snapshot output drift: {filename}")
    if model["input_hashes"]["estateline_projects"] != source_hash:
        raise ValueError("model run does not match source generation")
    if model["registry_snapshot_id"] != f"building-registry-{source_hash[:16]}":
        raise ValueError("model registry snapshot ID mismatch")
    if visibility["source_sha256"] != source_hash or visibility["source_manifest_sha256"] != manifest_hash:
        raise ValueError("visibility run does not match source generation")
    visibility_records = (visibility_path.parent / "records.jsonl")
    if sha256(visibility_records) != visibility["records_sha256"]:
        raise ValueError("visibility records drift")
    public_manifest = root / "assets" / model["public_root"] / "public_manifest.json"
    if not re.fullmatch(r"public-project-models-v1-[a-f0-9]{16}", model["public_root"]):
        raise ValueError("invalid model public root")
    if sha256(public_manifest) != model["public_manifest_sha256"]:
        raise ValueError("public model asset manifest drift")
    public_index = read_json(public_manifest)
    if public_index.get("public_redacted_source_ids") is not True:
        raise ValueError("public model asset root is not sanitized")
    listed = set()
    for item in public_index.get("files", []):
        name = item["path"]
        if (not re.fullmatch(r"model_[a-f0-9]{64}\.(?:glb|tileset\.json)", name)
                or name in listed):
            raise ValueError("invalid or duplicate public model asset name")
        listed.add(name)
        path = public_manifest.parent / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise ValueError(f"public model asset drift: {name}")
    core = {
        "schema_version": "estate-generation/1",
        "normalized_snapshot": {
            "path": snapshot.relative_to(root).as_posix(),
            "projects_sha256": source_hash,
            "source_manifest_sha256": manifest_hash,
            "review_required_sha256": sha256(snapshot / "review_required.jsonl"),
            "schema_sha256": sha256(snapshot / "schema.json"),
        },
        "source_snapshot_id": f"building-registry-{source_hash[:16]}",
        "model": {"run_id": model_run_id, "manifest_sha256": sha256(model_path),
                  "public_root": model["public_root"],
                  "public_manifest_sha256": model["public_manifest_sha256"]},
        "visibility": {"run_id": visibility_run_id, "manifest_sha256": sha256(visibility_path),
                       "records_sha256": visibility["records_sha256"]},
    }
    encoded = (json.dumps(core, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    bundle_hash = hashlib.sha256(encoded).hexdigest()
    generation_id = "generation-" + bundle_hash[:16]
    bundle_path = normalized_root / "generations" / generation_id / "bundle.json"
    if bundle_path.is_file():
        if sha256(bundle_path) != bundle_hash:
            raise ValueError("immutable generation bundle differs")
    else:
        atomic_bytes(bundle_path, encoded)
    return {"generation_id": generation_id, "bundle_path": bundle_path.relative_to(root).as_posix(),
            "bundle_sha256": bundle_hash}


def _active_generation(root: Path) -> dict[str, Any] | None:
    """Resolve exactly one committed generation; invalid pointers never fall back."""
    pointer_path = root / "data/property/estateline/normalized/active.json"
    if not pointer_path.is_file():
        return None
    pointer = read_json(pointer_path)
    if pointer.get("schema_version") != "estate-generation-pointer/1":
        raise ValueError("invalid estate generation pointer schema")
    digest = pointer.get("bundle_sha256", "")
    generation_id = pointer.get("generation_id", "")
    if len(digest) != 64 or generation_id != f"generation-{digest[:16]}":
        raise ValueError("invalid estate generation pointer identity")
    bundle_path = (root / "data/property/estateline/normalized/generations"
                   / generation_id / "bundle.json")
    if not bundle_path.is_file() or sha256(bundle_path) != digest:
        raise ValueError("active estate generation bundle hash mismatch")
    bundle = read_json(bundle_path)
    if bundle.get("schema_version") != "estate-generation/1":
        raise ValueError("invalid estate generation bundle schema")
    expected_snapshot = ("data/property/estateline/normalized/snapshots/estate-"
                         + bundle["normalized_snapshot"]["source_manifest_sha256"][:16])
    if bundle["normalized_snapshot"].get("path") != expected_snapshot:
        raise ValueError("invalid estate generation snapshot path")
    return {**pointer, "bundle": bundle, "bundle_path": bundle_path}


def _snapshot_for_active(root: Path) -> Path:
    generation = _active_generation(root)
    return (root / generation["bundle"]["normalized_snapshot"]["path"] if generation
            else root / "data/property/estateline")


def stage_bootstrap_generation(root: Path = ROOT) -> dict[str, Any]:
    """Copy currently validated legacy files once; leave legacy readers untouched."""
    root = root.resolve()
    if _active_generation(root) is not None:
        raise ValueError("estate generation already active")
    estate = root / "data/property/estateline"
    source_manifest = read_json(estate / "source_manifest.json")
    model_pointer = read_json(root / "data/models/active.json")
    visibility_pointer = read_json(estate / "visibility/active.json")
    snapshot = (estate / "normalized/snapshots"
                / f"estate-{sha256(estate / 'source_manifest.json')[:16]}")
    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".estate-bootstrap-", dir=snapshot.parent))
        try:
            for name in ESTATE_FILES:
                shutil.copyfile(estate / name, temporary / name)
            temporary.rename(snapshot)
            directory_fd = os.open(snapshot.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    for name in ESTATE_FILES:
        if sha256(snapshot / name) != sha256(estate / name):
            raise ValueError(f"bootstrap immutable snapshot drift: {name}")
    if source_manifest["source_files_parsed"] != source_manifest["source_files_total"]:
        raise ValueError("bootstrap source import incomplete")
    generation = build_generation_bundle(root, snapshot, model_pointer["run_id"],
                                         visibility_pointer["run_id"])
    pending_path = root / "data/runtime/refresh/pending" / f"bootstrap-{generation['generation_id']}.json"
    write_json(pending_path, {"schema_version": "estate-generation-pending/1", "kind": "bootstrap",
                              "expected_previous_bundle_sha256": None,
                              "source_manifest_sha256": sha256(snapshot / "source_manifest.json"),
                              "raw_inventory_sha256": _estate_inventory(root)["raw_inventory_sha256"],
                              **generation})
    return {**generation, "pending_path": pending_path.relative_to(root).as_posix(),
            "state": "staged_not_active"}


def promote_generation(root: Path, pending_path: Path) -> dict[str, Any]:
    """Commit one fully verified generation with compare-and-swap pointer flip."""
    root = root.resolve()
    pending_path = pending_path.resolve()
    pending_root = (root / "data/runtime/refresh/pending").resolve()
    if pending_path.parent != pending_root:
        raise ValueError("generation must be promoted from pending directory")
    pending = read_json(pending_path)
    current = _active_generation(root)
    current_hash = current["bundle_sha256"] if current else None
    if current_hash != pending.get("expected_previous_bundle_sha256"):
        raise ValueError("active generation changed since staging")
    inventory = _estate_inventory(root)
    if inventory["raw_inventory_sha256"] != pending["raw_inventory_sha256"]:
        raise ValueError("approved local raw inventory changed since staging")
    bundle_path = root / pending["bundle_path"]
    if sha256(bundle_path) != pending["bundle_sha256"]:
        raise ValueError("pending generation bundle changed")
    bundle = read_json(bundle_path)
    snapshot = root / bundle["normalized_snapshot"]["path"]
    rebuilt = build_generation_bundle(root, snapshot, bundle["model"]["run_id"],
                                      bundle["visibility"]["run_id"])
    if rebuilt["bundle_sha256"] != pending["bundle_sha256"]:
        raise ValueError("pending source/model/visibility changed")
    if pending["kind"] == "bootstrap" and sha256(
            root / "data/property/estateline/projects.jsonl") != bundle["normalized_snapshot"]["projects_sha256"]:
        raise ValueError("legacy registry changed before bootstrap")
    # The backend accepts audited model roots named in the active assets pointer
    # or its compatibility list. This pre-allowlisting exposes the sanitized
    # versioned static files before the bundle flip; API records remain old.
    asset_pointer_path = root / "assets/manifest.json"
    asset_pointer = read_json(asset_pointer_path)
    public_root = bundle["model"]["public_root"]
    allowed = set(asset_pointer.get("previous_public_roots", []))
    allowed.add(asset_pointer.get("project_models_root"))
    if public_root not in allowed:
        asset_pointer["previous_public_roots"] = sorted(
            set(asset_pointer.get("previous_public_roots", [])) | {public_root})
        write_json(asset_pointer_path, asset_pointer)
    new_pointer = {"schema_version": "estate-generation-pointer/1",
                   "generation_id": pending["generation_id"],
                   "bundle_sha256": pending["bundle_sha256"]}
    transition = (root / "data/runtime/refresh/generation-transitions"
                  / f"{pending['bundle_sha256']}.json")
    write_json(transition, {"from_pointer": {key: current[key] for key in
                                  ("schema_version", "generation_id", "bundle_sha256")} if current else None,
                            "to_pointer": new_pointer})
    write_json(root / "data/property/estateline/normalized/active.json", new_pointer)
    _record_published(root, [snapshot, bundle_path.parent,
                             root / "data/models" / bundle["model"]["run_id"],
                             root / "assets" / public_root])
    return {**new_pointer, "previous_bundle_sha256": current_hash,
            "source_snapshot_id": bundle["source_snapshot_id"]}


def rollback_generation(root: Path, previous_pointer: dict[str, Any],
                        *, expected_current_hash: str) -> dict[str, Any]:
    """Manual CAS rollback to a retained, validated prior immutable bundle."""
    current = _active_generation(root)
    if current is None or current["bundle_sha256"] != expected_current_hash:
        raise ValueError("active generation changed; rollback refused")
    old_path = (root / "data/property/estateline/normalized/generations"
                / previous_pointer["generation_id"] / "bundle.json")
    if sha256(old_path) != previous_pointer["bundle_sha256"]:
        raise ValueError("previous generation bundle changed")
    old_bundle = read_json(old_path)
    build_generation_bundle(root, root / old_bundle["normalized_snapshot"]["path"],
                            old_bundle["model"]["run_id"], old_bundle["visibility"]["run_id"])
    write_json(root / "data/property/estateline/normalized/active.json", previous_pointer)
    return previous_pointer


def rollback_active_generation(root: Path = ROOT) -> dict[str, Any]:
    """Rollback the current pointer only to its recorded immediate predecessor."""
    current = _active_generation(root)
    if current is None:
        raise ValueError("no active generation")
    cleanup_committed_pending(root)
    transition = (root / "data/runtime/refresh/generation-transitions"
                  / f"{current['bundle_sha256']}.json")
    if not transition.is_file():
        raise ValueError("no retained predecessor transition")
    record = read_json(transition)
    previous = record["from_pointer"]
    if record["to_pointer"]["bundle_sha256"] != current["bundle_sha256"]:
        raise ValueError("generation transition does not match active pointer")
    if previous is None:
        # Bootstrap rollback restores the still-preserved legacy reader path.
        (root / "data/property/estateline/normalized/active.json").unlink()
        directory_fd = os.open(root / "data/property/estateline/normalized", os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {"rolled_back_to": "legacy_validated_files"}
    rollback_generation(root, previous, expected_current_hash=current["bundle_sha256"])
    return {"rolled_back_to": previous["generation_id"]}


def cleanup_committed_pending(root: Path) -> int:
    """Recover a stop after pointer replace but before pending-file unlink."""
    active = _active_generation(root)
    if active is None:
        return 0
    removed = 0
    for path in (root / "data/runtime/refresh/pending").glob("*.json"):
        pending = read_json(path)
        if (pending.get("bundle_sha256") != active["bundle_sha256"]
                or pending.get("generation_id") != active["generation_id"]):
            continue
        bundle_path = root / pending.get("bundle_path", "")
        if (bundle_path != active["bundle_path"] or not bundle_path.is_file()
                or sha256(bundle_path) != pending["bundle_sha256"]):
            raise ValueError("committed pending points to a changed bundle")
        path.unlink()
        removed += 1
    return removed


def run(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    if root != ROOT:
        raise ValueError("product refresh is allowed only on the repository root")
    with exclusive_lock(root):
        recovered = recover_incomplete(root)
        cleaned_pending = cleanup_committed_pending(root)
        before = status(root)
        active_generation = _active_generation(root)
        registry_blocked = before["dependencies"]["registry"]["state"] in {"invalid_active", "input_unavailable"}
        model_blocked = before["dependencies"]["model_assessments"]["state"] in {"invalid_active", "input_unavailable"}
        actions: list[str] = []
        stage: Path | None = None
        try:
            registry_state = before["dependencies"]["registry"]
            pending_path = (root / registry_state["pending_generation"]
                            if registry_state.get("pending_generation") else None)
            if pending_path is not None:
                _verify_pending(root, pending_path, registry_state["raw_inventory_sha256"])
                if active_generation is not None:
                    promote_generation(root, pending_path)
                    actions.append("pending_registry_generation_published")
                    pending_path.unlink()
                    pending_path = None
                else:
                    actions.append("pending_registry_generation_retained_until_reader_gate")
            elif not registry_blocked and registry_state["state"] == "changed":
                from sources.property.estateline import import_corpus

                stage = Path(tempfile.mkdtemp(prefix="estate-", dir=root / "data/runtime/refresh"))
                import_corpus(root / "../Загрузки/1_xls_estateline", stage,
                              root / "data/city/local_address_index")
                _validate_estate(stage, _snapshot_for_active(root))
                snapshot_hash = sha256(stage / "source_manifest.json")
                snapshot = (root / "data/property/estateline/normalized/snapshots"
                            / f"estate-{snapshot_hash[:16]}")
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                if snapshot.exists():
                    for name in ESTATE_FILES:
                        if sha256(stage / name) != sha256(snapshot / name):
                            raise ValueError("existing immutable normalized snapshot differs")
                    shutil.rmtree(stage)
                else:
                    stage.rename(snapshot)
                    directory_fd = os.open(snapshot.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                stage = None
                actions.append("registry_generation_validated_and_staged")
            pinned_inputs_valid = all(before["dependencies"].get(name, {}).get("state") == "current"
                                      for name in ("osm_construction", "minstroy_events"))
            if "registry_generation_validated_and_staged" in actions and (model_blocked or not pinned_inputs_valid):
                raise ValueError("model dependency invalid; pending registry generation not publishable")
            registry_stage = "registry_generation_validated_and_staged" in actions
            model_needed = (not model_blocked and pinned_inputs_valid and pending_path is None and
                            (registry_stage or before["dependencies"]["model_assessments"]["state"] == "stale_derived"))
            model_release = None
            if model_needed:
                from sources.models import build as model_build
                from sources.models import audit as model_audit

                original_build, original_audit = model_build.INPUTS, model_audit.INPUTS
                selected_inputs = dict(original_build)
                selected_snapshot = snapshot if registry_stage else _snapshot_for_active(root)
                selected_inputs["estateline_projects"] = selected_snapshot / "projects.jsonl"
                selected_inputs["estateline_review"] = selected_snapshot / "review_required.jsonl"
                model_build.INPUTS = selected_inputs
                model_audit.INPUTS = selected_inputs
                try:
                    manifest = model_build.build(root / "data/models", root / "assets")
                    run_dir = root / "data/models" / manifest["run_id"]
                    digest = sha256(run_dir / "manifest.json")
                    model_audit.audit(run_dir, root / "assets")
                finally:
                    model_build.INPUTS, model_audit.INPUTS = original_build, original_audit
                model_release = {"run_id": manifest["run_id"], "manifest_sha256": digest}
                actions.append("model_run_audited")
            from pipeline.maintenance.visibility import build as build_visibility

            vis_needed = (registry_stage or before["dependencies"]["registry_visibility"]["state"]
                          in {"stale_derived", "invalid_active"}) and not registry_blocked and pending_path is None
            vis_release = None
            if vis_needed:
                selected_snapshot = snapshot if registry_stage else _snapshot_for_active(root)
                vis_release = build_visibility(root,
                    source_path=selected_snapshot / "projects.jsonl",
                    source_manifest_path=selected_snapshot / "source_manifest.json")
                actions.append("visibility_run_validated")
            if registry_stage:
                if model_release is None or vis_release is None:
                    raise ValueError("staged registry lacks model or visibility candidate")
                generation = build_generation_bundle(root, snapshot, model_release["run_id"],
                                                     vis_release["run_id"])
                pending_path = (root / "data/runtime/refresh/pending"
                                / f"estate-{registry_state['raw_inventory_sha256'][:16]}.json")
                pending_doc = {
                    "schema_version": "estate-generation-pending/1", "kind": "refresh",
                    "raw_inventory_sha256": registry_state["raw_inventory_sha256"],
                    "active_registry_source_sha256": sha256(_snapshot_for_active(root) / "projects.jsonl"),
                    "expected_previous_bundle_sha256": active_generation["bundle_sha256"] if active_generation else None,
                    "normalized_snapshot": snapshot.relative_to(root).as_posix(),
                    "normalized_manifest_sha256": sha256(snapshot / "source_manifest.json"),
                    "model_run_id": model_release["run_id"],
                    "model_manifest_sha256": model_release["manifest_sha256"],
                    "visibility_run_id": vis_release["run_id"],
                    "visibility_manifest_sha256": sha256(root / "data/property/estateline/visibility/runs"
                                                           / vis_release["run_id"] / "manifest.json"),
                    **generation}
                write_json(pending_path, pending_doc)
                if active_generation is not None:
                    promote_generation(root, pending_path)
                    pending_path.unlink()
                    pending_path = None
                    actions.append("generation_pointer_published")
                else:
                    actions.append("pending_generation_saved_until_reader_gate")
            elif active_generation is not None and (model_release or vis_release):
                bundle = active_generation["bundle"]
                generation = build_generation_bundle(
                    root, _snapshot_for_active(root),
                    model_release["run_id"] if model_release else bundle["model"]["run_id"],
                    vis_release["run_id"] if vis_release else bundle["visibility"]["run_id"])
                pending_update = root / "data/runtime/refresh/pending" / f"update-{generation['generation_id']}.json"
                write_json(pending_update, {"schema_version": "estate-generation-pending/1", "kind": "refresh",
                                            "raw_inventory_sha256": registry_state["raw_inventory_sha256"],
                                            "expected_previous_bundle_sha256": active_generation["bundle_sha256"],
                                            **generation})
                promote_generation(root, pending_update)
                pending_update.unlink()
                actions.append("generation_pointer_published")
            elif model_release or vis_release:
                _transaction(root, {}, model=model_release, visibility=vis_release)
                if model_release:
                    manifest = read_json(root / "data/models" / model_release["run_id"] / "manifest.json")
                    _record_published(root, [root / "data/models" / model_release["run_id"],
                                             root / "assets" / manifest["public_root"]])
                actions.append("validated_snapshot_published")
            if before["dependencies"]["nearby_poi_graph_oms"]["state"] == "stale_derived":
                from sources.social.oms_registry import build_sidecar

                social = root / "data/social"
                sidecar, manifest = build_sidecar(
                    poi_path=social / "osm_moscow_social_pois_261008.parquet",
                    poi_manifest_path=social / "osm_moscow_social_pois_261008.manifest.json",
                    evidence_path=social / "oms_registry_records_2026.json")
                old_oms = read_json(social / "oms_verified_clinics_2026.json")
                key = lambda row: (row["organization_code"], row["osm_source_id"])
                old_keys = {key(row) for row in old_oms["records"]}
                new_keys = {key(row) for row in sidecar["records"]}
                if not sidecar["records"] or len(new_keys) != len(sidecar["records"]) or not old_keys <= new_keys:
                    raise ValueError("OMS source identity disappeared or duplicated; prior sidecar retained")
                stage_oms = Path(tempfile.mkdtemp(prefix="oms-", dir=root / "data/runtime/refresh"))
                sidecar_bytes = (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode()
                atomic_bytes(stage_oms / "sidecar.json", sidecar_bytes)
                manifest["output_sha256"] = hashlib.sha256(sidecar_bytes).hexdigest()
                write_json(stage_oms / "manifest.json", manifest)
                _transaction(root, {social / "oms_verified_clinics_2026.json": stage_oms / "sidecar.json",
                                    social / "oms_verified_clinics_2026.manifest.json": stage_oms / "manifest.json"})
                actions.append("nearby_oms_sidecar_published")
            after = status(root)
            result = {"result": "staged" if pending_path is not None and actions else
                      "updated" if actions else "retained_last_valid" if before["blocking_states"] else "nochange",
                      "blocked": before["blocking_states"], "actions": actions,
                      "recovered_transactions": recovered,
                      "cleaned_committed_pending": cleaned_pending,
                      "dependencies": after["dependencies"]}
            write_json(root / "data/runtime/refresh/last_result.json", result)
            return result
        except Exception as exc:
            if stage is not None:
                diagnostic = {"reason": type(exc).__name__, "message": str(exc),
                              "staged_manifest_sha256": sha256(stage / "source_manifest.json")
                              if (stage / "source_manifest.json").is_file() else None,
                              "stale_records": read_json(stage / "stale_records.json")
                              if (stage / "stale_records.json").is_file() else None}
                token = hashlib.sha256(json.dumps(diagnostic, sort_keys=True).encode()).hexdigest()[:16]
                write_json(root / "data/runtime/refresh/rejected" / f"estate-{token}.json", diagnostic)
            raise
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "dry-run", "run", "stage-bootstrap",
                                            "promote", "rollback-generation", "rollback-last"))
    parser.add_argument("--pending", type=Path, help="staged pending generation JSON for promote")
    arguments = parser.parse_args()
    if arguments.command == "run":
        result = run()
    elif arguments.command in {"stage-bootstrap", "promote", "rollback-generation", "rollback-last"}:
        with exclusive_lock(ROOT):
            if arguments.command == "stage-bootstrap":
                result = stage_bootstrap_generation()
            elif arguments.command == "promote":
                if arguments.pending is None:
                    parser.error("promote requires --pending")
                result = promote_generation(ROOT, ROOT / arguments.pending)
                (ROOT / arguments.pending).unlink()
            elif arguments.command == "rollback-generation":
                result = rollback_active_generation()
            else:
                result = rollback_last()
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
