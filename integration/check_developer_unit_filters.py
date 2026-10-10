"""Offline list/map/point parity for source-observed apartment constraints."""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_DATA_MODE"] = "real"
os.environ["NOVOSTROY_REAL_STORE"] = "empty"
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api, developer_experience, developer_map, developer_units  # noqa: E402


async def _asgi(path: str) -> tuple[int, dict[str, Any]]:
    raw_path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": raw_path,
             "raw_path": raw_path.encode(), "root_path": "", "query_string": query.encode(),
             "server": ("localhost", 8000), "client": ("127.0.0.1", 12345),
             "headers": []}
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(api.app(scope, receive, send), timeout=30)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    return start["status"], json.loads(body)


def _schema(name: str, payload: dict[str, Any]) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"},
                         format_checker=FormatChecker()).validate(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--menu-run-id")
    mode.add_argument("--active", action="store_true")
    args = parser.parse_args()
    map_run = developer_map.resolve_active()
    assert map_run is not None
    if args.active:
        experience = developer_experience.resolve_active(map_run)
        assert experience is not None
        menu = experience[0]
    else:
        menu = developer_units.load_staged(args.menu_run_id, map_run)
        api.developer_experience.resolve_active = lambda _: (menu, {})

    async def inline_run_sync(func: Any, *args: Any, **_: Any) -> Any:
        return func(*args)

    anyio.to_thread.run_sync = inline_run_sync
    positive = "query=%D0%9B%D1%8D%D0%B9%D0%BA&price_max_rub=52000000&area_min_m2=80&rooms=3&unit_floor_min=10"
    negative = "query=%D0%9B%D1%8D%D0%B9%D0%BA&price_max_rub=52000000&unit_floor_min=13"
    routes = (("/v1/registry/buildings", "DeveloperRegistryListResult", "records_total"),
              ("/v1/registry/map", "DeveloperRegistryMapResult", "counts"),
              ("/v1/map/points", "DeveloperMapPointsResult", "counts"))

    async def check() -> None:
        positive_ids: list[set[str]] = []
        for route, schema, _ in routes:
            status, body = await _asgi(route + "?" + positive)
            assert status == 200, (route, body)
            _schema(schema, body)
            assert body["unit_run_id"] == menu["manifest"]["run_id"]
            assert body["applied_filters"]["rooms"] == 3
            assert len(body["items"]) == 1
            assert body["items"][0]["matching_units_count"] == 1
            positive_ids.append({item["registry_id"] for item in body["items"]})
            status, empty = await _asgi(route + "?" + negative)
            assert status == 200 and not empty["items"], (route, empty)
            status, invalid = await _asgi(route + "?area_min_m2=80&area_max_m2=40")
            assert status == 422 and invalid["detail"]["code"] == "invalid_request"
        assert positive_ids[0] == positive_ids[1] == positive_ids[2]
        registry_id = next(iter(positive_ids[0]))
        units_route = f"/v1/developer-buildings/{registry_id}/units"
        unit_filters = positive.split("&", 1)[1]
        status, units = await _asgi(units_route + "?" + unit_filters + "&limit=1")
        assert status == 200, units
        _schema("DeveloperUnitsResult", units)
        assert units["total"] == 1 and len(units["items"]) == 1
        assert units["applied_filters"]["rooms"] == 3
        assert units["unit_run_id"] == menu["manifest"]["run_id"]
        status, page_two = await _asgi(units_route + "?" + unit_filters + "&limit=1&offset=1")
        assert status == 200 and page_two["total"] == 1 and page_two["items"] == []
        status, empty_units = await _asgi(units_route + "?price_max_rub=52000000&unit_floor_min=13")
        assert status == 200 and empty_units["total"] == 0 and empty_units["items"] == []
        status, invalid_units = await _asgi(units_route + "?area_min_m2=80&area_max_m2=40")
        assert status == 422 and invalid_units["detail"]["code"] == "invalid_request"

    asyncio.run(check())
    print("PASS developer apartment filters: one-unit AND, list/map/points/menu parity, pagination, reversed-range 422")


if __name__ == "__main__":
    main()
