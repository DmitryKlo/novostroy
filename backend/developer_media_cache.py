"""Bounded, source-bound cache for approved developer media previews.

The caller supplies only a row from the immutable developer-media registry.
This module never accepts a caller-provided URL, follows only same-host HTTPS
redirects, connects to a validated public address while retaining TLS hostname
verification, and stores only normalized JPEG or PNG bytes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterator, Mapping
from urllib.parse import urljoin, urlsplit
import warnings
import xml.etree.ElementTree as ET

from io import BytesIO
from PIL import Image, ImageOps


ALLOWED_HOSTS = frozenset({"cdn-dataout.trendagent.ru", "cdn.a101.ru", "cdn.fsk.ru"})
SUPPORTED_ROLES = frozenset({
    "apartment_plan", "furnished_apartment_plan", "floor_plan",
    "facade_photo", "project_rendering", "project_plan",
})
UNIT_ROLES = frozenset({"apartment_plan", "furnished_apartment_plan"})
PROJECT_ROLES = frozenset({"facade_photo", "project_rendering", "project_plan"})
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".svg": "image/svg+xml",
}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MEDIA_ID = re.compile(r"media_[0-9a-f]{24}\Z")
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PREPARED_BYTES = 12 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 8_000_000
FETCH_DEADLINE_SECONDS = 12.0
CONNECT_TIMEOUT_SECONDS = 1.5
RESPONSE_READ_TIMEOUT_SECONDS = 4.0
MAX_DNS_ADDRESSES = 8
HOST_MIN_INTERVAL_SECONDS = 1.0
HOST_FETCH_SLOTS = 2
HOST_QUEUE_TIMEOUT_SECONDS = 2.0
HOST_STATE_LOCK_TIMEOUT_SECONDS = 0.25
MAX_REDIRECTS = 2
SVG_TAGS = frozenset({
    "svg", "g", "path", "rect", "polygon", "polyline", "circle",
    "ellipse", "line", "text", "tspan", "defs", "clipPath",
    "linearGradient", "radialGradient", "stop",
})
SVG_ATTRIBUTES = frozenset({
    "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry",
    "width", "height", "viewBox", "version", "transform", "points", "d",
    "style", "id", "class", "fill", "stroke", "stroke-width", "opacity",
    "space", "offset", "stop-color", "stop-opacity", "font-size",
    "font-family", "text-anchor",
})
SVG_FORBIDDEN = ("javascript:", "http:", "https:", "data:", "@import", "expression(")
SYSTEM_CA_BUNDLES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/ca-bundle.pem"),
    Path("/etc/ssl/cert.pem"),
)


def _context_has_trust(context: ssl.SSLContext) -> bool:
    try:
        stats = context.cert_store_stats()
    except (AttributeError, OSError, ssl.SSLError):
        return False
    return isinstance(stats, dict) and stats.get("x509_ca", 0) > 0


def _create_ssl_context(
        bundle_paths: tuple[Path, ...] = SYSTEM_CA_BUNDLES) -> ssl.SSLContext:
    """Build a verified TLS context, falling back only when default trust is empty.

    Some Python installations retain compiled OpenSSL paths to a removed Conda
    environment. In that case use a known system CA bundle while preserving
    certificate-chain and hostname verification. Never create an unverified
    context or accept an arbitrary bundle path from a request.
    """
    default_context: ssl.SSLContext | None = None
    try:
        default_context = ssl.create_default_context()
    except (OSError, ValueError, ssl.SSLError):
        pass
    if (default_context is not None and
            default_context.verify_mode == ssl.CERT_REQUIRED and
            default_context.check_hostname and _context_has_trust(default_context)):
        return default_context

    for bundle in bundle_paths:
        try:
            info = bundle.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                continue
            candidate = ssl.create_default_context(cafile=str(bundle))
        except (OSError, ValueError, ssl.SSLError):
            continue
        if (candidate.verify_mode == ssl.CERT_REQUIRED and candidate.check_hostname and
                _context_has_trust(candidate)):
            return candidate

    # A capath can be a valid lazy-loaded trust store with no eagerly counted
    # certificates. Keep it only when OpenSSL reports an existing default path.
    if (default_context is not None and
            default_context.verify_mode == ssl.CERT_REQUIRED and
            default_context.check_hostname):
        paths = ssl.get_default_verify_paths()
        capath_exists = bool(paths.capath and Path(paths.capath).is_dir())
        cafile_exists = bool(paths.cafile and Path(paths.cafile).is_file())
        if capath_exists or cafile_exists:
            return default_context
    raise RuntimeError("No usable verified TLS trust store is available")


_SSL_CONTEXT = _create_ssl_context()


class MediaCacheError(Exception):
    """A stable, URL-free error suitable for mapping to a generic API response."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedMedia:
    media_id: str
    status: str
    sha256: str | None
    mime_type: str | None
    byte_length: int | None
    path: Path | None
    rights_status: str
    reason_code: str | None = None


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _safe_media_binding(row: Mapping[str, Any]) -> dict[str, str]:
    media_id = row.get("media_id")
    registry_id = row.get("registry_id")
    unit_id = row.get("unit_id")
    floor_id = row.get("floor_id")
    scope = row.get("scope")
    role = row.get("role")
    if not isinstance(media_id, str) or MEDIA_ID.fullmatch(media_id) is None:
        raise MediaCacheError("invalid_media_id")
    if not isinstance(registry_id, str) or not registry_id or len(registry_id) > 160:
        raise MediaCacheError("invalid_registry_binding")
    if scope not in {"unit_plan", "floor_plan", "project_gallery_single_corpus", "project_gallery"}:
        raise MediaCacheError("invalid_scope")
    if role not in SUPPORTED_ROLES:
        raise MediaCacheError("unsupported_role")
    if scope == "unit_plan" and (role not in UNIT_ROLES or not isinstance(unit_id, str) or floor_id is not None):
        raise MediaCacheError("invalid_unit_binding")
    if scope == "floor_plan" and (role != "floor_plan" or not isinstance(floor_id, str) or unit_id is not None):
        raise MediaCacheError("invalid_floor_binding")
    if scope in {"project_gallery_single_corpus", "project_gallery"} and (
            role not in PROJECT_ROLES or unit_id is not None or floor_id is not None):
        raise MediaCacheError("invalid_project_binding")
    for value in (unit_id, floor_id):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 160):
            raise MediaCacheError("invalid_media_binding")
    evidence = row.get("source_evidence_sha256")
    if not isinstance(evidence, str) or HEX64.fullmatch(evidence) is None:
        raise MediaCacheError("invalid_source_evidence")
    rights = row.get("rights_status", "unknown")
    if not isinstance(rights, str) or not rights or len(rights) > 80:
        raise MediaCacheError("invalid_rights_status")
    return {
        "media_id": media_id,
        "registry_id": registry_id,
        "unit_id": unit_id or "",
        "floor_id": floor_id or "",
        "scope": str(scope),
        "role": str(role),
        "source_evidence_sha256": evidence,
        "rights_status": rights,
    }


def _validated_url(row: Mapping[str, Any]) -> tuple[str, str, str]:
    raw = row.get("source_url")
    allowed_host = row.get("allowed_host")
    expected = row.get("expected_mime")
    if not isinstance(raw, str) or len(raw) > 2048:
        raise MediaCacheError("invalid_source_url")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise MediaCacheError("invalid_source_url") from None
    if (parsed.scheme != "https" or host not in ALLOWED_HOSTS or host != allowed_host or
            parsed.username or parsed.password or port is not None or parsed.query or
            parsed.fragment or not parsed.path.startswith("/") or
            any(part in {".", ".."} for part in parsed.path.split("/")) or
            "\\" in parsed.path or any(ord(char) < 0x20 for char in raw)):
        raise MediaCacheError("source_url_not_allowlisted")
    lowered = parsed.path.lower()
    if any(token in lowered for token in ("%2e", "%2f", "%5c")):
        raise MediaCacheError("source_url_not_allowlisted")
    suffix_mime = MIME_BY_SUFFIX.get(Path(parsed.path).suffix.lower())
    if expected not in {"image/jpeg", "image/png", "image/webp", "image/svg+xml"} or suffix_mime != expected:
        raise MediaCacheError("unsupported_source_type")
    return host, raw, expected


def _ensure_private_root(cache_root: Path) -> Path:
    root = Path(cache_root)
    if not root.is_absolute():
        raise MediaCacheError("cache_root_must_be_absolute")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MediaCacheError("unsafe_cache_path")
    root.chmod(0o700)
    return root


def _private_child(root: Path, name: str) -> Path:
    path = root / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MediaCacheError("unsafe_cache_path")
    path.chmod(0o700)
    return path


@contextmanager
def _file_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    import fcntl

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise MediaCacheError("cache_io_error") from None
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
        else:
            lock_deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaCacheError("cache_lock_timeout") from None
                    time.sleep(min(0.02, remaining))
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _host_fetch_slot(state_root: Path, host: str,
                     deadline: float) -> Iterator[int]:
    """Acquire one of two host request slots with a hard queue deadline."""
    host_key = hashlib.sha256(host.encode()).hexdigest()[:16]
    paths = [state_root / f"host-{host_key}-slot-{index}.lock"
             for index in range(HOST_FETCH_SLOTS)]
    queue_deadline = min(deadline, time.monotonic() + HOST_QUEUE_TIMEOUT_SECONDS)
    while True:
        for index, path in enumerate(paths):
            manager = _file_lock(path, timeout=0)
            try:
                manager.__enter__()
            except MediaCacheError as exc:
                if exc.code == "cache_lock_timeout":
                    continue
                raise
            try:
                yield index
            finally:
                manager.__exit__(None, None, None)
            return
        remaining = queue_deadline - time.monotonic()
        if remaining <= 0:
            raise MediaCacheError("source_host_busy")
        time.sleep(min(0.02, remaining))


def _reserve_host_start(state_root: Path, host: str, state_path: Path,
                        deadline: float) -> None:
    """Apply a cross-process per-host start interval without holding a network lock."""
    host_key = hashlib.sha256(host.encode()).hexdigest()[:16]
    state_lock = state_root / f"host-{host_key}-state.lock"
    queue_deadline = min(deadline, time.monotonic() + HOST_QUEUE_TIMEOUT_SECONDS)
    while True:
        remaining = queue_deadline - time.monotonic()
        if remaining <= 0:
            raise MediaCacheError("source_host_busy")
        sleep_for = 0.02
        try:
            with _file_lock(state_lock,
                            timeout=min(HOST_STATE_LOCK_TIMEOUT_SECONDS, remaining)):
                host_state = _read_json(state_path) or {}
                if host_state.get("blocked") is True:
                    raise MediaCacheError("source_host_temporarily_blocked")
                now = time.time()
                last = host_state.get("last_attempt_epoch")
                wait = (HOST_MIN_INTERVAL_SECONDS - (now - float(last))
                        if isinstance(last, (int, float)) and not isinstance(last, bool)
                        else 0.0)
                if wait <= 0:
                    _write_json_atomic(state_path, {
                        "last_attempt_epoch": now, "blocked": False,
                    })
                    return
                sleep_for = min(wait, remaining)
        except MediaCacheError as exc:
            if exc.code != "cache_lock_timeout":
                raise
        remaining = queue_deadline - time.monotonic()
        if remaining <= 0:
            raise MediaCacheError("source_host_busy")
        time.sleep(min(sleep_for, remaining))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                info.st_mode & 0o077 or info.st_size > 16_384):
            raise MediaCacheError("unsafe_cache_file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except MediaCacheError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise MediaCacheError("cache_metadata_invalid") from None
    if not isinstance(value, dict):
        raise MediaCacheError("cache_metadata_invalid")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    blob = (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    fd = -1
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_dir(path.parent)
    except OSError:
        raise MediaCacheError("cache_io_error") from None
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _public_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise MediaCacheError("source_dns_unavailable") from None
    addresses = sorted({info[4][0].split("%", 1)[0] for info in infos})
    if not addresses:
        raise MediaCacheError("source_dns_unavailable")
    try:
        parsed = [ipaddress.ip_address(address) for address in addresses]
    except ValueError:
        raise MediaCacheError("source_dns_invalid") from None
    if any(not address.is_global for address in parsed):
        raise MediaCacheError("source_address_not_public")
    return addresses[:MAX_DNS_ADDRESSES]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float) -> None:
        super().__init__(host, port=443, timeout=timeout, context=_SSL_CONTEXT)
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _open_request(host: str, address: str, request_target: str,
                  connect_timeout: float,
                  deadline: float) -> tuple[_PinnedHTTPSConnection, http.client.HTTPResponse]:
    connection = _PinnedHTTPSConnection(host, address, connect_timeout)
    try:
        connection.connect()
    except ssl.SSLCertVerificationError:
        connection.close()
        raise MediaCacheError("source_tls_certificate_rejected") from None
    except ssl.SSLError:
        connection.close()
        raise MediaCacheError("source_tls_failed") from None
    except (socket.timeout, TimeoutError):
        connection.close()
        raise MediaCacheError("source_connect_timeout") from None
    except (OSError, http.client.HTTPException):
        connection.close()
        raise MediaCacheError("source_connect_failed") from None

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        connection.close()
        raise MediaCacheError("source_timeout")
    socket_timeout = min(RESPONSE_READ_TIMEOUT_SECONDS, remaining)
    try:
        if connection.sock is not None:
            connection.sock.settimeout(socket_timeout)
        connection.request("GET", request_target, headers={
            "Accept": "image/png,image/jpeg,image/webp,image/svg+xml",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "NovostroyMediaCache/1.0",
        })
        return connection, connection.getresponse()
    except (socket.timeout, TimeoutError):
        connection.close()
        raise MediaCacheError("source_headers_timeout") from None
    except (OSError, http.client.HTTPException):
        connection.close()
        raise MediaCacheError("source_response_failed") from None


def _read_response(response: http.client.HTTPResponse, deadline: float) -> bytes:
    encoding = response.getheader("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise MediaCacheError("source_encoding_rejected")
    content_length = response.getheader("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_SOURCE_BYTES:
                raise MediaCacheError("source_too_large")
        except ValueError:
            raise MediaCacheError("source_length_invalid") from None
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_SOURCE_BYTES:
        if time.monotonic() >= deadline:
            raise MediaCacheError("source_timeout")
        remaining = min(64 * 1024, MAX_SOURCE_BYTES + 1 - total)
        try:
            chunk = response.read(remaining)
        except (OSError, http.client.HTTPException):
            raise MediaCacheError("source_read_failed") from None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_SOURCE_BYTES:
        raise MediaCacheError("source_too_large")
    if total == 0:
        raise MediaCacheError("source_empty")
    return b"".join(chunks)


def _fetch_source(url: str, host: str, expected_mime: str,
                  state_root: Path, *, deadline: float | None = None) -> bytes:
    if deadline is None:
        deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    if deadline <= time.monotonic():
        raise MediaCacheError("source_timeout")
    host_key = hashlib.sha256(host.encode()).hexdigest()[:16]
    state_path = state_root / f"host-{host_key}.json"
    with _host_fetch_slot(state_root, host, deadline):
        _reserve_host_start(state_root, host, state_path, deadline)
        current = url
        for redirect_index in range(MAX_REDIRECTS + 1):
            parsed = urlsplit(current)
            current_host = (parsed.hostname or "").lower()
            if (parsed.scheme != "https" or current_host != host or parsed.port is not None or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    not parsed.path.startswith("/")):
                raise MediaCacheError("source_redirect_rejected")
            addresses = _public_addresses(host)
            connection: _PinnedHTTPSConnection | None = None
            response: http.client.HTTPResponse | None = None
            last_error: MediaCacheError | None = None
            for address in addresses:
                if time.monotonic() >= deadline:
                    raise MediaCacheError("source_timeout")
                try:
                    connection, response = _open_request(
                        host, address, parsed.path, min(CONNECT_TIMEOUT_SECONDS,
                                                       max(0.1, deadline - time.monotonic())),
                        deadline)
                    last_error = None
                    break
                except MediaCacheError:
                    raise
                except (OSError, ssl.SSLError, http.client.HTTPException):
                    last_error = MediaCacheError("source_connect_failed")
            if response is None or connection is None:
                raise last_error or MediaCacheError("source_connect_failed")
            try:
                active_socket = getattr(connection, "sock", None)
                if active_socket is not None:
                    active_socket.settimeout(min(RESPONSE_READ_TIMEOUT_SECONDS,
                                                 max(0.1, deadline - time.monotonic())))
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if redirect_index >= MAX_REDIRECTS or not location:
                        raise MediaCacheError("source_redirect_rejected")
                    target = urljoin(current, location)
                    target_parts = urlsplit(target)
                    if (target_parts.scheme != "https" or
                            (target_parts.hostname or "").lower() != host or
                            target_parts.port is not None or target_parts.username or
                            target_parts.password or target_parts.query or target_parts.fragment):
                        raise MediaCacheError("source_redirect_rejected")
                    current = target
                    continue
                if response.status in {401, 403, 429}:
                    state_lock = state_root / f"host-{host_key}-state.lock"
                    try:
                        with _file_lock(state_lock,
                                        timeout=HOST_STATE_LOCK_TIMEOUT_SECONDS):
                            current_state = _read_json(state_path) or {}
                            current_state.update({"blocked": True,
                                                  "http_status": response.status})
                            _write_json_atomic(state_path, current_state)
                    except MediaCacheError:
                        # The current denied response still stops this call;
                        # future requests are also bounded by slot/queue limits.
                        pass
                    raise MediaCacheError("source_host_temporarily_blocked")
                if response.status != 200:
                    raise MediaCacheError("source_unavailable")
                actual_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if actual_type != expected_mime:
                    raise MediaCacheError("source_mime_mismatch")
                blob = _read_response(response, deadline)
            finally:
                connection.close()
            return blob
        raise MediaCacheError("source_redirect_rejected")


def _inert_svg(blob: bytes) -> bytes:
    if len(blob) > 2 * 1024 * 1024:
        raise MediaCacheError("source_too_large")
    upper = blob.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise MediaCacheError("svg_active_content_rejected")
    if blob.count(b"<") > 10_000:
        raise MediaCacheError("svg_active_content_rejected")
    try:
        root = ET.fromstring(blob)
    except (ET.ParseError, ValueError):
        raise MediaCacheError("source_image_invalid") from None
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise MediaCacheError("source_mime_mismatch")

    def number(raw: str | None) -> float | None:
        if raw is None:
            return None
        match = re.fullmatch(r"\s*(\d{1,5}(?:\.\d{1,4})?)(?:px)?\s*", raw)
        return float(match.group(1)) if match else None

    width, height = number(root.attrib.get("width")), number(root.attrib.get("height"))
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view_box:
        parts = re.split(r"[\s,]+", view_box.strip())
        if len(parts) == 4:
            try:
                view_x, view_y = float(parts[0]), float(parts[1])
                view_width, view_height = float(parts[2]), float(parts[3])
            except ValueError:
                raise MediaCacheError("svg_dimensions_rejected") from None
            else:
                if (not -1_000_000 <= view_x <= 1_000_000 or
                        not -1_000_000 <= view_y <= 1_000_000 or
                        not 1 <= view_width <= 10000 or
                        not 1 <= view_height <= 10000 or
                        not 0.1 <= view_height / view_width <= 10):
                    raise MediaCacheError("svg_dimensions_rejected")
                width = width if width is not None else view_width
                height = height if height is not None else view_height
        else:
            raise MediaCacheError("svg_dimensions_rejected")
    if (width is None or height is None or not (1 <= width <= 10000) or
            not (1 <= height <= 10000) or not 0.1 <= height / width <= 10):
        raise MediaCacheError("svg_dimensions_rejected")
    count = 0
    for node in root.iter():
        count += 1
        if count > 10_000 or node.tag.rsplit("}", 1)[-1] not in SVG_TAGS:
            raise MediaCacheError("svg_active_content_rejected")
        for name, value in node.attrib.items():
            plain = name.rsplit("}", 1)[-1]
            lowered = value.casefold()
            if (plain not in SVG_ATTRIBUTES or len(value) > 100_000 or
                    "url(" in lowered or any(term in lowered for term in SVG_FORBIDDEN)):
                raise MediaCacheError("svg_active_content_rejected")
    max_dim = 1536
    scale = min(1.0, max_dim / width, max_dim / height)
    output_width = max(1, round(width * scale))
    output_height = max(1, round(height * scale))
    root.set("width", str(output_width))
    root.set("height", str(output_height))
    if view_box is None:
        root.set("viewBox", f"0 0 {width:g} {height:g}")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _sniff_mime(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    prefix = blob.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")[:512].lower()
    if b"<svg" in prefix and (prefix.startswith(b"<svg") or prefix.startswith(b"<?xml")):
        return "image/svg+xml"
    raise MediaCacheError("source_mime_mismatch")


def _rasterize_svg(blob: bytes) -> bytes:
    sanitized = _inert_svg(blob)
    try:
        result = subprocess.run(
            ["/usr/bin/rsvg-convert", "--format", "png", "--width", "1536",
             "--height", "1536", "--keep-aspect-ratio", "--no-keep-image-data", "-"],
            input=sanitized, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=4.0, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MediaCacheError("svg_rasterizer_unavailable") from None
    if result.returncode != 0 or len(result.stdout) > MAX_PREPARED_BYTES:
        raise MediaCacheError("svg_rasterization_failed")
    return result.stdout


def _normalize_image(blob: bytes, expected_mime: str, response_type: str) -> bytes:
    sniffed = _sniff_mime(blob)
    if sniffed != expected_mime or response_type != expected_mime:
        raise MediaCacheError("source_mime_mismatch")
    if sniffed == "image/svg+xml":
        blob = _rasterize_svg(blob)
        sniffed = _sniff_mime(blob)
    if sniffed not in {"image/png", "image/jpeg", "image/webp"}:
        raise MediaCacheError("source_mime_mismatch")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(blob)) as image:
                if image.format != {"image/png": "PNG", "image/jpeg": "JPEG",
                                    "image/webp": "WEBP"}[sniffed]:
                    raise MediaCacheError("source_mime_mismatch")
                if getattr(image, "n_frames", 1) != 1:
                    raise MediaCacheError("animated_image_rejected")
                if (image.width < 1 or image.height < 1 or image.width > MAX_DIMENSION or
                        image.height > MAX_DIMENSION or image.width * image.height > MAX_PIXELS):
                    raise MediaCacheError("image_dimensions_rejected")
                image.load()
                normalized = ImageOps.exif_transpose(image)
                mode = "RGBA" if "A" in normalized.getbands() else "RGB"
                normalized = normalized.convert(mode)
                output = BytesIO()
                if sniffed == "image/jpeg":
                    normalized.convert("RGB").save(
                        output, format="JPEG", quality=88, optimize=True, progressive=True)
                else:
                    normalized.save(output, format="PNG", optimize=True)
                prepared = output.getvalue()
    except MediaCacheError:
        raise
    except Exception:
        raise MediaCacheError("source_image_invalid") from None
    if len(prepared) > MAX_PREPARED_BYTES:
        raise MediaCacheError("prepared_image_too_large")
    return prepared


def _atomic_write(path: Path, blob: bytes) -> None:
    fd = -1
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_dir(path.parent)
    except OSError:
        raise MediaCacheError("cache_io_error") from None
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _quota_reserve(state_root: Path, object_path: Path, size: int) -> bool:
    quota_path = state_root / "quota.json"
    lock_path = state_root / "quota.lock"
    with _file_lock(lock_path):
        current = _read_json(quota_path)
        if current is None:
            used = 0
            objects_root = object_path.parents[1]
            for path in objects_root.glob("*/*"):
                if path.suffix not in {".png", ".jpg"}:
                    raise MediaCacheError("unsafe_cache_file")
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise MediaCacheError("unsafe_cache_file")
                used += info.st_size
            current = {"reserved_bytes": used}
        used = current.get("reserved_bytes")
        if not isinstance(used, int) or used < 0:
            raise MediaCacheError("cache_metadata_invalid")
        if object_path.exists():
            return False
        if used + size > MAX_CACHE_BYTES:
            raise MediaCacheError("cache_capacity_reached")
        _write_json_atomic(quota_path, {"reserved_bytes": used + size})
        return True


def _cache_object(cache_root: Path, state_root: Path, blob: bytes,
                  mime_type: str) -> tuple[str, Path]:
    if mime_type not in {"image/png", "image/jpeg"}:
        raise MediaCacheError("unsupported_prepared_type")
    digest = _sha(blob)
    object_dir = _private_child(_private_child(cache_root, "objects"), digest[:2])
    extension = ".jpg" if mime_type == "image/jpeg" else ".png"
    path = object_dir / f"{digest}{extension}"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                info.st_mode & 0o077 or info.st_size != len(blob) or
                _sha(path.read_bytes()) != digest):
            raise MediaCacheError("cache_object_corrupt")
        return digest, path
    reserved = _quota_reserve(state_root, path, len(blob))
    try:
        if not path.exists():
            _atomic_write(path, blob)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                info.st_size != len(blob) or _sha(path.read_bytes()) != digest):
            raise MediaCacheError("cache_object_corrupt")
    except BaseException:
        # Reservation is conservative if a process is interrupted. A normal
        # failure rolls it back while holding the same quota lock.
        if reserved:
            quota_path = state_root / "quota.json"
            with _file_lock(state_root / "quota.lock"):
                current = _read_json(quota_path) or {}
                used = current.get("reserved_bytes")
                if isinstance(used, int) and used >= len(blob):
                    _write_json_atomic(quota_path, {"reserved_bytes": used - len(blob)})
        raise
    return digest, path


def _binding_signature(binding: Mapping[str, str], source_url: str,
                       expected_mime: str, capture_sha: str | None) -> str:
    payload = {
        **binding,
        "source_url_sha256": _sha(source_url.encode("utf-8")),
        "expected_mime": expected_mime,
        "known_capture_sha256": capture_sha or "",
    }
    return _sha(json.dumps(payload, sort_keys=True,
                            separators=(",", ":")).encode("utf-8"))


def _cache_paths(cache_root: Path, binding: Mapping[str, str]) -> tuple[Path, Path, Path, Path]:
    root = _ensure_private_root(cache_root)
    state_root = _private_child(root, "state")
    binding_root = _private_child(root, "bindings")
    binding_dir = _private_child(binding_root, binding["media_id"][:9])
    _private_child(root, "objects")
    binding_path = binding_dir / f"{binding['media_id']}.json"
    lock_path = state_root / f"media-{binding['media_id']}.lock"
    return root, state_root, binding_path, lock_path


def _existing_result(root: Path, binding: Mapping[str, str], binding_path: Path,
                     signature: str) -> PreparedMedia | None:
    existing = _read_json(binding_path)
    if existing is None:
        return None
    if existing.get("binding_sha256") != signature:
        raise MediaCacheError("media_binding_changed")
    digest = existing.get("prepared_sha256")
    length = existing.get("byte_length")
    mime = existing.get("mime_type")
    if not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
        raise MediaCacheError("cache_metadata_invalid")
    if mime not in {"image/png", "image/jpeg"} or not isinstance(length, int) or length <= 0:
        raise MediaCacheError("cache_metadata_invalid")
    extension = ".jpg" if mime == "image/jpeg" else ".png"
    object_path = root / "objects" / digest[:2] / f"{digest}{extension}"
    try:
        info = object_path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                info.st_mode & 0o077 or info.st_size != length or
                _sha(object_path.read_bytes()) != digest):
            raise MediaCacheError("cache_object_corrupt")
    except FileNotFoundError:
        raise MediaCacheError("cache_object_missing") from None
    return PreparedMedia(binding["media_id"], "cached", digest, mime,
                         length, object_path, binding["rights_status"])


def _publish_prepared(row: Mapping[str, Any], binding: Mapping[str, str],
                      source_url: str, expected_mime: str,
                      cache_root: Path, prepared: bytes, status: str) -> PreparedMedia:
    capture_sha = row.get("known_capture_sha256")
    if capture_sha is not None and (
            not isinstance(capture_sha, str) or HEX64.fullmatch(capture_sha) is None):
        raise MediaCacheError("invalid_capture_reference")
    signature = _binding_signature(binding, source_url, expected_mime,
                                    capture_sha if isinstance(capture_sha, str) else None)
    root, state_root, binding_path, lock_path = _cache_paths(cache_root, binding)
    with _file_lock(lock_path):
        existing = _existing_result(root, binding, binding_path, signature)
        if existing is not None:
            return existing
        prepared_mime = _sniff_mime(prepared)
        if prepared_mime not in {"image/png", "image/jpeg"}:
            raise MediaCacheError("unsupported_prepared_type")
        digest, object_path = _cache_object(root, state_root, prepared, prepared_mime)
        _write_json_atomic(binding_path, {
            "media_id": binding["media_id"],
            "binding_sha256": signature,
            "prepared_sha256": digest,
            "mime_type": prepared_mime,
            "byte_length": len(prepared),
            "rights_status": binding["rights_status"],
        })
        return PreparedMedia(binding["media_id"], status, digest, prepared_mime,
                             len(prepared), object_path, binding["rights_status"])


def _get_or_prepare(media_row: Mapping[str, Any], *, cache_root: Path) -> PreparedMedia | None:
    """Return a validated raster cache object for one immutable media row.

    Rows already backed by the pinned menu asset set return
    ``status='already_prepared'`` and no path so the caller can use its existing
    menu asset route. Unsupported roles return ``None``; unsafe rows and fetch
    failures raise ``MediaCacheError`` with a fixed URL-free code.
    """
    role = media_row.get("role")
    if role not in SUPPORTED_ROLES:
        return None
    binding = _safe_media_binding(media_row)
    host, source_url, expected_mime = _validated_url(media_row)
    capture_sha = media_row.get("known_capture_sha256")
    if capture_sha is not None and (
            not isinstance(capture_sha, str) or HEX64.fullmatch(capture_sha) is None):
        raise MediaCacheError("invalid_capture_reference")
    prepared_sha = media_row.get("prepared_sha256")
    if prepared_sha is not None:
        if (media_row.get("prepared_source") != "menu_asset" or
                not isinstance(prepared_sha, str) or HEX64.fullmatch(prepared_sha) is None or
                media_row.get("prepared_mime") != "image/png" or
                not isinstance(media_row.get("prepared_bytes"), int) or
                media_row["prepared_bytes"] <= 0):
            raise MediaCacheError("invalid_prepared_reference")
        return PreparedMedia(binding["media_id"], "already_prepared", prepared_sha,
                             "image/png", media_row["prepared_bytes"], None,
                             binding["rights_status"])

    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    root, state_root, binding_path, lock_path = _cache_paths(cache_root, binding)
    signature = _binding_signature(
        binding, source_url, expected_mime,
        capture_sha if isinstance(capture_sha, str) else None)
    with _file_lock(lock_path, timeout=min(
            HOST_QUEUE_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic()))):
        existing = _existing_result(root, binding, binding_path, signature)
        if existing is not None:
            return existing
        blob = _fetch_source(source_url, host, expected_mime, state_root,
                             deadline=deadline)
        prepared = _normalize_image(blob, expected_mime, expected_mime)
        prepared_mime = _sniff_mime(prepared)
        if prepared_mime not in {"image/png", "image/jpeg"}:
            raise MediaCacheError("unsupported_prepared_type")
        digest, object_path = _cache_object(root, state_root, prepared, prepared_mime)
        _write_json_atomic(binding_path, {
            "media_id": binding["media_id"],
            "binding_sha256": signature,
            "prepared_sha256": digest,
            "mime_type": prepared_mime,
            "byte_length": len(prepared),
            "rights_status": binding["rights_status"],
        })
        return PreparedMedia(binding["media_id"], "prepared", digest, prepared_mime,
                             len(prepared), object_path, binding["rights_status"])


def get_or_prepare(media_row: Mapping[str, Any], *, cache_root: Path) -> PreparedMedia | None:
    """Map internal OS/decoder errors to a stable URL-free cache failure."""
    try:
        return _get_or_prepare(media_row, cache_root=cache_root)
    except MediaCacheError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise MediaCacheError("media_cache_failed") from None


def seed_verified_capture(media_row: Mapping[str, Any], source_bytes: bytes, *,
                          expected_source_sha256: str,
                          cache_root: Path) -> PreparedMedia:
    """Seed a pinned, separately verified source capture without another GET."""
    try:
        binding = _safe_media_binding(media_row)
        _host, source_url, expected_mime = _validated_url(media_row)
        declared_sha = media_row.get("known_capture_sha256")
        if (not isinstance(expected_source_sha256, str) or
                HEX64.fullmatch(expected_source_sha256) is None or
                declared_sha != expected_source_sha256 or len(source_bytes) > MAX_SOURCE_BYTES or
                _sha(source_bytes) != expected_source_sha256):
            raise MediaCacheError("capture_hash_mismatch")
        prepared = _normalize_image(source_bytes, expected_mime, expected_mime)
        return _publish_prepared(media_row, binding, source_url, expected_mime,
                                 cache_root, prepared, "seeded")
    except MediaCacheError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise MediaCacheError("media_cache_failed") from None
