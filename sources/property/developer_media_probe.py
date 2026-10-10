"""Bounded private HTTP verification of a deterministic sample of media refs.

This probes at most 12 source URLs; it never publishes downloaded bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from sources.property.developer_harvest import atomic_bytes, canonical_json, checked_url, digest


MAX_MEDIA_BYTES = 10 * 1024 * 1024
MEDIA_HOSTS = {
    "a101": {"cdn.a101.ru"},
    "fsk": {"cdn.fsk.ru"},
    "granel": {"storage.yandexcloud.net", "granelle.ru"},
    "brusnika": {"cdn.brusnika.ru"},
    "proplanirovki": {"cdn-dataout.trendagent.ru"},
}
ROLE_PLAN = {
    "a101": ("apartment_plan", "floor_plan", "project_genplan"),
    "fsk": ("apartment_plan", "floor_plan"),
    "granel": ("apartment_plan", "floor_plan"),
    "brusnika": ("apartment_plan", "floor_plan"),
    "proplanirovki": ("apartment_plan", "project_rendering", "project_cover"),
}


def _verified_refs(delivery_dir: Path) -> list[dict[str, Any]]:
    manifest = json.loads((delivery_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "developer-delivery/1":
        raise ValueError("Unsupported private delivery")
    info = manifest.get("files", {}).get("media_refs")
    if not isinstance(info, dict) or info.get("path") != "media_refs.jsonl":
        raise ValueError("Missing media reference manifest")
    raw = (delivery_dir / info["path"]).read_bytes()
    if digest(raw) != info.get("sha256") or raw.count(b"\n") != info.get("records"):
        raise ValueError("Media reference file hash/count mismatch")
    return [json.loads(line) for line in raw.splitlines() if line]


def select_samples(refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for ref in refs:
        provider, role, url = ref.get("provider"), ref.get("role"), ref.get("url")
        if provider not in ROLE_PLAN or role not in ROLE_PLAN[provider] or not isinstance(url, str):
            continue
        try:
            checked_url(url, MEDIA_HOSTS[provider])
        except ValueError:
            continue
        if ref.get("public_display_allowed") is not False:
            continue
        buckets.setdefault((provider, role), {}).setdefault(url, ref)
    selected: list[dict[str, Any]] = []
    for provider, roles in ROLE_PLAN.items():
        for role in roles:
            group = buckets.get((provider, role), {})
            if group:
                # Stable for the same source snapshot, without favoring early cards.
                url = min(group, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
                selected.append(group[url])
    if not 10 <= len(selected) <= 12:
        raise ValueError(f"Required cross-provider typed sample is incomplete: {len(selected)}")
    return selected


@dataclass(frozen=True)
class MediaResponse:
    status: int | None
    body: bytes
    content_type: str | None
    final_url: str
    error: str | None = None


def fetch_media(url: str, allowed_hosts: set[str]) -> MediaResponse:
    current = checked_url(url, allowed_hosts)
    with tempfile.TemporaryDirectory(prefix="developer-media-probe-") as temporary:
        root = Path(temporary)
        body, headers = root / "body", root / "headers"
        for redirect in range(3):
            try:
                result = subprocess.run([
                    "/usr/bin/curl", "--silent", "--show-error", "--proto", "=https",
                    "--connect-timeout", "8", "--max-time", "25",
                    "--max-filesize", str(MAX_MEDIA_BYTES), "--max-redirs", "0",
                    "--dump-header", str(headers), "--output", str(body),
                    "--write-out", "%{http_code}", "--header", "Accept: image/*,application/pdf",
                    "--", current,
                ], capture_output=True, check=False, timeout=28)
            except subprocess.TimeoutExpired:
                return MediaResponse(None, b"", None, current, "curl_timeout")
            except OSError:
                return MediaResponse(None, b"", None, current, "curl_unavailable")
            if result.returncode:
                return MediaResponse(None, b"", None, current, f"curl_exit_{result.returncode}")
            try:
                status = int(result.stdout.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError):
                return MediaResponse(None, b"", None, current, "invalid_curl_status")
            parsed = {}
            for line in headers.read_text(encoding="iso-8859-1").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    parsed[key.strip().lower()] = value.strip()
            if status in {301, 302, 303, 307, 308}:
                location = parsed.get("location")
                if not location:
                    return MediaResponse(status, b"", parsed.get("content-type"), current,
                                         "redirect_without_location")
                try:
                    current = checked_url(location, allowed_hosts, current)
                except ValueError:
                    return MediaResponse(status, b"", parsed.get("content-type"), current,
                                         "redirect_outside_approved_media_hosts")
                if redirect == 2:
                    break
                time.sleep(1.0)
                continue
            data = body.read_bytes() if body.is_file() else b""
            if len(data) > MAX_MEDIA_BYTES:
                return MediaResponse(status, b"", parsed.get("content-type"), current,
                                     "body_too_large")
            return MediaResponse(status, data, parsed.get("content-type"), current,
                                 None if status == 200 else f"http_{status}")
    return MediaResponse(None, b"", None, current, "redirect_limit")


def detected_media_type(body: bytes) -> str | None:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"%PDF-"):
        return "application/pdf"
    prefix = body[:2048].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if prefix.startswith(b"<?xml") or prefix.startswith(b"<svg"):
        if b"<svg" in prefix[:2048]:
            return "image/svg+xml"
    return None


def probe(delivery_dir: Path, output_dir: Path, *, live: bool,
          fetch: Callable[[str, set[str]], MediaResponse] = fetch_media,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    sample = select_samples(_verified_refs(delivery_dir))
    delivery_sha = digest((delivery_dir / "manifest.json").read_bytes())
    if not live:
        return {"delivery_manifest_sha256": delivery_sha, "sample": [
            {"provider": row["provider"], "role": row["role"], "url": row["url"]}
            for row in sample], "network_attempts": 0}
    completed = output_dir / "manifest.json"
    if completed.exists():
        prior = json.loads(completed.read_text(encoding="utf-8"))
        if (prior.get("schema_version") != "developer-media-probe/1"
                or prior.get("delivery_manifest_sha256") != delivery_sha):
            raise ValueError("Existing media probe belongs to another delivery")
        return prior
    output_dir.mkdir(parents=True, exist_ok=True)
    observed: list[dict[str, Any]] = []
    blocked_hosts: set[str] = set()
    last_request: dict[str, float] = {}
    for ref in sample:
        provider, url = ref["provider"], ref["url"]
        host = urlsplit(url).hostname or ""
        entry = {"provider": provider, "role": ref["role"],
                 "scope": ref.get("scope"), "observation_id": ref["observation_id"],
                 "source_page_url": ref.get("source_page_url"),
                 "original_url": url, "rights_status": "unknown",
                 "public_display_allowed": False,
                 "source_updated_at": ref.get("source_updated_at")}
        if host in blocked_hosts:
            observed.append({**entry, "fetch_status": "skipped_blocked_host"})
            continue
        previous = last_request.get(host)
        if previous is not None:
            sleep(max(0.0, 1.0 - (time.monotonic() - previous)))
        last_request[host] = time.monotonic()
        response = fetch(url, MEDIA_HOSTS[provider])
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if response.status in {401, 403, 429}:
            blocked_hosts.add(host)
        content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
        detected = detected_media_type(response.body) if response.status == 200 else None
        valid = (response.status == 200 and detected is not None
                 and content_type in {detected, "application/octet-stream"})
        content_sha = digest(response.body) if response.body else None
        if valid and content_sha:
            extension = {"image/png": ".png", "image/jpeg": ".jpg",
                         "image/webp": ".webp", "image/svg+xml": ".svg",
                         "application/pdf": ".pdf"}[detected]
            target = output_dir / "raw" / (content_sha + extension)
            if not target.exists():
                atomic_bytes(target, response.body)
        observed.append({
            **entry, "observed_at": at, "http_status": response.status,
            "final_url": response.final_url, "content_type": content_type or None,
            "detected_type": detected, "bytes": len(response.body),
            "sha256": content_sha, "fetch_status": (
                "verified_image_or_plan" if valid else response.error or "not_image_or_type_mismatch"),
        })
    result = {"schema_version": "developer-media-probe/1",
              "delivery_manifest_sha256": delivery_sha,
              "sample_count": len(sample),
              "network_attempts": sum("http_status" in row for row in observed),
              "blocked_hosts": sorted(blocked_hosts),
              "media_reuse_rights": "unknown", "publication_status": "private_research_only",
              "results": observed}
    atomic_bytes(output_dir / "manifest.json", canonical_json(result) + b"\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    result = probe(args.delivery_dir, args.output_dir, live=args.live)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
