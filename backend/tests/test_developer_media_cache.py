from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
import socket
import stat
import time
from unittest.mock import patch

from PIL import Image
import pytest

from backend import developer_media_cache as media


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 5), (120, 40, 20)).save(output, format="PNG")
    return output.getvalue()


def _jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 5), (20, 80, 120)).save(output, format="JPEG")
    return output.getvalue()


def _row(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "media_id": "media_0123456789abcdef01234567",
        "registry_id": "registry-sample",
        "unit_id": "unit-sample",
        "floor_id": None,
        "scope": "unit_plan",
        "role": "apartment_plan",
        "source_url": "https://cdn.fsk.ru/plans/sample.png",
        "allowed_host": "cdn.fsk.ru",
        "expected_mime": "image/png",
        "source_evidence_sha256": "a" * 64,
        "rights_status": "unknown",
    }
    value.update(updates)
    return value


class _TrustContext:
    def __init__(self, ca_count: int) -> None:
        self.verify_mode = media.ssl.CERT_REQUIRED
        self.check_hostname = True
        self.ca_count = ca_count

    def cert_store_stats(self) -> dict[str, int]:
        return {"x509_ca": self.ca_count}


class _Response:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.offset = 0
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        value = self.body[self.offset:self.offset + size]
        self.offset += len(value)
        return value


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _TimedSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class _HeaderTimeoutConnection:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.sock = _TimedSocket()
        self.closed = False

    def connect(self) -> None:
        return None

    def request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def getresponse(self) -> None:
        raise socket.timeout()

    def close(self) -> None:
        self.closed = True


def test_private_dns_answer_is_rejected_before_connect() -> None:
    answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
    with (patch.object(media.socket, "getaddrinfo", return_value=[answer]),
          pytest.raises(media.MediaCacheError, match="source_address_not_public")):
        media._public_addresses("cdn.fsk.ru")


def test_file_lock_has_optional_bounded_wait(tmp_path: Path) -> None:
    lock = tmp_path / "bounded.lock"
    started = time.monotonic()
    with media._file_lock(lock):
        with pytest.raises(media.MediaCacheError, match="cache_lock_timeout"):
            with media._file_lock(lock, timeout=0.03):
                raise AssertionError("contended lock unexpectedly acquired")
    assert time.monotonic() - started < 0.5


def test_host_slots_allow_two_downloads_and_bound_excess_queue(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_root = media._ensure_private_root(tmp_path / "cache")
    state = media._private_child(cache_root, "state")
    host = "cdn.fsk.ru"
    key = hashlib.sha256(host.encode()).hexdigest()[:16]
    slot0 = state / f"host-{key}-slot-0.lock"
    slot1 = state / f"host-{key}-slot-1.lock"
    deadline = time.monotonic() + 5
    with media._file_lock(slot0):
        with media._host_fetch_slot(state, host, deadline) as selected:
            assert selected == 1
    monkeypatch.setattr(media, "HOST_QUEUE_TIMEOUT_SECONDS", 0.03)
    started = time.monotonic()
    with media._file_lock(slot0):
        with media._file_lock(slot1):
            with pytest.raises(media.MediaCacheError, match="source_host_busy"):
                with media._host_fetch_slot(state, host, time.monotonic() + 1):
                    raise AssertionError("both occupied slots unexpectedly acquired")
    assert time.monotonic() - started < 0.5


def test_empty_default_ca_store_falls_back_to_existing_system_bundle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "system-ca.pem"
    bundle.write_text("test CA bundle placeholder", encoding="ascii")
    empty = _TrustContext(0)
    trusted = _TrustContext(3)
    calls: list[tuple[object, ...]] = []

    def create_context(*, cafile: str | None = None) -> _TrustContext:
        calls.append((cafile,))
        return empty if cafile is None else trusted

    monkeypatch.setattr(media.ssl, "create_default_context", create_context)
    selected = media._create_ssl_context((bundle,))
    assert selected is trusted
    assert calls == [(None,), (str(bundle),)]
    assert selected.verify_mode == media.ssl.CERT_REQUIRED
    assert selected.check_hostname is True


def test_empty_default_ca_store_fails_closed_without_system_bundle(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.ssl, "create_default_context",
                        lambda **_kwargs: _TrustContext(0))
    monkeypatch.setattr(media.ssl, "get_default_verify_paths",
                        lambda: type("Paths", (), {"capath": None, "cafile": None})())
    with pytest.raises(RuntimeError, match="No usable verified TLS trust store"):
        media._create_ssl_context(())


def test_redirect_to_other_host_is_rejected_without_second_request(tmp_path: Path) -> None:
    response = _Response(302, headers={"Location": "https://evil.test/image.png"})
    connection = _Connection(response)
    with (patch.object(media, "_public_addresses", return_value=["93.184.216.34"]),
          patch.object(media, "_open_request", return_value=(connection, response)) as opened,
          pytest.raises(media.MediaCacheError, match="source_redirect_rejected")):
        media._fetch_source("https://cdn.fsk.ru/plans/a.png", "cdn.fsk.ru",
                            "image/png", tmp_path)
    assert opened.call_count == 1
    assert connection.closed


def test_same_host_redirect_revalidates_dns(tmp_path: Path) -> None:
    source = _png()
    first = _Response(302, headers={"Location": "/plans/final.png"})
    second = _Response(200, source, {"Content-Type": "image/png"})
    calls = iter([(_Connection(first), first), (_Connection(second), second)])
    with (patch.object(media, "_public_addresses", return_value=["93.184.216.34"]) as resolved,
          patch.object(media, "_open_request", side_effect=lambda *args: next(calls)) as opened):
        result = media._fetch_source("https://cdn.fsk.ru/plans/a.png", "cdn.fsk.ru",
                                     "image/png", tmp_path)
    assert result == source
    assert opened.call_count == 2
    assert resolved.call_count == 2


def test_unresponsive_dns_answer_does_not_starve_next_public_address(tmp_path: Path) -> None:
    source = _png()
    response = _Response(200, source, {"Content-Type": "image/png"})
    addresses = ["93.184.216.34", "1.1.1.1"]
    with (patch.object(media, "_public_addresses", return_value=addresses),
          patch.object(media, "_open_request",
                       side_effect=[TimeoutError(), (_Connection(response), response)]) as opened):
        result = media._fetch_source("https://cdn.fsk.ru/plans/a.png", "cdn.fsk.ru",
                                     "image/png", tmp_path)
    assert result == source
    assert opened.call_count == 2
    assert opened.call_args_list[0].args[3] <= media.CONNECT_TIMEOUT_SECONDS


def test_verified_connection_gets_separate_bounded_header_timeout(
        monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _HeaderTimeoutConnection()
    monkeypatch.setattr(media, "_PinnedHTTPSConnection", lambda *_args, **_kwargs: fake)
    with pytest.raises(media.MediaCacheError, match="source_headers_timeout"):
        media._open_request("cdn.fsk.ru", "93.184.216.34", "/plans/a.png",
                            1.5, media.time.monotonic() + 10)
    assert fake.sock.timeout is not None
    assert fake.sock.timeout <= media.RESPONSE_READ_TIMEOUT_SECONDS
    assert fake.closed


def test_denied_host_is_stopped_for_later_media(tmp_path: Path) -> None:
    response = _Response(403)
    with (patch.object(media, "_public_addresses", return_value=["93.184.216.34"]),
          patch.object(media, "_open_request", return_value=(_Connection(response), response)) as opened):
        with pytest.raises(media.MediaCacheError, match="source_host_temporarily_blocked"):
            media._fetch_source("https://cdn.fsk.ru/plans/a.png", "cdn.fsk.ru",
                                "image/png", tmp_path)
        with pytest.raises(media.MediaCacheError, match="source_host_temporarily_blocked"):
            media._fetch_source("https://cdn.fsk.ru/plans/b.png", "cdn.fsk.ru",
                                "image/png", tmp_path)
    assert opened.call_count == 1


def test_media_cached_by_hash_and_binding_without_second_get(tmp_path: Path) -> None:
    source = _png()
    response = _Response(200, source, {
        "Content-Type": "image/png", "Content-Length": str(len(source)),
    })
    connection = _Connection(response)
    with (patch.object(media, "_public_addresses", return_value=["93.184.216.34"]),
          patch.object(media, "_open_request", return_value=(connection, response)) as opened):
        first = media.get_or_prepare(_row(), cache_root=tmp_path / "media")
        second = media.get_or_prepare(_row(), cache_root=tmp_path / "media")
    assert first is not None and first.status == "prepared"
    assert second is not None and second.status == "cached"
    assert first.path == second.path
    assert first.path is not None and first.path.is_file()
    assert first.mime_type == "image/png"
    assert first.rights_status == "unknown"
    assert first.sha256 is not None
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    assert opened.call_count == 1


def test_same_opaque_id_cannot_change_source_binding(tmp_path: Path) -> None:
    source = _png()
    response = _Response(200, source, {"Content-Type": "image/png"})
    with (patch.object(media, "_public_addresses", return_value=["93.184.216.34"]),
          patch.object(media, "_open_request", return_value=(_Connection(response), response))):
        media.get_or_prepare(_row(), cache_root=tmp_path / "media")
        changed = _row(source_url="https://cdn.fsk.ru/plans/changed.png")
        with pytest.raises(media.MediaCacheError, match="media_binding_changed"):
            media.get_or_prepare(changed, cache_root=tmp_path / "media")


def test_prepared_menu_asset_never_fetches_or_creates_duplicate_cache(tmp_path: Path) -> None:
    row = _row(prepared_source="menu_asset", prepared_sha256="b" * 64,
               prepared_mime="image/png", prepared_bytes=128)
    result = media.get_or_prepare(row, cache_root=tmp_path / "unused")
    assert result is not None and result.status == "already_prepared"
    assert result.path is None
    assert not (tmp_path / "unused").exists()


def test_exact_project_gallery_capture_can_seed_cache_without_get(tmp_path: Path) -> None:
    source = _jpeg()
    source_sha = media._sha(source)
    row = _row(
        media_id="media_89abcdef0123456789abcdef",
        registry_id="registry-project",
        unit_id=None,
        scope="project_gallery",
        role="project_rendering",
        source_url="https://cdn-dataout.trendagent.ru/gallery/render.jpg",
        allowed_host="cdn-dataout.trendagent.ru",
        expected_mime="image/jpeg",
        known_capture_sha256=source_sha,
    )
    seeded = media.seed_verified_capture(row, source,
                                         expected_source_sha256=source_sha,
                                         cache_root=tmp_path / "media")
    assert seeded.status == "seeded"
    assert seeded.rights_status == "unknown"
    assert seeded.mime_type == "image/jpeg"
    assert seeded.byte_length is not None and seeded.byte_length < len(source)
    assert seeded.path is not None and seeded.path.is_file()
    with patch.object(media, "_open_request", side_effect=AssertionError("must not fetch")):
        cached = media.get_or_prepare(row, cache_root=tmp_path / "media")
    assert cached is not None and cached.status == "cached"
    assert cached.sha256 == seeded.sha256


def test_seeded_capture_requires_row_pinned_hash(tmp_path: Path) -> None:
    source = _jpeg()
    row = _row(scope="project_gallery_single_corpus", role="facade_photo",
               unit_id=None, source_url="https://cdn.fsk.ru/gallery/photo.jpg",
               expected_mime="image/jpeg")
    with pytest.raises(media.MediaCacheError, match="capture_hash_mismatch"):
        media.seed_verified_capture(row, source,
                                    expected_source_sha256=media._sha(source),
                                    cache_root=tmp_path / "media")


def test_body_cap_and_magic_mime_are_enforced() -> None:
    too_large = _Response(200, b"x" * (media.MAX_SOURCE_BYTES + 1))
    with pytest.raises(media.MediaCacheError, match="source_too_large"):
        media._read_response(too_large, media.time.monotonic() + 30)
    with pytest.raises(media.MediaCacheError, match="source_mime_mismatch"):
        media._normalize_image(_png(), "image/jpeg", "image/jpeg")


def test_svg_is_rasterized_to_bounded_metadata_free_png() -> None:
    source = (b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">'
              b'<rect x="0" y="0" width="320" height="180" fill="#abc"/></svg>')
    prepared = media._normalize_image(source, "image/svg+xml", "image/svg+xml")
    assert media._sniff_mime(prepared) == "image/png"
    with Image.open(BytesIO(prepared)) as image:
        assert image.format == "PNG"
        assert image.width <= 1536 and image.height <= 1536
        assert image.info == {}


def test_total_cache_storage_limit_is_enforced(tmp_path: Path) -> None:
    root = tmp_path / "media"
    media._ensure_private_root(root)
    state = media._private_child(root, "state")
    media._write_json_atomic(state / "quota.json", {
        "reserved_bytes": media.MAX_CACHE_BYTES,
    })
    with pytest.raises(media.MediaCacheError, match="cache_capacity_reached"):
        media._cache_object(root, state, _png(), "image/png")


def test_active_svg_and_non_allowlisted_url_are_rejected() -> None:
    with pytest.raises(media.MediaCacheError, match="svg_active_content_rejected"):
        media._inert_svg(b'<svg width="20" height="10"><script>alert(1)</script></svg>')
    with pytest.raises(media.MediaCacheError, match="source_url_not_allowlisted"):
        media._validated_url(_row(source_url="https://127.0.0.1/private.png",
                                  allowed_host="127.0.0.1"))


def test_cache_root_symlink_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(media.MediaCacheError, match="unsafe_cache_path"):
        media._ensure_private_root(link / "media")
