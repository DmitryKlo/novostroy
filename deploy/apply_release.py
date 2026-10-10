"""Apply a checked Novostroy data/image release on a Docker Compose VM.

The release and runtime directories are never deleted. Failed updates attempt
to restore the previously recorded image tag and data release.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.request
from urllib.error import HTTPError
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
COMPOSE = ROOT / "deploy/compose.yaml"
IMAGE_REPOSITORIES = (
    "registry.spirit.culab.ru/hack-l2sh-2/novostroy-api",
    "registry.spirit.culab.ru/hack-l2sh-2/novostroy-web",
)
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DEVELOPER_RUN_RE = re.compile(r"developer-map-v1-[0-9a-f]{16}\Z")
DEVELOPER_SNAPSHOT_RE = re.compile(r"developer-map-snapshot-[0-9a-f]{16}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
MODEL_RUN_RE = re.compile(r"models-v1-[0-9a-f]{16}\Z")
MODEL_ROOT_RE = re.compile(r"public-project-models-v1-[0-9a-f]{16}\Z")
GENERATION_RE = re.compile(r"generation-[0-9a-f]{16}\Z")
SNAPSHOT_PATH_RE = re.compile(
    r"data/property/estateline/normalized/snapshots/estate-[0-9a-f]{16}\Z")
UNIT_RUN_RE = re.compile(r"developer-units-v1-[0-9a-f]{16}\Z")
SELECTED_MODEL_RUN_RE = re.compile(r"developer-models-v1-[0-9a-f]{16}\Z")


def _release_json(release: Path, relative: str, hashes: dict[str, str]) -> dict[str, Any]:
    if relative not in hashes:
        raise ValueError(f"Release lacks pinned file: {relative}")
    path = release / relative
    if path.stat().st_size > 100_000:
        raise ValueError(f"Release pointer/manifest exceeds bound: {relative}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Release pointer/manifest is not an object: {relative}")
    return value


def _require_pinned_hash(hashes: dict[str, str], relative: str,
                         expected: Any) -> None:
    if (not isinstance(expected, str) or HASH_RE.fullmatch(expected) is None
            or hashes.get(relative) != expected):
        raise ValueError(f"Release file differs from its active pointer: {relative}")


def _verify_registry_model_pins(release: Path, hashes: dict[str, str]) -> None:
    """Accept either the atomic generation or legacy pointer, never a mixed run."""
    asset_pointer = _release_json(release, "assets/manifest.json", hashes)
    generation_path = "data/property/estateline/normalized/active.json"
    if generation_path in hashes:
        pointer = _release_json(release, generation_path, hashes)
        generation_id = pointer.get("generation_id")
        bundle_sha = pointer.get("bundle_sha256")
        if (pointer.get("schema_version") != "estate-generation-pointer/1"
                or not isinstance(generation_id, str)
                or GENERATION_RE.fullmatch(generation_id) is None
                or not isinstance(bundle_sha, str)
                or HASH_RE.fullmatch(bundle_sha) is None
                or generation_id != f"generation-{bundle_sha[:16]}"):
            raise ValueError("Invalid normalized generation pointer")
        bundle_path = ("data/property/estateline/normalized/generations/"
                       f"{generation_id}/bundle.json")
        _require_pinned_hash(hashes, bundle_path, bundle_sha)
        bundle = _release_json(release, bundle_path, hashes)
        model = bundle.get("model")
        normalized = bundle.get("normalized_snapshot")
        visibility = bundle.get("visibility")
        if (bundle.get("schema_version") != "estate-generation/1"
                or not isinstance(model, dict) or not isinstance(normalized, dict)
                or not isinstance(visibility, dict)):
            raise ValueError("Invalid normalized generation bundle")
        snapshot_path = normalized.get("path")
        if not isinstance(snapshot_path, str) or SNAPSHOT_PATH_RE.fullmatch(snapshot_path) is None:
            raise ValueError("Invalid normalized source snapshot path")
        for field, filename in (("projects_sha256", "projects.jsonl"),
                                ("source_manifest_sha256", "source_manifest.json"),
                                ("review_required_sha256", "review_required.jsonl"),
                                ("schema_sha256", "schema.json")):
            _require_pinned_hash(hashes, f"{snapshot_path}/{filename}",
                                 normalized.get(field))
        visibility_id = visibility.get("run_id")
        if (not isinstance(visibility_id, str)
                or re.fullmatch(r"visibility-v1-[0-9a-f]{16}", visibility_id) is None):
            raise ValueError("Invalid pinned visibility run")
        visibility_prefix = f"data/property/estateline/visibility/runs/{visibility_id}/"
        _require_pinned_hash(hashes, visibility_prefix + "manifest.json",
                             visibility.get("manifest_sha256"))
        _require_pinned_hash(hashes, visibility_prefix + "records.jsonl",
                             visibility.get("records_sha256"))
        source_snapshot_id = bundle.get("source_snapshot_id")
    else:
        model = _release_json(release, "data/models/active.json", hashes)
        source_snapshot_id = None
    run_id = model.get("run_id")
    root = model.get("public_root", asset_pointer.get("project_models_root"))
    if (not isinstance(run_id, str) or MODEL_RUN_RE.fullmatch(run_id) is None
            or not isinstance(root, str) or MODEL_ROOT_RE.fullmatch(root) is None
            or asset_pointer.get("project_models_root") != root):
        raise ValueError("Invalid active model run or public asset root")
    prefix = f"data/models/{run_id}/"
    _require_pinned_hash(hashes, prefix + "manifest.json", model.get("manifest_sha256"))
    manifest = _release_json(release, prefix + "manifest.json", hashes)
    if (manifest.get("schema_version") != "model-candidates/1"
            or manifest.get("run_id") != run_id
            or manifest.get("public_root") != root
            or (source_snapshot_id is not None
                and manifest.get("registry_snapshot_id") != source_snapshot_id)
            or type(manifest.get("records_total")) is not int
            or manifest["records_total"] < 1):
        raise ValueError("Active model run does not match the registry generation")
    for name in ("assessments", "candidates", "source_evaluations"):
        _require_pinned_hash(hashes, prefix + name + ".jsonl",
                             manifest.get(name + "_sha256"))
    public_manifest_sha = manifest.get("public_manifest_sha256")
    if isinstance(model.get("public_manifest_sha256"), str) \
            and model["public_manifest_sha256"] != public_manifest_sha:
        raise ValueError("Generation and model public manifests differ")
    _require_pinned_hash(hashes, f"assets/{root}/public_manifest.json", public_manifest_sha)


def _verify_developer_map(release: Path, hashes: dict[str, str]) -> None:
    """Pin the active publisher map to its exact immutable runtime files."""
    pointer_path = "data/property/developer_map/active.json"
    if pointer_path not in hashes:
        raise ValueError("The release lacks the active developer-building map")
    pointer = json.loads((release / pointer_path).read_text(encoding="utf-8"))
    run_id = pointer.get("run_id")
    expected = pointer.get("manifest_sha256")
    if (set(pointer) != {"schema_version", "run_id", "manifest_sha256"}
            or pointer.get("schema_version") != "developer-map-pointer/1"
            or not isinstance(run_id, str) or DEVELOPER_RUN_RE.fullmatch(run_id) is None
            or not isinstance(expected, str) or HASH_RE.fullmatch(expected) is None):
        raise ValueError("Invalid developer-building map pointer")
    prefix = f"data/property/developer_map/runs/{run_id}/"
    manifest_path = prefix + "manifest.json"
    if hashes.get(manifest_path) != expected:
        raise ValueError("Developer-building map pointer does not pin its manifest")
    manifest = json.loads((release / manifest_path).read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id")
    if (manifest.get("schema_version") != "developer-map-snapshot/1"
            or manifest.get("run_id") != run_id
            or not isinstance(snapshot_id, str)
            or DEVELOPER_SNAPSHOT_RE.fullmatch(snapshot_id) is None
            or type(manifest.get("buildings_total")) is not int
            or manifest["buildings_total"] < 1
            or type(manifest.get("map_points_total")) is not int
            or not 1 <= manifest["map_points_total"] <= manifest["buildings_total"]):
        raise ValueError("Invalid active developer-building map manifest")
    for name in ("points", "groups", "rejections"):
        digest = manifest.get(f"{name}_sha256")
        count = manifest.get(f"{name}_total")
        if (not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None
                or type(count) is not int or count < 0
                or hashes.get(prefix + name + ".jsonl") != digest):
            raise ValueError(f"Developer-building {name} file differs from active manifest")


def _asset_references(release: Path, hashes: dict[str, str], relative: str,
                      asset_prefix: str, extension: str,
                      expected_rows: Any) -> tuple[int, int]:
    """Ensure selected model/plan assets are all present in the checked release."""
    if type(expected_rows) is not int or not 0 <= expected_rows <= 200_000:
        raise ValueError(f"Invalid row count for {relative}")
    count = 0
    assets: dict[str, int] = {}
    with (release / relative).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Invalid JSONL record in {relative}")
            count += 1
            digest = row.get("asset_sha256")
            if digest is None:
                continue
            if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
                raise ValueError(f"Invalid asset digest in {relative}")
            path = f"{asset_prefix}/{digest}.{extension}"
            _require_pinned_hash(hashes, path, digest)
            size = (release / path).stat().st_size
            if row.get("asset_bytes") is not None and row["asset_bytes"] != size:
                raise ValueError(f"Asset byte count differs from selected row: {path}")
            assets[digest] = size
    if count != expected_rows:
        raise ValueError(f"Row count differs from manifest: {relative}")
    return len(assets), sum(assets.values())


def _verify_developer_experience(release: Path, hashes: dict[str, str]) -> None:
    """Pin map, unit menu, selected models and their assets with one pointer."""
    path = "data/property/developer_experience/active.json"
    pointer = _release_json(release, path, hashes)
    keys = {"schema_version", "map_run_id", "map_snapshot_id",
                         "map_manifest_sha256", "unit_run_id", "unit_manifest_sha256",
                         "model_run_id", "model_manifest_sha256"}
    version = pointer.get("schema_version")
    if version == "developer-experience-pointer/2":
        keys |= {"enrichment_run_id", "enrichment_manifest_sha256"}
    if (set(pointer) != keys
            or version not in {"developer-experience-pointer/1", "developer-experience-pointer/2"}):
        raise ValueError("Invalid unified developer experience pointer")
    map_run, map_snapshot = pointer.get("map_run_id"), pointer.get("map_snapshot_id")
    unit_run, model_run = pointer.get("unit_run_id"), pointer.get("model_run_id")
    if (not isinstance(map_run, str) or DEVELOPER_RUN_RE.fullmatch(map_run) is None
            or not isinstance(map_snapshot, str)
            or DEVELOPER_SNAPSHOT_RE.fullmatch(map_snapshot) is None
            or not isinstance(unit_run, str) or UNIT_RUN_RE.fullmatch(unit_run) is None
            or not isinstance(model_run, str)
            or SELECTED_MODEL_RUN_RE.fullmatch(model_run) is None):
        raise ValueError("Invalid developer experience run IDs")
    map_pointer = _release_json(release, "data/property/developer_map/active.json", hashes)
    map_manifest_path = f"data/property/developer_map/runs/{map_run}/manifest.json"
    if (map_pointer.get("run_id") != map_run
            or map_pointer.get("manifest_sha256") != pointer.get("map_manifest_sha256")):
        raise ValueError("Developer experience map differs from active map pointer")
    _require_pinned_hash(hashes, map_manifest_path, pointer.get("map_manifest_sha256"))
    map_manifest = _release_json(release, map_manifest_path, hashes)
    if map_manifest.get("snapshot_id") != map_snapshot:
        raise ValueError("Developer experience map snapshot differs")
    identity = {"map_run_id": map_run, "map_snapshot_id": map_snapshot,
                "map_manifest_sha256": pointer["map_manifest_sha256"]}

    unit_prefix = f"data/property/developer_units/runs/{unit_run}/"
    _require_pinned_hash(hashes, unit_prefix + "manifest.json",
                         pointer.get("unit_manifest_sha256"))
    unit_manifest = _release_json(release, unit_prefix + "manifest.json", hashes)
    if (unit_manifest.get("schema_version") != "developer-units-snapshot/1"
            or unit_manifest.get("run_id") != unit_run
            or any(unit_manifest.get(field) != value for field, value in identity.items())
            or unit_manifest.get("buildings_total") != map_manifest.get("buildings_total")):
        raise ValueError("Unit menu differs from the pinned developer map")
    for name in ("buildings", "units", "floors", "plans", "unit_aliases"):
        _require_pinned_hash(hashes, unit_prefix + name + ".jsonl",
                             unit_manifest.get(name + "_sha256"))
        if type(unit_manifest.get(name + "_total")) is not int \
                or unit_manifest[name + "_total"] < 0:
            raise ValueError(f"Invalid unit menu count: {name}")
    prepared, prepared_bytes = _asset_references(
        release, hashes, unit_prefix + "plans.jsonl", unit_prefix + "assets", "png",
        unit_manifest["plans_total"])
    if (unit_manifest.get("plan_assets_prepared") != prepared
            or unit_manifest.get("plan_asset_bytes", 0) != prepared_bytes):
        raise ValueError("Prepared unit plan assets differ from menu manifest")

    model_prefix = f"data/property/developer_models/runs/{model_run}/"
    _require_pinned_hash(hashes, model_prefix + "manifest.json",
                         pointer.get("model_manifest_sha256"))
    model_manifest = _release_json(release, model_prefix + "manifest.json", hashes)
    if (model_manifest.get("schema_version") != "developer-models-snapshot/1"
            or model_manifest.get("run_id") != model_run
            or any(model_manifest.get(field) != value for field, value in identity.items())
            or model_manifest.get("menu_run_id") != unit_run
            or model_manifest.get("menu_manifest_sha256") != pointer["unit_manifest_sha256"]):
        raise ValueError("Selected models differ from pinned map or unit menu")
    _require_pinned_hash(hashes, model_prefix + "models.jsonl",
                         model_manifest.get("models_sha256"))
    selected, selected_bytes = _asset_references(
        release, hashes, model_prefix + "models.jsonl", model_prefix + "assets", "glb",
        model_manifest.get("models_total"))
    if (model_manifest.get("assets_total") != selected
            or model_manifest.get("assets_bytes") != selected_bytes):
        raise ValueError("Selected model assets differ from model manifest")
    for old_pointer in ("data/property/developer_units/active.json",
                        "data/property/developer_models/active.json",
                        "data/property/developer_enrichment/active.json"):
        if old_pointer in hashes:
            raise ValueError("Release has a competing developer active pointer")
    if version == "developer-experience-pointer/2":
        from scripts.package_deploy_data import developer_enrichment_files
        developer_enrichment_files(release, pointer, hashes)


def _directory(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"Expected an existing absolute directory, not a link: {path}")
    return path.resolve(strict=True)


def verify_release(release: Path, *, allow_legacy_missing_route_inputs: bool = False) -> dict[str, Any]:
    """Check the allowlisted export's file hashes before changing containers."""
    release = _directory(release)
    manifest_path = release / "release-manifest.json"
    if (manifest_path.is_symlink() or not manifest_path.is_file()
            or manifest_path.stat().st_size > 64_000_000):
        raise ValueError("Release manifest is missing, linked or oversized")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "novostroy-deploy-data/1"
            or manifest.get("data_mode") != "real"
            or manifest.get("registry_mode") != "estateline"
            or manifest.get("profile") not in {"current-only", "full-compat"}):
        raise ValueError("This deployment requires a real checked data release")
    records = manifest.get("files")
    if (not isinstance(records, list) or not 1 <= len(records) <= 100_000
            or len(records) != manifest.get("file_count")):
        raise ValueError("Invalid release inventory")
    seen: set[str] = set()
    hashes: dict[str, str] = {}
    total_bytes = 0
    for record in records:
        relative = record.get("path") if isinstance(record, dict) else None
        if (not isinstance(relative, str) or "\\" in relative or "\0" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))
                or type(record.get("bytes")) is not int or record["bytes"] < 0):
            raise ValueError("Invalid release path")
        parts = PurePosixPath(relative).parts
        if (not parts or parts[0] not in {"data", "assets"}
                or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
                or relative in seen):
            raise ValueError(f"Unsafe or duplicate release path: {relative}")
        seen.add(relative)
        path = release
        for part in parts:
            path /= part
            if path.is_symlink():
                raise ValueError(f"Release contains a link: {relative}")
        if not path.is_file() or path.stat().st_size != record.get("bytes"):
            raise ValueError(f"Release file missing or has wrong size: {relative}")
        expected = record.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"Invalid release hash: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError(f"Release file hash mismatch: {relative}")
        hashes[relative] = expected
        total_bytes += record["bytes"]
    if type(manifest.get("bytes_total")) is not int or manifest["bytes_total"] != total_bytes:
        raise ValueError("Release total bytes differ from inventory")
    required = {
        "assets/manifest.json", "data/property/estateline/projects.jsonl",
        "data/property/estateline/source_manifest.json",
    }
    if not required.issubset(seen):
        raise ValueError("The release lacks the real registry or asset pointer")
    _verify_registry_model_pins(release, hashes)
    _verify_developer_map(release, hashes)
    _verify_developer_experience(release, hashes)
    from deploy.entrypoint import ROUTING_INPUT_FILES, verify_routing_inputs
    # A SHA-pinned historical release may be used only as a delta input. New
    # releases and deploys always require the connector and park inputs.
    if not (allow_legacy_missing_route_inputs and not set(ROUTING_INPUT_FILES) & seen):
        verify_routing_inputs(release, hashes)
    allowed_files = seen | {"release-manifest.json"}
    for path in release.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Release contains a link or special file: {path}")
        if path.is_file() and str(path.relative_to(release)) not in allowed_files:
            raise ValueError(f"Release contains an unlisted file: {path}")
    return manifest


def _settings(release: Path, image_tag: str, bind_address: str,
              port: int) -> dict[str, str]:
    if SHA_RE.fullmatch(image_tag) is None:
        raise ValueError("Image tag must be a full 40-character source commit SHA")
    if bind_address not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("Only loopback or explicitly selected all-interface binding is supported")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid HTTP port")
    release = _directory(release)
    return {
        "NOVOSTROY_RELEASE_DIR": str(release),
        "NOVOSTROY_IMAGE_TAG": image_tag,
        "NOVOSTROY_BIND_ADDR": bind_address,
        "NOVOSTROY_HTTP_PORT": str(port),
    }


def _control_directory(path: Path, release: Path) -> Path:
    control = _directory(path)
    if control.is_relative_to(release) or control == release:
        raise ValueError("Deploy state must be outside the data release mounts")
    info = control.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Deploy state directory must be owned by this user and mode 0700")
    return control


def _compose(settings: dict[str, str], *args: str) -> None:
    env = {**os.environ, **settings, "COMPOSE_PROJECT_NAME": "novostroy"}
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args],
                   cwd=ROOT, env=env, check=True)


def _compose_output(settings: dict[str, str], *args: str) -> str:
    env = {**os.environ, **settings, "COMPOSE_PROJECT_NAME": "novostroy"}
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args],
                          cwd=ROOT, env=env, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def _verify_local_images(image_tag: str) -> None:
    """Require both locally built images to identify the reviewed source commit."""
    if SHA_RE.fullmatch(image_tag) is None:
        raise ValueError("Local images require a full source commit SHA tag")
    for repository in IMAGE_REPOSITORIES:
        image = f"{repository}:{image_tag}"
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise RuntimeError(f"Reviewed local image is missing: {repository}")
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local image labels are invalid: {repository}") from exc
        if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != image_tag:
            raise RuntimeError(f"Local image source revision differs from the requested tag: {repository}")


def _read_json(url: str) -> dict[str, Any]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect()).open(url, timeout=10) as response:
            value = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Deploy smoke returned HTTP {exc.code}: {url}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Invalid JSON response: {url}")
    return value


def verify_developer_payloads(health: dict[str, Any], buildings: dict[str, Any],
                              points: dict[str, Any]) -> dict[str, int | str]:
    """Require one coherent, non-demo building/map generation after deployment."""
    run_id = health.get("map_run_id")
    unit_run_id = health.get("unit_run_id")
    snapshot_id = health.get("registry_snapshot_id")
    if (health.get("data_mode") != "real" or health.get("catalog_view") != "developer_buildings"
            or health.get("status") != "developer_buildings_ready"
            or not isinstance(run_id, str) or DEVELOPER_RUN_RE.fullmatch(run_id) is None
            or not isinstance(unit_run_id, str) or UNIT_RUN_RE.fullmatch(unit_run_id) is None
            or not isinstance(snapshot_id, str) or not snapshot_id):
        raise RuntimeError("Proxy health did not select a ready real developer-building map")
    total = buildings.get("dataset_total")
    mapped = buildings.get("facets", {}).get("map_eligible_count")
    if (buildings.get("dataset_id") != "developer_building_registry"
            or buildings.get("snapshot_id") != snapshot_id
            or buildings.get("map_run_id") != run_id
            or buildings.get("unit_run_id") != unit_run_id
            or not isinstance(total, int) or total < 1
            or buildings.get("source_total") != total
            or buildings.get("archived_total") != 0
            or not isinstance(mapped, int) or not 1 <= mapped <= total):
        raise RuntimeError("Developer building list does not match the active map generation")
    counts = points.get("counts")
    items = points.get("items")
    groups = points.get("group_evidence")
    if (points.get("dataset_id") != "developer_map_points"
            or points.get("registry_snapshot_id") != snapshot_id
            or points.get("map_run_id") != run_id
            or points.get("unit_run_id") != unit_run_id
            or points.get("dataset_total") != total
            or not isinstance(counts, dict) or not isinstance(items, list)
            or counts.get("points") != len(items)
            or counts.get("developer_building_points") != len(items)
            or counts.get("developer_building_points_total") != mapped
            or counts.get("unlocated_buildings") != total - mapped
            or counts.get("mapped_construction_points") != 0
            or not isinstance(groups, dict)
            or groups.get("schema_version") != 1
            or groups.get("visibility") != "public"
            or groups.get("registry_snapshot_id") != snapshot_id
            or groups.get("map_run_id") != run_id
            or not isinstance(groups.get("groups"), list)
            or len(items) != mapped
            or any(item.get("point_type") != "developer_building" for item in items)):
        raise RuntimeError("Developer map points do not match the active building list")
    return {"map_run_id": run_id, "unit_run_id": unit_run_id,
            "buildings": total, "points": mapped}


def verify_experience_payloads(verified: dict[str, int | str],
                               building_id: str, floors: dict[str, Any],
                               model: dict[str, Any], pointer: dict[str, Any]) -> None:
    """Confirm that selected menu/model routes use one committed experience."""
    snapshot_id = pointer.get("map_snapshot_id")
    common = {"registry_snapshot_id": snapshot_id,
              "map_run_id": verified["map_run_id"],
              "unit_run_id": verified["unit_run_id"],
              "registry_id": building_id}
    if (pointer.get("schema_version") != "developer-experience-pointer/1"
            or pointer.get("map_run_id") != verified["map_run_id"]
            or pointer.get("unit_run_id") != verified["unit_run_id"]
            or any(floors.get(key) != value or model.get(key) != value
                   for key, value in common.items())
            or model.get("model_run_id") != pointer.get("model_run_id")
            or model.get("status") not in {"ready", "unavailable"}):
        raise RuntimeError("Developer menu/model routes differ from selected generation")
    if model["status"] == "ready":
        asset_sha = model.get("asset_sha256")
        if (not isinstance(asset_sha, str) or HASH_RE.fullmatch(asset_sha) is None
                or model.get("asset_url") != f"/v1/developer-model-assets/{asset_sha}"):
            raise RuntimeError("Selected model lacks its pinned asset URL")
    elif model.get("asset_url") is not None:
        raise RuntimeError("Unavailable selected model exposes an asset URL")


def _smoke(settings: dict[str, str]) -> None:
    base = f"http://127.0.0.1:{settings['NOVOSTROY_HTTP_PORT']}"
    health = _read_json(f"{base}/v1/health")
    buildings = _read_json(f"{base}/v1/registry/buildings?limit=1")
    points = _read_json(f"{base}/v1/map/points")
    verified = verify_developer_payloads(health, buildings, points)
    first_building = buildings["items"][0]["registry_id"]
    floors = _read_json(f"{base}/v1/developer-buildings/{first_building}/floors")
    model = _read_json(f"{base}/v1/developer-buildings/{first_building}/model")
    release = Path(settings["NOVOSTROY_RELEASE_DIR"])
    experience = json.loads((release / "data/property/developer_experience/active.json")
                            .read_text(encoding="utf-8"))
    verify_experience_payloads(verified, first_building, floors, model, experience)
    models = _read_json(f"{base}/v1/models/candidates/summary")
    if models.get("run_status") != "ready" or models.get("counts", {}).get("scanned", 0) < 1:
        raise RuntimeError("The selected model review run is unavailable")


def _write_state(path: Path, state: dict[str, Any]) -> None:
    pending = path.with_suffix(".json.tmp")
    pending.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def apply(settings: dict[str, str], state_dir: Path, *, local_images: bool = False) -> None:
    state_dir = _control_directory(state_dir, Path(settings["NOVOSTROY_RELEASE_DIR"]))
    state_path = state_dir / "deploy-state.json"
    lock_path = state_dir / "deploy.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = verify_release(Path(settings["NOVOSTROY_RELEASE_DIR"]))
        previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
        previous_settings = previous.get("current") if isinstance(previous, dict) else None
        if (previous_settings and manifest["profile"] != "full-compat"
                and previous_settings["NOVOSTROY_RELEASE_DIR"] != settings["NOVOSTROY_RELEASE_DIR"]):
            raise ValueError("Updates require full-compat assets for existing cached map URLs")
        if previous_settings:
            previous_settings = _settings(
                Path(previous_settings["NOVOSTROY_RELEASE_DIR"]),
                previous_settings["NOVOSTROY_IMAGE_TAG"],
                previous_settings["NOVOSTROY_BIND_ADDR"],
                int(previous_settings["NOVOSTROY_HTTP_PORT"]),
            )
        _compose(settings, "config", "--quiet")
        if not previous_settings and _compose_output(settings, "ps", "-a", "-q"):
            raise RuntimeError("Compose containers exist without a recorded deploy state; inspect them first")
        if local_images:
            _verify_local_images(settings["NOVOSTROY_IMAGE_TAG"])
        else:
            _compose(settings, "pull", "api", "web")  # Pull before replacing a running release.
        try:
            _compose(settings, "up", "-d", "--wait", "--no-build", "--pull", "never", "api", "web")
            _smoke(settings)
            state = {"current": settings, "previous": previous_settings}
            _write_state(state_path, state)
        except Exception:
            if previous_settings:
                print("New release failed; restoring the previous Compose release", file=sys.stderr)
                try:
                    _compose(previous_settings, "up", "-d", "--wait", "--no-build", "--pull", "never", "api", "web")
                    _smoke(previous_settings)
                except Exception as rollback_error:
                    raise RuntimeError("New release failed and automatic rollback also failed") from rollback_error
            raise
        print(f"Applied source image {settings['NOVOSTROY_IMAGE_TAG']} with {settings['NOVOSTROY_RELEASE_DIR']}")


def rollback(state_dir: Path) -> None:
    state_dir = _directory(state_dir)
    state_path = state_dir / "deploy-state.json"
    with (state_dir / "deploy.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        current = state.get("current")
        previous = state.get("previous")
        if not isinstance(current, dict) or not isinstance(previous, dict):
            raise RuntimeError("No previously successful release is recorded")
        _control_directory(state_dir, Path(current["NOVOSTROY_RELEASE_DIR"]))
        previous = _settings(Path(previous["NOVOSTROY_RELEASE_DIR"]),
                             previous["NOVOSTROY_IMAGE_TAG"], previous["NOVOSTROY_BIND_ADDR"],
                             int(previous["NOVOSTROY_HTTP_PORT"]))
        verify_release(Path(previous["NOVOSTROY_RELEASE_DIR"]))
        _compose(previous, "up", "-d", "--wait", "--no-build", "--pull", "never", "api", "web")
        _smoke(previous)
        _write_state(state_path, {"current": previous, "previous": state.get("current")})
        print(f"Restored source image {previous['NOVOSTROY_IMAGE_TAG']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--release-dir", type=Path, required=True)
    apply_parser.add_argument("--state-dir", type=Path, required=True,
                              help="private 0700 deploy-control directory outside API mounts")
    apply_parser.add_argument("--image-tag", required=True)
    apply_parser.add_argument("--local-images", action="store_true",
                              help="use SHA-labelled local images without contacting Registry")
    apply_parser.add_argument("--bind-address", default="127.0.0.1",
                              choices=("127.0.0.1", "0.0.0.0"))
    apply_parser.add_argument("--http-port", type=int, default=8080)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "apply":
        settings = _settings(args.release_dir, args.image_tag,
                             args.bind_address, args.http_port)
        apply(settings, args.state_dir, local_images=args.local_images)
    else:
        rollback(args.state_dir)


if __name__ == "__main__":
    main()
