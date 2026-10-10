"""Exercise locally built images with an isolated, real-data Compose stack.

No registry login, remote pull, data mutation, or production Compose action.
Run from the repository root via ``rtk proxy python deploy/smoke_local.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.apply_release import (  # noqa: E402
    verify_developer_payloads,
    verify_experience_payloads,
    verify_release,
)


BASE_COMPOSE = ROOT / "deploy/compose.yaml"
SMOKE_COMPOSE = ROOT / "deploy/compose.smoke.yaml"
PROJECT = "novostroy-smoke"
PORT = 18080
BASE_URL = f"http://127.0.0.1:{PORT}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: urllib.request.Request, fp: Any,
                         code: int, msg: str, headers: Any,
                         newurl: str) -> None:
        return None


def _http(path: str, *, method: str = "GET", etag: str | None = None,
          expected: int = 200) -> tuple[dict[str, Any] | None, Any]:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Smoke paths must stay on the local HTTP origin")
    headers = {"If-None-Match": etag} if etag else {}
    request = urllib.request.Request(BASE_URL + path, method=method, headers=headers)
    try:
        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        ).open(request, timeout=30) as response:
            status, body, response_headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        status, body, response_headers = exc.code, b"", exc.headers
        exc.close()
    if status != expected:
        raise RuntimeError(f"{method} {path}: HTTP {status}, expected {expected}")
    if method == "HEAD" or expected in {304, 404}:
        return None, response_headers
    if path == "/healthz" or path.startswith("/models"):
        return {"body_prefix": body[:40].decode("utf-8", errors="replace")}, response_headers
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise RuntimeError(f"{path}: response is not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: response is not a JSON object")
    return payload, response_headers


def _checks(release: Path) -> None:
    _http("/healthz")
    health, _ = _http("/v1/health")
    registry, _ = _http("/v1/registry/buildings?limit=1")
    points, point_headers = _http("/v1/map/points")
    if health is None or registry is None or points is None:
        raise RuntimeError("Developer map smoke response is missing")
    verified = verify_developer_payloads(health, registry, points)
    point_etag = point_headers.get("ETag")
    if not point_etag or "private" not in point_headers.get("Cache-Control", ""):
        raise RuntimeError("Point response lacks private revalidation headers")
    _http("/v1/map/points", etag=point_etag, expected=304)

    models, _ = _http("/v1/models/candidates/summary")
    if (models is None or models.get("run_status") != "ready"
            or not isinstance(models.get("counts", {}).get("scanned"), int)
            or models["counts"]["scanned"] < 1):
        raise RuntimeError("The active model assessment differs from the checked run")

    first_building = registry["items"][0]["registry_id"]
    floors, _ = _http(f"/v1/developer-buildings/{first_building}/floors")
    selected_model, _ = _http(f"/v1/developer-buildings/{first_building}/model")
    if floors is None or selected_model is None:
        raise RuntimeError("The committed developer menu/model response is missing")
    experience = json.loads((release / "data/property/developer_experience/active.json")
                            .read_text(encoding="utf-8"))
    verify_experience_payloads(verified, first_building, floors,
                               selected_model, experience)

    route_capability = False
    for developer_point in _route_control_points(points["items"]):
        nearby, nearby_headers = _http(
            "/v1/map/nearby?point_id=" + developer_point["point_id"])
        if nearby is None:
            raise RuntimeError("Nearby route response is missing")
        route_capability |= _verify_nearby(
            nearby, developer_point["point_id"],
            health["registry_snapshot_id"], verified["map_run_id"])
        if "private" not in nearby_headers.get("Cache-Control", ""):
            raise RuntimeError("Nearby response is not private")
        if route_capability:
            break
    if not route_capability:
        raise RuntimeError("No routed walking path found at bounded control origins")

    pointer = json.loads((release / "assets/manifest.json").read_text(encoding="utf-8"))
    for key in ("city_tileset_url", "ground_tileset_url", "roads_tileset_url"):
        asset_path = pointer[key]
        _, headers = _http(asset_path, method="HEAD")
        if "immutable" not in headers.get("Cache-Control", ""):
            raise RuntimeError(f"Public asset is not immutable: {key}")
    city_root = pointer["city_tileset_url"].rsplit("/", 1)[0]
    _http(city_root + "/region_752_1115.tileset.json", method="HEAD")
    _http("/assets/manifest.json", expected=404)
    _http(city_root + "/manifest.json", expected=404)

    page, headers = _http("/models?decision=rejected")
    if page is None or "html" not in headers.get("Content-Type", ""):
        raise RuntimeError("The model-review SPA deep link did not resolve")
    print("PASS local Compose HTTP: developer buildings "
          f"{verified['buildings']}, points {verified['points']}, "
          "models, nearby, public cache and private asset gates, /models")


def _route_control_points(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use stable central anchors; try at most three actual publisher points."""
    anchors = ((37.6176, 55.7558), (37.6410, 55.6900), (37.5750, 55.8300))
    selected: list[dict[str, Any]] = []
    for lon, lat in anchors:
        candidates = [item for item in items if isinstance(item.get("location"), list)
                      and len(item["location"]) == 2
                      and all(type(value) in (int, float) and math.isfinite(value)
                              for value in item["location"])]
        if not candidates:
            raise RuntimeError("The active developer map has no valid control coordinates")
        nearest = min(candidates, key=lambda item: (
            (item["location"][0] - lon) ** 2 + (item["location"][1] - lat) ** 2,
            item["point_id"]))
        if nearest["point_id"] not in {item["point_id"] for item in selected}:
            selected.append(nearest)
    return selected


def _verify_nearby(payload: dict[str, Any], point_id: str,
                   snapshot_id: str, map_run_id: str) -> bool:
    """Check bounded, truthful walking evidence without demanding full POI quotas."""
    layers = payload.get("layers")
    if (payload.get("status") != "ok" or payload.get("point_id") != point_id
            or payload.get("registry_snapshot_id") != snapshot_id
            or payload.get("map_run_id") != map_run_id
            or not isinstance(layers, dict)
            or set(layers) != {"metro", "education", "medical"}):
        raise RuntimeError("Nearby response differs from the selected map generation")
    routed = False
    for name, cap, categories in (
        ("metro", 2, {"metro_station"}),
        ("education", 4, {"school", "kindergarten"}),
        ("medical", 2, {"clinic"}),
    ):
        layer = layers[name]
        items = layer.get("items") if isinstance(layer, dict) else None
        if not isinstance(items, list) or len(items) > cap:
            raise RuntimeError(f"Nearby {name} exceeds its selected-POI bound")
        if name == "education" and any(
            sum(item.get("category") == category for item in items) > 2
            for category in categories
        ):
            raise RuntimeError("Nearby education exceeds its per-kind bound")
        if len({item.get("id") for item in items}) != len(items):
            raise RuntimeError(f"Nearby {name} repeats a destination")
        for item in items:
            if item.get("category") not in categories:
                raise RuntimeError(f"Nearby {name} has a wrong destination type")
            route = item.get("route")
            if not isinstance(route, dict):
                raise RuntimeError(f"Nearby {name} lacks an explicit route outcome")
            if route.get("status") == "routed":
                if (type(route.get("estimated_minutes")) is not int
                        or route["estimated_minutes"] < 1
                        or not isinstance(route.get("distance_m"), (int, float))
                        or route["distance_m"] <= 0
                        or not isinstance(route.get("geometry"), list)
                        or len(route["geometry"]) < 2):
                    raise RuntimeError("Nearby routed path lacks usable distance/time/shape")
                routed = True
            elif (route.get("status") not in {"unavailable", "no_route",
                                                 "snap_exceeds_threshold", "invalid_route",
                                                 "contains_ferry"}
                  or route.get("estimated_minutes") is not None
                  or route.get("geometry") is not None):
                raise RuntimeError("Nearby failed route is presented as a walking route")
    return routed


def _docker(command: list[str], env: dict[str, str], *, capture: bool = False) -> str:
    result = subprocess.run(command, cwd=ROOT, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-bin", type=Path, default=Path("docker"))
    parser.add_argument("--docker-host", required=True,
                        help="Rootless Docker unix:///absolute/path/docker.sock")
    parser.add_argument("--docker-config", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True,
                        help="explicit, verified developer-building data release")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the isolated smoke containers for manual inspection")
    args = parser.parse_args(argv)
    if not args.docker_host.startswith("unix://"):
        parser.error("Only a rootless Unix Docker socket is accepted")
    socket_path = Path(args.docker_host.removeprefix("unix://"))
    if not socket_path.is_absolute() or not socket_path.exists() or not socket_path.is_socket():
        parser.error("Docker socket must be an existing absolute Unix socket")
    if not args.docker_config.is_absolute() or not args.docker_config.is_dir():
        parser.error("Docker config must be an existing absolute directory")
    release = args.release_dir.resolve(strict=True)
    verify_release(release)
    env = {**os.environ, "DOCKER_HOST": args.docker_host,
           "DOCKER_CONFIG": str(args.docker_config), "COMPOSE_PROJECT_NAME": PROJECT,
           "NOVOSTROY_RELEASE_DIR": str(release), "NOVOSTROY_IMAGE_TAG": "0" * 40,
           "NOVOSTROY_BIND_ADDR": "127.0.0.1", "NOVOSTROY_HTTP_PORT": str(PORT)}
    docker = str(args.docker_bin)
    compose = [docker, "compose", "-p", PROJECT, "-f", str(BASE_COMPOSE),
               "-f", str(SMOKE_COMPOSE)]
    started = False
    try:
        for image in ("novostroy-api:smoke", "novostroy-web:smoke"):
            _docker([docker, "image", "inspect", image], env, capture=True)
        _docker(compose + ["config", "--quiet"], env)
        if _docker(compose + ["ps", "-a", "-q"], env, capture=True):
            raise RuntimeError("Existing novostroy-smoke containers must be inspected first")
        started = True
        _docker(compose + ["up", "-d", "--wait", "--no-build", "--pull", "never",
                           "api", "web"], env)
        _checks(release)
        return 0
    finally:
        if started and not args.keep:
            _docker(compose + ["down", "--remove-orphans"], env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError,
            AssertionError) as error:
        print(f"FAIL local Compose smoke: {error}", file=sys.stderr)
        raise SystemExit(1) from error
