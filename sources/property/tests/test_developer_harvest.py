"""Offline gates for bounded developer discovery, resume, and private output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sources.property import developer_harvest as harvest


def _seeds(tmp_path: Path) -> Path:
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps({"schema_version": "developer-harvest-seeds/1",
                                "providers": {"a101": {
                                    "allowed_hosts": ["example.com"],
                                    "seeds": [{"url": "https://example.com/catalog",
                                               "page_kind": "catalog"}]} }}), encoding="utf-8")
    return path


def _plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harvest, "plugin_hashes", lambda _: {"a101": "a" * 64})

    def discover(text: str, page_url: str) -> list[dict]:
        return ([{"url": "https://example.com/flat/42", "page_kind": "listing"}]
                if page_url.endswith("/catalog") else [])

    def parse(text: str, page_url: str, observed_at: str, raw_sha256: str) -> list[dict]:
        if not page_url.endswith("/catalog"):
            return []
        return [{"record_type": "unit", "source_url": "https://example.com/flat/42",
                 "publisher_unit_id": "42", "publisher_project_id": "P",
                 "project_name": "Fixture", "unit_number": "7", "price_rub": 123,
                 "price_basis": "unknown", "rights_status": "unknown",
                 "source_fields": {"location_scope": "in_scope_moscow",
                                   "catalog_total_claimed": 2},
                 "media": [{"url": "https://media.example.com/plan.png",
                            "role": "apartment_plan", "rights_status": "cleared"}]}]

    monkeypatch.setattr(harvest, "plugin_functions", lambda _: (discover, parse))


def test_public_fetch_checkpoint_blocks_host_and_deduplicates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    calls: list[str] = []

    def fake_fetch(url: str, hosts: set[str], headers: dict) -> harvest.FetchResult:
        calls.append(url)
        if url.endswith("/catalog"):
            body = b"<title>Homes</title><form class='g-recaptcha'></form>"
            return harvest.FetchResult(200, body, {"Content-Type": "text/html"}, url)
        return harvest.FetchResult(403, b"", {}, url, "http_403")

    seeds = _seeds(tmp_path)
    first = harvest.Harvest(seeds, tmp_path / "state", fetch=fake_fetch,
                            sleep=lambda _: None, clock=lambda: 1.0)
    try:
        assert first.run(max_pages=10, include_listings=True) == 2
        result = json.loads((first.directory / "manifest.json").read_text())
        assert result["files"]["observations"]["records"] == 1
        assert result["files"]["developer_units"]["records"] == 1
        assert result["providers"]["a101"]["catalog_exhausted"] is False
        assert result["providers"]["a101"]["blocked_hosts"] == ["example.com"]
        row = json.loads((first.directory / "observations.jsonl").read_text())
        assert row["identity_quality"] == "publisher_asserted"
        assert row["rights_status"] == "unknown"
        assert row["media"][0]["public_display_allowed"] is False
        assert row["media"][0]["rights_status"] == "unknown"
        assert first.run(max_pages=10) == 0
        first_dir = first.directory
    finally:
        first.close()
    resumed = harvest.Harvest(seeds, tmp_path / "state", fetch=fake_fetch,
                              sleep=lambda _: None, clock=lambda: 1.0)
    try:
        assert resumed.directory == first_dir
        assert resumed.run(max_pages=10) == 0
        assert calls == ["https://example.com/catalog", "https://example.com/flat/42"]
    finally:
        resumed.close()


def test_local_capture_is_hashed_idempotent_and_preserves_observation_time(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    seeds = _seeds(tmp_path)
    captured = tmp_path / "catalog.html"
    captured.write_text("<title>Homes</title>", encoding="utf-8")
    manifest = tmp_path / "captures.json"
    manifest.write_text(json.dumps({"schema_version": "developer-harvest-captures/1",
                                    "captures": [{"provider": "a101", "page_kind": "catalog",
                                                  "url": "https://example.com/catalog",
                                                  "path": "catalog.html",
                                                  "observed_at": "2026-10-10T15:38:58+00:00",
                                                  "sha256": harvest.digest(captured.read_bytes())}]}))
    current = harvest.Harvest(seeds, tmp_path / "state")
    try:
        assert current.ingest_captures(manifest) == 1
        assert current.ingest_captures(manifest) == 0
        row = json.loads((current.directory / "observations.jsonl").read_text())
        assert row["observed_at"] == "2026-10-10T15:38:58+00:00"
        assert row["source_updated_at"] is None
        assert len((current.directory / "requests.jsonl").read_text().splitlines()) == 1
    finally:
        current.close()


def test_only_approved_https_hosts_and_exact_address_budget(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    with pytest.raises(ValueError, match="approved HTTPS"):
        harvest.checked_url("http://example.com/catalog", {"example.com"})
    with pytest.raises(ValueError, match="approved HTTPS"):
        harvest.checked_url("https://other.example/catalog", {"example.com"})
    current = harvest.Harvest(_seeds(tmp_path), tmp_path / "state")
    try:
        with current.database:
            count = sum(current._enqueue("a101", {
                "url": f"https://example.com/flat/{number}", "page_kind": "listing",
                "address_hint": "Москва, точный адрес"}, None, 1)
                for number in range(7))
        assert count == 5
    finally:
        current.close()


def test_conflicting_catalog_project_does_not_clear_selected_unit_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    current = harvest.Harvest(_seeds(tmp_path), tmp_path / "state")
    try:
        base = {
            "observation_id": "devobs_same_unit", "provider": "a101",
            "record_type": "unit", "publisher_unit_id": "42",
            "source_url": "https://example.com/flat/42",
            "source_page_url": "https://example.com/flat/42",
            "observed_at": "2026-10-10T15:00:00+00:00", "raw_sha256": "a" * 64,
            "source_fields": {}, "identity_conflict": False,
        }
        with current.database:
            current._save_observation({**base, "page_kind": "listing",
                                       "publisher_project_id": "project-a"})
            current._save_observation({**base, "page_kind": "catalog",
                                       "source_page_url": "https://example.com/catalog",
                                       "publisher_project_id": "project-b"})
        stored = json.loads(current.database.execute(
            "SELECT payload_json FROM observations WHERE observation_id='devobs_same_unit'"
        ).fetchone()[0])
        assert stored["publisher_project_id"] == "project-a"
        assert stored["identity_conflict"] is True
        assert stored["source_fields"]["conflicting_project_ids"] == ["project-a", "project-b"]
    finally:
        current.close()


def test_parser_revision_reuses_verified_capture_without_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    seeds = _seeds(tmp_path)
    captured = tmp_path / "catalog.html"
    captured.write_text("<title>Homes</title>", encoding="utf-8")
    capture_manifest = tmp_path / "captures.json"
    capture_manifest.write_text(json.dumps({
        "schema_version": "developer-harvest-captures/1",
        "captures": [{"provider": "a101", "page_kind": "catalog",
                      "url": "https://example.com/catalog", "path": "catalog.html",
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "sha256": harvest.digest(captured.read_bytes())}],
    }), encoding="utf-8")
    old = harvest.Harvest(seeds, tmp_path / "state")
    try:
        assert old.ingest_captures(capture_manifest) == 1
    finally:
        old.close()
    monkeypatch.setattr(harvest, "plugin_hashes", lambda _: {"a101": "b" * 64})

    def no_network(*args: object) -> harvest.FetchResult:
        raise AssertionError("verified capture should avoid a second GET")

    current = harvest.Harvest(seeds, tmp_path / "state", fetch=no_network)
    try:
        assert current.directory != old.directory
        assert current.run_cached_only(max_pages=1) == 1
        assert current.last_run_stats["reused_pages"] == 1
        assert current.last_run_stats["network_attempts"] == 0
        assert current.export()["files"]["observations"]["records"] == 1
    finally:
        current.close()


def test_curl_redirect_to_unapproved_host_is_never_requested(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        header_path = Path(command[command.index("--dump-header") + 1])
        header_path.write_text("HTTP/2 302\r\nLocation: https://elsewhere.example/private\r\n")
        return SimpleNamespace(returncode=0, stdout=b"302")

    monkeypatch.setattr(harvest.subprocess, "run", fake_run)
    result = harvest.curl_fetch("https://example.com/catalog", {"example.com"}, {})
    assert result.error == "redirect_outside_scope"
    assert len(calls) == 1


def test_scope_inheritance_requires_same_publisher_project_id(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    current = harvest.Harvest(_seeds(tmp_path), tmp_path / "state")
    try:
        common = {"provider": "a101", "source_page_url": "https://example.com/catalog",
                  "source_url": "https://example.com/catalog", "page_kind": "catalog",
                  "observed_at": "2026-10-10T15:00:00+00:00", "raw_sha256": "a" * 64,
                  "identity_quality": "publisher_asserted", "identity_conflict": False,
                  "publisher_domain": "example.com"}
        project = {**common, "observation_id": "project-p1", "record_type": "project",
                   "publisher_project_id": "p1", "project_name": "Shared label",
                   "address": "Москва, проверенный проектный адрес",
                   "source_fields": {"location_scope": "in_scope_moscow"}}
        unit = {**common, "observation_id": "unit-u1", "record_type": "unit",
                "publisher_unit_id": "u1", "publisher_project_id": "p1",
                "project_name": "Shared label", "address": None,
                "source_fields": {"location_scope": "unknown"}}
        other = {**unit, "observation_id": "unit-u2", "publisher_unit_id": "u2",
                 "publisher_project_id": "p2"}
        with current.database:
            for value in (project, unit, other):
                current._save_observation(value)
        assert current.export()["files"]["developer_units"]["records"] == 1
        selected = json.loads((current.directory / "developer_units.jsonl").read_text())
        assert selected["location_scope_basis"] == "same_publisher_project_id"
        assert selected["scope_evidence_observation_id"] == "project-p1"
        assert selected["project"]["address"] == project["address"]
        assert selected["building"]["full_address"] is None
    finally:
        current.close()


def test_source_linked_catalog_pagination_passes_six_pages(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harvest, "plugin_hashes", lambda _: {"a101": "a" * 64})

    def discover(_: str, page_url: str) -> list[dict]:
        number = int(page_url.split("page=")[-1]) if "page=" in page_url else 0
        if number == 8:
            return [{"url": "https://example.com/project/late", "page_kind": "project"}]
        return [{"url": f"https://example.com/catalog?page={number + 1}",
                 "page_kind": "catalog"}]

    monkeypatch.setattr(harvest, "plugin_functions",
                        lambda _: (discover, lambda *args: []))
    current = harvest.Harvest(
        _seeds(tmp_path), tmp_path / "state",
        fetch=lambda url, hosts, headers: harvest.FetchResult(
            200, b"<title>Homes</title>", {"Content-Type": "text/html"}, url),
        sleep=lambda _: None, clock=lambda: 1.0)
    try:
        assert current.run(max_pages=9) == 9
        assert current.database.execute(
            "SELECT COUNT(*) FROM pages WHERE page_kind='catalog' AND state='fetched'"
        ).fetchone()[0] == 9
        assert current.database.execute(
            "SELECT MAX(depth) FROM pages WHERE page_kind='catalog'"
        ).fetchone()[0] == 0
        # A project discovered on catalog page 9 must remain reachable.
        late = current.database.execute(
            "SELECT state,depth FROM pages WHERE url='https://example.com/project/late'"
        ).fetchone()
        assert tuple(late) == ("pending", 1)
    finally:
        current.close()


def test_status_is_read_only_before_first_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    state_root = tmp_path / "state"
    status = harvest.read_only_status(_seeds(tmp_path), state_root)
    assert status["read_only"] is True
    assert status["providers"]["a101"]["pages"] == {"not_started": 1}
    assert not state_root.exists()


def test_host_denial_remains_stopped_across_parser_revision(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugins(monkeypatch)
    seeds = _seeds(tmp_path)
    old = harvest.Harvest(
        seeds, tmp_path / "state",
        fetch=lambda url, hosts, headers: harvest.FetchResult(
            403, b"", {}, url, "http_403"),
        sleep=lambda _: None)
    try:
        assert old.run(max_pages=1) == 1
    finally:
        old.close()
    monkeypatch.setattr(harvest, "plugin_hashes", lambda _: {"a101": "b" * 64})

    def no_network(*args: object) -> harvest.FetchResult:
        raise AssertionError("denied host must stay stopped")

    newer = harvest.Harvest(seeds, tmp_path / "state", fetch=no_network)
    try:
        assert newer.run(max_pages=1) == 0
        assert newer.export()["providers"]["a101"]["blocked_hosts"] == ["example.com"]
    finally:
        newer.close()


def test_truncated_source_unit_array_is_rejected_not_published(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harvest, "plugin_hashes", lambda _: {"a101": "a" * 64})
    monkeypatch.setattr(harvest, "plugin_functions", lambda _: (
        lambda text, url: [],
        lambda text, url, at, sha: [{"record_type": "project", "source_url": url,
                                    "source_fields": {"unit_records_truncated": True}}]))
    current = harvest.Harvest(
        _seeds(tmp_path), tmp_path / "state",
        fetch=lambda url, hosts, headers: harvest.FetchResult(
            200, b"<title>Homes</title>", {"Content-Type": "text/html"}, url),
        sleep=lambda _: None)
    try:
        assert current.run(max_pages=1) == 1
        page = current.database.execute("SELECT state,error_code FROM pages").fetchone()
        assert tuple(page) == ("parse_error", "ValueError")
        assert current.export()["files"]["observations"]["records"] == 0
    finally:
        current.close()
