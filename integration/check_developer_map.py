"""Offline gate for a staged or active publisher-building map snapshot.

Run ``rtk proxy .runtime/dev-env/bin/python integration/check_developer_map.py
--run-id developer-map-v1-...`` before pointer publication, then use
``--active`` after publication. This does not fetch pages or change a pointer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_DATA_MODE"] = "real"
os.environ["NOVOSTROY_REAL_STORE"] = "empty"
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api, developer_map  # noqa: E402


def _validate(schema: dict[str, Any], name: str, value: dict[str, Any]) -> None:
    Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"},
                         format_checker=FormatChecker()).validate(value)


async def _asgi(path: str, *, etag: str | None = None) -> tuple[int, dict[str, str], Any]:
    headers = [] if etag is None else [(b"if-none-match", etag.encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "server": ("localhost", 8000), "client": ("127.0.0.1", 12345),
             "headers": headers}
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(api.app(scope, receive, send), timeout=15)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    result = json.loads(body) if body else None
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, result


def check(snapshot: dict[str, Any], *, staged: bool) -> dict[str, Any]:
    if staged:
        # The publisher's active pointer remains untouched during the stage gate.
        api.developer_map.resolve_active = lambda: snapshot
    token = developer_map.pin(snapshot)
    try:
        list_view = api.registry_buildings(query=None, status=None, kind=None,
                                           limit=100, offset=0)
        all_list_rows = []
        offset = 0
        while offset < list_view["records_total"]:
            page = api.registry_buildings(query=None, status=None, kind=None,
                                          limit=100, offset=offset)
            assert page["snapshot_id"] == list_view["snapshot_id"]
            all_list_rows.extend(page["items"])
            offset += len(page["items"])
            assert page["items"], "Developer list pagination ended early"
        map_view = api.registry_map(query=None, status=None, kind=None)
        schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
        _validate(schema, "DeveloperRegistryListResult", list_view)
        _validate(schema, "DeveloperRegistryMapResult", map_view)
        assert list_view["dataset_total"] == len(all_list_rows) == len(snapshot["rows"])
        assert len({row["registry_id"] for row in all_list_rows}) == len(all_list_rows)
        assert map_view["counts"]["geocoded"] == len(snapshot["by_point"])
        assert map_view["counts"]["records"] == len(snapshot["rows"])
        assert {row["registry_id"] for row in map_view["items"]} == {
            row["registry_id"] for row in snapshot["rows"] if row["map_eligible"]}
        assert all("publisher_project_id" not in row and "source_group" not in row
                   for row in all_list_rows + map_view["items"])
        request = Request({"type": "http", "method": "GET", "path": "/v1/map/nearby",
                           "headers": []})
        try:
            api.map_nearby(request, "mappt_" + "f" * 24, "all")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("A point outside the publisher snapshot was accepted")
    finally:
        developer_map.reset(token)

    async def inline_run_sync(func: Any, *args: Any, **_: Any) -> Any:
        return func(*args)

    anyio.to_thread.run_sync = inline_run_sync

    async def routes() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        code, _, health = await _asgi("/v1/health")
        assert code == 200
        code, _, coverage = await _asgi("/v1/data-coverage")
        assert code == 200
        code, headers, points = await _asgi("/v1/map/points")
        assert code == 200 and headers.get("etag", "").startswith('W/"')
        cached, _, _ = await _asgi("/v1/map/points", etag=headers["etag"])
        assert cached == 304
        return health, coverage, points

    health, coverage, points = asyncio.run(routes())
    _validate(schema, "DataCoverageResult", coverage)
    _validate(schema, "DeveloperMapPointsResult", points)
    run_id = snapshot["manifest"]["run_id"]
    snapshot_id = snapshot["manifest"]["snapshot_id"]
    assert (health["data_mode"] == "real" and health["catalog_view"] == "developer_buildings"
            and health["status"] == "developer_buildings_ready"
            and health["counts"]["offers"] == health["counts"]["complexes"] == 0)
    assert (list_view["snapshot_id"] == map_view["snapshot_id"] ==
            points["registry_snapshot_id"] == health["registry_snapshot_id"] == snapshot_id)
    assert (list_view["map_run_id"] == map_view["map_run_id"] ==
            points["map_run_id"] == points["group_evidence"]["map_run_id"] == run_id)
    assert points["counts"]["points"] == len(snapshot["by_point"])
    assert points["counts"]["mapped_construction_points"] == 0
    assert all(row["point_type"] == "developer_building" for row in points["items"])
    assert all("publisher_project_id" not in row and "source_group" not in row
               for row in points["items"])
    assert any(dataset["dataset_id"] == "developer_building_registry" and
               dataset["records_total"] == len(snapshot["rows"])
               for dataset in coverage["datasets"])
    group_members = {point_id for group in points["group_evidence"]["groups"]
                     for point_id in group["point_ids"]}
    assert group_members <= {row["point_id"] for row in points["items"]}
    return {"run_id": run_id, "snapshot_id": snapshot_id,
            "buildings": len(snapshot["rows"]), "points": len(points["items"]),
            "groups_visible": len(points["group_evidence"]["groups"]),
            "unlocated": list_view["facets"]["unlocated_count"],
            "data_mode": health["data_mode"], "offers": health["counts"]["offers"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-id")
    mode.add_argument("--active", action="store_true")
    args = parser.parse_args()
    snapshot = developer_map.resolve_active() if args.active else developer_map.load_staged(args.run_id)
    if snapshot is None:
        raise SystemExit("Developer map active pointer is absent")
    result = check(snapshot, staged=not args.active)
    print("PASS " + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
