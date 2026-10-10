"""Offline direct/ASGI gate for one immutable developer apartment menu.

Use ``--run-id`` before publication and ``--active`` afterward. This checker
never fetches source pages, mutates pointers or restarts a managed server.
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
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_DATA_MODE"] = "real"
os.environ["NOVOSTROY_REAL_STORE"] = "empty"
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api, developer_experience, developer_map, developer_units  # noqa: E402


def _schema(name: str, payload: dict[str, Any]) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"},
                         format_checker=FormatChecker()).validate(payload)


async def _asgi(path: str) -> tuple[int, Any]:
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
    return start["status"], json.loads(body) if body else None


def check(menu: dict[str, Any], map_run: dict[str, Any], *, staged: bool) -> dict[str, Any]:
    if staged:
        api.developer_experience.resolve_active = lambda _map: (menu, {})
    map_id = map_run["manifest"]["run_id"]
    snapshot_id = map_run["manifest"]["snapshot_id"]
    menu_id = menu["manifest"]["run_id"]
    assert menu["manifest"]["map_run_id"] == map_id
    assert menu["manifest"]["map_snapshot_id"] == snapshot_id
    assert set(menu["buildings"]) == set(map_run["by_registry"])
    assert len(menu["units"]) == sum(row["unit_count"] for row in map_run["rows"])
    selected = next((row for row in map_run["rows"] if "Лэйк" in row["name"]),
                    map_run["rows"][0])
    registry_id = selected["registry_id"]
    units = api.developer_building_units(registry_id, section_id=None, floor_id=None,
                                         floor_known=None, limit=100, offset=0)
    floors = api.developer_building_floors(registry_id)
    _schema("DeveloperUnitsResult", units)
    _schema("DeveloperFloorsResult", floors)
    assert units["total"] == selected["unit_count"]
    assert units["registry_snapshot_id"] == floors["registry_snapshot_id"] == snapshot_id
    assert units["map_run_id"] == floors["map_run_id"] == map_id
    assert units["unit_run_id"] == floors["unit_run_id"] == menu_id
    assert floors["unassigned_unit_count"] == sum(
        row["floor_number"] is None for row in menu["units_by_building"][registry_id])
    if floors["unassigned_unit_count"]:
        unknown = api.developer_building_units(registry_id, section_id=None, floor_id=None,
                                               floor_known=False, limit=100, offset=0)
        _schema("DeveloperUnitsResult", unknown)
        assert unknown["total"] == floors["unassigned_unit_count"]
    first_unit = units["items"][0]
    detail = api.developer_building_unit(registry_id, first_unit["unit_id"])
    _schema("DeveloperUnitDetail", detail)
    if floors["sections"]:
        first_floor = floors["sections"][0]["floors"][0]
        floor_detail = api.developer_building_floor(registry_id, first_floor["floor_id"])
        _schema("DeveloperFloorDetail", floor_detail)
        assert floor_detail["hotspots"] == []
    for source_floor_id, plans in menu["plans_by_floor"].items():
        if not any(plan.get("regions") for plan in plans):
            continue
        floor = menu["floors"][source_floor_id]
        detail = api.developer_building_floor(floor["registry_id"], source_floor_id)
        _schema("DeveloperFloorDetail", detail)
        assert detail["plan"] is not None
        assert detail["plan_regions"]
        assert all(region["unit_id"] is None and region["unit_action"] is False
                   for region in detail["plan_regions"])
        assert all(region["source_area_m2"] is None or region["source_area_m2"] > 0
                   for region in detail["plan_regions"])
    for asset_sha in menu["assets"]:
        content, mime = developer_units.asset_bytes(menu, asset_sha)
        assert content and mime in {"image/png", "image/jpeg", "image/webp"}
    public = json.dumps([units, floors, detail], ensure_ascii=False)
    for private in ("source_group", "publisher_unit_id", "publisher_building_id",
                    "source_url", "source_evidence_sha256", "http://", "https://"):
        assert private not in public

    async def inline_run_sync(func: Any, *args: Any, **_: Any) -> Any:
        return func(*args)

    anyio.to_thread.run_sync = inline_run_sync

    async def routes() -> None:
        status, route_units = await _asgi(
            f"/v1/developer-buildings/{registry_id}/units?limit=2&offset=0")
        assert status == 200
        _schema("DeveloperUnitsResult", route_units)
        assert route_units["total"] == units["total"] and len(route_units["items"]) <= 2
        status, route_detail = await _asgi(
            f"/v1/developer-buildings/{registry_id}/units/{first_unit['unit_id']}")
        assert status == 200
        _schema("DeveloperUnitDetail", route_detail)
        status, unavailable = await _asgi(
            f"/v1/developer-buildings/{registry_id}/units/unit_{'f' * 24}")
        assert status == 404 and unavailable["detail"]["code"] == "not_found"

    asyncio.run(routes())
    return {"map_run_id": map_id, "menu_run_id": menu_id,
            "buildings": len(menu["buildings"]), "units": len(menu["units"]),
            "floors": len(menu["floors"]), "prepared_assets": len(menu["assets"]),
            "sample_building_units": units["total"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-id")
    mode.add_argument("--active", action="store_true")
    args = parser.parse_args()
    map_run = developer_map.resolve_active()
    if map_run is None:
        raise SystemExit("Developer map pointer is absent")
    experience = developer_experience.resolve_active(map_run) if args.active else None
    menu = (experience[0] if experience is not None else None) if args.active else \
        developer_units.load_staged(args.run_id, map_run)
    if menu is None:
        raise SystemExit("Developer unit menu pointer is absent")
    print("PASS " + json.dumps(check(menu, map_run, staged=not args.active),
                               ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
