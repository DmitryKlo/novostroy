#!/usr/bin/env python3
"""Fetch a small, pinned Overture environmental-reference GeoParquet layer.

Only `division_area` and `segment` are supported. Every run has hard byte/time
caps, a durable checkpoint, and a new immutable output name. No building layer
is queried by this adapter.
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
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data/city/environment"
CHECKPOINT = DATA_DIR / "fetch_checkpoint.jsonl"
RELEASE_DEFAULT = "2026-09-23.1"
CLI_VERSION = "1.0.2"
MAX_BYTES = 128_000_000
MAX_SECONDS = 600
THEME_TYPES = {"division_area", "segment"}


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part.strip()) for part in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be xmin,ymin,xmax,ymax") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be xmin,ymin,xmax,ymax")
    xmin, ymin, xmax, ymax = parts
    if not all(math.isfinite(value) for value in parts):
        raise argparse.ArgumentTypeError("bbox values must be finite")
    if not (-180 <= xmin < xmax <= 180 and -90 <= ymin < ymax <= 90):
        raise argparse.ArgumentTypeError("bbox must be ordered WGS84 lon/lat")
    return parts  # type: ignore[return-value]


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    xmin, ymin, xmax, ymax = bbox
    latitude = math.radians((ymin + ymax) / 2)
    return (xmax - xmin) * 111.32 * math.cos(latitude) * (ymax - ymin) * 111.32


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def append_event(event: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def request_history(data_type: str, release: str, bbox: tuple[float, ...]) -> list[dict[str, Any]]:
    if not CHECKPOINT.is_file():
        return []
    results = []
    for line in CHECKPOINT.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            event.get("type") == data_type
            and event.get("release") == release
            and event.get("bbox_wgs84") == list(bbox)
        ):
            results.append(event)
    return results


def capped_file_size(limit_bytes: int):
    def apply() -> None:
        _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        limit = min(limit_bytes, hard) if hard != resource.RLIM_INFINITY else limit_bytes
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply


def parquet_rows(path: Path) -> int | None:
    try:
        import pyarrow.parquet as pq

        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=sorted(THEME_TYPES), required=True)
    parser.add_argument("--bbox", type=parse_bbox, required=True, help="WGS84 xmin,ymin,xmax,ymax; do not pass an unbounded region")
    parser.add_argument("--release", default=RELEASE_DEFAULT)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--timeout-seconds", type=int, default=MAX_SECONDS)
    args = parser.parse_args()
    if not 1 <= args.max_bytes <= MAX_BYTES:
        parser.error(f"--max-bytes must be between 1 and {MAX_BYTES}")
    if not 1 <= args.timeout_seconds <= MAX_SECONDS:
        parser.error(f"--timeout-seconds must be between 1 and {MAX_SECONDS}")
    area = bbox_area_km2(args.bbox)
    area_cap = 8_000 if args.type == "division_area" else 2_000
    if area > area_cap:
        parser.error(f"bbox is about {area:.1f} km2; {args.type} cap is {area_cap} km2")

    history = request_history(args.type, args.release, args.bbox)
    if any(item.get("event") == "complete" for item in history):
        print(json.dumps({"status": "already_complete", "type": args.type, "release": args.release, "bbox_wgs84": args.bbox, "checkpoint": str(CHECKPOINT)}))
        return 0
    attempt = 1 + sum(item.get("event") == "started" for item in history)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"overture_{args.type}_{args.release}_attempt{attempt:02d}_{stamp}"
    output = DATA_DIR / f"{stem}.parquet"
    manifest_path = DATA_DIR / f"{stem}.manifest.json"
    uvx = shutil.which("uvx")
    if not uvx:
        parser.error("uvx is required")

    request = {
        "type": args.type,
        "theme": "divisions" if args.type == "division_area" else "transportation",
        "release": args.release,
        "schema_version": "2.0.0",
        "cli": f"overturemaps=={CLI_VERSION}",
        "bbox_wgs84": list(args.bbox),
        "bbox_area_km2_approx": round(area, 3),
        "max_bytes": args.max_bytes,
        "timeout_seconds": args.timeout_seconds,
        "attempt": attempt,
        "output_path": str(output),
    }
    started = dt.datetime.now(dt.timezone.utc)
    append_event({"event": "started", "at_utc": started.isoformat(), **request})
    command = [
        uvx, "--system-certs", "--from", f"overturemaps=={CLI_VERSION}",
        "overturemaps", "download", "--bbox=" + ",".join(map(str, args.bbox)),
        "--release=" + args.release, "--type=" + args.type, "-f", "geoparquet", "-o", str(output),
    ]
    child_env = os.environ.copy()
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    configured_ca = Path(child_env.get("SSL_CERT_FILE", "/no/such/certificate/file"))
    if system_ca.is_file() and not configured_ca.is_file():
        child_env["SSL_CERT_FILE"] = str(system_ca)
    process = subprocess.Popen(command, env=child_env, preexec_fn=capped_file_size(args.max_bytes), start_new_session=True)
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
        time.sleep(1)
    return_code = process.wait()
    finished = dt.datetime.now(dt.timezone.utc)
    output_bytes = output.stat().st_size if output.exists() else 0
    rows = parquet_rows(output) if return_code == 0 and not timed_out and output.exists() else None
    if timed_out:
        status = "timed_out_partial_or_incomplete"
    elif return_code == 0 and rows is not None:
        status = "bbox_download_complete_unclipped"
    elif output_bytes >= args.max_bytes:
        status = "file_size_limit_reached_partial_or_incomplete"
    else:
        status = "failed_partial_or_incomplete"
    manifest = {
        **request,
        "status": status,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "process_return_code": return_code,
        "row_count": rows,
        "output_bytes": output_bytes,
        "output_sha256": sha256_file(output) if output.exists() else None,
        "license": "ODbL-1.0; retain upstream feature sources and attribution",
        "attribution": "© OpenStreetMap contributors, Overture Maps Foundation",
        "source_urls": [
            "https://docs.overturemaps.org/schema/reference/divisions/division_area/" if args.type == "division_area" else "https://docs.overturemaps.org/schema/reference/transportation/segment/",
            "https://docs.overturemaps.org/attribution/",
        ],
        "coverage": "bbox query only; not clipped to Moscow legal district boundaries and not proof of complete real-world coverage",
        "checkpoint_file": CHECKPOINT.name,
        "retry_policy": "failed partial outputs remain immutable; retry creates a new attempt because CLI does not expose byte-range resume",
    }
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    event_name = "complete" if rows is not None and status == "bbox_download_complete_unclipped" else "partial_or_failed"
    append_event({"event": event_name, "at_utc": finished.isoformat(), **manifest})
    print(json.dumps({"status": status, "type": args.type, "output": str(output), "manifest": str(manifest_path), "rows": rows, "bytes": output_bytes, "sha256": manifest["output_sha256"]}, ensure_ascii=False))
    return 0 if event_name == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
