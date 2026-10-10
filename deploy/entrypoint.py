"""Fail closed on a missing real-data release, then start the API process."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath


APP_ROOT = Path(__file__).resolve().parents[1]


def preflight() -> dict[str, int | str]:
    if os.environ.get("NOVOSTROY_DATA_MODE") != "real":
        raise RuntimeError("Production API requires NOVOSTROY_DATA_MODE=real")
    if os.environ.get("NOVOSTROY_REAL_STORE") != "empty":
        raise RuntimeError("This release has no cleared apartment offer store")
    if os.environ.get("NOVOSTROY_REGISTRY_MODE") != "estateline":
        raise RuntimeError("Production API requires the checked building registry")
    if os.environ.get("NOVOSTROY_EXPOSE_SOURCES", "0") != "0":
        raise RuntimeError("Production API must hide provider source fields")
    if os.environ.get("NOVOSTROY_GEODATA_PYTHON") != sys.executable:
        raise RuntimeError("Nearby worker must use the image Python")
    for module in ("fastapi", "pydantic", "pyarrow.parquet", "shapely", "valhalla"):
        importlib.import_module(module)

    sys.path.insert(0, str(APP_ROOT))
    from backend.developer_experience import resolve_active as load_developer_experience
    from backend.developer_map import resolve_active as load_developer_map
    from backend.models import load_model_run
    from backend.nearby import _graph_version
    from backend.point_labels import load_point_labels
    from backend.registry import load_registry
    from backend.store import ASSET_ROOT, asset_manifest

    pointer = asset_manifest()
    if pointer.get("public_redacted_source_ids") is not True:
        raise RuntimeError("Release asset pointer is not sanitized")
    for key in ("city_tileset_url", "ground_tileset_url", "roads_tileset_url",
                "parks_tileset_url", "water_tileset_url"):
        url = pointer.get(key)
        if not isinstance(url, str) or not url.startswith("/assets/"):
            raise RuntimeError(f"Missing public asset pointer: {key}")
        relative = PurePosixPath(url.removeprefix("/assets/"))
        if (len(relative.parts) != 2 or relative.name != "tileset.json"
                or not (ASSET_ROOT / str(relative)).is_file()):
            raise RuntimeError(f"Missing public tileset: {key}")
        manifest = ASSET_ROOT / relative.parts[0] / "manifest.json"
        if json.loads(manifest.read_text(encoding="utf-8")).get("public_redacted_source_ids") is not True:
            raise RuntimeError(f"Unsanitized public tileset: {key}")
    registry = load_registry()
    run = load_model_run()
    developer = load_developer_map()
    load_point_labels(run)
    if not registry["items"] or not run["items"] or not developer["rows"]:
        raise RuntimeError("Registry, model, or developer-building dataset is empty")
    developer_manifest = developer["manifest"]
    if developer_manifest["map_points_total"] < 1:
        raise RuntimeError("Developer-building map has no verified points")
    experience = load_developer_experience(developer)
    if experience is None:
        raise RuntimeError("The atomic developer menu/model pointer is missing")
    menu, selected_models = experience
    if (len(menu["buildings"]) != developer_manifest["buildings_total"]
            or menu["manifest"]["map_run_id"] != developer_manifest["run_id"]
            or selected_models["manifest"]["menu_run_id"] != menu["manifest"]["run_id"]):
        raise RuntimeError("Developer menu and selected models differ from active map")

    tile_dir = Path(os.environ.get("NOVOSTROY_VALHALLA_TILE_DIR", ""))
    expected_tiles = APP_ROOT / "data/social/valhalla_moscow/tiles"
    if tile_dir != expected_tiles or not tile_dir.is_dir():
        raise RuntimeError("Verified Valhalla tile mount is missing")
    if _graph_version() in {"invalid", "missing"}:
        raise RuntimeError("Verified walking graph metadata is missing")
    graph = json.loads((APP_ROOT / "data/social/valhalla_moscow/build_manifest.json")
                       .read_text(encoding="utf-8"))
    if len(list(tile_dir.rglob("*.gph"))) != graph["tile_file_count"]:
        raise RuntimeError("Walking graph tile count differs from manifest")
    social = APP_ROOT / "data/social"
    for name in ("osm_moscow_social_pois_261008.parquet",
                 "osm_moscow_social_pois_261008.manifest.json",
                 "oms_verified_clinics_2026.json",
                 "oms_verified_clinics_2026.manifest.json",
                 "metro_station_access_261008.json",
                 "metro_station_access_261008.manifest.json"):
        if not (social / name).is_file():
            raise RuntimeError(f"Missing nearby input: {name}")

    runtime = APP_ROOT / "data/runtime"
    if not runtime.is_dir():
        raise RuntimeError("Writable analytics runtime mount is missing")
    with tempfile.NamedTemporaryFile(dir=runtime, prefix=".write-check-", delete=True):
        pass
    return {"catalog_view": "developer_buildings", "registry_records": len(registry["items"]),
            "model_candidates": len(run["items"]), "model_run_id": run["model_run_id"],
            "map_run_id": developer_manifest["run_id"],
            "developer_buildings": len(developer["rows"]),
            "developer_map_points": developer_manifest["map_points_total"],
            "unit_run_id": menu["manifest"]["run_id"],
            "selected_model_run_id": selected_models["manifest"]["run_id"],
            "selected_models": len(selected_models["by_registry"])}


def main() -> None:
    result = preflight()
    print("Production data preflight passed: " + json.dumps(result, sort_keys=True), flush=True)
    port = os.environ.get("PORT", "8000")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("PORT must be a TCP port number")
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "backend.app:app",
                              "--host", "0.0.0.0", "--port", port, "--proxy-headers"])


if __name__ == "__main__":
    main()
