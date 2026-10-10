"""Resume the official-domain entry audit with four independent host workers.

Each worker handles one host at a time. A single main-thread writer fsyncs each
response before that host may request its second page. Prior denials and
completed URLs remain terminal; this command never changes the evidence plan.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from sources.property.developer_domain_audit import (
    DEFAULT_OUTPUT, _write_csv, page_signals,
)
from sources.property.developer_harvest import (
    BLOCK_STATUSES, CHALLENGE_TITLE, canonical_json, curl_fetch, digest,
    atomic_bytes, utc_now,
)


def _blocked(result) -> bool:
    return result.status in BLOCK_STATUSES or (
        result.status == 200 and bool(CHALLENGE_TITLE.search(
            result.body[:100_000].decode("utf-8", "replace"))))


def run_parallel(output: Path, *, workers: int = 4, max_hosts: int = 500,
                 delay: float = 1.0) -> dict:
    if not 1 <= workers <= 4 or not 0 <= max_hosts <= 500 or delay < 1:
        raise ValueError("invalid bounded worker settings")
    with (output / "audit.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_bytes = (output / "plan.json").read_bytes()
        plan = json.loads(plan_bytes)
        if plan.get("schema_version") != "developer-domain-entry-plan/1":
            raise ValueError("invalid frozen evidence plan")
        ledger = output / "requests.jsonl"
        old = ([json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
               if ledger.exists() else [])
        events: dict[str, list[dict]] = defaultdict(list)
        for event in old:
            events[event["host"]].append(event)
        attempted = {(event["host"], event["url"]) for event in old}
        tasks = []
        for item in plan["hosts"]:
            host = item["host"]
            if item["status"] != "pending" or any(e.get("blocked") for e in events[host]):
                continue
            urls = [url for url in item["urls"] if (host, url) not in attempted]
            if urls:
                tasks.append((host, urls))
            if len(tasks) >= max_hosts:
                break

        messages: queue.Queue = queue.Queue(maxsize=workers)

        def work(host: str, urls: list[str]) -> None:
            for index, url in enumerate(urls):
                hostname = (urlsplit(url).hostname or "").lower()
                result = curl_fetch(url, {hostname}, {}, 20)
                blocked = _blocked(result)
                ack = threading.Event()
                messages.put((host, url, result, blocked, ack))
                ack.wait()
                if blocked:
                    break
                if index + 1 < len(urls):
                    time.sleep(delay)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, host, urls) for host, urls in tasks]
            while not all(future.done() for future in futures) or not messages.empty():
                try:
                    host, url, result, blocked, ack = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    error = result.error or (
                        "challenge_interstitial" if blocked and result.status == 200 else None)
                    event = {"at": utc_now(), "host": host, "url": url,
                             "final_url": result.final_url, "status": result.status,
                             "error": error, "blocked": blocked,
                             "body_bytes": len(result.body), "body_sha256": None,
                             "content_type": result.headers.get("Content-Type") or
                                             result.headers.get("content-type"),
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
                    events[host].append(event)
                    attempted.add((host, url))
                finally:
                    ack.set()
                    messages.task_done()
            for future in futures:
                future.result()

        _write_csv(output / "domains.csv", plan, events)
        all_events = [event for group in events.values() for event in group]
        remaining = sum(item["status"] == "pending" and
            not any(e.get("blocked") for e in events[item["host"]]) and
            any((item["host"], url) not in attempted for url in item["urls"])
            for item in plan["hosts"])
        summary = {"schema_version": "developer-domain-entry-audit/1",
                   "created_at": utc_now(), "plan_sha256": digest(plan_bytes),
                   "evidence_rows": plan["evidence_rows"],
                   "supported_rows": plan["supported_rows"],
                   "supported_hosts": len(plan["hosts"]),
                   "prior_401_hosts": sorted(item["host"] for item in plan["hosts"]
                                             if item["status"] == "prior_401_stop"),
                   "already_harvested_hosts": sorted(item["host"] for item in plan["hosts"]
                                                     if item["status"] == "already_harvested"),
                   "requests": len(all_events),
                   "http_200": sum(e["status"] == 200 and not e["error"] for e in all_events),
                   "blocked_hosts": sorted({e["host"] for e in all_events if e["blocked"]}),
                   "transport_errors": sum(e["status"] is None for e in all_events),
                   "hosts_with_unattempted_urls": remaining,
                   "all_moscow_coverage_claimed": False,
                   "publication_allowed": False,
                   "request_ledger_sha256": digest(ledger.read_bytes()) if ledger.exists() else None,
                   "domains_csv_sha256": digest((output / "domains.csv").read_bytes())}
        atomic_bytes(output / "summary.json", canonical_json(summary) + b"\n")
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-hosts", type=int, default=500)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    args = parser.parse_args()
    print(json.dumps(run_parallel(args.output, workers=args.workers,
                                  max_hosts=args.max_hosts, delay=args.delay_seconds),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
