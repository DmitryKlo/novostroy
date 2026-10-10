"""Start, stop, or inspect the two local demo servers safely on Linux.

Invoke with `rtk python scripts/dev.py start|recover [--lan] [--preview]|restart-api|verify|stop|status`.
Stop controls managed process groups; recover signals only same-checkout legacy
listeners after strict PID, command, directory, and port verification.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "dev-processes.json"
RTK = shutil.which("rtk")
DEV_PYTHON = ROOT / ".runtime/dev-env/bin/python"
FALLBACK_PYTHON = ROOT / ".venv/bin/python"
DEVELOPER_MAP_POINTER = ROOT / "data/property/developer_map/active.json"


def _api_environment() -> dict[str, str]:
    env = dict(os.environ)
    if DEVELOPER_MAP_POINTER.exists() or DEVELOPER_MAP_POINTER.is_symlink():
        # The publisher map is a real-data view. Never pair it with demo offers.
        env["NOVOSTROY_DATA_MODE"] = "real"
        env["NOVOSTROY_REAL_STORE"] = "empty"
    else:
        env["NOVOSTROY_DATA_MODE"] = "demo"
    return env


class LocalSocketUnavailable(RuntimeError):
    """The host policy does not permit the local TCP preflight."""


def _city_requires_ground(route: str) -> bool:
    match = re.fullmatch(r"public-citywide-v([0-9]+)", PurePosixPath(route).parent.name)
    return match is not None and int(match.group(1)) >= 8


def _python_command(*args: str) -> list[str]:
    assert RTK is not None
    if DEV_PYTHON.is_file():
        return [RTK, "proxy", str(DEV_PYTHON), *args]
    if FALLBACK_PYTHON.is_file():
        return [RTK, "proxy", str(FALLBACK_PYTHON), *args]
    return [RTK, "python", *args]


def _start_ticks(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _owned(process: dict[str, object]) -> bool:
    return _start_ticks(int(process["pid"])) == int(process["start_ticks"])


def _port_open(port: int) -> bool:
    try:
        with socket.socket() as connection:
            connection.settimeout(0.3)
            result = connection.connect_ex(("127.0.0.1", port))
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES):
            raise LocalSocketUnavailable(
                "Local TCP sockets are denied by this environment; the demo servers "
                "cannot start here. Run offline checks here and start the demo in an "
                "environment that permits local TCP sockets."
            ) from exc
        raise
    if result in (errno.EPERM, errno.EACCES):
        raise LocalSocketUnavailable(
            "Local TCP connections are denied by this environment; the demo servers "
            "cannot start here. Run offline checks here and start the demo in an "
            "environment that permits local TCP sockets."
        )
    if result not in (0, errno.ECONNREFUSED):
        raise OSError(result, f"Could not check local TCP port {port}")
    return result == 0


def _asset_tree_ready() -> bool:
    manifest_path = ROOT / "assets/manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    city_route = manifest.get("city_tileset_url", "/assets/city/tileset.json")
    if not isinstance(city_route, str) or not city_route.startswith("/assets/"):
        raise RuntimeError("Asset manifest has an invalid city tileset URL")
    routes = [city_route]
    ground_route = manifest.get("ground_tileset_url")
    if _city_requires_ground(city_route) and not ground_route:
        raise RuntimeError("Active city v8+ requires a published ground tileset URL")
    if ground_route is not None:
        routes.insert(0, ground_route)
    tilesets = []
    for asset_route in routes:
        if (not isinstance(asset_route, str) or not asset_route.startswith("/assets/")
                or not asset_route.endswith("/tileset.json")
                or ".." in PurePosixPath(asset_route).parts):
            raise RuntimeError("Asset manifest has an invalid public tileset URL")
        tilesets.append(ROOT / asset_route.lstrip("/"))
    for index in range(1, int(manifest.get("facade_models", 0)) + 1):
        tilesets.append(ROOT / f"assets/facades/demo-complex-{index:03d}/tileset.json")
    for tileset_path in tilesets:
        if not tileset_path.is_file():
            raise RuntimeError(f"Published asset is missing: {tileset_path}; rebuild the matching tileset explicitly")
        pending_documents = [tileset_path]
        visited_documents: set[Path] = set()
        while pending_documents:
            document_path = pending_documents.pop()
            if document_path in visited_documents:
                continue
            visited_documents.add(document_path)
            document = json.loads(document_path.read_text(encoding="utf-8"))
            stack = [document.get("root")]
            while stack:
                tile = stack.pop()
                if not isinstance(tile, dict):
                    continue
                stack.extend(tile.get("children") or [])
                contents = tile.get("contents") or ([tile["content"]] if tile.get("content") else [])
                for content in contents:
                    uri = content.get("uri") if isinstance(content, dict) else None
                    if uri is None:
                        continue
                    if not isinstance(uri, str):
                        raise RuntimeError(f"Published tile has an invalid URI: {uri}")
                    relative = PurePosixPath(uri)
                    if (relative.is_absolute()
                            or ".." in relative.parts or "?" in uri or "#" in uri):
                        raise RuntimeError(f"Published tile has an invalid URI: {uri}")
                    content_path = document_path.parent / uri
                    if not content_path.is_file():
                        raise RuntimeError(f"Published tile is missing: {content_path}")
                    if content_path.suffix == ".json":
                        pending_documents.append(content_path)
    return True


def _web_bundle_stale() -> bool:
    index = ROOT / "web/dist/index.html"
    if not index.is_file():
        return True
    built_at = index.stat().st_mtime_ns
    inputs = [ROOT / "web/package.json", ROOT / "web/package-lock.json",
              ROOT / "web/vite.config.ts", ROOT / "web/index.html"]
    inputs.extend((ROOT / "web/src").rglob("*"))
    return any(path.is_file() and path.stat().st_mtime_ns > built_at for path in inputs)


def _wait_url(url: str, process: subprocess.Popen[bytes], seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with code {process.returncode}; inspect .runtime logs")
        try:
            with urlopen(url, timeout=0.7) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"Server did not respond at {url}; inspect .runtime logs")


def _expected_catalog_view() -> str:
    developer_pointer = ROOT / "data/property/developer_map/active.json"
    if developer_pointer.exists() or developer_pointer.is_symlink():
        return "developer_buildings"
    mode = os.environ.get("NOVOSTROY_REGISTRY_MODE", "auto")
    if mode not in {"auto", "off", "estateline"}:
        raise RuntimeError("NOVOSTROY_REGISTRY_MODE must be auto, off or estateline")
    if mode == "off":
        return "demo"
    registry = ROOT / "data/property/estateline"
    present = ((registry / "projects.jsonl").is_file(),
               (registry / "source_manifest.json").is_file())
    if present == (True, True):
        return "building_registry"
    if present != (False, False) or mode == "estateline":
        raise RuntimeError("The local registry import is incomplete or missing")
    return "demo"


def _read_public_json(url: str) -> dict[str, object]:
    try:
        with urlopen(url, timeout=5) as response:
            document = json.load(response)
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(
            f"Public stack check failed at {url}: {exc}. The API/proxy may be stale "
            "or serving another checkout; stop its owning process before starting this checkout."
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"Public stack returned non-object JSON at {url}")
    return document


def _external_tileset_routes(route: str, document: dict[str, object]) -> list[str]:
    pending = [document.get("root")]
    external: set[str] = set()
    while pending:
        tile = pending.pop()
        if not isinstance(tile, dict):
            continue
        pending.extend(tile.get("children") or [])
        contents = tile.get("contents") or ([tile["content"]] if tile.get("content") else [])
        for content in contents:
            uri = content.get("uri") if isinstance(content, dict) else None
            if not isinstance(uri, str) or not uri.endswith(".tileset.json"):
                continue
            relative = PurePosixPath(uri)
            if (relative.is_absolute() or ".." in relative.parts or "?" in uri
                    or "#" in uri):
                raise RuntimeError(f"Invalid external tileset reference in {route}: {uri}")
            external.add(str(PurePosixPath(route).parent / relative))
    return sorted(external)


def _wait_public_city_tileset(base: str, process: subprocess.Popen[bytes] | None = None) -> None:
    """Verify the live proxy serves this checkout's catalog and active tile branches."""
    del process  # API and proxy readiness are checked before this consistency gate.
    manifest = json.loads((ROOT / "assets/manifest.json").read_text(encoding="utf-8"))
    expected = {
        "city_volumes": manifest.get("city_tileset_url"),
        "ground_buildings": manifest.get("ground_tileset_url"),
    }
    if isinstance(expected["city_volumes"], str) \
            and _city_requires_ground(expected["city_volumes"]) \
            and expected["ground_buildings"] is None:
        raise RuntimeError("Active city v8+ requires a published ground tileset URL")
    health = _read_public_json(f"{base}/v1/health")
    catalog_view = health.get("catalog_view")
    expected_view = _expected_catalog_view()
    if catalog_view != expected_view:
        raise RuntimeError(
            f"Live catalog_view={catalog_view!r}, local checkout expects {expected_view!r}. "
            "The API may be stale or serving another checkout."
        )
    map_route = "/v1/registry/map" if catalog_view in {"building_registry", "developer_buildings"} else "/v1/map"
    map_document = _read_public_json(f"{base}{map_route}")
    tilesets = map_document.get("tilesets")
    if not isinstance(tilesets, list):
        raise RuntimeError(f"The map API returned invalid tilesets at {map_route}")
    for kind in ("ground_buildings", "city_volumes"):
        wanted = expected[kind]
        tile = next((item for item in tilesets
                     if isinstance(item, dict) and item.get("kind") == kind), None)
        if wanted is None and tile is None:
            continue
        route = tile.get("url") if isinstance(tile, dict) else None
        if (not isinstance(route, str) or not route.startswith("/assets/")
                or not route.endswith("/tileset.json")
                or ".." in PurePosixPath(route).parts):
            raise RuntimeError(f"The map API did not publish a valid public {kind} tileset URL")
        if route != wanted:
            raise RuntimeError(
                f"Live {kind} URL {route!r} differs from active manifest {wanted!r}; "
                "the API may be stale or serving another checkout."
            )
        root = _read_public_json(f"{base}{route}")
        if not isinstance(root.get("root"), dict):
            raise RuntimeError(f"The public tileset has no root: {route}")
        external = _external_tileset_routes(route, root)
        for branch in sorted({external[0], external[len(external) // 2], external[-1]}) if external else ():
            branch_document = _read_public_json(f"{base}{branch}")
            if not isinstance(branch_document.get("root"), dict):
                raise RuntimeError(f"The external tileset has no root: {branch}")

    # A published model run is part of this checkout's API contract. Catch a
    # stale API/proxy before reporting a successful managed start or restart.
    pointer_path = ROOT / "data/models/active.json"
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            run_id = pointer.get("run_id") if isinstance(pointer, dict) else None
            if (not isinstance(run_id, str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", run_id) is None):
                raise ValueError("invalid run ID")
            run_manifest_path = ROOT / "data/models" / run_id / "manifest.json"
            run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
            if not isinstance(run_manifest, dict) or run_manifest.get("run_id") != run_id:
                raise ValueError("run ID differs from manifest")
        except (OSError, ValueError) as exc:
            raise RuntimeError("Local published model run pointer or manifest is invalid") from exc
        model_summary = _read_public_json(f"{base}/v1/models/candidates/summary")
        if (model_summary.get("dataset_id") != "model_candidates"
                or model_summary.get("model_run_id") != run_id
                or model_summary.get("run_status") != "ready"
                or not isinstance(model_summary.get("counts"), dict)
                or model_summary["counts"].get("scanned") != run_manifest.get("records_total")):
            raise RuntimeError(
                "Live model-candidate API differs from the local published run; "
                "the API may be stale or serving another checkout."
            )
        if expected_view == "building_registry":
            point_map = _read_public_json(f"{base}/v1/map/points")
            counts = point_map.get("counts")
            markers = point_map.get("items")
            registry_count = map_document.get("counts", {}).get("geocoded")
            if (point_map.get("dataset_id") != "map_points"
                    or point_map.get("model_run_id") != run_id
                    or point_map.get("registry_snapshot_id") != health.get("registry_snapshot_id")
                    or not isinstance(counts, dict) or not isinstance(markers, list)
                    or point_map.get("dataset_total") != len(markers)
                    or counts.get("points") != len(markers)
                    or counts.get("registry_points") != registry_count
                    or counts.get("registry_points_total") != registry_count
                    or counts.get("registry_points", 0) + counts.get(
                        "mapped_construction_points", 0) != len(markers)):
                raise RuntimeError(
                    "Live point-map API differs from the local registry/model run; "
                    "the API may be stale or serving another checkout."
                )
    if expected_view == "developer_buildings":
        pointer_path = ROOT / "data/property/developer_map/active.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            map_run_id = pointer["run_id"]
            if (pointer.get("schema_version") != "developer-map-pointer/1"
                    or not isinstance(map_run_id, str)
                    or re.fullmatch(r"developer-map-v1-[0-9a-f]{16}", map_run_id) is None):
                raise ValueError("invalid developer map pointer")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Local developer map pointer is invalid") from exc
        listing = _read_public_json(f"{base}/v1/registry/buildings?limit=1")
        point_map = _read_public_json(f"{base}/v1/map/points")
        map_counts = map_document.get("counts")
        point_counts = point_map.get("counts")
        markers = point_map.get("items")
        snapshot_id = health.get("registry_snapshot_id")
        if (health.get("data_mode") != "real" or health.get("map_run_id") != map_run_id
                or map_document.get("dataset_id") != "developer_building_registry"
                or listing.get("dataset_id") != "developer_building_registry"
                or point_map.get("dataset_id") != "developer_map_points"
                or map_document.get("snapshot_id") != snapshot_id
                or listing.get("snapshot_id") != snapshot_id
                or point_map.get("registry_snapshot_id") != snapshot_id
                or any(document.get("map_run_id") != map_run_id
                       for document in (map_document, listing, point_map))
                or not isinstance(map_counts, dict) or not isinstance(point_counts, dict)
                or not isinstance(markers, list)
                or listing.get("dataset_total") != map_counts.get("records")
                or point_map.get("dataset_total") != map_counts.get("records")
                or point_counts.get("points") != len(markers)
                or point_counts.get("registry_points") != len(markers)
                or point_counts.get("registry_points_total") != map_counts.get("geocoded")
                or point_counts.get("mapped_construction_points") != 0
                or any(item.get("point_type") != "developer_building" for item in markers)):
            raise RuntimeError(
                "Live developer-building list/map/points differ from the local active run; "
                "the API may be stale or serving another checkout."
            )


def _save(processes: dict[str, dict[str, object]]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    pending = STATE.with_suffix(".json.tmp")
    pending.write_text(json.dumps(processes, indent=2), encoding="utf-8")
    pending.replace(STATE)


def _load() -> dict[str, dict[str, object]]:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}


def _spawn(name: str, command: list[str], log_name: str,
           processes: dict[str, dict[str, object]], *, env: dict[str, str]) -> subprocess.Popen[bytes]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / log_name).open("ab") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    ticks = _start_ticks(process.pid)
    if ticks is None:
        raise RuntimeError(f"{name} exited immediately; inspect {log_name}")
    processes[name] = {"pid": process.pid, "start_ticks": ticks, "command": command}
    _save(processes)
    return process


def _prepare_start(*, preview: bool) -> None:
    if RTK is None:
        raise RuntimeError("rtk is required; see /home/pirojban/.codex/RTK.md")
    if not (ROOT / "web/node_modules/.bin/vite").is_file():
        raise RuntimeError("Web packages missing: run `rtk npm --prefix web ci`")
    if not (ROOT / "data/demo/catalog.json").is_file():
        subprocess.run(_python_command("-m", "pipeline.generate_demo"), cwd=ROOT, check=True)
    if not _asset_tree_ready():
        subprocess.run(_python_command("-m", "pipeline.build_tiles"), cwd=ROOT, check=True)
    assert RTK is not None
    if preview and _web_bundle_stale():
        subprocess.run([RTK, "npm", "--prefix", "web", "run", "build"], cwd=ROOT, check=True)


def start(*, lan: bool = False, preview: bool = False) -> None:
    if RTK is None:
        raise RuntimeError("rtk is required; see /home/pirojban/.codex/RTK.md")
    if _load():
        raise RuntimeError("Managed servers already recorded; use status or stop first")
    if _port_open(8000) or _port_open(5173):
        raise RuntimeError(
            "Port 8000 or 5173 is occupied outside this managed launch. Stop the owning "
            "server in its original terminal or service, then run this start command again; "
            "this script will not stop an unmanaged process."
        )
    _prepare_start(preview=preview)
    env = _api_environment()
    processes: dict[str, dict[str, object]] = {}
    try:
        api = _spawn("api", _python_command("-m", "uvicorn", "backend.app:app",
                                            "--host", "0.0.0.0" if lan else "127.0.0.1", "--port", "8000"),
                     "api.log", processes, env=env)
        _wait_url("http://127.0.0.1:8000/v1/health", api)
        web = _spawn("web", [RTK, "npm", "--prefix", "web", "run", "preview" if preview else "dev"],
                     "web.log", processes, env=env)
        _wait_url("http://127.0.0.1:5173/", web)
        _wait_url("http://127.0.0.1:5173/v1/health", web)
        _wait_public_city_tileset("http://127.0.0.1:5173", web)
    except Exception:
        stop()
        raise
    print("API: http://127.0.0.1:8000/v1/health")
    print(f"Web ({'preview' if preview else 'dev'}): http://localhost:5173; iPhone: http://<computer-LAN-IP>:5173")
    print("Native mobile API: http://<computer-LAN-IP>:8000" if lan else
          "Native mobile API via Vite proxy: http://<computer-LAN-IP>:5173")
    print("Stop: rtk python scripts/dev.py stop")


def stop() -> None:
    processes = _load()
    if not processes:
        print("No managed demo servers are running")
        return
    for name in ("web", "api"):
        process = processes.get(name)
        if process and _owned(process):
            os.killpg(int(process["pid"]), signal.SIGTERM)
            print(f"Stopped {name} process group")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_owned(item) for item in processes.values()):
        time.sleep(0.1)
    for process in processes.values():
        if _owned(process):
            os.killpg(int(process["pid"]), signal.SIGKILL)
    STATE.unlink(missing_ok=True)


def restart_api() -> None:
    """Reload backend code while leaving the managed web preview available."""
    processes = _load()
    previous = processes.get("api")
    web = processes.get("web")
    if previous is None or web is None or not _owned(previous) or not _owned(web):
        raise RuntimeError("Both managed servers must be running to restart only the API")
    host = "0.0.0.0" if "0.0.0.0" in previous["command"] else "127.0.0.1"
    os.killpg(int(previous["pid"]), signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (_owned(previous) or _port_open(8000)):
        time.sleep(0.1)
    if _port_open(8000):
        raise RuntimeError("Managed API did not release port 8000; inspect .runtime/api.log")
    env = _api_environment()
    api = _spawn("api", _python_command("-m", "uvicorn", "backend.app:app",
                                        "--host", host, "--port", "8000"),
                 "api.log", processes, env=env)
    _wait_url("http://127.0.0.1:8000/v1/health", api)
    _wait_url("http://127.0.0.1:5173/v1/health", api)
    _wait_public_city_tileset("http://127.0.0.1:5173", api)
    print("Reloaded managed API; web preview stayed running")


def verify() -> None:
    """Read-only check of the running proxy against this checkout's active release."""
    if not _asset_tree_ready():
        raise RuntimeError("Local public tilesets are missing; build them before launch")
    processes = _load()
    if not processes:
        print("No managed server state; any listeners on 8000/5173 belong to another launch")
    if not _port_open(8000) or not _port_open(5173):
        raise RuntimeError("API and web preview must both be listening on 8000/5173")
    _wait_public_city_tileset("http://127.0.0.1:5173")
    print("PASS: live catalog, active tileset URLs, and sampled external regions match this checkout")


def _listener_pids(port: int) -> set[int]:
    """Resolve TCP listener owners in this PID/network namespace without opening a socket."""
    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.is_file():
            continue
        for line in table.read_text(encoding="ascii").splitlines()[1:]:
            columns = line.split()
            if len(columns) > 9 and columns[3] == "0A" \
                    and int(columns[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(columns[9])
    if not inodes:
        return set()
    owners: set[int] = set()
    for pid_path in Path("/proc").iterdir():
        if not pid_path.name.isdecimal():
            continue
        try:
            descriptors = (pid_path / "fd").iterdir()
            for descriptor in descriptors:
                try:
                    link = os.readlink(descriptor)
                except (OSError, PermissionError):
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    owners.add(int(pid_path.name))
                    break
        except (OSError, PermissionError):
            continue
    return owners


def _matches_listener_command(port: int, cwd: Path, arguments: list[str]) -> bool:
    """Require the expected project directory, executable and exact bound port."""
    if port == 8000:
        module_form = any(arguments[index:index + 2] == ["-m", "uvicorn"]
                          for index in range(len(arguments) - 1))
        binary_form = Path(arguments[0]).name == "uvicorn"
        return (cwd == ROOT and "backend.app:app" in arguments
                and (module_form or binary_form)
                and any(arguments[index:index + 2] == ["--port", "8000"]
                        for index in range(len(arguments) - 1)))
    if port == 5173:
        vite_entry = (ROOT / "web/node_modules/vite/bin/vite.js").resolve()
        vite_command = any((cwd / argument).resolve() == vite_entry
                           for argument in arguments[1:] if "vite" in argument)
        return (cwd in {ROOT, ROOT / "web"} and vite_command
                and any(arguments[index:index + 2] == ["--port", "5173"]
                        for index in range(len(arguments) - 1))
                and "--strictPort" in arguments)
    return False


def _old_listener_matches(port: int, pid: int) -> bool:
    """Accept only this checkout's exact Uvicorn or Vite listener command."""
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        arguments = [part.decode("utf-8") for part in
                     Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
    except (OSError, UnicodeDecodeError):
        return False
    return _matches_listener_command(port, cwd, arguments) if arguments else False


def recover(*, lan: bool = False, preview: bool = False) -> None:
    """Replace only verified legacy listeners from this checkout, then start managed."""
    if _load():
        raise RuntimeError("Managed state exists; use status/stop rather than recover")
    occupied = [port for port in (8000, 5173) if _port_open(port)]
    legacy: dict[int, tuple[int, int]] = {}
    for port in occupied:
        owners = _listener_pids(port)
        if len(owners) != 1:
            raise RuntimeError(
                f"Cannot uniquely identify listener on port {port} in this process namespace; "
                "no process was stopped. Run recovery from the original host/session."
            )
        pid = next(iter(owners))
        ticks = _start_ticks(pid)
        if ticks is None or not _old_listener_matches(port, pid):
            raise RuntimeError(
                f"Listener on port {port} is not a verified Novostroy server from this "
                "checkout; no process was stopped."
            )
        legacy[port] = (pid, ticks)
    _prepare_start(preview=preview)
    for port, (pid, ticks) in legacy.items():
        if (_start_ticks(pid) != ticks or _listener_pids(port) != {pid}
                or not _old_listener_matches(port, pid)):
            raise RuntimeError("A listener changed during recovery; no process was stopped")
    for port, (pid, _) in legacy.items():
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped verified legacy Novostroy listener on {port} (pid {pid})")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_port_open(port) for port in legacy):
        time.sleep(0.1)
    if any(_port_open(port) for port in legacy):
        raise RuntimeError("A legacy listener did not release its port; no new server was started")
    start(lan=lan, preview=preview)


def status() -> None:
    processes = _load()
    if not processes:
        print("No managed demo servers recorded")
        return
    for name, process in processes.items():
        print(f"{name}: {'running' if _owned(process) else 'stopped'} (pid {process['pid']})")


def main() -> None:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "start" and set(arguments[1:]).issubset({"--lan", "--preview"}) \
            and len(arguments[1:]) == len(set(arguments[1:])):
        start(lan="--lan" in arguments, preview="--preview" in arguments)
    elif arguments and arguments[0] == "recover" and set(arguments[1:]).issubset({"--lan", "--preview"}) \
            and len(arguments[1:]) == len(set(arguments[1:])):
        recover(lan="--lan" in arguments, preview="--preview" in arguments)
    elif sys.argv[1:] == ["stop"]:
        stop()
    elif sys.argv[1:] == ["restart-api"]:
        restart_api()
    elif sys.argv[1:] == ["verify"]:
        verify()
    elif sys.argv[1:] == ["status"]:
        status()
    else:
        raise SystemExit("Usage: rtk python scripts/dev.py start|recover [--lan] [--preview]|verify|restart-api|stop|status")


if __name__ == "__main__":
    try:
        main()
    except LocalSocketUnavailable as exc:
        raise SystemExit(str(exc)) from None
