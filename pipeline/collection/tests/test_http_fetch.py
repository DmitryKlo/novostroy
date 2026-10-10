"""Local wire checks for conditional public fetching without external access."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from pipeline.collection.http_fetch import TransientFetchError, fetch_public_file, latest_minstroy_xls


class FixtureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, str]] = []

    def do_GET(self) -> None:
        self.requests.append(dict(self.headers.items()))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://unapproved.example/escape")
            self.end_headers()
            return
        if self.path == "/retry":
            self.send_response(503)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"version-1"':
            self.send_response(304)
            self.send_header("ETag", '"version-1"')
            self.end_headers()
            return
        payload = b"reviewed-public-fixture"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"version-1"')
        self.send_header("Last-Modified", "Fri, 09 Oct 2026 00:00:00 GMT")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args) -> None:
        return


@pytest.fixture
def local_http():
    FixtureHandler.requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_conditional_get_and_bounded_transfer(local_http: str) -> None:
    first = fetch_public_file(local_http + "/source", allowed_hosts={"127.0.0.1"}, max_bytes=100)
    assert first.status == "modified" and first.body == b"reviewed-public-fixture"
    assert first.etag == '"version-1"'
    second = fetch_public_file(local_http + "/source", allowed_hosts={"127.0.0.1"},
                               max_bytes=100, previous_etag=first.etag,
                               previous_last_modified=first.last_modified)
    assert second.status == "not_modified" and second.body is None
    assert FixtureHandler.requests[-1]["If-None-Match"] == '"version-1"'
    assert FixtureHandler.requests[-1]["If-Modified-Since"] == first.last_modified
    with pytest.raises(ValueError, match="byte limit"):
        fetch_public_file(local_http + "/source", allowed_hosts={"127.0.0.1"}, max_bytes=5)


def test_redirect_host_validation_and_transient_status(local_http: str) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        fetch_public_file(local_http + "/redirect", allowed_hosts={"127.0.0.1"}, max_bytes=100)
    with pytest.raises(TransientFetchError):
        fetch_public_file(local_http + "/retry", allowed_hosts={"127.0.0.1"}, max_bytes=100)
    with pytest.raises(ValueError, match="allowlist"):
        fetch_public_file(local_http + "/source", allowed_hosts={"example.org"}, max_bytes=100)
    with pytest.raises(ValueError, match="allowlist"):
        fetch_public_file("https://user:password@minstroyrf.gov.ru/data.xls",
                          allowed_hosts={"minstroyrf.gov.ru"}, max_bytes=100)


def test_latest_version_is_selected_only_from_reviewed_page_section() -> None:
    page = b'''<a href="/upload/iblock/xxx/yyy/data_20990101t0000_structure_20161117t1330.xls">outside</a>
    <div class="dataset-files"><a href="/upload/iblock/f74/abc/data_20260430t0000_structure_20161117t1330.xls">old</a>
    <a href="/upload/iblock/f74/def/data_20260529t0000_structure_20161117t1330.xls">new</a></div>'''
    url, version = latest_minstroy_xls(page, "https://minstroyrf.gov.ru/opendata/registry/")
    assert version == "2026-05-29"
    assert url == "https://minstroyrf.gov.ru/upload/iblock/f74/def/data_20260529t0000_structure_20161117t1330.xls"
    duplicate = page.replace(b"</div>",
        b'<a href="/upload/iblock/f74/other/data_20260529t0000_structure_20161117t1330.xls">tie</a></div>')
    with pytest.raises(ValueError, match="Ambiguous"):
        latest_minstroy_xls(duplicate, "https://minstroyrf.gov.ru/opendata/registry/")
