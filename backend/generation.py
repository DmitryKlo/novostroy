"""Resolve one immutable registry/model/visibility generation for an API request.

The generation pointer is the only authority after publication. A broken
pointer fails closed; legacy mutable files are used only before first publish.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from backend.store import ROOT, asset_manifest


ACTIVE_PATH = ROOT / "data/property/estateline/normalized/active.json"
GENERATION_ROOT = ACTIVE_PATH.parent
GENERATION_RE = re.compile(r"generation-[0-9a-f]{16}\Z")
SNAPSHOT_RE = re.compile(
    r"data/property/estateline/normalized/snapshots/estate-[0-9a-f]{16}\Z")
VISIBILITY_RE = re.compile(r"visibility-v1-[0-9a-f]{16}\Z")
MODEL_RE = re.compile(r"models-v1-[0-9a-f]{16}\Z")
MODEL_ROOT_RE = re.compile(r"public-project-models-v1-[0-9a-f]{16}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNSET = object()
_PINNED: ContextVar[object] = ContextVar("novostroy_generation", default=_UNSET)


def _sha(path: Path, *, max_bytes: int) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError("Generation file exceeds its size limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file(relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (not parts or relative.startswith("/") or any(
            part in {"", ".", ".."} or part.startswith(".") for part in parts)):
        raise ValueError("Unsafe generation path")
    path = ROOT
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Generation path contains a symlink")
    if not path.is_file():
        raise FileNotFoundError("Generation file is missing")
    return path


def _required_sha(value: Any) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ValueError("Invalid generation SHA-256")
    return value


@lru_cache(maxsize=4)
def _bundle(bundle_path: Path, bundle_hash: str, mtime_ns: int, size: int) -> dict[str, Any]:
    if _sha(bundle_path, max_bytes=64_000) != bundle_hash:
        raise ValueError("Generation bundle differs from pointer")
    doc = json.loads(bundle_path.read_text(encoding="utf-8"))
    if (not isinstance(doc, dict) or doc.get("schema_version") != "estate-generation/1"
            or set(doc) != {"schema_version", "normalized_snapshot", "visibility",
                                "model", "source_snapshot_id"}):
        raise ValueError("Unsupported generation bundle")
    source = doc["normalized_snapshot"]
    visibility = doc["visibility"]
    model = doc["model"]
    if (not isinstance(source, dict) or set(source) != {"path", "projects_sha256",
                                                     "source_manifest_sha256",
                                                     "review_required_sha256", "schema_sha256"}
            or not isinstance(source.get("path"), str)
            or SNAPSHOT_RE.fullmatch(source["path"]) is None):
        raise ValueError("Invalid normalized snapshot reference")
    for key in ("projects_sha256", "source_manifest_sha256", "review_required_sha256",
                "schema_sha256"):
        _required_sha(source[key])
    if (doc["source_snapshot_id"] != f"building-registry-{source['projects_sha256'][:16]}"
            or not isinstance(visibility, dict)
            or set(visibility) != {"run_id", "manifest_sha256", "records_sha256"}
            or not isinstance(visibility.get("run_id"), str)
            or VISIBILITY_RE.fullmatch(visibility["run_id"]) is None
            or not isinstance(model, dict)
            or set(model) != {"run_id", "manifest_sha256", "public_root",
                               "public_manifest_sha256"}
            or not isinstance(model.get("run_id"), str)
            or MODEL_RE.fullmatch(model["run_id"]) is None
            or not isinstance(model.get("public_root"), str)
            or MODEL_ROOT_RE.fullmatch(model["public_root"]) is None):
        raise ValueError("Invalid generation component reference")
    for section, keys in ((visibility, ("manifest_sha256", "records_sha256")),
                          (model, ("manifest_sha256", "public_manifest_sha256"))):
        for key in keys:
            _required_sha(section[key])
    return doc


@lru_cache(maxsize=4)
def _validated(bundle_hash: str, signatures: tuple[tuple[str, int, int], ...]) -> dict[str, Any]:
    bundle_path = _safe_file(signatures[0][0])
    doc = _bundle(bundle_path, bundle_hash, signatures[0][1], signatures[0][2])
    source, visibility, model = (doc[key] for key in
                                 ("normalized_snapshot", "visibility", "model"))
    paths = {
        "projects": f"{source['path']}/projects.jsonl",
        "source_manifest": f"{source['path']}/source_manifest.json",
        "review_required": f"{source['path']}/review_required.jsonl",
        "schema": f"{source['path']}/schema.json",
        "visibility_manifest": f"data/property/estateline/visibility/runs/{visibility['run_id']}/manifest.json",
        "visibility_records": f"data/property/estateline/visibility/runs/{visibility['run_id']}/records.jsonl",
        "model_manifest": f"data/models/{model['run_id']}/manifest.json",
        "public_manifest": f"assets/{model['public_root']}/public_manifest.json",
    }
    expected = {
        "projects": source["projects_sha256"],
        "source_manifest": source["source_manifest_sha256"],
        "review_required": source["review_required_sha256"],
        "schema": source["schema_sha256"],
        "visibility_manifest": visibility["manifest_sha256"],
        "visibility_records": visibility["records_sha256"],
        "model_manifest": model["manifest_sha256"],
        "public_manifest": model["public_manifest_sha256"],
    }
    limits = {"projects": 100_000_000, "review_required": 20_000_000,
              "visibility_records": 50_000_000, "public_manifest": 5_000_000}
    for label, relative in paths.items():
        if _sha(_safe_file(relative), max_bytes=limits.get(label, 1_000_000)) != expected[label]:
            raise ValueError(f"Generation {label} hash mismatch")
    source_manifest = json.loads(_safe_file(paths["source_manifest"]).read_text(encoding="utf-8"))
    visibility_manifest = json.loads(_safe_file(paths["visibility_manifest"]).read_text(encoding="utf-8"))
    model_manifest = json.loads(_safe_file(paths["model_manifest"]).read_text(encoding="utf-8"))
    public_manifest = json.loads(_safe_file(paths["public_manifest"]).read_text(encoding="utf-8"))
    if (source_manifest.get("projects_sha256") != expected["projects"]
            or visibility_manifest.get("source_sha256") != expected["projects"]
            or visibility_manifest.get("source_manifest_sha256") != expected["source_manifest"]
            or visibility_manifest.get("records_sha256") != expected["visibility_records"]
            or model_manifest.get("registry_snapshot_id") != doc["source_snapshot_id"]
            or model_manifest.get("public_root") != model["public_root"]
            or model_manifest.get("public_manifest_sha256") != expected["public_manifest"]
            or public_manifest.get("public_redacted_source_ids") is not True
            or public_manifest.get("run_id") != model["run_id"]):
        raise ValueError("Generation component cross-link mismatch")
    asset_roots = asset_manifest()
    if model["public_root"] not in {asset_roots.get("project_models_root"),
                                    *asset_roots.get("previous_public_roots", [])}:
        raise ValueError("Generation model root is not a published sanitized asset root")
    return {**doc, "bundle_sha256": bundle_hash,
            "paths": {key: _safe_file(relative) for key, relative in paths.items()}}


def resolve_generation() -> dict[str, Any] | None:
    """Read one active pointer and validate its pinned immutable components."""
    if not ACTIVE_PATH.exists() and not ACTIVE_PATH.is_symlink():
        return None
    active = _safe_file(str(ACTIVE_PATH.relative_to(ROOT)))
    if active.stat().st_size > 4096:
        raise ValueError("Generation pointer exceeds its size limit")
    pointer = json.loads(active.read_text(encoding="utf-8"))
    if (not isinstance(pointer, dict)
            or set(pointer) != {"schema_version", "generation_id", "bundle_sha256"}
            or pointer["schema_version"] != "estate-generation-pointer/1"):
        raise ValueError("Invalid generation pointer")
    bundle_hash = _required_sha(pointer["bundle_sha256"])
    generation_id = pointer["generation_id"]
    if (not isinstance(generation_id, str) or GENERATION_RE.fullmatch(generation_id) is None
            or generation_id != f"generation-{bundle_hash[:16]}"):
        raise ValueError("Generation pointer identity differs from bundle hash")
    relative = f"data/property/estateline/normalized/generations/{generation_id}/bundle.json"
    bundle_path = _safe_file(relative)
    stat = bundle_path.stat()
    doc = _bundle(bundle_path, bundle_hash, stat.st_mtime_ns, stat.st_size)
    source, vis, model = (doc[key] for key in ("normalized_snapshot", "visibility", "model"))
    relatives = (relative,
                 *(f"{source['path']}/{name}" for name in
                   ("projects.jsonl", "source_manifest.json", "review_required.jsonl", "schema.json")),
                 f"data/property/estateline/visibility/runs/{vis['run_id']}/manifest.json",
                 f"data/property/estateline/visibility/runs/{vis['run_id']}/records.jsonl",
                 f"data/models/{model['run_id']}/manifest.json",
                 f"assets/{model['public_root']}/public_manifest.json",
                 "assets/manifest.json")
    signatures = tuple((name, path.stat().st_mtime_ns, path.stat().st_size)
                       for name in relatives for path in (_safe_file(name),))
    return _validated(bundle_hash, signatures)


def current_generation() -> dict[str, Any] | None:
    pinned = _PINNED.get()
    return resolve_generation() if pinned is _UNSET else pinned  # type: ignore[return-value]


def pin_generation(generation: dict[str, Any] | None) -> Token[object]:
    return _PINNED.set(generation)


def reset_generation(token: Token[object]) -> None:
    _PINNED.reset(token)
