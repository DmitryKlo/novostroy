"""Bounded, resumable private harvest of confirmed public developer pages.

Parsers are pure provider plugins. This module never publishes offers, media,
or an asset pointer, and a reachable page does not clear reuse rights.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib
import io
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_ROOT = ROOT / ".runtime/research/developer-harvest"
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_PAGES_PER_ADDRESS = 5
USER_AGENT = "NovostroyResearch/1.0 (public-page provenance; no auth)"
PLUGIN_MODULES = {
    "a101": "sources.property.developers_a101",
    "fsk": "sources.property.developers_fsk",
    "granel": "sources.property.developers_granel",
    "brusnika": "sources.property.developers_brusnika",
    "proplanirovki": "sources.property.developers_proplanirovki",
    "level": "sources.property.developers_level",
}
RULES_VERSION = "developer-harvest/2"
CROSS_RUN_CACHE_AGE = timedelta(hours=24)
KIND_PRIORITY = {"catalog": 0, "project": 1, "listing": 2}
REQUIRED_RECORD_TYPES = {"project", "corpus", "unit"}
BLOCK_STATUSES = {401, 403, 429}
CHALLENGE_TITLE = re.compile(
    r"(?is)<title[^>]*>[^<]*(?:captcha|access denied|verify you are human|robot check)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_event(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def checked_url(url: str, allowed_hosts: set[str], base: str | None = None) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("URL is missing or too long")
    parsed = urlsplit(urljoin(base, url) if base else url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not hostname or hostname not in allowed_hosts
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or not parsed.path.startswith("/")):
        raise ValueError("URL is outside the approved HTTPS host scope")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP-literal URL is not a developer host")
    return urlunsplit(("https", hostname, parsed.path or "/", parsed.query, ""))


def load_seed_file(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    config = json.loads(raw)
    if config.get("schema_version") != "developer-harvest-seeds/1":
        raise ValueError("Unsupported seed schema")
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError("No approved providers configured")
    for provider, spec in providers.items():
        if provider not in PLUGIN_MODULES or not isinstance(spec, dict):
            raise ValueError("Unknown provider plugin")
        hosts = spec.get("allowed_hosts")
        seeds = spec.get("seeds")
        if (not isinstance(hosts, list) or not hosts or len(hosts) > 12
                or not all(isinstance(host, str) and re.fullmatch(r"[a-z0-9.-]+", host)
                           for host in hosts)
                or not isinstance(seeds, list) or not seeds or len(seeds) > 100):
            raise ValueError("Provider host or seed scope is invalid")
        if len(set(hosts)) != len(hosts):
            raise ValueError("Duplicate approved hosts")
        for seed in seeds:
            if not isinstance(seed, dict) or seed.get("page_kind") not in KIND_PRIORITY:
                raise ValueError("Invalid seed page kind")
            checked_url(seed.get("url"), set(hosts))
    return config, digest(canonical_json(config))


def plugin_hashes(providers: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for provider in providers:
        module = importlib.import_module(PLUGIN_MODULES[provider])
        values[provider] = digest(Path(module.__file__).read_bytes())
    return values


def plugin_functions(provider: str) -> tuple[Callable[..., list[dict]], Callable[..., list[dict]]]:
    module = importlib.import_module(PLUGIN_MODULES[provider])
    return (getattr(module, f"discover_{provider}_pages"),
            getattr(module, f"parse_{provider}_page"))


def run_identity(config_sha: str, parser_shas: Mapping[str, str]) -> str:
    return "developer-harvest-" + digest(canonical_json({
        "config_sha256": config_sha, "parser_shas": parser_shas,
        "rules": RULES_VERSION,
    }))[:16]


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    body: bytes
    headers: Mapping[str, str]
    final_url: str
    error: str | None = None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def public_fetch(url: str, allowed_hosts: set[str], headers: Mapping[str, str],
                 timeout: float = 12) -> FetchResult:
    opener = urllib.request.build_opener(NoRedirect())
    current = checked_url(url, allowed_hosts)
    for _ in range(3):
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT,
                                                            "Accept": "text/html,application/xhtml+xml",
                                                            **headers})
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    return FetchResult(exc.code, b"", dict(exc.headers), current, "redirect_without_location")
                try:
                    current = checked_url(location, allowed_hosts, current)
                except ValueError:
                    return FetchResult(exc.code, b"", dict(exc.headers), current,
                                       "redirect_outside_scope")
                continue
            return FetchResult(exc.code, b"", dict(exc.headers), current,
                               f"http_{exc.code}" if exc.code != 304 else None)
        except (OSError, TimeoutError) as exc:
            return FetchResult(None, b"", {}, current, type(exc).__name__)
        with response:
            content_type = response.headers.get("Content-Type", "").lower()
            if not any(token in content_type for token in ("html", "json")) and response.status != 304:
                return FetchResult(response.status, b"", dict(response.headers), current,
                                   "unexpected_content_type")
            body = response.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                return FetchResult(response.status, b"", dict(response.headers), current,
                                   "body_too_large")
            return FetchResult(response.status, body, dict(response.headers), current)
    return FetchResult(None, b"", {}, current, "redirect_limit")


def curl_fetch(url: str, allowed_hosts: set[str], headers: Mapping[str, str],
               timeout: float = 20) -> FetchResult:
    """Ordinary HTTPS GET through curl; redirects remain inside the exact host allowlist."""
    current = checked_url(url, allowed_hosts)
    with tempfile.TemporaryDirectory(prefix="novostroy-harvest-") as temporary:
        root = Path(temporary)
        body_path, header_path = root / "body", root / "headers"
        for redirect in range(3):
            request_headers = ["Accept: text/html,application/xhtml+xml,application/json"]
            for name in ("If-None-Match", "If-Modified-Since"):
                value = headers.get(name)
                if value and len(value) <= 200 and "\r" not in value and "\n" not in value:
                    request_headers.append(f"{name}: {value}")
            command = ["/usr/bin/curl", "--silent", "--show-error", "--proto", "=https",
                       "--connect-timeout", "8", "--max-time", str(int(timeout)),
                       "--max-filesize", str(MAX_HTML_BYTES), "--max-redirs", "0",
                       "--user-agent", USER_AGENT, "--dump-header", str(header_path),
                       "--output", str(body_path), "--write-out", "%{http_code}"]
            for item in request_headers:
                command.extend(("--header", item))
            command.extend(("--", current))
            try:
                result = subprocess.run(command, capture_output=True, check=False,
                                        timeout=timeout + 2)
            except subprocess.TimeoutExpired:
                return FetchResult(None, b"", {}, current, "curl_timeout")
            except OSError:
                return FetchResult(None, b"", {}, current, "curl_unavailable")
            if result.returncode:
                if result.returncode == 63:
                    return FetchResult(None, b"", {}, current, "body_too_large")
                return FetchResult(None, b"", {}, current, f"curl_exit_{result.returncode}")
            try:
                status = int(result.stdout.decode("ascii").strip())
            except (ValueError, UnicodeDecodeError):
                return FetchResult(None, b"", {}, current, "invalid_curl_status")
            parsed_headers: dict[str, str] = {}
            for line in header_path.read_text(encoding="iso-8859-1").splitlines():
                if ":" in line:
                    name, value = line.split(":", 1)
                    parsed_headers[name.strip()] = value.strip()
            if status in {301, 302, 303, 307, 308}:
                location = next((value for key, value in parsed_headers.items()
                                 if key.lower() == "location"), None)
                if not location:
                    return FetchResult(status, b"", parsed_headers, current,
                                       "redirect_without_location")
                try:
                    next_url = checked_url(location, allowed_hosts, current)
                except ValueError:
                    return FetchResult(status, b"", parsed_headers, current,
                                       "redirect_outside_scope")
                if redirect == 2:
                    break
                time.sleep(1.0)
                current = next_url
                continue
            if status == 304:
                return FetchResult(status, b"", parsed_headers, current)
            if status != 200:
                return FetchResult(status, b"", parsed_headers, current, f"http_{status}")
            content_type = next((value.lower() for key, value in parsed_headers.items()
                                 if key.lower() == "content-type"), "")
            if not any(token in content_type for token in ("html", "json")):
                return FetchResult(status, b"", parsed_headers, current,
                                   "unexpected_content_type")
            if not body_path.is_file() or body_path.stat().st_size > MAX_HTML_BYTES:
                return FetchResult(status, b"", parsed_headers, current, "body_too_large")
            return FetchResult(status, body_path.read_bytes(), parsed_headers, current)
    return FetchResult(None, b"", {}, current, "redirect_limit")


class Harvest:
    def __init__(self, seed_file: Path, state_root: Path = DEFAULT_STATE_ROOT,
                 fetch: Callable[[str, set[str], Mapping[str, str]], FetchResult] = public_fetch,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config, self.config_sha = load_seed_file(seed_file)
        self.state_root = state_root
        self.parser_shas = plugin_hashes(self.config["providers"])
        self.run_id = run_identity(self.config_sha, self.parser_shas)
        self.directory = state_root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cache = self.directory / "cache"
        self.cache.mkdir(exist_ok=True)
        self.fetch = fetch
        self.sleep = sleep
        self.clock = clock
        self.last_request: dict[str, float] = {}
        self.database = sqlite3.connect(self.directory / "state.sqlite3")
        self.database.row_factory = sqlite3.Row
        self.database.execute("PRAGMA journal_mode=WAL")
        self.database.execute("PRAGMA synchronous=FULL")
        self._create_tables()
        self._seed()
        self._repair_cached_catalog_discovery()
        self.reusable_captures = self._load_reusable_captures()
        self.last_run_stats = {"network_attempts": 0, "network_successes": 0,
                               "http_responses": 0, "reused_pages": 0}

    def close(self) -> None:
        self.database.close()

    def _create_tables(self) -> None:
        self.database.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                provider TEXT NOT NULL, url TEXT NOT NULL, page_kind TEXT NOT NULL,
                parent_url TEXT, depth INTEGER NOT NULL, address_hint TEXT,
                state TEXT NOT NULL DEFAULT 'pending', status_code INTEGER,
                content_sha256 TEXT, observed_at TEXT, etag TEXT, last_modified TEXT,
                error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, url)
            );
            CREATE TABLE IF NOT EXISTS hosts (
                hostname TEXT PRIMARY KEY, blocked_reason TEXT NOT NULL,
                blocked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                record_type TEXT NOT NULL, publisher_unit_id TEXT,
                page_kind TEXT NOT NULL, source_url TEXT NOT NULL,
                payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observation_versions (
                observation_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                source_page_url TEXT NOT NULL, observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (observation_id, payload_sha256, source_page_url)
            );
        """)

    def _hosts(self, provider: str) -> set[str]:
        return set(self.config["providers"][provider]["allowed_hosts"])

    def _enqueue(self, provider: str, ref: Mapping[str, Any], parent_url: str | None,
                 depth: int) -> bool:
        kind = ref.get("page_kind")
        if kind not in KIND_PRIORITY or depth > 6:
            return False
        url = checked_url(ref.get("url"), self._hosts(provider), parent_url)
        hint = ref.get("address_hint")
        if hint is not None and (not isinstance(hint, str) or len(hint) > 256):
            return False
        if hint and kind != "catalog":
            count = self.database.execute(
                "SELECT COUNT(*) FROM pages WHERE provider=? AND address_hint=? AND page_kind!='catalog'",
                (provider, hint)).fetchone()[0]
            if count >= MAX_PAGES_PER_ADDRESS:
                return False
        cursor = self.database.execute(
            "INSERT OR IGNORE INTO pages(provider,url,page_kind,parent_url,depth,address_hint) "
            "VALUES(?,?,?,?,?,?)", (provider, url, kind, parent_url, depth, hint))
        return cursor.rowcount > 0

    def _seed(self) -> None:
        with self.database:
            for provider, spec in self.config["providers"].items():
                for seed in spec["seeds"]:
                    self._enqueue(provider, seed, None, 0)

    def _repair_cached_catalog_discovery(self) -> None:
        """Resume next links previously lost to an older pagination depth limit."""
        with self.database:
            self.database.execute("UPDATE pages SET depth=0 WHERE page_kind='catalog' AND depth!=0")
        pages = self.database.execute(
            "SELECT * FROM pages WHERE page_kind='catalog' AND state='fetched' "
            "AND content_sha256 IS NOT NULL"
        ).fetchall()
        with self.database:
            for page in pages:
                cached = self.cache / f"{page['content_sha256']}.body"
                if cached.is_symlink() or not cached.is_file():
                    continue
                raw = cached.read_bytes()
                if digest(raw) != page["content_sha256"]:
                    continue
                discover, _ = plugin_functions(page["provider"])
                for ref in discover(raw.decode("utf-8", "replace"), page["url"]):
                    if isinstance(ref, dict):
                        try:
                            self._enqueue(page["provider"], ref, page["url"],
                                          self._reference_depth(page, ref))
                        except ValueError:
                            continue
            self.database.execute(
                "UPDATE pages SET depth=1 WHERE page_kind IN ('project','listing') "
                "AND depth!=1 AND parent_url IN "
                "(SELECT url FROM pages WHERE page_kind='catalog')"
            )

    @staticmethod
    def _reference_depth(page: sqlite3.Row, ref: Mapping[str, Any]) -> int:
        # All pages in a source-linked catalog are one logical root. Projects
        # and listed units are direct children regardless of catalog page N.
        if page["page_kind"] == "catalog":
            return 0 if ref.get("page_kind") == "catalog" else 1
        return page["depth"] + 1

    def _load_reusable_captures(self) -> dict[tuple[str, str], tuple[Path, str, str]]:
        """Index recent immutable bodies from earlier parser revisions without refetching."""
        reusable: dict[tuple[str, str], tuple[Path, str, str]] = {}
        now = datetime.now(timezone.utc)
        for directory in self.state_root.glob("developer-harvest-*"):
            database_path = directory / "state.sqlite3"
            if directory == self.directory or directory.is_symlink() or not database_path.is_file():
                continue
            try:
                other = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
                try:
                    rows = other.execute(
                        "SELECT provider,url,content_sha256,observed_at FROM pages "
                        "WHERE state='fetched' AND content_sha256 IS NOT NULL"
                    ).fetchall()
                    blocked = other.execute(
                        "SELECT hostname,blocked_reason,blocked_at FROM hosts"
                    ).fetchall()
                finally:
                    other.close()
            except sqlite3.Error:
                continue
            approved_hosts = {host for spec in self.config["providers"].values()
                              for host in spec["allowed_hosts"]}
            with self.database:
                for hostname, reason, blocked_at in blocked:
                    if hostname not in approved_hosts:
                        continue
                    try:
                        when = datetime.fromisoformat(blocked_at.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    age = now - when.astimezone(timezone.utc)
                    if timedelta() <= age <= CROSS_RUN_CACHE_AGE:
                        self.database.execute(
                            "INSERT OR IGNORE INTO hosts VALUES(?,?,?)",
                            (hostname, reason, blocked_at))
            for provider, url, content_sha, observed_at in rows:
                if provider not in self.config["providers"]:
                    continue
                try:
                    checked_url(url, self._hosts(provider))
                    captured_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                age = now - captured_at.astimezone(timezone.utc)
                if age < -timedelta(hours=1) or age > CROSS_RUN_CACHE_AGE:
                    continue
                body_path = directory / "cache" / f"{content_sha}.body"
                if (body_path.is_symlink() or not body_path.is_file()
                        or body_path.stat().st_size > MAX_HTML_BYTES):
                    continue
                key = (provider, url)
                if key not in reusable or observed_at > reusable[key][2]:
                    reusable[key] = (body_path, content_sha, observed_at)
        return reusable

    def _reuse_capture(self, page: sqlite3.Row) -> bool:
        candidate = self.reusable_captures.get((page["provider"], page["url"]))
        if not candidate:
            return False
        body_path, content_sha, observed_at = candidate
        raw = body_path.read_bytes()
        if digest(raw) != content_sha:
            return False
        self._store_success(page, raw, observed_at, 200, {}, "reused_verified_capture",
                            page["url"])
        return True

    def _block_host(self, url: str, reason: str) -> None:
        host = urlsplit(url).hostname
        self.database.execute(
            "INSERT OR REPLACE INTO hosts(hostname,blocked_reason,blocked_at) VALUES(?,?,?)",
            (host, reason, utc_now()))

    def _pace(self, url: str, seconds: float) -> None:
        host = urlsplit(url).hostname or ""
        previous = self.last_request.get(host)
        if previous is None:
            self.sleep(seconds)
        else:
            wait = seconds - (self.clock() - previous)
            if wait > 0:
                self.sleep(wait)
        self.last_request[host] = self.clock()

    def _observation_id(self, provider: str, row: Mapping[str, Any],
                        page_url: str, ordinal: int) -> tuple[str, str]:
        record_type = row["record_type"]
        if record_type == "unit" and row.get("publisher_unit_id"):
            identity = f"unit:{row['publisher_unit_id']}"
            quality = "publisher_asserted"
        elif record_type == "project" and row.get("publisher_project_id"):
            identity = f"project:{row['publisher_project_id']}"
            quality = "publisher_asserted"
        elif record_type == "corpus" and row.get("publisher_building_id"):
            identity = f"corpus:{row['publisher_building_id']}"
            quality = "publisher_asserted"
        else:
            identity = f"page:{page_url}:{record_type}:{ordinal}"
            quality = "page_local"
        return "devobs_" + digest(f"{provider}|{identity}".encode())[:24], quality

    def _normalize(self, provider: str, page: sqlite3.Row, row: Mapping[str, Any],
                   ordinal: int, observed_at: str, source_sha: str) -> dict[str, Any]:
        if not isinstance(row, Mapping) or row.get("record_type") not in REQUIRED_RECORD_TYPES:
            raise ValueError("Parser record_type is missing or unsupported")
        page_url = page["url"]
        source_url = checked_url(row.get("source_url") or page_url, self._hosts(provider), page_url)
        observation_id, identity_quality = self._observation_id(provider, row, page_url, ordinal)
        source_fields = row.get("source_fields") or {}
        if not isinstance(source_fields, dict):
            raise ValueError("Parser source_fields must be an object")
        scope = source_fields.get("location_scope", "unknown")
        if scope not in {"in_scope_moscow", "out_of_scope", "unknown"}:
            scope = "unknown"
        media = row.get("media") or []
        if not isinstance(media, list) or len(media) > 50:
            raise ValueError("Parser media exceeds per-record cap")
        normalized_media = []
        for item in media:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            parsed = urlsplit(item["url"])
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                continue
            normalized_media.append({**item, "rights_status": "unknown",
                                     "public_display_allowed": False})
        value = {**row, "provider": provider, "source_group": provider,
                 "observation_id": observation_id, "identity_quality": identity_quality,
                 "source_url": source_url, "publisher_domain": urlsplit(source_url).hostname,
                 "source_page_url": page_url, "page_kind": page["page_kind"],
                 "observed_at": observed_at, "raw_sha256": source_sha,
                 "source_updated_at": row.get("source_updated_at"),
                 "source_fields": {**source_fields, "location_scope": scope},
                 "media": normalized_media, "rights_status": "unknown",
                 "public_display_allowed": False, "identity_conflict": False}
        if len(canonical_json(value)) > 128_000:
            raise ValueError("Parser observation exceeds size cap")
        return value

    def _save_observation(self, value: dict[str, Any]) -> None:
        observation_id = value["observation_id"]
        previous = self.database.execute(
            "SELECT payload_json,page_kind,publisher_unit_id,first_seen FROM observations "
            "WHERE observation_id=?", (observation_id,)).fetchone()
        if previous and value["record_type"] == "unit" and value.get("publisher_unit_id"):
            old = json.loads(previous["payload_json"])
            old_project = old.get("publisher_project_id")
            new_project = value.get("publisher_project_id")
            if old_project and new_project and old_project != new_project:
                conflicting_ids = sorted({old_project, new_project, *
                                          old.get("source_fields", {}).get("conflicting_project_ids", [])})
                value["identity_conflict"] = True
                value["source_fields"]["conflicting_project_ids"] = conflicting_ids
                # Keep the selected higher-fidelity record flagged even when a
                # lower-fidelity catalog page reveals the contradiction.
                if KIND_PRIORITY[previous["page_kind"]] > KIND_PRIORITY[value["page_kind"]]:
                    old["identity_conflict"] = True
                    old["source_fields"]["conflicting_project_ids"] = conflicting_ids
                    old_sha = digest(canonical_json({key: val for key, val in old.items()
                                                     if key not in {"observed_at", "raw_sha256"}}))
                    self.database.execute(
                        "UPDATE observations SET payload_json=?,payload_sha256=? WHERE observation_id=?",
                        (canonical_json(old).decode("utf-8"), old_sha, observation_id))
        serialized = canonical_json(value).decode("utf-8")
        content_sha = digest(canonical_json({key: val for key, val in value.items()
                                             if key not in {"observed_at", "raw_sha256"}}))
        self.database.execute(
            "INSERT OR IGNORE INTO observation_versions VALUES(?,?,?,?,?)",
            (observation_id, content_sha, value["source_page_url"], value["observed_at"], serialized))
        old_rank = KIND_PRIORITY[previous["page_kind"]] if previous else -1
        new_rank = KIND_PRIORITY[value["page_kind"]]
        if previous is None or new_rank >= old_rank:
            self.database.execute(
                "INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (observation_id, value["provider"], value["record_type"],
                 value.get("publisher_unit_id"), value["page_kind"], value["source_url"],
                 serialized, content_sha, previous["first_seen"] if previous else value["observed_at"],
                 value["observed_at"]))
        else:
            self.database.execute("UPDATE observations SET last_seen=? WHERE observation_id=?",
                                  (value["observed_at"], observation_id))

    def _parse_cached_page(self, page: sqlite3.Row, raw: bytes, observed_at: str,
                           source_sha: str) -> int:
        provider = page["provider"]
        discover, parse = plugin_functions(provider)
        text = raw.decode("utf-8", errors="replace")
        rows = parse(text, page["url"], observed_at, source_sha)
        references = discover(text, page["url"])
        if not isinstance(rows, list) or not isinstance(references, list):
            raise ValueError("Parser must return lists")
        if len(rows) > 5000 or len(references) > 5000:
            raise ValueError("Parser output exceeds per-page cap")
        if any(isinstance(row, dict) and (row.get("source_fields") or {}).get(
                "unit_records_truncated") for row in rows):
            raise ValueError("Parser truncated a source apartment array")
        with self.database:
            for ordinal, row in enumerate(rows):
                self._save_observation(self._normalize(provider, page, row, ordinal,
                                                      observed_at, source_sha))
            for ref in references:
                if isinstance(ref, dict):
                    try:
                        self._enqueue(provider, ref, page["url"],
                                      self._reference_depth(page, ref))
                    except ValueError:
                        append_event(self.directory / "errors.jsonl", {
                            "at": utc_now(), "provider": provider, "url": page["url"],
                            "error_code": "discovered_url_outside_scope"})
        return len(rows)

    def _store_success(self, page: sqlite3.Row, raw: bytes, observed_at: str,
                       status_code: int, headers: Mapping[str, str],
                       transport: str, final_url: str) -> None:
        source_sha = digest(raw)
        cached = self.cache / f"{source_sha}.body"
        if not cached.exists():
            atomic_bytes(cached, raw)
        if CHALLENGE_TITLE.search(raw[:100_000].decode("utf-8", "replace")):
            with self.database:
                self._block_host(page["url"], "challenge_interstitial")
                self.database.execute(
                    "UPDATE pages SET state='blocked', status_code=?, error_code='challenge_interstitial', "
                    "attempts=attempts+1 WHERE provider=? AND url=?",
                    (status_code, page["provider"], page["url"]))
            append_event(self.directory / "errors.jsonl", {
                "at": observed_at, "provider": page["provider"], "url": page["url"],
                "error_code": "challenge_interstitial"})
            return
        try:
            row_count = self._parse_cached_page(page, raw, observed_at, source_sha)
        except Exception as exc:
            with self.database:
                self.database.execute(
                    "UPDATE pages SET state='parse_error',status_code=?,content_sha256=?,"
                    "observed_at=?,error_code=?,attempts=attempts+1 WHERE provider=? AND url=?",
                    (status_code, source_sha, observed_at, type(exc).__name__,
                     page["provider"], page["url"]))
            append_event(self.directory / "errors.jsonl", {
                "at": observed_at, "provider": page["provider"], "url": page["url"],
                "error_code": "parse_error", "error_type": type(exc).__name__,
                "raw_sha256": source_sha})
            return
        with self.database:
            self.database.execute(
                "UPDATE pages SET state='fetched',status_code=?,content_sha256=?,"
                "observed_at=?,etag=?,last_modified=?,error_code=NULL,attempts=attempts+1 "
                "WHERE provider=? AND url=?",
                (status_code, source_sha, observed_at, headers.get("ETag"),
                 headers.get("Last-Modified"), page["provider"], page["url"]))
        append_event(self.directory / "requests.jsonl", {
            "at": observed_at, "transport": transport, "provider": page["provider"],
            "url": page["url"], "final_url": final_url, "status": status_code,
            "bytes": len(raw), "sha256": source_sha, "parsed_records": row_count})

    def ingest_captures(self, capture_manifest: Path) -> int:
        captures = json.loads(capture_manifest.read_text(encoding="utf-8"))
        if (captures.get("schema_version") != "developer-harvest-captures/1"
                or not isinstance(captures.get("captures"), list)
                or len(captures["captures"]) > 1000):
            raise ValueError("Invalid local capture manifest")
        ingested = 0
        for capture in captures["captures"]:
            provider = capture["provider"]
            if provider not in self.config["providers"]:
                raise ValueError("Capture provider is outside seed configuration")
            ref = {"url": capture["url"], "page_kind": capture["page_kind"]}
            with self.database:
                self._enqueue(provider, ref, None, 0)
            url = checked_url(capture["url"], self._hosts(provider))
            page = self.database.execute(
                "SELECT * FROM pages WHERE provider=? AND url=?", (provider, url)).fetchone()
            observed_at = capture["observed_at"]
            when = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("Capture observed_at requires timezone")
            local_entry = capture_manifest.parent / capture["path"]
            local = local_entry.resolve(strict=True)
            if (local_entry.is_symlink() or not local.is_file()
                    or local.stat().st_size > MAX_HTML_BYTES):
                raise ValueError("Capture is not a bounded regular file")
            raw = local.read_bytes()
            expected = capture.get("sha256")
            if expected and digest(raw) != expected:
                raise ValueError("Local capture SHA mismatch")
            if page["state"] == "fetched" and page["content_sha256"] == digest(raw):
                continue
            self._store_success(page, raw, observed_at, 200, {}, "local_capture", url)
            ingested += 1
        self.export()
        return ingested

    def _fetch_one(self, page: sqlite3.Row, delay_seconds: float) -> None:
        provider = page["provider"]
        url = page["url"]
        self._pace(url, delay_seconds)
        headers = {}
        if page["etag"]:
            headers["If-None-Match"] = page["etag"]
        if page["last_modified"]:
            headers["If-Modified-Since"] = page["last_modified"]
        result = self.fetch(url, self._hosts(provider), headers)
        observed_at = utc_now()
        if result.status == 304 and page["content_sha256"]:
            cached = self.cache / f"{page['content_sha256']}.body"
            if cached.is_file():
                self._store_success(page, cached.read_bytes(), observed_at, 304,
                                    result.headers, "conditional_cache", result.final_url)
                return
        if result.status == 200 and not result.error:
            self._store_success(page, result.body, observed_at, 200,
                                result.headers, "public_https", result.final_url)
            return
        error = result.error or f"http_{result.status or 'network'}"
        with self.database:
            if result.status in BLOCK_STATUSES:
                self._block_host(url, error)
            self.database.execute(
                "UPDATE pages SET state=?, status_code=?,error_code=?,attempts=attempts+1 "
                "WHERE provider=? AND url=?",
                ("blocked" if result.status in BLOCK_STATUSES else "error",
                 result.status, error, provider, url))
        append_event(self.directory / "requests.jsonl", {
            "at": observed_at, "transport": "public_https", "provider": provider,
            "url": url, "final_url": result.final_url, "status": result.status,
            "bytes": 0, "error_code": error})
        append_event(self.directory / "errors.jsonl", {
            "at": observed_at, "provider": provider, "url": url,
            "status": result.status, "error_code": error})

    def run(self, max_pages: int = 100, delay_seconds: float = 1.0,
            retry_errors: bool = False, include_listings: bool = False) -> int:
        if not 1 <= max_pages <= 1000 or not 1.0 <= delay_seconds <= 60:
            raise ValueError("Run bounds must preserve one request per host per second")
        if retry_errors:
            with self.database:
                self.database.execute(
                    "UPDATE pages SET state='pending' WHERE state='error' AND attempts<3 "
                    "AND (error_code IN ('URLError','TimeoutError','OSError') "
                    "OR error_code='curl_timeout' OR error_code LIKE 'curl_exit_%')")
        completed = 0
        attempts = successes = http_responses = reused = 0
        while completed < max_pages:
            page = self.database.execute("""
                SELECT p.* FROM pages p LEFT JOIN hosts h
                  ON h.hostname=lower(substr(p.url,9,instr(substr(p.url,9),'/')-1))
                WHERE p.state='pending' AND h.hostname IS NULL
                  AND (? OR p.page_kind!='listing')
                ORDER BY CASE p.page_kind WHEN 'catalog' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,
                         p.depth,p.provider,p.url LIMIT 1
            """, (int(include_listings),)).fetchone()
            if page is None:
                break
            if self._reuse_capture(page):
                reused += 1
            else:
                self._fetch_one(page, delay_seconds)
                attempts += 1
                state = self.database.execute(
                    "SELECT state,status_code FROM pages WHERE provider=? AND url=?",
                    (page["provider"], page["url"])).fetchone()
                successes += state["state"] == "fetched"
                http_responses += state["status_code"] is not None
            completed += 1
            if completed % 10 == 0:
                self.export()
        self.export()
        self.last_run_stats = {"network_attempts": attempts, "network_successes": successes,
                               "http_responses": http_responses, "reused_pages": reused}
        return attempts

    def run_cached_only(self, max_pages: int = 1000) -> int:
        """Replay verified bodies from prior parser revisions without any network call."""
        if not 1 <= max_pages <= 1000:
            raise ValueError("Offline replay page cap must be between 1 and 1000")
        reused = 0
        while reused < max_pages:
            pending = self.database.execute(
                "SELECT * FROM pages WHERE state='pending' ORDER BY "
                "CASE page_kind WHEN 'catalog' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,"
                "depth,provider,url"
            ).fetchall()
            replayed = False
            for page in pending:
                if (page["provider"], page["url"]) not in self.reusable_captures:
                    continue
                if self._reuse_capture(page):
                    reused += 1
                    replayed = True
                    break
            if not replayed:
                break
        self.export()
        self.last_run_stats = {"network_attempts": 0, "network_successes": 0,
                               "http_responses": 0, "reused_pages": reused}
        return reused

    def _all_observations(self) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.database.execute(
            "SELECT payload_json FROM observations ORDER BY provider,observation_id")]

    @staticmethod
    def _unit_projection(row: Mapping[str, Any],
                         project_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        fields = row.get("source_fields") or {}
        scope = fields.get("location_scope", "unknown")
        inherited = None
        address = row.get("address")
        if scope == "unknown" and project_evidence is not None:
            scope = "in_scope_moscow"
            inherited = project_evidence["observation_id"]
            address = address or project_evidence["address"]
        return {
            "observation_id": row["observation_id"],
            "source_group": row["provider"], "source_url": row["source_url"],
            "publisher_domain": row["publisher_domain"],
            "observed_at": row["observed_at"],
            "source_updated_at": row.get("source_updated_at"),
            "source_updated_at_basis": fields.get("source_updated_at_basis"),
            "raw_sha256": row["raw_sha256"],
            "project": {"publisher_project_id": row.get("publisher_project_id"),
                        "name": row.get("project_name"), "address": address},
            "building": {"publisher_building_id": row.get("publisher_building_id"),
                         "corpus": row.get("corpus"), "structure": row.get("structure"),
                         "full_address": row.get("address"),
                         "address_components": fields.get("address_components")},
            "unit": {"publisher_unit_id": row.get("publisher_unit_id"),
                     "number": row.get("unit_number"), "floor": row.get("floor"),
                     "area_m2": row.get("area_m2"), "rooms": row.get("rooms"),
                     "section": row.get("section")},
            "source_rights_status": "unknown",
            "identity_quality": row["identity_quality"],
            "identity_conflict": row.get("identity_conflict", False),
            "location_scope": scope,
            "location_scope_basis": ("same_publisher_project_id"
                                     if inherited else "unit_source_assertion"),
            "scope_evidence_observation_id": inherited,
            "price_rub": row.get("price_rub"), "price_basis": row.get("price_basis"),
            "promo_conditions": row.get("promo_conditions"),
            "media": row.get("media") or [],
            "public_display_allowed": False,
        }

    @staticmethod
    def _project_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        fields = row.get("source_fields") or {}
        return {
            "observation_id": row["observation_id"], "record_type": row["record_type"],
            "source_group": row["provider"], "source_url": row["source_url"],
            "publisher_domain": row["publisher_domain"],
            "observed_at": row["observed_at"],
            "source_updated_at": row.get("source_updated_at"),
            "source_updated_at_basis": fields.get("source_updated_at_basis"),
            "publisher_project_id": row.get("publisher_project_id"),
            "publisher_building_id": row.get("publisher_building_id"),
            "project_name": row.get("project_name"), "address": row.get("address"),
            "corpus": row.get("corpus"),
            "coordinates": row.get("coordinates"),
            "coordinate_scope": fields.get("project_coordinates_scope")
                                or fields.get("coordinate_scope"),
            "source_geometry": row.get("source_geometry"),
            "geometry_basis": (fields.get("geometry_basis")
                               if row.get("source_geometry") is not None else None),
            "price_from_rub": row.get("price_from_rub"),
            "price_basis": row.get("price_basis"),
            "media": row.get("media") or [],
            "public_display_allowed": False,
            "address_components": fields.get("address_components"),
            "location_scope": fields.get("location_scope", "unknown"),
            "identity_quality": row["identity_quality"],
            "source_rights_status": "unknown",
        }

    def _counts(self) -> dict[str, Any]:
        provider_counts = {}
        for provider, spec in self.config["providers"].items():
            states = {row[0]: row[1] for row in self.database.execute(
                "SELECT state,COUNT(*) FROM pages WHERE provider=? GROUP BY state", (provider,))}
            observations = self.database.execute(
                "SELECT record_type,COUNT(*) FROM observations WHERE provider=? GROUP BY record_type",
                (provider,)).fetchall()
            claimed = 0
            without_alterplans_claimed = 0
            projects_claimed = 0
            for raw, in self.database.execute(
                    "SELECT payload_json FROM observations WHERE provider=?", (provider,)):
                fields = json.loads(raw).get("source_fields") or {}
                total = fields.get("catalog_total_claimed")
                if isinstance(total, int) and not isinstance(total, bool):
                    claimed = max(claimed, total)
                without_alterplans = fields.get("catalog_count_without_alterplans")
                if isinstance(without_alterplans, int) and not isinstance(without_alterplans, bool):
                    without_alterplans_claimed = max(without_alterplans_claimed,
                                                     without_alterplans)
                project_total = fields.get("catalog_projects_total_claimed")
                if isinstance(project_total, int) and not isinstance(project_total, bool):
                    projects_claimed = max(projects_claimed, project_total)
            unit_count = next((count for kind, count in observations if kind == "unit"), 0)
            catalog_slots, catalog_distinct = self.database.execute(
                "SELECT COUNT(*),COUNT(DISTINCT v.observation_id) "
                "FROM observation_versions v JOIN pages p "
                "ON p.provider=? AND p.url=v.source_page_url AND p.page_kind='catalog' "
                "WHERE json_extract(v.payload_json,'$.provider')=? "
                "AND json_extract(v.payload_json,'$.record_type')='unit'",
                (provider, provider)).fetchone()
            blocked_hosts = [row[0] for row in self.database.execute(
                "SELECT hostname FROM hosts WHERE hostname IN (" +
                ",".join("?" for _ in spec["allowed_hosts"]) + ")",
                spec["allowed_hosts"]).fetchall()]
            incomplete = {kind: count for kind, count in self.database.execute(
                "SELECT page_kind,COUNT(*) FROM pages WHERE provider=? "
                "AND state IN ('pending','error','parse_error','blocked') GROUP BY page_kind",
                (provider,))}
            catalog_remaining = incomplete.get("catalog", 0)
            project_remaining = incomplete.get("project", 0)
            catalog_work = catalog_remaining + project_remaining
            project_pages = self.database.execute(
                "SELECT COUNT(*),SUM(CASE WHEN state='fetched' THEN 1 ELSE 0 END) "
                "FROM pages WHERE provider=? AND page_kind='project'",
                (provider,)).fetchone()
            project_page_total, project_page_fetched = project_pages[0], project_pages[1] or 0
            provider_counts[provider] = {
                "allowed_hosts": spec["allowed_hosts"],
                "seed_urls": [seed["url"] for seed in spec["seeds"]],
                "pages": states, "unit_observations": unit_count,
                "catalog_source_unit_slots": catalog_slots,
                "catalog_distinct_unit_ids": catalog_distinct,
                "catalog_duplicate_slots": catalog_slots - catalog_distinct,
                "catalog_slot_scope": "all_discovered_queries_including_overlapping_filters",
                "claimed_minus_unique_units": max(0, claimed - unit_count),
                "project_observations": sum(count for kind, count in observations
                                            if kind in {"project", "corpus"}),
                "catalog_total_claimed": claimed or None,
                "catalog_count_without_alterplans_claimed": (
                    without_alterplans_claimed or None),
                "catalog_projects_total_claimed": projects_claimed or None,
                "catalog_exhausted": (bool(claimed) and not blocked_hosts
                                      and catalog_remaining == 0
                                      and (catalog_slots == claimed
                                           and unit_count == without_alterplans_claimed
                                           if provider == "level" else unit_count >= claimed)),
                "project_pages_total": project_page_total,
                "project_pages_fetched": project_page_fetched,
                "project_inventory_exhausted": (bool(projects_claimed) and not blocked_hosts
                                                and project_remaining == 0
                                                and project_page_fetched >= projects_claimed),
                "catalog_pages_remaining": catalog_remaining,
                "project_pages_remaining": project_remaining,
                "catalog_work_remaining": catalog_work,
                "listing_detail_pending": self.database.execute(
                    "SELECT COUNT(*) FROM pages WHERE provider=? AND page_kind='listing' "
                    "AND state='pending'", (provider,)).fetchone()[0],
                "blocked_hosts": blocked_hosts,
            }
        return provider_counts

    def export(self) -> dict[str, Any]:
        observations = self._all_observations()
        project_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in observations:
            project_id = row.get("publisher_project_id")
            if (row["record_type"] == "project" and project_id
                    and row["identity_quality"] == "publisher_asserted"):
                project_candidates.setdefault((row["provider"], str(project_id)), []).append(row)
        project_evidence: dict[tuple[str, str], dict[str, Any]] = {}
        for key, rows in project_candidates.items():
            addresses = {row.get("address") for row in rows}
            scopes = {row["source_fields"].get("location_scope") for row in rows}
            if scopes == {"in_scope_moscow"} and len(addresses) == 1 and None not in addresses:
                project_evidence[key] = rows[0]
        projected_units = [self._unit_projection(
            row, project_evidence.get((row["provider"], str(row.get("publisher_project_id")))))
            for row in observations if row["record_type"] == "unit"]
        units = [row for row in projected_units if row["location_scope"] == "in_scope_moscow"]
        projects = [self._project_projection(row) for row in observations
                    if row["record_type"] in {"project", "corpus"}]
        files = {}
        for key, name, rows in (
                ("observations", "observations.jsonl", observations),
                ("developer_units", "developer_units.jsonl", units),
                ("developer_projects", "developer_projects.jsonl", projects)):
            content = b"".join(canonical_json(row) + b"\n" for row in rows)
            atomic_bytes(self.directory / name, content)
            files[key] = {"path": name, "sha256": digest(content), "records": len(rows)}
        csv_fields = ["observation_id", "provider", "record_type", "source_url", "observed_at",
                      "source_updated_at", "project_name", "corpus", "unit_number", "floor",
                      "area_m2", "price_rub", "price_basis", "location_scope", "rights_status"]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=csv_fields)
        writer.writeheader()
        for row in observations:
            writer.writerow({key: (row["source_fields"].get("location_scope") if key == "location_scope"
                                   else row.get(key)) for key in csv_fields})
        csv_bytes = output.getvalue().encode("utf-8")
        atomic_bytes(self.directory / "observations.csv", csv_bytes)
        files["observations_csv"] = {"path": "observations.csv", "sha256": digest(csv_bytes),
                                     "records": len(observations)}
        observed = [row["observed_at"] for row in observations]
        manifest = {
            "schema_version": "developer-harvest-run/1", "run_id": self.run_id,
            "seed_manifest_sha256": self.config_sha, "parser_sha256": self.parser_shas,
            "observed_first": min(observed) if observed else None,
            "observed_last": max(observed) if observed else None,
            "providers": self._counts(), "files": files,
            "publication_status": "private_research_only",
            "rights_status": "unknown", "all_moscow_coverage_claimed": False,
            "unit_scope_inherited_from_exact_project_id": sum(
                row["scope_evidence_observation_id"] is not None for row in units),
        }
        atomic_bytes(self.directory / "manifest.json", canonical_json(manifest) + b"\n")
        return manifest

    def status(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "directory": str(self.directory),
                "providers": self._counts()}


def read_only_status(seed_file: Path, state_root: Path) -> dict[str, Any]:
    config, config_sha = load_seed_file(seed_file)
    identity = run_identity(config_sha, plugin_hashes(config["providers"]))
    directory = state_root / identity
    database_path = directory / "state.sqlite3"
    if not database_path.is_file():
        return {"run_id": identity, "directory": str(directory), "read_only": True,
                "providers": {provider: {
                    "allowed_hosts": spec["allowed_hosts"], "seed_urls": [
                        seed["url"] for seed in spec["seeds"]],
                    "pages": {"not_started": len(spec["seeds"])},
                    "catalog_exhausted": False, "catalog_total_claimed": None,
                } for provider, spec in config["providers"].items()}}
    current = object.__new__(Harvest)
    current.config, current.run_id, current.directory = config, identity, directory
    current.database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    current.database.row_factory = sqlite3.Row
    try:
        return {**current.status(), "read_only": True}
    finally:
        current.database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-file", type=Path, required=True,
                        help="reviewed local JSON file of official hosts and catalog seeds")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    actions.add_parser("dry-run")
    run_parser = actions.add_parser("run")
    run_parser.add_argument("--capture-manifest", type=Path,
                            help="ingest ordinary-GET files with exact source URLs and observed times")
    run_parser.add_argument("--live", action="store_true",
                            help="make bounded unauthenticated HTTPS GETs after cached capture ingestion")
    run_parser.add_argument("--offline-cache-only", action="store_true",
                            help="replay only verified bodies from previous runs; never call network")
    run_parser.add_argument("--transport", choices=("curl", "urllib"), default="curl",
                            help="ordinary public HTTPS transport (default: curl)")
    run_parser.add_argument("--max-pages", type=int, default=100)
    run_parser.add_argument("--delay-seconds", type=float, default=1.0)
    run_parser.add_argument("--retry-transport-errors", action="store_true",
                            help="requeue only prior network/transport failures, never HTTP denials")
    run_parser.add_argument("--include-listings", action="store_true",
                            help="opt in to bounded unit detail GETs; catalogs/projects run first")
    args = parser.parse_args()
    if args.action == "run" and args.live and args.offline_cache_only:
        parser.error("--live and --offline-cache-only cannot be combined")
    if args.action in {"status", "dry-run"}:
        print(json.dumps(read_only_status(args.seed_file, args.state_root),
                         ensure_ascii=False, sort_keys=True, indent=2))
        return
    selected_fetch = curl_fetch if args.transport == "curl" else public_fetch
    config, config_sha = load_seed_file(args.seed_file)
    identity = run_identity(config_sha, plugin_hashes(config["providers"]))
    run_directory = args.state_root / identity
    run_directory.mkdir(parents=True, exist_ok=True)
    with (run_directory / "run.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("This harvest run is already active") from exc
        harvest = Harvest(args.seed_file, args.state_root, fetch=selected_fetch)
        try:
            ingested = harvest.ingest_captures(args.capture_manifest) if args.capture_manifest else 0
            if args.offline_cache_only:
                harvest.run_cached_only(args.max_pages)
                fetched = 0
            else:
                fetched = harvest.run(args.max_pages, args.delay_seconds,
                                      args.retry_transport_errors,
                                      args.include_listings) if args.live else 0
            manifest = harvest.export()
            print(json.dumps({"run_id": harvest.run_id, "directory": str(harvest.directory),
                              "captured_pages": ingested, "network_attempts": fetched,
                              "network_successes": harvest.last_run_stats["network_successes"],
                              "http_responses": harvest.last_run_stats["http_responses"],
                              "reused_pages": harvest.last_run_stats["reused_pages"],
                              "files": manifest["files"], "providers": manifest["providers"]},
                             ensure_ascii=False, sort_keys=True, indent=2))
        finally:
            harvest.close()


if __name__ == "__main__":
    main()
