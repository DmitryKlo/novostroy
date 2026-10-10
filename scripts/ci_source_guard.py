"""Reject private, generated, linked, or credential-bearing tracked CI inputs.

Run against the actual GitLab checkout, not a previously prepared local source
snapshot. It scans file bytes but never prints a credential value.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from scripts.publish_gitlab import (
    MAX_FILE_BYTES,
    SECRET_PATTERNS,
    _check_source_relative,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCKER_EXCLUSIONS = {
    ".runtime", "data", "assets", "**/.venv", "**/node_modules",
    "**/*.jsonl", "**/*.glb", "**/*.parquet", "**/*.key", "**/*.pem",
}


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    paths = [Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Tracked source checkout is empty or contains duplicate paths")
    return paths


def validate_checkout(root: Path) -> dict[str, int]:
    root = root.resolve(strict=True)
    paths = tracked_paths(root)
    total_bytes = 0
    for relative in paths:
        _check_source_relative(relative)
        if any(ord(character) < 32 for character in str(relative)):
            raise ValueError("Tracked source path contains control characters")
        path = root
        for component in relative.parts:
            path = path / component
            if path.is_symlink():
                raise ValueError(f"Tracked source contains a link: {relative}")
        if not path.is_file():
            raise ValueError(f"Tracked source file missing: {relative}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"Tracked source file exceeds review cap: {relative}")
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Possible credential in tracked source file: {relative}")
        total_bytes += size
    dockerignore = root / ".dockerignore"
    exclusions = {line.strip() for line in dockerignore.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")}
    if any(line.startswith("!") for line in exclusions):
        raise ValueError("Docker context re-inclusion is not allowed for source-only CI")
    missing = REQUIRED_DOCKER_EXCLUSIONS - exclusions
    if missing:
        raise ValueError("Docker context exclusion is incomplete: " + ", ".join(sorted(missing)))
    return {"tracked_files": len(paths), "tracked_bytes": total_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = validate_checkout(args.root)
    print(f"source boundary PASS: {result['tracked_files']} tracked files, "
          f"{result['tracked_bytes']} bytes; no private/generated inputs")


if __name__ == "__main__":
    main()
