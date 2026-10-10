"""Read-only direct/ASGI gate for selected schematic building models."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_DATA_MODE"] = "real"
os.environ["NOVOSTROY_REAL_STORE"] = "empty"
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api, developer_experience, developer_map, developer_models, developer_units  # noqa: E402


def _schema(payload: dict[str, Any]) -> None:
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperSelectedModel"}).validate(payload)


async def _asgi(path: str) -> tuple[int, bytes]:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "server": ("localhost", 8000),
             "client": ("127.0.0.1", 12345), "headers": []}
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await asyncio.wait_for(api.app(scope, receive, send), timeout=30)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages
                    if item["type"] == "http.response.body")
    return start["status"], body


def check(map_run: dict[str, Any], menu: dict[str, Any],
          models: dict[str, Any], *, staged: bool) -> dict[str, Any]:
    menu_path = (developer_units.BASE / "runs" / menu["manifest"]["run_id"] /
                 "manifest.json")
    assert models["manifest"]["menu_run_id"] == menu["manifest"]["run_id"]
    assert models["manifest"]["menu_manifest_sha256"] == hashlib.sha256(
        menu_path.read_bytes()).hexdigest()
    if staged:
        api.developer_experience.resolve_active = lambda _: (menu, models)
    registry_id = next(iter(models["by_registry"]))
    descriptor = api.developer_building_model(registry_id)
    _schema(descriptor)
    assert descriptor["status"] == "ready"
    assert descriptor["unit_run_id"] == menu["manifest"]["run_id"]
    assert descriptor["map_run_id"] == map_run["manifest"]["run_id"]
    assert descriptor["model_run_id"] == models["manifest"]["run_id"]
    assert descriptor["label"] == "реконструкция"
    assert descriptor["geometry_basis"] == "source_building_footprint"
    assert "source_geometry_basis" not in json.dumps(descriptor)
    # These small selected-only assets are fully validated, rather than merely
    # trusting the manifest's model count or sampling one file.
    for row in models["by_registry"].values():
        assert len(developer_models.asset_bytes(models, row["asset_sha256"])) == row["asset_bytes"]

    async def inline_run_sync(func: Any, *args: Any, **_: Any) -> Any:
        return func(*args)

    anyio.to_thread.run_sync = inline_run_sync

    async def routes() -> None:
        status, body = await _asgi(f"/v1/developer-buildings/{registry_id}/model")
        assert status == 200
        _schema(json.loads(body))
        status, body = await _asgi(descriptor["asset_url"])
        assert status == 200 and body.startswith(b"glTF")
        status, body = await _asgi("/v1/developer-model-assets/" + "0" * 64)
        assert status == 404 and json.loads(body)["detail"]["code"] == "not_found"

    asyncio.run(routes())
    return {"map_run_id": map_run["manifest"]["run_id"],
            "menu_run_id": menu["manifest"]["run_id"],
            "model_run_id": models["manifest"]["run_id"],
            "selected_models": len(models["by_registry"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--model-run-id")
    mode.add_argument("--active", action="store_true")
    parser.add_argument("--menu-run-id")
    args = parser.parse_args()
    map_run = developer_map.resolve_active()
    if map_run is None:
        raise SystemExit("Developer map pointer is absent")
    experience = developer_experience.resolve_active(map_run) if args.active else None
    menu = (experience[0] if experience is not None else None) if args.active else \
        developer_units.load_staged(args.menu_run_id, map_run)
    if menu is None:
        raise SystemExit("Developer unit menu pointer is absent")
    models = (experience[1] if experience is not None else None) if args.active else \
        developer_models.load_staged(args.model_run_id, map_run)
    if models is None:
        raise SystemExit("Selected model pointer is absent")
    print("PASS " + json.dumps(check(map_run, menu, models, staged=not args.active),
                               ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
