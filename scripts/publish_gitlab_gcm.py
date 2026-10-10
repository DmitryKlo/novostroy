"""Publish the reviewed source snapshot through isolated browser OAuth/GCM.

Spirit's ordinary user session configures a GitLab OAuth client. The GitLab
credential is kept only in this invocation's private Git cache socket; GCM's
own store is disabled. No personal Git config or existing cache is changed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import publish_gitlab as publisher  # noqa: E402
from scripts.spirit_deploy_access import (  # noqa: E402
    DEFAULT_DP, _known_key_literals, classify_cli_failure,
    safe_diagnostic_snippets, user_dp_environment,
)


def _executable(path: Path, name: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{name} must be an absolute regular executable")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"{name} is not executable")
    return path.resolve(strict=True)


def _git_cache_probe(socket_path: Path, env: dict[str, str]) -> None:
    """Prove that this one socket stores and returns a synthetic credential."""
    helper = f"cache --socket={socket_path} --timeout=300"
    base = ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
            "-c", "credential.useHttpPath=true", "credential"]
    identity = b"protocol=https\nhost=novostroy-smoke.invalid\npath=fixture.git\n"
    fake = identity + b"username=fixture\npassword=synthetic-only\n\n"
    approved = subprocess.run(base + ["approve"], input=fake, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False)
    filled = subprocess.run(base + ["fill"], input=identity + b"\n", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            check=False)
    if (approved.returncode or filled.returncode
            or b"password=synthetic-only\n" not in filled.stdout
            or not socket_path.is_socket()):
        raise RuntimeError("The private in-memory Git credential cache failed its synthetic probe")
    subprocess.run(base + ["reject"], input=identity + b"\n", env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False)


def probe_cache_only() -> None:
    """Run the socket check without touching GitLab, GCM, or user OAuth state."""
    publisher.STAGING.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gcm-cache-probe-", dir=publisher.STAGING) as temporary:
        private = Path(temporary)
        socket_path = private / "cache.sock"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        try:
            _git_cache_probe(socket_path, env)
        finally:
            if socket_path.exists():
                subprocess.run(["git", "credential-cache", f"--socket={socket_path}", "exit"],
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
    print("PASS isolated synthetic Git credential-cache socket")


def _isolated_environment(gcm_bin: Path, config: Path) -> dict[str, str]:
    env = user_dp_environment()
    env = {key: value for key, value in env.items()
           if not key.startswith(("GIT_TRACE", "GIT_CONFIG_", "GCM_TRACE"))
           and key not in {"GIT_CURL_VERBOSE", "GCM_CREDENTIAL_CACHE_OPTIONS",
                           "GIT_TERMINAL_PROMPT"}}
    env.update({
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GCM_INTERACTIVE": "1",
        "GCM_GUI_PROMPT": "0",
        "GCM_GITLAB_AUTHMODES": "browser",
        "GCM_CREDENTIAL_STORE": "none",
        "GCM_PROVIDER": "gitlab",
        "NOVOSTROY_GIT_QUIET_AUTH": "1",
        "PATH": str(gcm_bin.parent) + os.pathsep + env.get("PATH", ""),
    })
    return env


def _configure_git(dp_bin: Path, env: dict[str, str], config: Path) -> None:
    """Suppress raw dp output; expose only bounded, allowlisted failure labels."""
    result = subprocess.run(
        [str(dp_bin), "auth", "configure-git"], cwd=ROOT, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False,
    )
    if result.returncode:
        output = result.stdout + "\n" + result.stderr
        category = classify_cli_failure(output)
        if category is None:
            lowered = output.casefold()
            if "git-credential-manager" in lowered and "not found" in lowered:
                category = "GCM executable unavailable"
            elif "git-credential-manager" in lowered and "configure" in lowered:
                category = "GCM configuration failed"
            else:
                category = "unclassified CLI failure"
        snippets = safe_diagnostic_snippets(output, _known_key_literals())
        detail = f"; {' | '.join(snippets)}" if snippets else ""
        raise RuntimeError(
            f"Isolated Spirit GitLab OAuth configuration failed "
            f"(exit {result.returncode}; {category}{detail})"
        )
    if not config.stat().st_size or stat.S_IMODE(config.stat().st_mode) & 0o077:
        raise RuntimeError("Isolated Git OAuth configuration is absent or not private")


def probe_configuration(*, gcm_bin: Path, dp_bin: Path) -> None:
    """Replay the exact cache-then-dp setup, without GitLab or push."""
    gcm_bin = _executable(gcm_bin, "Git Credential Manager")
    dp_bin = _executable(dp_bin, "Spirit CLI")
    publisher.STAGING.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gcm-config-probe-", dir=publisher.STAGING) as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        config = private / "gitconfig"
        config.touch(mode=0o600)
        socket_path = private / "cache.sock"
        env = _isolated_environment(gcm_bin, config)
        try:
            _git_cache_probe(socket_path, env)
            _configure_git(dp_bin, env, config)
        finally:
            if socket_path.exists():
                subprocess.run(["git", "credential-cache", f"--socket={socket_path}", "exit"],
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
    print("PASS isolated cache + Spirit/GCM configuration")


def _gitlab_access_probe(*, options: tuple[str, ...], env: dict[str, str]) -> None:
    """Check browser OAuth while retaining only a safe failure category."""
    result = subprocess.run(
        ["git", *options, "ls-remote", "--heads", publisher.REMOTE,
         publisher.BRANCH], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        timeout=publisher.AUTH_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode:
        diagnostic = result.stderr.casefold()
        category = classify_cli_failure(result.stderr)
        if category is None:
            if "interactivity has been disabled" in diagnostic:
                category = "credential interaction disabled"
            elif "could not read username" in diagnostic:
                category = "credential helper returned no username"
            elif "oauth" in diagnostic and "client" in diagnostic:
                category = "OAuth client unavailable"
            elif "credential-manager" in diagnostic or "credential manager" in diagnostic:
                category = "Git Credential Manager failed"
            else:
                category = "unclassified Git authentication failure"
        snippets = safe_diagnostic_snippets(
            result.stderr.replace("fatal:", "error:"), _known_key_literals())
        detail = f"; {' | '.join(snippets)}" if snippets else ""
        raise RuntimeError(f"GitLab browser OAuth or repository access failed "
                           f"(exit {result.returncode}; {category}{detail})")


def _config_audit(config: Path) -> str:
    """Expose only allowlisted booleans about dp's private Git config."""
    result = subprocess.run(
        ["git", "config", "--file", str(config), "--name-only", "--list"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        return "private Git config key audit unavailable"
    keys = {line.strip().casefold() for line in result.stdout.splitlines()}
    host_keys = {key for key in keys if "gitlab.spirit.culab.ru" in key}
    helper = "credential.helper" in keys or any(
        key.endswith(".helper") for key in host_keys)
    client_id = any(key.endswith(".gitlabdevclientid") for key in host_keys)
    client_secret = any(key.endswith(".gitlabdevclientsecret") for key in host_keys)
    provider = any(key.endswith(".provider") for key in host_keys)
    return ("private Git config keys: "
            f"helper={helper}, host_client_id={client_id}, "
            f"host_client_secret={client_secret}, host_provider={provider}")


def probe_access(*, gcm_bin: Path, dp_bin: Path) -> None:
    """Replay browser-auth access without publishing or writing receipt status."""
    publisher._tracked_snapshot()
    gcm_bin = _executable(gcm_bin, "Git Credential Manager")
    dp_bin = _executable(dp_bin, "Spirit CLI")
    publisher._connection_preflight()
    with tempfile.TemporaryDirectory(prefix="gcm-access-probe-", dir=publisher.STAGING) as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        config = private / "gitconfig"
        config.touch(mode=0o600)
        socket_path = private / "cache.sock"
        env = _isolated_environment(gcm_bin, config)
        helper = f"cache --socket={socket_path} --timeout=300"
        options = (
            "-c", "credential.helper=",
            "-c", f"credential.helper={helper}",
            "-c", f"credential.helper={gcm_bin}",
            "-c", "credential.useHttpPath=true",
            "-c", "http.followRedirects=false",
            "-c", "http.extraHeader=",
        )
        try:
            _git_cache_probe(socket_path, env)
            _configure_git(dp_bin, env, config)
            print(_config_audit(config))
            _gitlab_access_probe(options=options, env=env)
        finally:
            if socket_path.exists():
                subprocess.run(["git", "credential-cache", f"--socket={socket_path}", "exit"],
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
    print("PASS GitLab browser OAuth and branch access; no push")


def publish(*, gcm_bin: Path, dp_bin: Path) -> str:
    publisher._tracked_snapshot()  # Fail before network, browser, or auth state.
    gcm_bin = _executable(gcm_bin, "Git Credential Manager")
    dp_bin = _executable(dp_bin, "Spirit CLI")
    if gcm_bin.name != "git-credential-manager":
        raise RuntimeError("Expected the official git-credential-manager executable name")
    publisher._write_publish_status("started")
    try:
        publisher._connection_preflight()
    except Exception:
        publisher._write_publish_status("connection_failed")
        raise
    with tempfile.TemporaryDirectory(prefix="gcm-auth-", dir=publisher.STAGING) as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        config = private / "gitconfig"
        config.touch(mode=0o600)
        socket_path = private / "cache.sock"
        env = _isolated_environment(gcm_bin, config)
        helper = f"cache --socket={socket_path} --timeout=300"
        options = (
            "-c", "credential.helper=",
            "-c", f"credential.helper={helper}",
            "-c", f"credential.helper={gcm_bin}",
            "-c", "credential.useHttpPath=true",
            "-c", "http.followRedirects=false",
            "-c", "http.extraHeader=",
        )
        try:
            _git_cache_probe(socket_path, env)
            try:
                _configure_git(dp_bin, env, config)
            except (RuntimeError, subprocess.TimeoutExpired):
                publisher._write_publish_status("auth_failed")
                raise
            publisher._write_publish_status("needs_browser")
            try:
                _gitlab_access_probe(options=options, env=env)
            except (RuntimeError, subprocess.TimeoutExpired):
                publisher._write_publish_status("auth_failed")
                raise
            try:
                committed = publisher.publish_to_remote(
                    publisher.REMOTE, options=options, env=env)
            except Exception:
                publisher._write_publish_status("publish_failed")
                raise
            publisher._write_publish_status("pushed", committed)
            return committed
        finally:
            # Never exit the user's default cache: this is our unique socket.
            if socket_path.exists():
                subprocess.run(["git", "credential-cache", f"--socket={socket_path}", "exit"],
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            shutil.rmtree(private, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcm-bin", type=Path)
    parser.add_argument("--dp-bin", type=Path, default=DEFAULT_DP)
    parser.add_argument("--probe-only", action="store_true",
                        help="Check only a private synthetic Git cache socket; no auth or push")
    parser.add_argument("--configure-probe", action="store_true",
                        help="Check exact cache + dp/GCM setup; no GitLab or push")
    parser.add_argument("--auth-probe", action="store_true",
                        help="Check browser OAuth and branch access; never push")
    args = parser.parse_args()
    try:
        if args.probe_only:
            probe_cache_only()
            return
        if args.configure_probe:
            if args.gcm_bin is None:
                parser.error("--gcm-bin is required for the configuration probe")
            probe_configuration(gcm_bin=args.gcm_bin, dp_bin=args.dp_bin)
            return
        if args.auth_probe:
            if args.gcm_bin is None:
                parser.error("--gcm-bin is required for the access probe")
            probe_access(gcm_bin=args.gcm_bin, dp_bin=args.dp_bin)
            return
        if args.gcm_bin is None:
            parser.error("--gcm-bin is required for OAuth publishing")
        committed = publish(gcm_bin=args.gcm_bin, dp_bin=args.dp_bin)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"GitLab OAuth publish failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Verified remote branch1 commit: {committed}")


if __name__ == "__main__":
    main()
