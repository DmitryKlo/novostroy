"""Bounded, credential-free HTTP fetch for reviewed public source URLs."""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class TransientFetchError(RuntimeError):
    """Retry without exposing a provider URL or response body in logs."""


@dataclass(frozen=True)
class FetchResult:
    status: str
    body: bytes | None
    etag: str | None
    last_modified: str | None
    content_type: str | None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")


class _DatasetFiles(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "dataset-files" in (values.get("class") or "").split():
                self.depth = 1
        elif tag == "a" and self.depth and values.get("href"):
            self.links.append(values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.depth:
            self.depth -= 1


VERSIONED_XLS = re.compile(
    r"^/upload/iblock/[A-Za-z0-9]+/[A-Za-z0-9]+/data_(\d{8})t0000_structure_\d{8}t\d{4}\.xls$"
)


def latest_minstroy_xls(page: bytes, page_url: str) -> tuple[str, str]:
    """Select a unique newest XLS only from the reviewed official page section."""
    parser = _DatasetFiles()
    parser.feed(page.decode("utf-8"))
    candidates = []
    for href in parser.links:
        match = VERSIONED_XLS.fullmatch(href)
        if not match:
            continue
        try:
            version = datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        candidates.append((version, href))
    if not candidates:
        raise ValueError("No reviewed versioned XLS link on dataset page")
    highest = max(version for version, _ in candidates)
    newest = {href for version, href in candidates if version == highest}
    if len(newest) != 1:
        raise ValueError("Ambiguous newest XLS version on dataset page")
    url = urljoin(page_url, newest.pop())
    if urlsplit(url).hostname != "minstroyrf.gov.ru":
        raise ValueError("Versioned XLS URL left the official host")
    return url, highest


def _header(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 256 or "\r" in value or "\n" in value:
        raise ValueError("Invalid HTTP validator header")
    return value


def fetch_public_file(url: str, *, allowed_hosts: set[str], max_bytes: int,
                      previous_etag: str | None = None,
                      previous_last_modified: str | None = None,
                      timeout_seconds: float = 15.0) -> FetchResult:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}):
        raise ValueError("Public fetch requires HTTPS")
    if host not in allowed_hosts or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Public fetch URL is outside the approved allowlist")
    if parsed.scheme == "https" and parsed.port not in {None, 443}:
        raise ValueError("Nonstandard public HTTPS port is not approved")
    if not 1 <= max_bytes <= 1073741824:
        raise ValueError("Invalid HTTP download byte limit")
    headers = {"User-Agent": "NovostroyPublicCollection/1.1", "Accept-Encoding": "identity"}
    if previous_etag:
        headers["If-None-Match"] = _header(previous_etag)
    if previous_last_modified:
        headers["If-Modified-Since"] = _header(previous_last_modified)
    if not SYSTEM_CA_BUNDLE.is_file():
        raise ValueError("System CA bundle is unavailable")
    opener = build_opener(ProxyHandler({}), NoRedirect(),
                          HTTPSHandler(context=ssl.create_default_context(cafile=str(SYSTEM_CA_BUNDLE))))
    request = Request(url, headers=headers, method="GET")
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        if exc.code == 304:
            return FetchResult("not_modified", None, _header(exc.headers.get("ETag")) or previous_etag,
                               _header(exc.headers.get("Last-Modified")) or previous_last_modified, None)
        if exc.code in {408, 429, 500, 502, 503, 504}:
            raise TransientFetchError("transient_http_status") from None
        raise ValueError("Public HTTP source unavailable") from None
    except (URLError, TimeoutError, socket.timeout):
        raise TransientFetchError("transient_connection_failure") from None
    with response:
        if response.status != 200:
            raise ValueError("Unexpected public HTTP response")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                announced = int(length)
            except ValueError as exc:
                raise ValueError("Invalid HTTP Content-Length") from exc
            if announced < 0 or announced > max_bytes:
                raise ValueError("HTTP source exceeds byte limit")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("HTTP source exceeds byte limit")
        return FetchResult("modified", body, _header(response.headers.get("ETag")),
                           _header(response.headers.get("Last-Modified")),
                           response.headers.get_content_type())
