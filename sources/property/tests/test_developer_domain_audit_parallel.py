"""Offline concurrency and resume gates for entry-page audit workers."""

import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlsplit

from sources.property.developer_domain_audit import build_plan
from sources.property.developer_domain_audit_parallel import run_parallel
from sources.property.developer_harvest import FetchResult, atomic_bytes, canonical_json


class ParallelDomainAuditTests(TestCase):
    def test_workers_overlap_hosts_but_not_pages_and_resume(self) -> None:
        rows = []
        for host in ("one.example.org", "two.example.org"):
            rows.append({"supported": True, "evidence_id": host, "label": host,
                         "official_domain_url": f"https://{host}/",
                         "project_or_catalog_entry_url": f"https://{host}/flats/"})
        plan = build_plan(rows)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        calls: list[tuple[str, str]] = []

        def fetch(url, _hosts, _headers, _timeout):
            host = url.split("/")[2]
            with lock:
                calls.append((host, url))
            if urlsplit(url).path == "/":
                barrier.wait(timeout=2)
            if host == "one.example.org":
                return FetchResult(403, b"", {}, url, "http_403")
            return FetchResult(200, b"<p>public catalog</p>",
                               {"Content-Type": "text/html"}, url)

        with TemporaryDirectory() as temp:
            output = Path(temp)
            atomic_bytes(output / "plan.json", canonical_json(plan) + b"\n")
            with patch("sources.property.developer_domain_audit_parallel.curl_fetch",
                       side_effect=fetch), \
                    patch("sources.property.developer_domain_audit_parallel.time.sleep"):
                first = run_parallel(output, workers=2)
                second = run_parallel(output, workers=2)
            self.assertEqual(first["requests"], 3)
            self.assertEqual(second["requests"], 3)
            self.assertEqual(first["blocked_hosts"], ["one.example.org"])
            self.assertEqual(len(calls), 3)
            events = [json.loads(line) for line in
                      (output / "requests.jsonl").read_text().splitlines()]
            self.assertEqual(len([e for e in events if e["host"] == "one.example.org"]), 1)
            self.assertEqual(len([e for e in events if e["host"] == "two.example.org"]), 2)
            sha = next(event["body_sha256"] for event in events if event["body_sha256"])
            self.assertTrue((output / "raw" / (sha + ".body")).exists())
