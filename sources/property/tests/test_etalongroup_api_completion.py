import json

from sources.property import etalongroup_api_completion as api
from sources.property.developer_harvest import FetchResult, atomic_bytes, canonical_json, digest
from sources.property.etalongroup_catalog_audit import _unit_row


def _flat(unit_id: int) -> dict:
    return {
        "id": unit_id,
        "link": f"https://etalongroup.ru/msk/choose/flat/mariin-park/{unit_id}/",
        "img": None,
        "objectTitle": "Мариин Парк",
        "title": f"1-комн. квартира № {unit_id}",
        "titleDataList": {"number": unit_id, "rooms": 1},
        "area": 42.5,
        "floor": 3,
        "price": 12_000_000 + unit_id,
        "priceTotal": 11_500_000 + unit_id,
        "priceSpecial": 0,
        "deliveryName": "IV кв. 2027",
        "isBooked": False,
    }


def _base_row(unit_id: int) -> dict:
    return _unit_row(
        _flat(unit_id), project_id=124, project_name="Мариин Парк",
        city_label="Москва", address_label="Москва", district_label="СВАО",
        page_url="https://www.etalongroup.ru/msk/object/mariinn-park/flat/",
        observed_at="2026-10-10T00:00:00+00:00", body_sha256="a" * 64,
    )


def _response(ids: list[int], pagination: dict) -> dict:
    return {"status": "success", "data": {
        "itemList": [_flat(unit_id) for unit_id in ids],
        "pagination": pagination,
        "isLoading": False,
    }, "errors": []}


def test_server_returned_limit_and_offset_drive_next_request_and_keep_last_row(tmp_path):
    start = {"haveItem": True, "page": 2, "object": 124, "offset": 8, "limit": 8}
    server_next = {"haveItem": True, "page": 3, "object": 124, "offset": 17, "limit": 9}
    final_state = {"haveItem": False, "page": 4, "object": 124, "offset": 19, "limit": 9}
    responses = [
        _response(list(range(9, 18)), server_next),
        _response([18, 19], final_state),
    ]
    states = []

    def loader(url, pagination):
        states.append(pagination)
        payload = responses.pop(0)
        body = json.dumps(payload, separators=(",", ":")).encode()
        sha = digest(body)
        atomic_bytes(tmp_path / "raw" / f"{sha}.body", body)
        return payload, {"body_sha256": sha, "at": "2026-10-10T00:00:00+00:00"}

    result = api.collect_project(
        project_id=124, project_name="Мариин Парк", start_pagination=start,
        seed_rows=[_base_row(unit_id) for unit_id in range(1, 9)],
        base_row=_base_row(1), source_total_count=19, loader=loader,
        capture_dir=tmp_path,
    )
    assert len(result["rows"]) == 11
    assert result["combined_unique_ids"] == 19
    assert result["complete_by_source_count"] is True
    assert result["rows"][-1]["source_unit_id"] == 19
    assert states == [start, server_next]
    assert api.api_url(124, server_next).find("offset%22%3A17") >= 0
    assert "limit%22%3A9" in api.api_url(124, server_next)


def test_existing_successful_capture_replays_without_get(tmp_path, monkeypatch):
    pagination = {"haveItem": True, "page": 2, "object": 124, "offset": 8, "limit": 8}
    url = api.api_url(124, pagination)
    body = json.dumps(_response([9], {"haveItem": False, "page": 3, "object": 124,
                                      "offset": 9, "limit": 8})).encode()
    sha = digest(body)
    raw_dir = tmp_path / "raw"
    atomic_bytes(raw_dir / f"{sha}.body", body)
    api.append_event(tmp_path / "requests.jsonl", {
        "url": url, "status": 200, "error": None, "blocked": False,
        "body_sha256": sha, "body_bytes": len(body), "request_pagination": pagination,
    })

    def forbidden_get(*args, **kwargs):
        raise AssertionError("saved page must not be fetched again")

    monkeypatch.setattr(api, "curl_fetch", forbidden_get)
    loader = api.PageLoader(capture_dir=tmp_path, live=True, total_attempts=1)
    payload, event = loader(url, pagination)
    assert payload["data"]["itemList"][0]["id"] == 9
    assert event["body_sha256"] == sha


def test_denial_stops_host_and_anonymous_fetch_sends_no_auth(tmp_path, monkeypatch):
    pagination = {"haveItem": True, "page": 2, "object": 124, "offset": 8, "limit": 8}
    url = api.api_url(124, pagination)
    calls = []

    def denied_get(request_url, hosts, headers, timeout):
        calls.append((request_url, hosts, headers, timeout))
        return FetchResult(403, b"", {"Content-Type": "application/json"}, request_url)

    monkeypatch.setattr(api, "curl_fetch", denied_get)
    loader = api.PageLoader(capture_dir=tmp_path, live=True, total_attempts=0)
    payload, event = loader(url, pagination)
    assert payload is None
    assert event["blocked"] is True
    assert calls[0][2] == {}
    assert calls[0][3] == 20
    payload, event = loader(url + "&retry=1", pagination)
    assert payload is None
    assert event["error"] == "host_stopped_after_denial"
    assert len(calls) == 1


def test_lowercase_json_mime_is_accepted_and_body_persisted(tmp_path, monkeypatch):
    pagination = {"haveItem": True, "page": 2, "object": 124, "offset": 8, "limit": 8}
    url = api.api_url(124, pagination)
    body = json.dumps(_response([9], {"haveItem": False, "page": 3, "object": 124,
                                      "offset": 9, "limit": 8})).encode()
    monkeypatch.setattr(api, "curl_fetch", lambda *args: FetchResult(
        200, body, {"content-type": "application/json"}, url))
    loader = api.PageLoader(capture_dir=tmp_path, live=True, total_attempts=0)
    payload, event = loader(url, pagination)
    assert payload["data"]["itemList"][0]["id"] == 9
    assert event["error"] is None
    assert (tmp_path / "raw" / f"{digest(body)}.body").read_bytes() == body


def test_one_time_mime_bug_recovery_preserves_old_event_and_replays(tmp_path, monkeypatch):
    pagination = {"haveItem": True, "page": 3, "object": 124, "offset": 17, "limit": 9}
    url = api.api_url(124, pagination)
    old_event = {
        "url": url, "status": 200, "error": "unexpected_content_type",
        "blocked": False, "body_bytes": 6674, "body_sha256": None,
        "headers": {"content-type": "application/json"},
    }
    api.append_event(tmp_path / "requests.jsonl", old_event)
    body = json.dumps(_response([18], {"haveItem": False, "page": 4, "object": 124,
                                       "offset": 18, "limit": 9})).encode()
    calls = []

    def one_recovery(*args):
        calls.append(args)
        return FetchResult(200, body, {"content-type": "application/json"}, url)

    monkeypatch.setattr(api, "curl_fetch", one_recovery)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    loader = api.PageLoader(capture_dir=tmp_path, live=True, total_attempts=1)
    payload, event = loader(url, pagination)
    assert payload["data"]["itemList"][0]["id"] == 18
    assert event["attempt_kind"] == "one-time-local-mime-recovery"
    assert event["recovery_reason"]
    assert len(calls) == 1
    original = json.loads((tmp_path / "requests.jsonl").read_text().splitlines()[0])
    recovered = json.loads((tmp_path / "recovery_requests.jsonl").read_text().splitlines()[0])
    assert original == old_event
    assert recovered["body_sha256"] == digest(body)

    replay = api.PageLoader(capture_dir=tmp_path, live=True, total_attempts=2)
    payload, _event = replay(url, pagination)
    assert payload["data"]["itemList"][0]["id"] == 18
    assert len(calls) == 1


def test_have_item_false_stops_after_processing_current_page(tmp_path):
    state = {"haveItem": True, "page": 2, "object": 124, "offset": 8, "limit": 8}
    response = _response([9], {"haveItem": False, "page": 3, "object": 124,
                              "offset": 9, "limit": 8})
    calls = []

    def loader(url, pagination):
        calls.append(url)
        body = json.dumps(response).encode()
        sha = digest(body)
        atomic_bytes(tmp_path / "raw" / f"{sha}.body", body)
        return response, {"body_sha256": sha, "at": "2026-10-10T00:00:00+00:00"}

    result = api.collect_project(
        project_id=124, project_name="Мариин Парк", start_pagination=state,
        seed_rows=[_base_row(unit_id) for unit_id in range(1, 9)],
        base_row=_base_row(1), source_total_count=None, loader=loader,
        capture_dir=tmp_path,
    )
    assert len(calls) == 1
    assert result["pagination_exhausted"] is True
    assert result["rows"][-1]["source_unit_id"] == 9
