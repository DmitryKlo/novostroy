"""One atomic pointer for the map-bound apartment menu and selected models."""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from backend import developer_enrichment, developer_map, developer_models, developer_units
from backend.store import ROOT


BASE = ROOT / "data/property/developer_experience"
ACTIVE_PATH = BASE / "active.json"
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_UNPINNED = object()
_REQUEST: ContextVar[object] = ContextVar("developer_experience_request", default=_UNPINNED)


def pin(value: tuple[dict[str, Any], dict[str, Any]] | None) -> Token[object]:
    return _REQUEST.set(value)


def reset(token: Token[object]) -> None:
    _REQUEST.reset(token)


def current(map_run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pinned = _REQUEST.get()
    return resolve_active(map_run) if pinned is _UNPINNED else pinned  # type: ignore[return-value]


def resolve_active(map_run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read the single commit marker once; invalid references never fall back."""
    if not ACTIVE_PATH.exists() and not ACTIVE_PATH.is_symlink():
        return None
    if ACTIVE_PATH.is_symlink() or not ACTIVE_PATH.is_file() or ACTIVE_PATH.stat().st_size > 4096:
        raise ValueError("Unsafe developer experience pointer")
    pointer = json.loads(ACTIVE_PATH.read_text(encoding="utf-8"))
    common_keys = {"schema_version", "map_run_id", "map_snapshot_id",
                   "map_manifest_sha256", "unit_run_id", "unit_manifest_sha256",
                   "model_run_id", "model_manifest_sha256"}
    version = pointer.get("schema_version") if isinstance(pointer, dict) else None
    expected_keys = (common_keys if version == "developer-experience-pointer/1"
                     else common_keys | {"enrichment_run_id", "enrichment_manifest_sha256"})
    if (not isinstance(pointer, dict) or set(pointer) != expected_keys or
            version not in {"developer-experience-pointer/1", "developer-experience-pointer/2"} or
            any(not isinstance(pointer[key], str) or SHA_RE.fullmatch(pointer[key]) is None
                for key in ("map_manifest_sha256", "unit_manifest_sha256",
                            "model_manifest_sha256") +
                           (("enrichment_manifest_sha256",) if version ==
                            "developer-experience-pointer/2" else ()))):
        raise ValueError("Invalid developer experience pointer")
    map_manifest = map_run["manifest"]
    map_manifest_path = (developer_map.BASE / "runs" / map_manifest["run_id"] /
                         "manifest.json")
    map_digest = hashlib.sha256(map_manifest_path.read_bytes()).hexdigest()
    if (pointer["map_run_id"] != map_manifest["run_id"] or
            pointer["map_snapshot_id"] != map_manifest["snapshot_id"] or
            pointer["map_manifest_sha256"] != map_digest):
        raise ValueError("Experience pointer differs from active map generation")
    menu = developer_units.load_staged(
        pointer["unit_run_id"], map_run,
        expected_manifest_sha256=pointer["unit_manifest_sha256"])
    models = developer_models.load_staged(
        pointer["model_run_id"], map_run,
        expected_manifest_sha256=pointer["model_manifest_sha256"])
    if (models["manifest"]["menu_run_id"] != menu["manifest"]["run_id"] or
            models["manifest"]["menu_manifest_sha256"] !=
            pointer["unit_manifest_sha256"]):
        raise ValueError("Experience model and apartment menu generations differ")
    if version == "developer-experience-pointer/2":
        overlay = developer_enrichment.load_staged(
            pointer["enrichment_run_id"], map_run, menu, models,
            expected_manifest_sha256=pointer["enrichment_manifest_sha256"])
        menu = {**menu, "enrichment": overlay}
    return menu, models
