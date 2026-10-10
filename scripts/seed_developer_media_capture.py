"""Seed one hash-pinned research capture into the writable media cache.

The media row is resolved from the active, fully validated experience overlay;
callers cannot pass a URL or row JSON. Input bytes must match that row's pinned
capture hash before they are normalized and written to the runtime cache.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys

from backend import developer_enrichment, developer_experience, developer_map
from backend.developer_media_cache import (
    MAX_SOURCE_BYTES,
    MediaCacheError,
    seed_verified_capture,
)
from backend.store import ROOT


def _read_regular_capture(raw_path: str) -> bytes:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if any(part in {".", ".."} for part in path.parts[1:]):
        raise ValueError("unsafe_capture_path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("unsafe_capture_path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("capture_not_bounded_regular_file")
    with path.open("rb") as stream:
        blob = stream.read(MAX_SOURCE_BYTES + 1)
    if not blob or len(blob) > MAX_SOURCE_BYTES or len(blob) != info.st_size:
        raise ValueError("capture_size_changed")
    return blob


def _active_media_row(media_id: str) -> dict[str, object]:
    map_run = developer_map.resolve_active()
    if map_run is None:
        raise ValueError("active_map_missing")
    experience = developer_experience.resolve_active(map_run)
    if experience is None:
        raise ValueError("active_experience_missing")
    menu, _models = experience
    overlay = menu.get("enrichment")
    if not isinstance(overlay, dict):
        raise ValueError("active_enrichment_missing")
    media_rows = overlay.get("media")
    if not isinstance(media_rows, dict):
        raise ValueError("active_media_registry_missing")
    row = media_rows.get(media_id)
    if not isinstance(row, dict) or row.get("media_id") != media_id:
        raise ValueError("media_id_not_in_active_registry")
    if row.get("prepared_source") == "menu_asset":
        raise ValueError("media_already_prepared")
    if not isinstance(row.get("known_capture_sha256"), str):
        raise ValueError("media_capture_not_pinned")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--capture", required=True,
                        help="regular local file matching the active row's pinned capture SHA")
    args = parser.parse_args()

    try:
        row = _active_media_row(args.media_id)
        source_bytes = _read_regular_capture(args.capture)
        expected_sha = row["known_capture_sha256"]
        if not isinstance(expected_sha, str):
            raise ValueError("media_capture_not_pinned")
        result = seed_verified_capture(
            row,
            source_bytes,
            expected_source_sha256=expected_sha,
            cache_root=ROOT / "data/runtime/developer_media/cache",
        )
    except (MediaCacheError, OSError, ValueError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, MediaCacheError) else str(exc)
        if code not in {
            "unsafe_capture_path", "capture_not_bounded_regular_file", "capture_size_changed",
            "active_map_missing", "active_experience_missing", "active_enrichment_missing",
            "active_media_registry_missing", "media_id_not_in_active_registry",
            "media_already_prepared", "media_capture_not_pinned", "capture_hash_mismatch",
            "invalid_media_id", "invalid_registry_binding", "invalid_scope", "unsupported_role",
            "invalid_unit_binding", "invalid_floor_binding", "invalid_project_binding",
            "invalid_media_binding", "invalid_source_evidence", "invalid_rights_status",
            "invalid_source_url", "source_url_not_allowlisted", "unsupported_source_type",
            "invalid_capture_reference", "source_too_large", "source_mime_mismatch",
            "source_image_invalid", "image_dimensions_rejected", "animated_image_rejected",
            "prepared_image_too_large", "unsupported_prepared_type", "cache_io_error",
            "cache_root_must_be_absolute", "unsafe_cache_path", "cache_metadata_invalid",
            "unsafe_cache_file", "media_binding_changed", "cache_object_corrupt",
            "cache_capacity_reached", "cache_object_missing", "media_cache_failed",
        }:
            code = "seed_failed"
        print(json.dumps({"status": "error", "reason_code": code}, sort_keys=True))
        return 2

    print(json.dumps({
        "status": result.status,
        "media_id": result.media_id,
        "sha256": result.sha256,
        "mime_type": result.mime_type,
        "byte_length": result.byte_length,
        "rights_status": result.rights_status,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
