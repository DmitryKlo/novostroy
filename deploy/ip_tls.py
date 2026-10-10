#!/usr/bin/env python3
"""Phased, non-destructive host edge setup for Novostroy on one IPv4 address.

Run on the verified Ubuntu host through Teleport. This script never installs
packages, edits Compose, changes firewall rules, prints certificate keys, or
overwrites a file it does not recognize as its own.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


TARGET_IP = "79.141.74.15"
APP_HEALTH_URL = "http://127.0.0.1:8080/healthz"
APP_UPSTREAM = "127.0.0.1:8080"
CERT_NAME = "novostroy-ip"
CERTBOT = Path("/opt/novostroy-certbot/bin/certbot")
WEBROOT = Path("/var/www/novostroy-acme")
STATE_DIR = Path("/var/lib/novostroy-ip-tls")
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = Path("/run/lock/novostroy-ip-tls.lock")
SITE_AVAILABLE = Path("/etc/nginx/sites-available/novostroy-ip.conf")
SITE_ENABLED = Path("/etc/nginx/sites-enabled/novostroy-ip.conf")
PRODUCTION_LIVE = Path(f"/etc/letsencrypt/live/{CERT_NAME}")
STAGING_ROOT = STATE_DIR / "staging"
DEPLOY_HOOK = Path("/usr/local/sbin/novostroy-ip-tls-deploy-hook")
SYSTEMD_SERVICE = Path("/etc/systemd/system/novostroy-ip-tls-renew.service")
SYSTEMD_TIMER = Path("/etc/systemd/system/novostroy-ip-tls-renew.timer")
STATE_SCHEMA = "novostroy-ip-tls-state/1"
MANAGED_BY = "deploy/ip_tls.py"
WEBROOT_MARKER = ".novostroy-ip-tls-owned"


HTTP_SITE = f"""# Managed by {MANAGED_BY}; challenge-only until activate-https.
server {{
    listen 80;
    server_name {TARGET_IP};

    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}

    location / {{
        return 404;
    }}
}}
"""

HTTPS_SITE = f"""# Managed by {MANAGED_BY}; public app proxy enabled after health check.
server {{
    listen 80;
    server_name {TARGET_IP};

    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name {TARGET_IP};

    ssl_certificate {PRODUCTION_LIVE}/fullchain.pem;
    ssl_certificate_key {PRODUCTION_LIVE}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {{
        proxy_pass http://{APP_UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""

HOOK_TEXT = """#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
"""

RENEW_SERVICE_TEXT = f"""[Unit]
Description=Renew Novostroy public IP TLS certificate
Wants=network-online.target
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart={CERTBOT} --config-dir /etc/letsencrypt --work-dir /var/lib/letsencrypt --logs-dir /var/log/letsencrypt renew --cert-name {CERT_NAME} --deploy-hook {DEPLOY_HOOK}
"""

RENEW_TIMER_TEXT = """[Unit]
Description=Check the Novostroy IP certificate for renewal

[Timer]
OnCalendar=*-*-* 00,06,12,18:17:00
RandomizedDelaySec=20m
Persistent=true
Unit=novostroy-ip-tls-renew.service

[Install]
WantedBy=timers.target
"""


class SetupError(RuntimeError):
    """A precondition failed; no unsafe fallback is attempted."""


def run(argv: Sequence[str], *, capture: bool = False, timeout: int = 60,
        check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(argv), text=True, stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None, check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Could not run {argv[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stdout or "").strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise SetupError(f"Command failed ({result.returncode}): {argv[0]}"
                         + (f"\n{detail}" if detail else ""))
    return result


def require_root() -> None:
    if os.geteuid() != 0:
        raise SetupError("Run this host phase as root (for example, with sudo).")


def require_target_confirmation(value: str | None) -> None:
    if value != TARGET_IP:
        raise SetupError(f"Write phases require --confirm-ip {TARGET_IP}.")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def check_os() -> None:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError as exc:
        raise SetupError(f"Cannot read /etc/os-release: {exc}") from exc
    if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
        raise SetupError("This staged host script is restricted to Ubuntu 24.04.")


def certbot_version() -> tuple[int, int, int]:
    if not CERTBOT.is_file() or not os.access(CERTBOT, os.X_OK):
        raise SetupError(f"Expected Certbot venv executable is missing: {CERTBOT}")
    result = run([str(CERTBOT), "--version"], capture=True, timeout=15)
    match = re.search(r"certbot\s+(\d+)\.(\d+)\.(\d+)", result.stdout or "", re.I)
    if not match:
        raise SetupError("Could not parse Certbot version; refusing IP issuance.")
    version = tuple(int(part) for part in match.groups())
    if version < (5, 4, 0) or version >= (6, 0, 0):
        raise SetupError(f"Certbot {'.'.join(map(str, version))} is outside the tested range >=5.4,<6.")
    return version


def command_exists(command: str) -> str:
    found = shutil.which(command)
    if not found:
        raise SetupError(f"Required command is missing: {command}")
    return found


def _read_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists() and not STATE_FILE.is_symlink():
        return None
    info = STATE_FILE.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or STATE_FILE.is_symlink():
        raise SetupError(f"State path is not a root-owned regular file: {STATE_FILE}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SetupError(f"State file must have mode 0600: {STATE_FILE}")
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Cannot parse state file: {exc}") from exc
    if (not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA
            or state.get("managed_by") != MANAGED_BY
            or state.get("target_ip") != TARGET_IP
            or not isinstance(state.get("phases"), dict)):
        raise SetupError("Existing state is not owned by this script; refusing to adopt it.")
    return state


def _ensure_state_dir() -> None:
    if STATE_DIR.exists() or STATE_DIR.is_symlink():
        info = STATE_DIR.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or STATE_DIR.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077):
            raise SetupError(f"Unsafe state directory; expected root-owned private directory: {STATE_DIR}")
    else:
        STATE_DIR.mkdir(mode=0o700, parents=False)
        os.chown(STATE_DIR, 0, 0)


def _blank_state() -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "managed_by": MANAGED_BY,
            "target_ip": TARGET_IP, "phases": {}}


def _write_state(state: dict[str, Any]) -> None:
    _ensure_state_dir()
    encoded = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", dir=STATE_DIR)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, STATE_FILE)
        dir_fd = os.open(STATE_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_or_start_state() -> dict[str, Any]:
    _ensure_state_dir()
    state = _read_state()
    if state is not None:
        return state
    # Never claim an existing site/certificate/webroot without our state marker.
    for path in (SITE_AVAILABLE, SITE_ENABLED, PRODUCTION_LIVE, WEBROOT):
        if path.exists() or path.is_symlink():
            raise SetupError(f"Existing path without this script's state; refusing adoption: {path}")
    state = _blank_state()
    _write_state(state)
    return state


def _set_phase(state: dict[str, Any], phase: str, value: bool = True) -> None:
    phases = state["phases"]
    if not isinstance(phases, dict):
        raise SetupError("Invalid state phases object.")
    if value:
        phases[phase] = utc_now()
    else:
        phases.pop(phase, None)
    _write_state(state)


def _state() -> dict[str, Any]:
    state = _read_state()
    if state is None:
        raise SetupError("No managed state yet; run stage-http first.")
    return state


def _ensure_regular_owned_file(path: Path, content: bytes, mode: int,
                               allowed_old: tuple[bytes, ...] = ()) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise SetupError(f"Refusing to replace non-root-owned regular file: {path}")
        current = path.read_bytes()
        if current == content:
            if stat.S_IMODE(info.st_mode) != mode:
                os.chmod(path, mode)
            return
        if current not in allowed_old:
            raise SetupError(f"Existing file differs from this script's known contents: {path}")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, mode)
            os.fchown(fd, 0, 0)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _ensure_owned_symlink(path: Path, target: Path) -> None:
    if path.is_symlink():
        if os.readlink(path) != str(target):
            raise SetupError(f"Refusing to replace foreign symlink: {path}")
        return
    if path.exists():
        raise SetupError(f"Refusing to replace existing non-symlink: {path}")
    os.symlink(str(target), path)


def _ensure_webroot() -> None:
    marker = WEBROOT / WEBROOT_MARKER
    if WEBROOT.exists() or WEBROOT.is_symlink():
        info = WEBROOT.lstat()
        if WEBROOT.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise SetupError(f"Unsafe ACME webroot path: {WEBROOT}")
        if marker.exists() or marker.is_symlink():
            marker_info = marker.lstat()
            if (marker.is_symlink() or not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != 0
                    or marker.read_text(encoding="utf-8") != f"{MANAGED_BY}\n"):
                raise SetupError(f"ACME webroot ownership marker is foreign or changed: {marker}")
        else:
            if any(WEBROOT.iterdir()):
                raise SetupError(f"Existing unmarked webroot is not empty; refusing adoption: {WEBROOT}")
            _ensure_regular_owned_file(marker, f"{MANAGED_BY}\n".encode(), 0o644)
    else:
        WEBROOT.mkdir(mode=0o755, parents=False)
        os.chown(WEBROOT, 0, 0)
        _ensure_regular_owned_file(marker, f"{MANAGED_BY}\n".encode(), 0o644)
    for path in (WEBROOT / ".well-known", WEBROOT / ".well-known/acme-challenge"):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                raise SetupError(f"Unsafe ACME challenge directory: {path}")
        else:
            path.mkdir(mode=0o755)
            os.chown(path, 0, 0)


def _lock():
    LOCK_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LOCK_FILE, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        os.close(fd)
        raise SetupError(f"Unsafe lock file: {LOCK_FILE}")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _listeners(port: int) -> list[str]:
    ss = command_exists("ss")
    result = run([ss, "-H", "-ltnp", f"sport = :{port}"], capture=True, timeout=10)
    return [line for line in (result.stdout or "").splitlines() if line.strip()]


def _check_port_owned_by_nginx(port: int, *, allow_nginx: bool) -> None:
    lines = _listeners(port)
    if not lines:
        return
    if not allow_nginx:
        raise SetupError(f"TCP/{port} has a listener before this phase; refusing to take it over.")
    for line in lines:
        process_names = re.findall(r'\(\("([^"]+)"', line)
        if not process_names or any(name != "nginx" for name in process_names):
            raise SetupError(f"TCP/{port} is not exclusively owned by Nginx; refusing to proceed.")


def _nginx_active() -> bool:
    systemctl = command_exists("systemctl")
    return run([systemctl, "is-active", "--quiet", "nginx"], check=False,
               timeout=10).returncode == 0


def _nginx_reload() -> None:
    if not _nginx_active():
        raise SetupError("Nginx is not active; start it through the reviewed SI bootstrap first.")
    nginx = command_exists("nginx")
    systemctl = command_exists("systemctl")
    run([nginx, "-t"], timeout=30)
    run([systemctl, "reload", "nginx"], timeout=30)


def _nginx_start_or_reload() -> None:
    """Start a deliberately stopped package service, or reload the active one."""
    nginx = command_exists("nginx")
    systemctl = command_exists("systemctl")
    run([nginx, "-t"], timeout=30)
    if _nginx_active():
        run([systemctl, "reload", "nginx"], timeout=30)
    else:
        run([systemctl, "start", "nginx"], timeout=30)
        if not _nginx_active():
            raise SetupError("Nginx did not become active after loading the challenge-only site.")


def _ensure_site(content: str, state: dict[str, Any]) -> None:
    if not SITE_AVAILABLE.parent.is_dir() or not SITE_ENABLED.parent.is_dir():
        raise SetupError("Ubuntu Nginx sites-available/sites-enabled directories are missing.")
    allowed: tuple[bytes, ...] = ()
    if state["phases"].get("http_staged"):
        allowed = (HTTP_SITE.encode(), HTTPS_SITE.encode())
    _ensure_regular_owned_file(SITE_AVAILABLE, content.encode(), 0o644, allowed)
    _ensure_owned_symlink(SITE_ENABLED, SITE_AVAILABLE)


def _certbot_path() -> str:
    version = certbot_version()
    print(f"Certbot {'.'.join(map(str, version))} OK")
    return str(CERTBOT)


def _webroot_marker_valid() -> bool:
    marker = WEBROOT / WEBROOT_MARKER
    return (marker.is_file() and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == f"{MANAGED_BY}\n")


def _http_challenge_smoke() -> None:
    """Check the local Nginx webroot mapping without exposing/app health."""
    if not _webroot_marker_valid():
        raise SetupError("ACME webroot ownership marker is absent or changed.")
    challenge_dir = WEBROOT / ".well-known/acme-challenge"
    token = f"novostroy-local-check-{os.getpid()}"
    marker = hashlib.sha256(f"{TARGET_IP}:{utc_now()}".encode()).hexdigest()
    test_path = challenge_dir / token
    fd = os.open(test_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(marker)
    except Exception:
        try:
            test_path.unlink()
        except OSError:
            pass
        raise
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"http://127.0.0.1/.well-known/acme-challenge/{token}",
            headers={"Host": TARGET_IP},
        )
        with opener.open(request, timeout=8) as response:
            body = response.read(256)
            expected_url = f"http://127.0.0.1/.well-known/acme-challenge/{token}"
            if (response.status != 200 or response.geturl() != expected_url
                    or body != marker.encode("ascii")):
                raise SetupError("Local Nginx ACME challenge path did not return the exact probe body.")
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"Local Nginx ACME challenge probe failed: {exc}") from exc
    finally:
        test_path.unlink(missing_ok=True)


def _assert_challenge_only_root() -> None:
    """Ensure requests for the target IP do not hit a default Nginx site."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request("http://127.0.0.1/", headers={"Host": TARGET_IP})
    try:
        with opener.open(request, timeout=8) as response:
            if response.status != 404 or response.geturl() != "http://127.0.0.1/":
                raise SetupError("Target IP HTTP root did not remain challenge-only (expected 404).")
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or exc.geturl() != "http://127.0.0.1/":
            raise SetupError("Target IP HTTP root did not remain challenge-only (expected 404).") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"Target IP HTTP root probe failed: {exc}") from exc


def _cert_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    live = root / "live" / CERT_NAME
    return live / "cert.pem", live / "chain.pem", live / "fullchain.pem", live / "privkey.pem"


def _verify_lineage_file(path: Path, root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise SetupError(f"Expected Certbot lineage file is missing: {path}") from exc
    if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
        raise SetupError(f"Certbot lineage file points outside its managed root: {path}")


def _verify_ip_cert(cert: Path, chain: Path, *, root: Path, verify_trust: bool) -> None:
    _verify_lineage_file(cert, root)
    _verify_lineage_file(chain, root)
    openssl = command_exists("openssl")
    run([openssl, "x509", "-in", str(cert), "-noout", "-checkip", TARGET_IP],
        capture=True, timeout=15)
    run([openssl, "x509", "-in", str(cert), "-noout", "-checkend", "3600"],
        capture=True, timeout=15)
    if verify_trust:
        run([openssl, "verify", "-CApath", "/etc/ssl/certs", "-untrusted", str(chain),
             "-verify_ip", TARGET_IP, str(cert)], capture=True, timeout=15)


def _prepare_staging_dirs() -> None:
    staging_config = STAGING_ROOT / "config"
    staging_work = STAGING_ROOT / "work"
    staging_logs = STAGING_ROOT / "logs"
    for path in (STAGING_ROOT, staging_config, staging_work, staging_logs):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                raise SetupError(f"Unsafe staging directory: {path}")
        else:
            path.mkdir(mode=0o700, parents=False)
            os.chown(path, 0, 0)


def _issue_args(*, staging: bool, certbot: str) -> list[str]:
    if staging:
        staging_config = STAGING_ROOT / "config"
        staging_work = STAGING_ROOT / "work"
        staging_logs = STAGING_ROOT / "logs"
        prefix = [certbot, "--config-dir", str(staging_config),
                  "--work-dir", str(staging_work), "--logs-dir", str(staging_logs)]
    else:
        prefix = [certbot, "--config-dir", "/etc/letsencrypt",
                  "--work-dir", "/var/lib/letsencrypt",
                  "--logs-dir", "/var/log/letsencrypt"]
    args = prefix + ["certonly", "--non-interactive", "--agree-tos",
                     "--register-unsafely-without-email", "--preferred-profile", "shortlived",
                     "--webroot", "--webroot-path", str(WEBROOT),
                     "--ip-address", TARGET_IP, "--cert-name", CERT_NAME]
    if staging:
        args.append("--staging")
        args.append("--force-renewal")
    return args


def do_preflight() -> None:
    check_os()
    for command in ("ss", "nginx", "systemctl", "openssl"):
        command_exists(command)
    version = certbot_version()
    state = _read_state()
    status = state["phases"] if state else {}
    print(json.dumps({
        "os": "Ubuntu 24.04",
        "target_ip_confirmation_required_for_mutations": TARGET_IP,
        "certbot_version": ".".join(map(str, version)),
        "nginx_active": _nginx_active(),
        "tcp_80_listeners": _listeners(80),
        "tcp_443_listeners": _listeners(443),
        "state_phases": sorted(status),
        "no_vm_or_firewall_changes_by_preflight": True,
    }, indent=2))


def do_stage_http() -> None:
    check_os()
    state = _load_or_start_state()
    if state["phases"].get("http_staged"):
        if SITE_AVAILABLE.read_text(encoding="utf-8") not in (HTTP_SITE, HTTPS_SITE):
            raise SetupError("Managed Nginx site changed since staging; refusing to overwrite.")
        _http_challenge_smoke()
        if not state["phases"].get("https_active"):
            _assert_challenge_only_root()
        print("HTTP-01 challenge-only site already staged; local probe passed.")
        return
    _check_port_owned_by_nginx(80, allow_nginx=True)
    _check_port_owned_by_nginx(443, allow_nginx=False)
    _ensure_webroot()
    _ensure_site(HTTP_SITE, state)
    _nginx_start_or_reload()
    _http_challenge_smoke()
    _assert_challenge_only_root()
    _set_phase(state, "http_staged")
    print("HTTP-01 challenge-only site staged; all non-challenge HTTP paths return 404.")


def do_issue_staging() -> None:
    state = _state()
    if not state["phases"].get("http_staged"):
        raise SetupError("Run stage-http before requesting the staging certificate.")
    _check_port_owned_by_nginx(80, allow_nginx=True)
    _http_challenge_smoke()
    _prepare_staging_dirs()
    certbot = _certbot_path()
    if state["phases"].get("staging_validated"):
        cert, chain, _, _ = _cert_paths(STAGING_ROOT / "config")
        _verify_ip_cert(cert, chain, root=STAGING_ROOT / "config", verify_trust=False)
        print("Let's Encrypt staging HTTP-01 validation already succeeded; proof is current.")
        return
    state["phases"]["staging_attempt_started"] = utc_now()
    _write_state(state)
    run(_issue_args(staging=True, certbot=certbot), timeout=300)
    cert, chain, _, _ = _cert_paths(STAGING_ROOT / "config")
    _verify_ip_cert(cert, chain, root=STAGING_ROOT / "config", verify_trust=False)
    _set_phase(state, "staging_validated")
    print("Let's Encrypt staging certificate validated the public HTTP-01 path.")


def do_issue_production() -> None:
    state = _state()
    if not state["phases"].get("staging_validated"):
        raise SetupError("Production is gated on successful external Let's Encrypt staging HTTP-01 validation.")
    if not state["phases"].get("http_staged"):
        raise SetupError("HTTP challenge site is not staged.")
    _check_port_owned_by_nginx(80, allow_nginx=True)
    _http_challenge_smoke()
    certbot = _certbot_path()
    cert, chain, _, _ = _cert_paths(Path("/etc/letsencrypt"))
    if state["phases"].get("production_issued"):
        _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
        print("Trusted production IP certificate already verified.")
        return
    if not state["phases"].get("production_attempt_started"):
        if PRODUCTION_LIVE.exists() or PRODUCTION_LIVE.is_symlink():
            raise SetupError("Production lineage exists without this script's issuance marker; refusing to overwrite/adopt it.")
        state["phases"]["production_attempt_started"] = utc_now()
        _write_state(state)
    elif cert.is_file() and chain.is_file():
        # Handles a process interruption after successful ACME issuance but before state commit.
        try:
            _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
        except SetupError as exc:
            raise SetupError(
                "A production lineage exists from the interrupted attempt but did not verify; "
                "refusing another issuance request. Review it manually before retrying."
            ) from exc
        else:
            _set_phase(state, "production_issued")
            print("Recovered a verified production issuance from the interrupted prior phase.")
            return
    run(_issue_args(staging=False, certbot=certbot), timeout=300)
    _, _, fullchain, privkey = _cert_paths(Path("/etc/letsencrypt"))
    if not fullchain.is_file() or not privkey.is_file():
        raise SetupError("Certbot completed but expected production lineage files are missing.")
    _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
    _set_phase(state, "production_issued")
    print("Trusted production IP certificate issued and locally chain/IP verified; key was not displayed.")


def do_activate_https() -> None:
    state = _state()
    if not state["phases"].get("production_issued"):
        raise SetupError("HTTPS activation is gated on a verified production certificate.")
    cert, chain, fullchain, privkey = _cert_paths(Path("/etc/letsencrypt"))
    _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
    if not fullchain.is_file() or not privkey.is_file():
        raise SetupError("Production certificate/key files are missing.")
    _check_port_owned_by_nginx(443, allow_nginx=bool(state["phases"].get("https_active")))
    _check_port_owned_by_nginx(80, allow_nginx=True)
    # App remains private on HTTP until the app owner's deployed health passes.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(APP_HEALTH_URL, timeout=8) as response:
            body = response.read(4096)
            if response.status != 200:
                raise SetupError(f"App health endpoint returned HTTP {response.status}.")
            if not body:
                raise SetupError("App health endpoint returned an empty body.")
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"App health check failed; HTTPS site remains unactivated: {exc}") from exc
    if state["phases"].get("https_active"):
        if SITE_AVAILABLE.read_text(encoding="utf-8") != HTTPS_SITE:
            raise SetupError("Active site config changed; refusing to overwrite.")
        _nginx_reload()
        print("HTTPS site already active; health and Nginx configuration revalidated.")
        return
    _ensure_site(HTTPS_SITE, state)
    _nginx_reload()
    _set_phase(state, "https_active")
    print("HTTPS proxy activated after local app health passed; external HTTPS still needs verification.")


def do_enable_renewal() -> None:
    state = _state()
    if not state["phases"].get("production_issued"):
        raise SetupError("Renewal timer requires a verified production certificate.")
    certbot = _certbot_path()
    cert, chain, fullchain, privkey = _cert_paths(Path("/etc/letsencrypt"))
    _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
    if not fullchain.is_file() or not privkey.is_file():
        raise SetupError("Production certificate/key files are missing.")
    if not _nginx_active():
        raise SetupError("Nginx must be active before installing a deploy hook.")
    _ensure_regular_owned_file(DEPLOY_HOOK, HOOK_TEXT.encode(), 0o755)
    _ensure_regular_owned_file(SYSTEMD_SERVICE, RENEW_SERVICE_TEXT.encode(), 0o644)
    _ensure_regular_owned_file(SYSTEMD_TIMER, RENEW_TIMER_TEXT.encode(), 0o644)
    run([command_exists("systemctl"), "daemon-reload"], timeout=30)
    # The lineage already stores its webroot/IP/profile options. Pass the hook
    # explicitly and scope both test and timer to this single managed lineage.
    run([certbot, "--config-dir", "/etc/letsencrypt", "--work-dir", "/var/lib/letsencrypt",
         "--logs-dir", "/var/log/letsencrypt", "renew", "--cert-name", CERT_NAME,
         "--deploy-hook", str(DEPLOY_HOOK), "--dry-run", "--run-deploy-hooks"], timeout=300)
    run([command_exists("systemctl"), "enable", "--now", "novostroy-ip-tls-renew.timer"], timeout=60)
    timer_active = run([command_exists("systemctl"), "is-active", "--quiet",
                        "novostroy-ip-tls-renew.timer"], check=False, timeout=10).returncode == 0
    if not timer_active:
        raise SetupError("Renewal timer was not active after enable --now.")
    _set_phase(state, "renewal_enabled")
    print("Scoped Certbot renewal dry-run and deploy hook passed; systemd timer enabled.")


def do_status() -> None:
    check_os()
    state = _read_state()
    phases = state["phases"] if state else {}
    output: dict[str, Any] = {
        "target_ip": TARGET_IP,
        "state_phases": phases,
        "nginx_active": _nginx_active(),
        "tcp_80_listeners": _listeners(80),
        "tcp_443_listeners": _listeners(443),
    }
    if state and phases.get("production_issued"):
        cert, chain, fullchain, privkey = _cert_paths(Path("/etc/letsencrypt"))
        _verify_ip_cert(cert, chain, root=Path("/etc/letsencrypt"), verify_trust=True)
        output["production_certificate"] = "trusted chain and IP SAN verify"
        output["private_key_present"] = privkey.is_file()
        output["fullchain_present"] = fullchain.is_file()
    print(json.dumps(output, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "stage-http", "issue-staging",
                                          "issue-production", "activate-https",
                                          "enable-renewal", "status"))
    parser.add_argument("--confirm-ip", help=f"Explicitly confirm target {TARGET_IP} for write phases.")
    args = parser.parse_args(argv)
    try:
        require_root()
        if args.phase not in ("preflight", "status"):
            require_target_confirmation(args.confirm_ip)
        if args.phase == "preflight":
            do_preflight()
            return 0
        if args.phase == "status":
            do_status()
            return 0
        lock_fd = _lock()
        try:
            if args.phase == "stage-http":
                do_stage_http()
            elif args.phase == "issue-staging":
                do_issue_staging()
            elif args.phase == "issue-production":
                do_issue_production()
            elif args.phase == "activate-https":
                do_activate_https()
            elif args.phase == "enable-renewal":
                do_enable_renewal()
        finally:
            os.close(lock_fd)
        return 0
    except (SetupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
