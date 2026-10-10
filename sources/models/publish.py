"""Publish one validated immutable model run after independent release gates.

The sanitized asset pointer switches first; the active data pointer switches
last, so the API never selects a run before its assets are permitted.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pipeline.build_tiles import save_bytes
from sources.models.audit import audit
from sources.models.build import ASSETS, OUTPUT, canonical, file_sha


def publish(run_id: str, expected_manifest_sha256: str,
            output_root: Path = OUTPUT, asset_root: Path = ASSETS) -> dict[str, Any]:
    if re.fullmatch(r"models-v1-[a-f0-9]{16}", run_id) is None:
        raise ValueError("Invalid immutable model run ID")
    if re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256) is None:
        raise ValueError("Invalid expected manifest SHA-256")
    run_dir = output_root / run_id
    if file_sha(run_dir / "manifest.json") != expected_manifest_sha256:
        raise ValueError("Run changed since independent review")
    audit_result = audit(run_dir, asset_root)
    if audit_result["manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("Run audit hash changed")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    pointer_path = asset_root / "manifest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    active_path = output_root / "active.json"
    old_root = pointer.get("project_models_root")
    if old_root and old_root != manifest["public_root"]:
        old_public = asset_root / old_root / "public_manifest.json"
        if not old_public.is_file() or json.loads(old_public.read_text(encoding="utf-8")).get(
            "public_redacted_source_ids") is not True:
            raise ValueError("Prior project-model root is not sanitized for compatibility")
        pointer["previous_public_roots"] = sorted(set(pointer.get("previous_public_roots", [])) | {old_root})
    pointer["project_models_root"] = manifest["public_root"]
    save_bytes(pointer_path, canonical(pointer))
    active = {"run_id": run_id, "manifest_sha256": expected_manifest_sha256}
    save_bytes(active_path, canonical(active))
    return {"active": active, "public_root": manifest["public_root"],
            "previous_public_roots": pointer.get("previous_public_roots", []),
            "audit": audit_result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("expected_manifest_sha256")
    arguments = parser.parse_args()
    print(json.dumps(publish(arguments.run_id, arguments.expected_manifest_sha256),
                     ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
