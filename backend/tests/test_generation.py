"""Atomic generation pointer checks without a server or source-sized copies."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from backend import app, generation


def _write(root: Path, relative: str, content: bytes) -> tuple[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return relative, hashlib.sha256(content).hexdigest()


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fixture(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path]:
    projects = b'{}\n'
    projects_sha = hashlib.sha256(projects).hexdigest()
    source_manifest = _json({"projects_sha256": projects_sha})
    source_manifest_sha = hashlib.sha256(source_manifest).hexdigest()
    snapshot = f"data/property/estateline/normalized/snapshots/estate-{source_manifest_sha[:16]}"
    _write(root, f"{snapshot}/projects.jsonl", projects)
    _write(root, f"{snapshot}/source_manifest.json", source_manifest)
    _, review_sha = _write(root, f"{snapshot}/review_required.jsonl", b"\n")
    _, schema_sha = _write(root, f"{snapshot}/schema.json", b"{}")
    visibility_id = "visibility-v1-" + "a" * 16
    visibility_records = b'{}\n'
    _, visibility_records_sha = _write(
        root, f"data/property/estateline/visibility/runs/{visibility_id}/records.jsonl",
        visibility_records)
    _, visibility_manifest_sha = _write(
        root, f"data/property/estateline/visibility/runs/{visibility_id}/manifest.json",
        _json({"source_sha256": projects_sha, "source_manifest_sha256": source_manifest_sha,
               "records_sha256": visibility_records_sha}))
    model_id = "models-v1-" + "b" * 16
    public_root = "public-project-models-v1-" + "b" * 16
    _, public_manifest_sha = _write(
        root, f"assets/{public_root}/public_manifest.json",
        _json({"public_redacted_source_ids": True, "run_id": model_id}))
    source_snapshot_id = f"building-registry-{projects_sha[:16]}"
    _, model_manifest_sha = _write(
        root, f"data/models/{model_id}/manifest.json",
        _json({"registry_snapshot_id": source_snapshot_id, "public_root": public_root,
               "public_manifest_sha256": public_manifest_sha}))
    bundle = {"schema_version": "estate-generation/1",
              "normalized_snapshot": {"path": snapshot, "projects_sha256": projects_sha,
                                      "source_manifest_sha256": source_manifest_sha,
                                      "review_required_sha256": review_sha,
                                      "schema_sha256": schema_sha},
              "visibility": {"run_id": visibility_id, "manifest_sha256": visibility_manifest_sha,
                             "records_sha256": visibility_records_sha},
              "model": {"run_id": model_id, "manifest_sha256": model_manifest_sha,
                        "public_root": public_root, "public_manifest_sha256": public_manifest_sha},
              "source_snapshot_id": source_snapshot_id}
    bundle_bytes = _json(bundle)
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
    generation_id = f"generation-{bundle_sha[:16]}"
    _write(root, f"data/property/estateline/normalized/generations/{generation_id}/bundle.json",
           bundle_bytes)
    active = root / "data/property/estateline/normalized/active.json"
    _write(root, "data/property/estateline/normalized/active.json",
           _json({"schema_version": "estate-generation-pointer/1",
                  "generation_id": generation_id, "bundle_sha256": bundle_sha}))
    _write(root, "assets/manifest.json", _json({"project_models_root": public_root}))
    monkeypatch.setattr(generation, "ROOT", root)
    monkeypatch.setattr(generation, "ACTIVE_PATH", active)
    monkeypatch.setattr(generation, "asset_manifest", lambda: {"project_models_root": public_root})
    generation._bundle.cache_clear()
    generation._validated.cache_clear()
    return active, root / snapshot / "projects.jsonl"


def test_bundle_hashes_and_paths_fail_closed(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    active, projects = _fixture(monkeypatch, tmp_path)
    pinned = generation.resolve_generation()
    assert pinned is not None
    assert pinned["paths"]["projects"] == projects
    assert pinned["source_snapshot_id"].startswith("building-registry-")
    projects.write_bytes(b'[]\n')
    with pytest.raises(ValueError, match="projects hash mismatch"):
        generation.resolve_generation()
    projects.unlink()
    projects.symlink_to(active)
    with pytest.raises(ValueError, match="symlink"):
        generation.resolve_generation()
    active.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError, match="pointer"):
        generation.resolve_generation()


def test_api_middleware_pins_one_generation_and_returns_safe_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # This fixture exercises the legacy source generation only. Keep the
    # workspace's active developer map/experience outside its request path.
    monkeypatch.setattr(app.developer_map, "ACTIVE_PATH", tmp_path / "no-developer-map.json")
    marker = {"source_snapshot_id": "one"}
    monkeypatch.setattr(app, "resolve_generation", lambda: marker)
    seen: list[dict | None] = []

    async def wrapped(scope: dict, receive, send) -> None:
        seen.append(generation.current_generation())
        seen.append(generation.current_generation())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = app.GenerationPinMiddleware(wrapped)
    sent: list[dict] = []
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/v1/health",
             "headers": [], "query_string": b""}
    asyncio.run(middleware(scope, receive, send))
    assert seen == [marker, marker]
    assert sent[0]["status"] == 200
    monkeypatch.setattr(app, "resolve_generation", lambda: (_ for _ in ()).throw(ValueError("private path")))
    sent.clear()
    asyncio.run(middleware(scope, receive, send))
    assert sent[0]["status"] == 503
    assert b"private path" not in sent[1]["body"]
