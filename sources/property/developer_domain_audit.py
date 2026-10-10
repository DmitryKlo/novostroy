"""Bounded private audit of source-backed developer entry pages.

This is an entry-page inventory, not a crawler or an apartment catalogue. It
never promotes a directory label to a project identity or publication right.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import time
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from sources.property.developer_harvest import (
    BLOCK_STATUSES, CHALLENGE_TITLE, DEFAULT_STATE_ROOT, atomic_bytes,
    canonical_json, checked_url, curl_fetch, digest, utc_now,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BATCH_DIR = DEFAULT_STATE_ROOT / "domain-review-luna_coverage"
DEFAULT_INITIAL = DEFAULT_STATE_ROOT / "official_domain_evidence.jsonl"
DEFAULT_OUTPUT = DEFAULT_STATE_ROOT / "domain-entry-audit-20261010"
KNOWN_DENIAL_HOSTS = {"pik.ru", "samolet.ru"}
ALREADY_HARVESTED_HOSTS = {"a101.ru", "fsk.ru", "granelle.ru", "moskva.brusnika.ru"}
PRICE_PATTERN = re.compile(r"(?:\bот\s*)?\d[\d\s\u00a0]{4,}\s*(?:₽|руб\.?)(?!\w)", re.I)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.").rstrip(".")


def _safe_page(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme == "https" and not parsed.path:
        url = urlunsplit((parsed.scheme, parsed.netloc, "/", parsed.query, ""))
    try:
        return checked_url(url, {hostname})
    except ValueError:
        return None


def read_evidence(batch_dir: Path, initial: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(batch_dir.glob("batch-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                item["evidence_file"] = str(path.relative_to(ROOT))
                item["evidence_id"] = item["review_id"]
                item["label"] = item["normalized_directory_label"]
                item["supported"] = item.get("official_domain_status") == "supported"
                rows.append(item)
    for line in initial.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            item["evidence_file"] = str(initial.relative_to(ROOT))
            item["label"] = item["directory_claimed_name"]
            item["supported"] = item.get("verification_status") == "official_domain_supported"
            rows.append(item)
    ids = [item["evidence_id"] for item in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate evidence ID")
    return rows


def build_plan(rows: list[dict]) -> dict:
    hosts: dict[str, dict] = {}
    for row in rows:
        if not row["supported"]:
            continue
        official = _safe_page(row.get("official_domain_url"))
        if official is None:
            continue
        key = _host(official)
        entry = hosts.setdefault(key, {"host": key, "labels": [], "evidence_ids": [],
                                       "candidate_urls": [], "status": "pending"})
        entry["labels"].append(row["label"])
        entry["evidence_ids"].append(row["evidence_id"])
        for value in (official, row.get("project_or_catalog_entry_url")):
            url = _safe_page(value)
            if url is not None and _host(url) == key and url not in entry["candidate_urls"]:
                entry["candidate_urls"].append(url)
    for key, entry in hosts.items():
        entry["urls"] = entry.pop("candidate_urls")[:2]
        if key in KNOWN_DENIAL_HOSTS:
            entry["status"] = "prior_401_stop"
        elif key in ALREADY_HARVESTED_HOSTS:
            entry["status"] = "already_harvested"
        entry["labels"] = sorted(set(entry["labels"]))
        entry["evidence_ids"] = sorted(set(entry["evidence_ids"]))
    return {"schema_version": "developer-domain-entry-plan/1", "evidence_rows": len(rows),
            "supported_rows": sum(row["supported"] for row in rows),
            "hosts": [hosts[key] for key in sorted(hosts)]}


class PageSignals(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.in_script = False
        self.ignored_tag: str | None = None
        self.script_chunks: list[str] = []
        self.script_size = 0
        self.jsonld_types: set[str] = set()
        self.image_urls: list[str] = []
        self.catalog_links: list[str] = []
        self.text_chunks: list[str] = []
        self.text_size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self.in_script = True
            self.script_chunks = []
            self.script_size = 0
        elif tag in {"script", "style"}:
            self.ignored_tag = tag
        if tag == "meta" and values.get("property") in {"og:image", "twitter:image"}:
            self._image(values.get("content"))
        if tag == "a":
            href = values.get("href")
            if href and re.search(r"/(?:apartments?|flats?|catalog|kvartiry|nedvizhimost)/", href, re.I):
                value = self._url(href)
                if value and value not in self.catalog_links and len(self.catalog_links) < 10:
                    self.catalog_links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.ignored_tag:
            self.ignored_tag = None
        if tag == "script" and self.in_script:
            raw = "".join(self.script_chunks)
            if len(raw) <= 200_000:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = None
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, dict):
                        kind = item.get("@type")
                        for one in kind if isinstance(kind, list) else [kind]:
                            if isinstance(one, str):
                                self.jsonld_types.add(one[:80])
                        image = item.get("image")
                        for one in image if isinstance(image, list) else [image]:
                            self._image(one)
            self.in_script = False
            self.script_chunks = []

    def handle_data(self, data: str) -> None:
        if self.in_script:
            if self.script_size < 200_001:
                self.script_chunks.append(data)
                self.script_size += len(data)
        elif not self.ignored_tag and data.strip() and self.text_size < 500_000:
            self.text_chunks.append(data)
            self.text_size += len(data)

    def _url(self, raw: object) -> str | None:
        if not isinstance(raw, str) or len(raw) > 2048:
            return None
        url = urljoin(self.base, raw)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return url

    def _image(self, raw: object) -> None:
        value = self._url(raw)
        if value and value not in self.image_urls and len(self.image_urls) < 10:
            self.image_urls.append(value)


def page_signals(body: bytes, url: str) -> dict:
    parser = PageSignals(url)
    parser.feed(body.decode("utf-8", "replace"))
    visible = " ".join(parser.text_chunks)
    return {"jsonld_types": sorted(parser.jsonld_types),
            "html_text_price_pattern_present": bool(PRICE_PATTERN.search(visible)),
            "source_linked_catalog_candidates": parser.catalog_links,
            "source_image_refs": parser.image_urls,
            "interpretation": "entry_page_signals_only_no_unit_or_media_rights_claim"}


def _write_csv(path: Path, plan: dict, events: dict[str, list[dict]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["host", "labels", "plan_status",
            "requested", "http_200", "blocked", "transport_errors", "jsonld_types",
            "html_text_price_pages", "catalog_link_candidates", "image_ref_candidates"])
        writer.writeheader()
        for host in plan["hosts"]:
            seen = events.get(host["host"], [])
            writer.writerow({"host": host["host"], "labels": " | ".join(host["labels"]),
                "plan_status": host["status"], "requested": len(seen),
                "http_200": sum(e.get("status") == 200 and not e.get("error") for e in seen),
                "blocked": any(e.get("blocked") for e in seen),
                "transport_errors": sum(e.get("status") is None for e in seen),
                "jsonld_types": " | ".join(sorted({kind for e in seen
                    for kind in (e.get("signals") or {}).get("jsonld_types", [])})),
                "html_text_price_pages": sum(bool((e.get("signals") or {}).get(
                    "html_text_price_pattern_present")) for e in seen),
                "catalog_link_candidates": sum(len((e.get("signals") or {}).get(
                    "source_linked_catalog_candidates", [])) for e in seen),
                "image_ref_candidates": sum(len((e.get("signals") or {}).get(
                    "source_image_refs", [])) for e in seen)})


def run(plan: dict, output: Path, *, live: bool, max_domains: int, delay: float,
        only_hosts: set[str] | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / "audit.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_bytes = canonical_json(plan) + b"\n"
        plan_path = output / "plan.json"
        ledger = output / "requests.jsonl"
        if plan_path.exists() and plan_path.read_bytes() != plan_bytes and ledger.exists():
            raise ValueError("evidence plan changed; use a new output directory")
        atomic_bytes(plan_path, plan_bytes)
        existing = ([json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
                    if ledger.exists() else [])
        attempted = {(e["host"], e["url"]) for e in existing}
        events: dict[str, list[dict]] = defaultdict(list)
        for event in existing:
            events[event["host"]].append(event)
        processed = 0
        for item in plan["hosts"]:
            if (item["status"] != "pending" or processed >= max_domains or
                    (only_hosts and item["host"] not in only_hosts)):
                continue
            host = item["host"]
            pending = [url for url in item["urls"] if (host, url) not in attempted]
            if not pending or any(e.get("blocked") for e in events[host]):
                continue
            processed += 1
            for url in pending:
                if not live:
                    break
                result = curl_fetch(url, {(urlsplit(url).hostname or "").lower()}, {}, 20)
                blocked = result.status in BLOCK_STATUSES or (
                    result.status == 200 and bool(CHALLENGE_TITLE.search(
                        result.body[:100_000].decode("utf-8", "replace"))))
                error = result.error or ("challenge_interstitial" if blocked and result.status == 200 else None)
                event = {"at": utc_now(), "host": host, "url": url, "final_url": result.final_url,
                         "status": result.status, "error": error, "blocked": blocked,
                         "body_bytes": len(result.body), "body_sha256": None,
                         "content_type": result.headers.get("Content-Type") or result.headers.get("content-type"),
                         "signals": None}
                if result.status == 200 and not error and result.body:
                    sha = digest(result.body)
                    body_path = output / "raw" / f"{sha}.body"
                    if not body_path.exists():
                        atomic_bytes(body_path, result.body)
                    event["body_sha256"] = sha
                    event["signals"] = page_signals(result.body, result.final_url)
                with ledger.open("ab") as stream:
                    stream.write(canonical_json(event) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                attempted.add((host, url))
                events[host].append(event)
                if blocked:
                    break
                time.sleep(max(1.0, delay))
        _write_csv(output / "domains.csv", plan, events)
        all_events = [event for group in events.values() for event in group]
        summary = {"schema_version": "developer-domain-entry-audit/1", "created_at": utc_now(),
                   "plan_sha256": digest(plan_bytes), "evidence_rows": plan["evidence_rows"],
                   "supported_rows": plan["supported_rows"], "supported_hosts": len(plan["hosts"]),
                   "prior_401_hosts": sorted(item["host"] for item in plan["hosts"]
                                             if item["status"] == "prior_401_stop"),
                   "already_harvested_hosts": sorted(item["host"] for item in plan["hosts"]
                                                     if item["status"] == "already_harvested"),
                   "requests": len(all_events), "http_200": sum(e["status"] == 200 and
                             not e["error"] for e in all_events),
                   "blocked_hosts": sorted({e["host"] for e in all_events if e["blocked"]}),
                   "transport_errors": sum(e["status"] is None for e in all_events),
                   "all_moscow_coverage_claimed": False,
                   "publication_allowed": False,
                   "request_ledger_sha256": digest(ledger.read_bytes()) if ledger.exists() else None,
                   "domains_csv_sha256": digest((output / "domains.csv").read_bytes())}
        atomic_bytes(output / "summary.json", canonical_json(summary) + b"\n")
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH_DIR)
    parser.add_argument("--initial", type=Path, default=DEFAULT_INITIAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-domains", type=int, default=500)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--only-host", action="append", default=[])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.max_domains <= 500 or args.delay_seconds < 1:
        parser.error("invalid bounds")
    rows = read_evidence(args.batch_dir, args.initial)
    hosts = {host.removeprefix("www.") for host in args.only_host}
    if not all(re.fullmatch(r"[a-z0-9.-]+", host) for host in hosts):
        parser.error("invalid host filter")
    print(json.dumps(run(build_plan(rows), args.output, live=args.live,
                         max_domains=args.max_domains, delay=args.delay_seconds,
                         only_hosts=hosts or None),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
