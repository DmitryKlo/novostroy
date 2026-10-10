#!/usr/bin/env python3
"""Configure private Docker registry auth and push two reviewed Novostroy images.

Registry credentials are written only by the official ``dp auth configure-kaniko``
command to a private Docker config outside the repository. This script never
prints config contents, command output, or registry credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__:
    from .spirit_deploy_access import SafeAccessError, ensure_private_dir, isolated_dp_environment, resolve_dp
else:
    from spirit_deploy_access import SafeAccessError, ensure_private_dir, isolated_dp_environment, resolve_dp


REGISTRY_HOST = "registry.spirit.culab.ru"
REGISTRY_NAMESPACE = "hack-l2sh-2"
REGISTRY_CONFIG_DIR = Path.home() / ".config" / "novostroy" / "spirit" / "registry-docker"
REGISTRY_CONFIG_FILE = REGISTRY_CONFIG_DIR / "config.json"
DEFAULT_DOCKER = Path(__file__).resolve().parents[1] / ".runtime/rootless-docker/docker-29.9.0/bin/docker"
DEFAULT_PUSH_TIMEOUT = 1800
MAX_CONFIG_BYTES = 64 * 1024
SOURCE_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class RegistryPushError(Exception):
    """A safe, bounded user-facing error with no subprocess output."""


def _safe_config_path(*, replace: bool = False) -> Path:
    ensure_private_dir(REGISTRY_CONFIG_DIR)
    try:
        info = REGISTRY_CONFIG_FILE.lstat()
    except FileNotFoundError:
        return REGISTRY_CONFIG_FILE
    except OSError as exc:
        raise RegistryPushError("Could not inspect the private Docker config path.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RegistryPushError("Private Docker config is not a regular file; refusing to use it.")
    if info.st_uid != os.getuid():
        raise RegistryPushError("Private Docker config is not owned by the current user.")
    if not replace:
        raise RegistryPushError("A private registry config already exists; use --replace only to refresh it.")
    try:
        REGISTRY_CONFIG_FILE.unlink()
    except OSError as exc:
        raise RegistryPushError("Could not replace the private Docker config.") from exc
    return REGISTRY_CONFIG_FILE


def _validate_config_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RegistryPushError("Registry credential command did not create the expected config file.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RegistryPushError("Generated Docker config failed ownership/type checks.")
    try:
        path.chmod(0o600)
        info = path.stat()
    except OSError as exc:
        raise RegistryPushError("Could not restrict Docker config permissions.") from exc
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size < 2 or info.st_size > MAX_CONFIG_BYTES:
        raise RegistryPushError("Generated Docker config has unsafe permissions or size.")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                raise RegistryPushError("Generated Docker config changed during validation.")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise RegistryPushError("Generated Docker config could not be safely inspected.") from exc
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise RegistryPushError("Generated Docker config exceeds the size limit.")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryPushError("Generated Docker config is not valid JSON.") from exc
    if not _has_registry_auth(document):
        raise RegistryPushError("Generated Docker config has no credentials entry for the configured registry.")


def _has_registry_auth(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    auths = document.get("auths")
    if not isinstance(auths, dict):
        return False
    for registry, details in auths.items():
        if not isinstance(registry, str) or not isinstance(details, dict):
            continue
        candidate = registry if "://" in registry else f"https://{registry}"
        try:
            hostname = (urlsplit(candidate).hostname or "").casefold()
        except ValueError:
            continue
        if hostname == REGISTRY_HOST and any(
            isinstance(details.get(key), str) and details.get(key)
            for key in ("auth", "identitytoken", "registrytoken")
        ):
            return True
    return False


def configure_registry_auth(dp_bin: str | None, *, replace: bool = False) -> int:
    try:
        destination = _safe_config_path(replace=replace)
        executable = resolve_dp(dp_bin)
        result = subprocess.run(
            [str(executable), "auth", "configure-kaniko", "--config-path", str(destination)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
            env=isolated_dp_environment(),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        print("Registry credential setup timed out; CLI output was suppressed.", file=sys.stderr)
        return 2
    except OSError:
        print("Could not start the registry credential command; output was suppressed.", file=sys.stderr)
        return 2
    except (SafeAccessError, RegistryPushError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if result.returncode != 0:
        print(f"Registry credential setup failed (exit code {result.returncode}); CLI output was suppressed.",
              file=sys.stderr)
        return result.returncode or 1
    try:
        _validate_config_file(destination)
    except RegistryPushError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Private registry Docker config is ready at {destination} (directory 0700, file 0600).")
    return 0


def _docker_host(value: str | None) -> str:
    host = value or os.environ.get("DOCKER_HOST", "")
    if not host.startswith("unix://"):
        raise RegistryPushError("Set DOCKER_HOST to this run's private rootless Docker socket.")
    socket_path = Path(host.removeprefix("unix://"))
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not socket_path.is_absolute() or socket_path.name != "docker.sock":
        raise RegistryPushError("DOCKER_HOST is not a valid private rootless Docker socket path.")
    expected_parent = runtime_dir / socket_path.parent.name
    if (socket_path.parent.parent != runtime_dir or
            not socket_path.parent.name.startswith("novostroy-rootless-docker-") or
            expected_parent != socket_path.parent):
        raise RegistryPushError("DOCKER_HOST is outside the isolated Novostroy rootless runtime path.")
    try:
        info = socket_path.lstat()
    except OSError as exc:
        raise RegistryPushError("The recorded rootless Docker socket is unavailable.") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RegistryPushError("Docker socket type or owner check failed.")
    return host


def _docker_path(value: str | None) -> Path:
    path = Path(value).expanduser() if value else DEFAULT_DOCKER
    try:
        info = path.stat()
    except OSError as exc:
        raise RegistryPushError("The selected Docker CLI is unavailable.") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise RegistryPushError("The selected Docker CLI is not executable.")
    return path


def _source_image(value: str) -> str:
    if value.startswith("-") or not SOURCE_IMAGE_RE.fullmatch(value):
        raise RegistryPushError("A local source image reference is invalid.")
    return value


def _private_config_ready() -> None:
    if not REGISTRY_CONFIG_FILE.is_file() or REGISTRY_CONFIG_FILE.is_symlink():
        raise RegistryPushError("Private registry config is missing; run `configure` first.")
    try:
        info = REGISTRY_CONFIG_FILE.stat()
    except OSError as exc:
        raise RegistryPushError("Private registry config is unavailable.") from exc
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RegistryPushError("Private registry config ownership or permissions are unsafe.")
    _validate_config_file(REGISTRY_CONFIG_FILE)


def _run_docker(docker: Path, host: str, args: list[str], *, timeout: int) -> bool:
    command = [str(docker), "--config", str(REGISTRY_CONFIG_DIR), "--host", host, *args]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def push_images(*, source_sha: str, api_image: str, web_image: str,
                docker_bin: str | None, docker_host: str | None, timeout: int) -> int:
    if not SHA_RE.fullmatch(source_sha):
        raise RegistryPushError("Source SHA must be a full 40-character lowercase commit SHA.")
    api_image = _source_image(api_image)
    web_image = _source_image(web_image)
    if api_image == web_image:
        raise RegistryPushError("API and web source image references must be different.")
    _private_config_ready()
    docker = _docker_path(docker_bin)
    host = _docker_host(docker_host)
    if not _run_docker(docker, host, ["info"], timeout=20):
        raise RegistryPushError("Rootless Docker daemon is unavailable; tag/push was not attempted.")

    sources = (("api", api_image), ("web", web_image))
    for component, image in sources:
        if not _run_docker(docker, host, ["image", "inspect", image], timeout=30):
            raise RegistryPushError(f"Local {component} image is missing; no registry push was attempted.")

    targets = (
        ("api", api_image, f"{REGISTRY_HOST}/{REGISTRY_NAMESPACE}/novostroy-api:{source_sha}"),
        ("web", web_image, f"{REGISTRY_HOST}/{REGISTRY_NAMESPACE}/novostroy-web:{source_sha}"),
    )
    for component, source, target in targets:
        if not _run_docker(docker, host, ["tag", source, target], timeout=30):
            raise RegistryPushError(f"Could not tag local {component} image; no registry push was attempted.")
    print(f"Tagged API and web images with source commit {source_sha}.", flush=True)

    for component, _source, target in targets:
        print(f"Pushing {component} image to the configured private registry...", flush=True)
        if not _run_docker(docker, host, ["push", target], timeout=timeout):
            print(f"{component.capitalize()} image push failed or timed out; Docker output was suppressed.",
                  file=sys.stderr)
            return 2
        print(f"{component.capitalize()} image push completed.", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Securely prepare registry auth and push reviewed images.")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Use dp to create private registry auth config.")
    configure.add_argument("--dp-bin", help="Optional path to dp.")
    configure.add_argument("--replace", action="store_true", help="Replace only this helper's private registry config.")
    push = commands.add_parser("push", help="Tag and push two local images under a source commit SHA.")
    push.add_argument("--source-sha", required=True, help="Full 40-character commit SHA for both registry tags.")
    push.add_argument("--api-image", required=True, help="Existing local API image reference.")
    push.add_argument("--web-image", required=True, help="Existing local web image reference.")
    push.add_argument("--docker-bin", help="Optional path to the Docker CLI.")
    push.add_argument("--docker-host", help="Private rootless Unix socket; defaults to DOCKER_HOST.")
    push.add_argument("--timeout", type=int, default=DEFAULT_PUSH_TIMEOUT,
                      help=f"Per-push timeout in seconds (default {DEFAULT_PUSH_TIMEOUT}).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            return configure_registry_auth(args.dp_bin, replace=args.replace)
        if args.command == "push":
            if args.timeout < 60 or args.timeout > 3600:
                raise RegistryPushError("Push timeout must be between 60 and 3600 seconds.")
            return push_images(
                source_sha=args.source_sha,
                api_image=args.api_image,
                web_image=args.web_image,
                docker_bin=args.docker_bin,
                docker_host=args.docker_host,
                timeout=args.timeout,
            )
    except (SafeAccessError, RegistryPushError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
