"""Checkpoint, retry, privacy, and local source integration tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.collection.adapters import Batch
from pipeline.collection.runner import DEFAULT_CONFIG, RetryableCollectionError, run


def config_file(tmp_path: Path, source_path: Path, *, interval: int = 0) -> Path:
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"version": "1", "jobs": [{
        "job_id": "local_sample", "kind": "osm_poi_file", "enabled": True,
        "source_id": "test-local", "input_path": str(source_path),
        "min_interval_seconds": interval, "max_bytes": 1000,
    }]}), encoding="utf-8")
    return path


def test_retry_etag_checkpoint_and_immutable_noop(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text('{"value":1}', encoding="utf-8")
    config = config_file(tmp_path, source)
    state = tmp_path / "state"
    calls = []
    delays = []

    def transient_adapter(path, job, manifest, rights, previous_etag):
        calls.append(previous_etag)
        if len(calls) == 1:
            raise RetryableCollectionError("transient")
        return Batch([{"value": json.loads(path.read_text())["value"]}],
                     {"record_count": 1, "fields": {"value": {"populated": 1, "missing": 0}}},
                     "2026-10-09T00:00:00Z", etag=f"etag-{len(calls)}")

    adapters = {"osm_poi_file": transient_adapter}
    dry = run(config, state, adapters=adapters, sleep=delays.append)
    assert dry["jobs"][0]["status"] == "planned"
    assert dry["jobs"][0]["attempts"] == 2
    assert not state.exists()

    calls.clear()
    applied = run(config, state, apply=True, adapters=adapters, sleep=delays.append)
    assert applied["jobs"][0]["status"] == "imported"
    assert applied["jobs"][0]["attempts"] == 2
    first_id = applied["jobs"][0]["snapshot_id"]
    first_folder = state / "snapshots/local_sample" / first_id
    assert first_folder.is_dir()
    assert (first_folder / "records.json").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(state / "checkpoints.sqlite3") as connection:
        assert connection.execute("SELECT etag FROM job_state WHERE job_id='local_sample'").fetchone()[0] == "etag-2"

    before = len(calls)
    repeated = run(config, state, apply=True, adapters=adapters)
    assert repeated["jobs"][0]["status"] == "no_op"
    assert repeated["jobs"][0]["coverage"]["fields"]["value"]["populated"] == 1
    assert len(calls) == before
    source.write_text('{"value":2}', encoding="utf-8")
    changed = run(config, state, apply=True, adapters=adapters)
    assert changed["jobs"][0]["status"] == "imported"
    assert changed["jobs"][0]["snapshot_id"] != first_id
    assert json.loads((first_folder / "records.json").read_text()) == [{"value": 1}]
    assert calls[-1] == "etag-2"


def test_failure_then_local_source_recovers_without_broad_retry(tmp_path: Path) -> None:
    source = tmp_path / "arrives_later.json"
    config = config_file(tmp_path, source)
    state = tmp_path / "state"
    def adapter(path, job, manifest, rights, previous_etag):
        return Batch([{"ok": True}], {"record_count": 1}, "2026-10-09T00:00:00Z")

    failed = run(config, state, apply=True, adapters={"osm_poi_file": adapter})
    assert failed["source_failures"] == [{"job_id": "local_sample", "code": "source_missing"}]
    source.write_text("{}", encoding="utf-8")
    recovered = run(config, state, apply=True, adapters={"osm_poi_file": adapter})
    assert recovered["jobs"][0]["status"] == "imported"
    assert run(config, state, apply=True, adapters={"osm_poi_file": adapter})["jobs"][0]["status"] == "no_op"


def test_bounded_rate_limit_after_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    config = config_file(tmp_path, source, interval=3600)
    state = tmp_path / "state"
    adapter = lambda *args: Batch([{"ok": True}], {"record_count": 1}, "2026-10-09T00:00:00Z")
    assert run(config, state, apply=True, adapters={"osm_poi_file": adapter})["jobs"][0]["status"] == "imported"
    source.write_text('{"changed":true}', encoding="utf-8")
    assert run(config, state, apply=True, adapters={"osm_poi_file": adapter})["jobs"][0]["status"] == "rate_limited"


def test_no_credential_fields_in_config(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    config = config_file(tmp_path, source)
    payload = json.loads(config.read_text())
    payload["jobs"][0]["api_key"] = "forbidden"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Credential fields"):
        run(config, tmp_path / "state")


def test_real_job_without_rights_is_missing_access(tmp_path: Path) -> None:
    source = tmp_path / "feed.xml"
    source.write_text("<root/>", encoding="utf-8")
    config = config_file(tmp_path, source)
    payload = json.loads(config.read_text())
    payload["jobs"][0]["kind"] = "developer_yrl_file"
    payload["jobs"][0]["data_mode"] = "real"
    config.write_text(json.dumps(payload), encoding="utf-8")
    result = run(config, tmp_path / "state", apply=True, allow_real=True)
    assert result["jobs"][0]["status"] == "missing_access"
    assert result["counts"]["imported"] == 0


def test_social_sample_enrichment_stays_fictional_and_offline(tmp_path: Path) -> None:
    state = tmp_path / "state"
    selection = {"synthetic_social_anchor"}
    applied = run(DEFAULT_CONFIG, state, apply=True, selected_jobs=selection)
    job = applied["jobs"][0]
    assert job["status"] == "imported"
    assert job["coverage"]["routed_requests"] == 0
    assert job["coverage"]["status"] == "illustrative_fixture_only"
    records = json.loads((state / "snapshots" / job["job_id"] / job["snapshot_id"] / "records.json").read_text())
    assert len(records) == 1 and records[0]["fixture_only"] is True
    assert records[0]["complex_id"] == "synthetic-origin-for-pipeline-only"
    assert all(fact["value"] is None or fact["status"] == "illustrative"
               for fact in records[0]["facts"].values())
    repeated = run(DEFAULT_CONFIG, state, apply=True, selected_jobs=selection)
    assert repeated["jobs"][0]["status"] == "no_op"
    assert repeated["jobs"][0]["coverage"] == job["coverage"]
