#!/usr/bin/env python3
"""One-attempt, byte- and time-bounded Overture Moscow envelope fetch.

Writes Overture GeoParquet, appends durable attempt events, preserves partial
outputs, and skips a completed release/bbox on restart. A retry starts a new
bounded fetch because the Overture CLI does not expose byte-range resume.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOUNDARY = ROOT / "data/city/osm_moscow_city_boundary.geojson"
DEFAULT_BOUNDARY_MANIFEST = ROOT / "data/city/osm_moscow_city_boundary.manifest.json"
DATA_DIR = ROOT / "data/city"
RELEASE = "2026-09-23.1"
CLI_VERSION = "1.0.2"
CHECKPOINT = DATA_DIR / "overture_moscow_bbox_checkpoint.jsonl"
MAX_BYTES = 1_000_000_000
MAX_SECONDS = 20 * 60


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def positions(coords: Any) -> Iterator[tuple[float, float]]:
    if isinstance(coords, (list, tuple)) and len(coords) >= 2 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords[:2]
    ):
        yield float(coords[0]), float(coords[1])
        return
    if isinstance(coords, (list, tuple)):
        for item in coords:
            yield from positions(item)


def boundary_bbox(path: Path) -> tuple[float, float, float, float]:
    geojson = json.loads(path.read_text(encoding="utf-8"))
    geometry = geojson["features"][0]["geometry"]
    points = list(positions(geometry["coordinates"]))
    if not points:
        raise ValueError("boundary GeoJSON contains no coordinates")
    if any(not math.isfinite(x) or not math.isfinite(y) for x, y in points):
        raise ValueError("boundary contains non-finite coordinates")
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def append_event(event: dict[str, Any]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def previous_attempts(bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    if not CHECKPOINT.exists():
        return []
    items = []
    for line in CHECKPOINT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("bbox_wgs84") == list(bbox) and item.get("release") == RELEASE:
            items.append(item)
    return items


def set_file_limit(max_bytes: int) -> None:
    def apply_limit() -> None:
        _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        limit = min(max_bytes, hard) if hard != resource.RLIM_INFINITY else max_bytes
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return apply_limit  # type: ignore[return-value]


def measure_parquet_rows(path: Path) -> int | None:
    try:
        import pyarrow.parquet as pq
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--boundary-manifest", type=Path, default=DEFAULT_BOUNDARY_MANIFEST)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--timeout-seconds", type=int, default=MAX_SECONDS)
    args = parser.parse_args()
    if not args.boundary.is_file() or not args.boundary_manifest.is_file():
        parser.error("boundary GeoJSON and its manifest are required")
    if not 1 <= args.max_bytes <= MAX_BYTES:
        parser.error(f"--max-bytes must be between 1 and {MAX_BYTES}")
    if not 1 <= args.timeout_seconds <= MAX_SECONDS:
        parser.error(f"--timeout-seconds must be between 1 and {MAX_SECONDS}")

    bbox = boundary_bbox(args.boundary)
    history = previous_attempts(bbox)
    if any(item.get("event") == "complete" for item in history):
        print(json.dumps({"status": "already_complete", "bbox_wgs84": list(bbox), "checkpoint": str(CHECKPOINT)}))
        return 0
    attempt = 1 + sum(item.get("event") == "started" for item in history)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = DATA_DIR / f"overture_moscow_envelope_{RELEASE}_attempt{attempt:02d}_{stamp}.parquet"
    manifest_path = output.with_suffix(".manifest.json")
    uvx = shutil.which("uvx")
    if not uvx:
        parser.error("uvx is required for the official Overture CLI fetch")

    boundary_manifest = json.loads(args.boundary_manifest.read_text(encoding="utf-8"))
    event_base = {
        "release": RELEASE,
        "cli": f"overturemaps=={CLI_VERSION}",
        "query_type": "building",
        "output_format": "geoparquet",
        "bbox_wgs84": list(bbox),
        "boundary_relation_id": boundary_manifest.get("relation_id"),
        "boundary_relation_version": boundary_manifest.get("relation_version"),
        "boundary_sha256": boundary_manifest.get("output_sha256"),
        "max_bytes": args.max_bytes,
        "timeout_seconds": args.timeout_seconds,
        "attempt": attempt,
        "output_path": str(output),
    }
    started = dt.datetime.now(dt.timezone.utc)
    append_event({"event": "started", "at_utc": started.isoformat(), **event_base})
    command = [
        uvx, "--system-certs", "--from", f"overturemaps=={CLI_VERSION}",
        "overturemaps", "download", "--bbox=" + ",".join(map(str, bbox)),
        "--release=" + RELEASE, "-f", "geoparquet", "--type=building", "-o", str(output),
    ]
    env = os.environ.copy()
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    configured_ca = Path(env.get("SSL_CERT_FILE", "/no/such/certificate/file"))
    if system_ca.is_file() and not configured_ca.is_file():
        env["SSL_CERT_FILE"] = str(system_ca)

    process = subprocess.Popen(command, env=env, preexec_fn=set_file_limit(args.max_bytes), start_new_session=True)
    deadline = time.monotonic() + args.timeout_seconds
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            break
        time.sleep(2)
    return_code = process.wait()
    finished = dt.datetime.now(dt.timezone.utc)
    size = output.stat().st_size if output.is_file() else 0
    output_hash = sha256(output) if output.is_file() else None
    rows = measure_parquet_rows(output) if output.is_file() and return_code == 0 and not timed_out else None
    if timed_out:
        status = "timed_out_partial_or_incomplete"
    elif return_code == 0:
        status = "bbox_download_complete_unclipped"
    elif size >= args.max_bytes:
        status = "file_size_limit_reached_partial_or_incomplete"
    else:
        status = "failed_partial_or_incomplete"

    manifest = {
        **event_base,
        "status": status,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "process_return_code": return_code,
        "measured_row_count": rows,
        "output_bytes": size,
        "output_sha256": output_hash,
        "output_validity": "parquet metadata readable" if rows is not None else "unverified; may be partial",
        "license": "ODbL-1.0 for the Overture Buildings theme; upstream records preserve source metadata",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
        "scope_note": "Moscow administrative boundary bbox only; output has not been clipped to the boundary polygon",
        "checkpoint_file": CHECKPOINT.name,
        "resume_note": "completed attempts are skipped; failed attempts are preserved and retried to a new file from the beginning",
    }
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    event_name = "complete" if return_code == 0 and not timed_out and rows is not None else "partial_or_failed"
    append_event({"event": event_name, "at_utc": finished.isoformat(), **manifest})
    print(json.dumps({"status": status, "output": str(output), "manifest": str(manifest_path), "bytes": size, "rows": rows, "sha256": output_hash}, ensure_ascii=False))
    return 0 if event_name == "complete" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"bounded Overture fetch error: {exc}")
