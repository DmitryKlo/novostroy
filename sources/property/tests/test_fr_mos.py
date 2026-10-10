from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sources.property import fr_mos


def _payloads() -> tuple[dict, dict]:
    items = [
        {
            "id": "p-1",
            "code": "building-one",
            "name": "=Тестовая улица, д. 1",
            "status_code": "PROCESSING",
            "coords": ["55.80", "37.49"],
            "county": 2450,
            "district": 2483,
            "flats": "291",
            "developer": "-",
            "img": "/image/one.jpg",
        },
        {
            "id": "p-2",
            "code": "building-two",
            "name": "Тестовая улица, д. 2",
            "status_code": "FINISHED",
            "coords": [55.81, 37.5],
            "county": 2450,
            "district": 2483,
            "flats": "174",
            "developer": "КП «УГС»",
        },
        {"id": "f-no-count", "name": "Finished no count", "status_code": "FINISHED", "flats": None},
        {"id": "old", "name": "Old home", "status_code": "OLD", "flats": "400"},
        {"id": "start", "name": "Start site", "status_code": "START", "flats": "200"},
        {"id": "zero", "name": "Zero flats", "status_code": "PROCESSING", "flats": "0"},
    ]
    filters = {
        "filters": {
            "county": {
                "2450": {
                    "name": "СЗАО",
                    "district": {"2483": {"name": "Щукино"}},
                }
            },
            "type": {"R": "Квартиры", "NR": "Коммерческие помещения", "P": "Паркинг"},
        }
    }
    return {"objects": {"items": items}}, filters


def _valid_route() -> dict[str, str | None]:
    return {
        "device": "enp10s0",
        "source_ip": "192.168.1.111",
        "gateway": "192.168.1.1",
        "route_line": "212.11.155.166 via 192.168.1.1 dev enp10s0 src 192.168.1.111",
        "target_ip": "212.11.155.166",
        "verified": "true",
    }


def test_projection_keeps_registry_scope_and_narrow_candidate_rule() -> None:
    map_payload, filter_payload = _payloads()
    records, manifest = fr_mos.project_payloads(
        map_payload, filter_payload, response_date="Sat, 10 Oct 2026 19:44:54 GMT"
    )

    assert len(records) == 6
    candidates = [row for row in records if row["address_crosscheck_candidate"]]
    assert [row["source_record_id"] for row in candidates] == ["p-1", "p-2"]
    assert candidates[0]["latitude"] == 55.8
    assert candidates[0]["longitude"] == 37.49
    assert candidates[0]["county_label"] == "СЗАО"
    assert candidates[0]["district_label"] == "Щукино"
    assert candidates[0]["developer_assessment"] == "unknown"
    assert candidates[1]["developer_source_claim"] == "КП «УГС»"
    assert candidates[1]["developer_assessment"] == "source_assertion_unverified"
    assert candidates[0]["image_role"] == "map_card_preview"
    assert candidates[0]["corpus_id"] is None
    assert all(row["price"] is None and row["availability"] is None for row in candidates)
    assert all(not row["unit_listing_present_in_map_payload"] for row in candidates)
    assert records[3]["source_status_code"] == "OLD"
    assert not records[3]["address_crosscheck_candidate"]
    assert manifest["dataset_scope"] == "renovation-map registry, not unit prices"
    assert manifest["source_status_counts"] == {
        "FINISHED": 2,
        "OLD": 1,
        "PROCESSING": 2,
        "START": 1,
    }
    assert manifest["candidate_status_counts"] == {"FINISHED": 1, "PROCESSING": 1}
    assert manifest["item_category_field_present"] is False


def test_processed_export_separates_raw_rows_and_writes_two_address_csvs(tmp_path: Path) -> None:
    map_payload, filter_payload = _payloads()
    records, manifest = fr_mos.project_payloads(
        map_payload, filter_payload, response_date="Sat, 10 Oct 2026 19:44:54 GMT"
    )
    processed = tmp_path / "processed"
    exported = fr_mos.write_processed(
        processed, map_payload["objects"]["items"], records, manifest
    )

    raw_rows = [
        json.loads(line)
        for line in (processed / "source-rows.jsonl").read_text().splitlines()
    ]
    candidate_rows = [
        json.loads(line)
        for line in (processed / "crosscheck-candidates.jsonl").read_text().splitlines()
    ]
    assert len(raw_rows) == 6
    assert raw_rows[0]["name"] == "=Тестовая улица, д. 1"
    assert len(candidate_rows) == 2
    assert exported["outputs"]["source_rows"]["sha256"] == fr_mos.sha256_file(
        processed / "source-rows.jsonl"
    )
    assert exported["outputs"]["source_csv"]["records"] == 6
    assert exported["outputs"]["candidate_csv"]["records"] == 2
    assert exported["unit_prices_or_availability_present"] is False

    with (processed / "source-records.csv").open(encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.DictReader(stream, delimiter=";"))
    assert csv_rows[0]["address_raw"] == "'=Тестовая улица, д. 1"
    assert csv_rows[0]["dataset_scope"] == "renovation-map registry, not unit prices"
    assert "price" not in csv_rows[0]
    assert (processed / "manifest.json").exists()


def test_route_guard_requires_the_recorded_physical_route() -> None:
    route = "212.11.155.166 via 192.168.1.1 dev enp10s0 src 192.168.1.111 uid 1000"
    parsed = fr_mos.validate_direct_route("enp10s0", route)
    assert parsed["verified"] == "true"
    assert parsed["target_ip"] == fr_mos.SERVER_IP

    with pytest.raises(fr_mos.CollectionError, match="no request was made"):
        fr_mos.validate_direct_route(
            "enp10s0", "212.11.155.166 dev tun0 src 10.0.0.2"
        )
    with pytest.raises(fr_mos.CollectionError, match="no request was made"):
        fr_mos.validate_direct_route(
            "enp10s0", "212.11.155.166 via 192.168.1.1 dev enp10s0 src 192.168.1.112"
        )
    with pytest.raises(fr_mos.CollectionError, match="no request was made"):
        fr_mos.validate_direct_route(
            "wlan0", "212.11.155.166 via 192.168.1.1 dev enp10s0 src 192.168.1.111"
        )


def test_route_probe_is_constrained_to_the_bound_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fr_mos.build_route_probe_command("enp10s0") == [
        "ip", "-4", "route", "get", fr_mos.SERVER_IP, "oif", "enp10s0"
    ]
    calls: list[list[str]] = []

    def route_runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "212.11.155.166 via 192.168.1.1 dev enp10s0 "
                "src 192.168.1.111 uid 1000\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(fr_mos.subprocess, "run", route_runner)
    evidence = fr_mos.verify_direct_route("enp10s0")
    assert calls == [["ip", "-4", "route", "get", fr_mos.SERVER_IP, "oif", "enp10s0"]]
    assert evidence["probe_command"] == "ip -4 route get 212.11.155.166 oif enp10s0"


def test_live_curl_command_is_pinned_and_has_no_retry_or_redirect() -> None:
    command = fr_mos.build_curl_command(
        fr_mos.MAP_URL,
        interface="enp10s0",
        headers_path=Path("/runtime/map.headers"),
        body_path=Path("/runtime/map.body"),
    )
    assert command[command.index("--interface") + 1] == "if!enp10s0"
    assert command[command.index("--noproxy") + 1] == "*"
    assert command[command.index("--resolve") + 1] == "fr.mos.ru:443:212.11.155.166"
    assert "--globoff" in command
    assert "--location" not in command and "-L" not in command
    assert "--retry" not in command
    assert not any("cookie" in part.lower() for part in command)
    writeout = command[command.index("--write-out") + 1]
    assert "local_ip=%{local_ip}" in writeout


def test_denial_stops_without_retry_and_saved_headers_drop_cookies(tmp_path: Path) -> None:
    def denied_runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        headers = Path(command[command.index("--dump-header") + 1])
        body = Path(command[command.index("--output") + 1])
        headers.write_bytes(
            b"HTTP/2 403\r\nDate: Sat, 10 Oct 2026 20:00:00 GMT\r\n"
            b"Content-Type: text/html\r\nSet-Cookie: PHPSESSID=do-not-save\r\n\r\n"
        )
        body.write_bytes(b"denied")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "http_code=403\nsize_download=6\nremote_ip=212.11.155.166\n"
                f"url_effective={fr_mos.FILTER_URL}\n"
            ),
            stderr="",
        )

    with pytest.raises(fr_mos.SourceDenied, match="stopped without retry") as error:
        fr_mos.fetch_endpoint(
            fr_mos.FILTER_URL,
            interface="enp10s0",
            noproxy=True,
            raw_dir=tmp_path / "raw",
            request_name="filters",
            route_evidence=_valid_route(),
            runner=denied_runner,
        )
    assert error.value.response["http_status"] == "403"
    saved_headers = (tmp_path / "raw" / "filters.headers").read_bytes()
    assert b"403" in saved_headers
    assert b"Set-Cookie" not in saved_headers
    assert b"PHPSESSID" not in saved_headers
    assert not list((tmp_path / "raw").glob("*.tmp-raw"))


def test_fetch_refuses_network_without_route_evidence(tmp_path: Path) -> None:
    called = False

    def should_not_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(fr_mos.CollectionError, match="route evidence is required"):
        fr_mos.fetch_endpoint(
            fr_mos.FILTER_URL,
            interface="enp10s0",
            noproxy=True,
            raw_dir=tmp_path / "raw",
            request_name="filters",
            route_evidence={},
            runner=should_not_run,
        )
    assert called is False


def test_fetch_stops_when_curl_uses_an_unverified_local_source_ip(tmp_path: Path) -> None:
    def wrong_source_runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        headers = Path(command[command.index("--dump-header") + 1])
        body = Path(command[command.index("--output") + 1])
        headers.write_bytes(
            b"HTTP/2 200\r\nContent-Type: application/json\r\n"
            b"Date: Sat, 10 Oct 2026 20:00:00 GMT\r\n\r\n"
        )
        body.write_bytes(b"{}")
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "http_code=200\nsize_download=2\nremote_ip=212.11.155.166\n"
                "local_ip=192.168.1.112\n"
                f"url_effective={fr_mos.FILTER_URL}\n"
            ),
            stderr="",
        )

    with pytest.raises(fr_mos.CollectionError, match="different local source IP") as error:
        fr_mos.fetch_endpoint(
            fr_mos.FILTER_URL,
            interface="enp10s0",
            noproxy=True,
            raw_dir=tmp_path / "raw",
            request_name="filters",
            route_evidence=_valid_route(),
            runner=wrong_source_runner,
        )
    assert error.value.response["local_ip"] == "192.168.1.112"


def test_live_cli_requires_explicit_interface_and_proxy_bypass() -> None:
    with pytest.raises(SystemExit):
        fr_mos.parse_args(["--live", "--interface", "enp10s0"])
    with pytest.raises(SystemExit):
        fr_mos.parse_args(["--live", "--interface", "wlan0", "--noproxy"])
    with pytest.raises(SystemExit):
        fr_mos.parse_args(["--map-json", "/tmp/untracked-capture.json"])
