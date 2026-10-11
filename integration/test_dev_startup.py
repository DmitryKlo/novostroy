"""Offline checks for the managed launch and public-stack consistency gate."""

from __future__ import annotations

import errno
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

from scripts import dev


BASE = "http://127.0.0.1:5173"


def test_direct_script_bootstraps_shared_validator_import_from_any_directory(tmp_path):
    script = Path(dev.__file__).resolve()
    result = subprocess.run([sys.executable, "-I", "-c",
        "import runpy,sys; runpy.run_path(sys.argv[1]); "
        "from scripts.runtime_overlay import developer_overlay_files; print('validator import PASS')",
        str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "validator import PASS"


def _public_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    registry: bool = True,
    city: str = "/assets/public-citywide-v9/tileset.json",
    ground: str | None = "/assets/public-ground-v3/tileset.json",
) -> tuple[dict[str, dict[str, object]], list[str]]:
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.delenv("NOVOSTROY_REGISTRY_MODE", raising=False)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifest.json").write_text(json.dumps({
        "city_tileset_url": city,
        "ground_tileset_url": ground,
    }), encoding="utf-8")
    if registry:
        source = tmp_path / "data/property/estateline"
        source.mkdir(parents=True)
        (source / "projects.jsonl").write_text("{}\n", encoding="utf-8")
        (source / "source_manifest.json").write_text("{}", encoding="utf-8")
    view = "building_registry" if registry else "demo"
    map_route = "/v1/registry/map" if registry else "/v1/map"
    tilesets = [{"kind": "city_volumes", "url": city}]
    if ground:
        tilesets.insert(0, {"kind": "ground_buildings", "url": ground})
    documents: dict[str, dict[str, object]] = {
        f"{BASE}/v1/health": {"catalog_view": view},
        f"{BASE}{map_route}": {"tilesets": tilesets},
        f"{BASE}{city}": {"root": {"children": [
            {"content": {"uri": f"region_{number}_0.tileset.json"}}
            for number in (1, 2, 3)
        ]}},
    }
    for number in (1, 2, 3):
        documents[f"{BASE}{city.rsplit('/', 1)[0]}/region_{number}_0.tileset.json"] = {
            "root": {"content": {"uri": f"region_{number}_0.glb"}},
        }
    if ground:
        documents[f"{BASE}{ground}"] = {"root": {"content": {"uri": "overview_city.glb"}}}
    read: list[str] = []

    def public_json(url: str) -> dict[str, object]:
        read.append(url)
        return documents[url]

    monkeypatch.setattr(dev, "_read_public_json", public_json)
    return documents, read


def test_socket_denial_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied_socket() -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(dev.socket, "socket", denied_socket)
    with pytest.raises(RuntimeError, match="Local TCP sockets are denied"):
        dev._port_open(8000)


def test_startup_checks_registry_and_external_city_regions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, read = _public_fixture(monkeypatch, tmp_path)
    dev._wait_public_city_tileset(BASE)
    assert f"{BASE}/v1/registry/map" in read
    assert f"{BASE}/assets/public-ground-v3/tileset.json" in read
    assert [url for url in read if "/region_" in url] == [
        f"{BASE}/assets/public-citywide-v9/region_{number}_0.tileset.json"
        for number in (1, 2, 3)
    ]


def test_startup_checks_active_developer_buildings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, read = _public_fixture(monkeypatch, tmp_path)
    run_id = "developer-map-v1-0123456789abcdef"
    snapshot_id = "developer-map-snapshot-0123456789abcdef"
    pointer = tmp_path / "data/property/developer_map/active.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"schema_version": "developer-map-pointer/1",
                                   "run_id": run_id, "manifest_sha256": "a" * 64}),
                       encoding="utf-8")
    documents[f"{BASE}/v1/health"] = {
        "catalog_view": "developer_buildings", "data_mode": "real",
        "registry_snapshot_id": snapshot_id, "map_run_id": run_id,
    }
    documents[f"{BASE}/v1/registry/map"].update({
        "dataset_id": "developer_building_registry", "snapshot_id": snapshot_id,
        "map_run_id": run_id, "counts": {"records": 2, "geocoded": 1},
    })
    documents[f"{BASE}/v1/registry/buildings?limit=1"] = {
        "dataset_id": "developer_building_registry", "snapshot_id": snapshot_id,
        "map_run_id": run_id, "dataset_total": 2,
    }
    documents[f"{BASE}/v1/map/points"] = {
        "dataset_id": "developer_map_points", "registry_snapshot_id": snapshot_id,
        "map_run_id": run_id, "dataset_total": 2,
        "counts": {"points": 1, "registry_points": 1,
                   "registry_points_total": 1, "mapped_construction_points": 0},
        "items": [{"point_type": "developer_building"}],
    }
    dev._wait_public_city_tileset(BASE)
    assert f"{BASE}/v1/registry/map" in read
    assert f"{BASE}/v1/registry/buildings?limit=1" in read
    assert f"{BASE}/v1/map/points" in read
    documents[f"{BASE}/v1/map/points"]["map_run_id"] = "developer-map-v1-ffffffffffffffff"
    with pytest.raises(RuntimeError, match="developer-building list/map/points"):
        dev._wait_public_city_tileset(BASE)


def test_startup_checks_published_model_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, read = _public_fixture(monkeypatch, tmp_path)
    run_id = "models-v1-0123456789abcdef"
    models = tmp_path / "data/models"
    (models / run_id).mkdir(parents=True)
    (models / "active.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (models / run_id / "manifest.json").write_text(
        json.dumps({"run_id": run_id, "records_total": 2317}), encoding="utf-8")
    summary_route = f"{BASE}/v1/models/candidates/summary"
    documents[summary_route] = {
        "dataset_id": "model_candidates", "model_run_id": run_id,
        "run_status": "ready", "counts": {"scanned": 2317},
    }
    documents[f"{BASE}/v1/health"]["registry_snapshot_id"] = "registry-test"
    documents[f"{BASE}/v1/registry/map"]["counts"] = {"geocoded": 1}
    points_route = f"{BASE}/v1/map/points"
    documents[points_route] = {
        "dataset_id": "map_points", "model_run_id": run_id,
        "registry_snapshot_id": "registry-test", "dataset_total": 2,
        "counts": {"points": 2, "registry_points": 1,
                   "registry_points_total": 1, "mapped_construction_points": 1},
        "items": [{"point_id": "mappt_registry"}, {"point_id": "mappt_mapped"}],
    }
    dev._wait_public_city_tileset(BASE)
    assert summary_route in read and points_route in read
    documents[summary_route]["model_run_id"] = "models-v1-0000000000000000"
    with pytest.raises(RuntimeError, match="model-candidate API differs"):
        dev._wait_public_city_tileset(BASE)
    documents[summary_route]["model_run_id"] = run_id
    documents[points_route]["model_run_id"] = "models-v1-0000000000000000"
    with pytest.raises(RuntimeError, match="point-map API differs"):
        dev._wait_public_city_tileset(BASE)


@pytest.mark.parametrize("failure", [None, "wrong_run", "stale_city", "stale_region", "stale_list"])
def test_startup_checks_hash_pinned_v3_city_patch(monkeypatch, tmp_path, failure):
    from scripts.tests.test_package_deploy_data import _fixture
    from scripts.tests.test_runtime_overlay import stage_city_patch
    _fixture(tmp_path)
    (_, pointer, run, _, _, _), _, _ = stage_city_patch(tmp_path)
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "_expected_catalog_view", lambda: "developer_buildings")
    common = {"map_run_id": pointer["map_run_id"], "overlay_run_id": pointer["overlay_run_id"]}
    snapshot = pointer["map_snapshot_id"]
    prefix = f"/v1/developer-overlay/{pointer['overlay_run_id']}/"
    assets = json.loads((tmp_path / "assets/manifest.json").read_text())
    models = json.loads((tmp_path / "data/models/active.json").read_text())
    documents = {
        "/v1/health": {**common, "catalog_view": "developer_buildings", "data_mode": "real",
                       "registry_snapshot_id": snapshot},
        "/v1/registry/map": {**common, "dataset_id": "developer_building_registry", "snapshot_id": snapshot,
            "counts": {"records": 1, "geocoded": 1}, "tilesets": [
                {"kind": "city_volumes", "url": prefix + "city-patch/tileset.json"},
                {"kind": "ground_buildings", "url": assets["ground_tileset_url"]}]},
        "/v1/registry/buildings?limit=1": {**common, "dataset_id": "developer_building_registry",
                                         "snapshot_id": snapshot, "dataset_total": 1},
        "/v1/map/points": {**common, "dataset_id": "developer_map_points", "registry_snapshot_id": snapshot,
            "dataset_total": 1, "counts": {"points": 1, "registry_points": 1,
                "registry_points_total": 1, "mapped_construction_points": 0},
            "items": [{"point_type": "developer_building"}]},
        "/v1/models/candidates/summary": {"dataset_id": "model_candidates", "run_id": models["run_id"],
            "model_run_id": models["run_id"], "run_status": "ready", "counts": {"scanned": 1}},
    }
    if failure == "wrong_run":
        documents["/v1/registry/map"]["overlay_run_id"] = "developer-overlay-v1-" + "f" * 16
    elif failure == "stale_city":
        documents["/v1/registry/map"]["tilesets"][0]["url"] = assets["city_tileset_url"]
    elif failure == "stale_list":
        documents["/v1/registry/buildings?limit=1"]["overlay_run_id"] = None
    read = []
    def public_json(url):
        route = url.removeprefix(BASE)
        read.append(route)
        if route in documents:
            return documents[route]
        path = (run / route.removeprefix(prefix) if route.startswith(prefix)
                else tmp_path / route.removeprefix("/"))
        document = json.loads(path.read_text())
        if failure == "stale_region" and "/city-patch/regions/" in route:
            document["root"] = {"stale": True}
        return document
    monkeypatch.setattr(dev, "_read_public_json", public_json)
    if failure is None:
        dev._wait_public_city_tileset(BASE)
        assert any("/city-patch/regions/" in route for route in read)
    else:
        with pytest.raises(RuntimeError):
            dev._wait_public_city_tileset(BASE)


def test_explicit_registry_off_allows_demo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, read = _public_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("NOVOSTROY_REGISTRY_MODE", "off")
    documents[f"{BASE}/v1/health"] = {"catalog_view": "demo"}
    documents[f"{BASE}/v1/map"] = documents[f"{BASE}/v1/registry/map"]
    dev._wait_public_city_tileset(BASE)
    assert f"{BASE}/v1/map" in read


def test_startup_rejects_stale_demo_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, _ = _public_fixture(monkeypatch, tmp_path)
    documents[f"{BASE}/v1/health"] = {"catalog_view": "demo"}
    with pytest.raises(RuntimeError, match="expects 'building_registry'"):
        dev._wait_public_city_tileset(BASE)


def test_startup_rejects_stale_asset_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, _ = _public_fixture(monkeypatch, tmp_path)
    tilesets = documents[f"{BASE}/v1/registry/map"]["tilesets"]
    assert isinstance(tilesets, list)
    tilesets[-1]["url"] = "/assets/public-citywide-v8/tileset.json"
    with pytest.raises(RuntimeError, match="differs from active manifest"):
        dev._wait_public_city_tileset(BASE)


def test_startup_rejects_missing_external_region(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, _ = _public_fixture(monkeypatch, tmp_path)
    missing = f"{BASE}/assets/public-citywide-v9/region_2_0.tileset.json"
    del documents[missing]
    with pytest.raises(KeyError, match="region_2_0"):
        dev._wait_public_city_tileset(BASE)


def test_public_json_error_identifies_stale_route(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_url: str, timeout: float) -> io.BytesIO:
        del timeout
        raise URLError("404 Not Found")

    monkeypatch.setattr(dev, "urlopen", missing)
    with pytest.raises(RuntimeError, match="stale or serving another checkout"):
        dev._read_public_json(f"{BASE}/assets/public-citywide-v9/region_2_0.tileset.json")


def test_startup_rejects_missing_ground(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, _ = _public_fixture(monkeypatch, tmp_path)
    tilesets = documents[f"{BASE}/v1/registry/map"]["tilesets"]
    assert isinstance(tilesets, list)
    tilesets.pop(0)
    with pytest.raises(RuntimeError, match="valid public ground_buildings"):
        dev._wait_public_city_tileset(BASE)


def test_city_v9_requires_ground_even_in_local_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _public_fixture(monkeypatch, tmp_path, ground=None)
    with pytest.raises(RuntimeError, match=r"city v8\+ requires"):
        dev._asset_tree_ready()
    with pytest.raises(RuntimeError, match=r"city v8\+ requires"):
        dev._wait_public_city_tileset(BASE)


def test_startup_rejects_non_public_city_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    documents, _ = _public_fixture(monkeypatch, tmp_path)
    tilesets = documents[f"{BASE}/v1/registry/map"]["tilesets"]
    assert isinstance(tilesets, list)
    tilesets[-1]["url"] = "/assets/../manifest.json"
    with pytest.raises(RuntimeError, match="valid public city_volumes"):
        dev._wait_public_city_tileset(BASE)


def test_asset_preflight_follows_external_region_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    assets = tmp_path / "assets"
    city = assets / "public-citywide-v8"
    ground = assets / "public-ground-v2"
    city.mkdir(parents=True)
    ground.mkdir(parents=True)
    (assets / "manifest.json").write_text(json.dumps({
        "city_tileset_url": "/assets/public-citywide-v8/tileset.json",
        "ground_tileset_url": "/assets/public-ground-v2/tileset.json",
    }), encoding="utf-8")
    (ground / "tileset.json").write_text(json.dumps({"root": {}}), encoding="utf-8")
    (city / "tileset.json").write_text(json.dumps({"root": {
        "content": {"uri": "region_1_2.tileset.json"},
    }}), encoding="utf-8")
    (city / "region_1_2.tileset.json").write_text(json.dumps({"root": {
        "content": {"uri": "region_1_2.glb"},
    }}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="region_1_2.glb"):
        dev._asset_tree_ready()
    (city / "region_1_2.glb").write_bytes(b"glTF")
    assert dev._asset_tree_ready()


def test_verify_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    visited: list[str] = []
    monkeypatch.setattr(dev, "_asset_tree_ready", lambda: True)
    monkeypatch.setattr(dev, "_load", lambda: {})
    monkeypatch.setattr(dev, "_port_open", lambda port: visited.append(str(port)) or True)
    monkeypatch.setattr(dev, "_wait_public_city_tileset", lambda base: visited.append(base))
    dev.verify()
    assert visited == ["8000", "5173", BASE]


def test_recovery_command_requires_exact_checkout_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    vite = tmp_path / "web/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("", encoding="utf-8")
    assert dev._matches_listener_command(8000, tmp_path, [
        "/usr/bin/python", "-m", "uvicorn", "backend.app:app", "--port", "8000",
    ])
    assert not dev._matches_listener_command(8000, tmp_path, [
        "/usr/bin/python", "-m", "uvicorn", "other.app:app", "--port", "8000",
    ])
    assert not dev._matches_listener_command(8000, tmp_path / "other", [
        "/usr/bin/python", "-m", "uvicorn", "backend.app:app", "--port", "8000",
    ])
    assert dev._matches_listener_command(5173, tmp_path / "web", [
        "/usr/bin/node", str(vite), "preview", "--port", "5173", "--strictPort",
    ])
    assert not dev._matches_listener_command(5173, tmp_path / "web", [
        "/usr/bin/node", str(vite), "preview", "--port", "5174", "--strictPort",
    ])


def test_recovery_refuses_unknown_owner_before_signaling(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[int] = []
    monkeypatch.setattr(dev, "_load", lambda: {})
    monkeypatch.setattr(dev, "_port_open", lambda _port: True)
    monkeypatch.setattr(dev, "_listener_pids", lambda port: {11 if port == 8000 else 22})
    monkeypatch.setattr(dev, "_start_ticks", lambda _pid: 7)
    monkeypatch.setattr(dev, "_old_listener_matches", lambda port, _pid: port == 8000)
    monkeypatch.setattr(dev.os, "kill", lambda pid, _signal: signals.append(pid))
    with pytest.raises(RuntimeError, match="not a verified Novostroy server"):
        dev.recover()
    assert signals == []


def test_recovery_stops_only_verified_listeners_then_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied = {8000, 5173}
    signals: list[int] = []
    started: list[tuple[bool, bool]] = []
    order: list[str] = []
    monkeypatch.setattr(dev, "_load", lambda: {})
    monkeypatch.setattr(dev, "_port_open", lambda port: port in occupied)
    monkeypatch.setattr(dev, "_listener_pids", lambda port: {11 if port == 8000 else 22})
    monkeypatch.setattr(dev, "_start_ticks", lambda _pid: 7)
    monkeypatch.setattr(dev, "_old_listener_matches", lambda _port, _pid: True)
    monkeypatch.setattr(dev, "_prepare_start", lambda *, preview: order.append(f"prepare:{preview}"))
    def stop_listener(pid: int, _signal: int) -> None:
        order.append(f"signal:{pid}")
        signals.append(pid)
        occupied.remove(8000 if pid == 11 else 5173)
    monkeypatch.setattr(dev.os, "kill", stop_listener)
    monkeypatch.setattr(dev, "start", lambda *, lan, preview: started.append((lan, preview)))
    dev.recover(lan=True, preview=True)
    assert signals == [11, 22]
    assert started == [(True, True)]
    assert order == ["prepare:True", "signal:11", "signal:22"]


def test_recovery_preflight_failure_keeps_legacy_site_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    monkeypatch.setattr(dev, "_load", lambda: {})
    monkeypatch.setattr(dev, "_port_open", lambda _port: True)
    monkeypatch.setattr(dev, "_listener_pids", lambda port: {11 if port == 8000 else 22})
    monkeypatch.setattr(dev, "_start_ticks", lambda _pid: 7)
    monkeypatch.setattr(dev, "_old_listener_matches", lambda _port, _pid: True)
    def preflight(*, preview: bool) -> None:
        assert preview
        raise RuntimeError("preview build failed")
    monkeypatch.setattr(dev, "_prepare_start", preflight)
    monkeypatch.setattr(dev.os, "kill", lambda pid, _signal: signals.append(pid))
    with pytest.raises(RuntimeError, match="preview build failed"):
        dev.recover(preview=True)
    assert signals == []
