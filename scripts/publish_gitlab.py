"""Prepare a reviewed source snapshot and publish it over GitLab branch1.

`prepare` never touches the live checkout's data or Git state. `publish` overlays
only files committed in the prepared snapshot onto the current remote branch tip.
It never deletes remote-only files or force-pushes.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / ".runtime/gitlab-publish"
SNAPSHOT = STAGING / "source"
BUNDLE = STAGING / "branch1.bundle"
RECEIPT = STAGING / "source-receipt.json"
REMOTE = "https://gitlab.spirit.culab.ru/hack-l2sh-2/novastroy.git"
BRANCH = "branch1"
SOURCE_DIRS = (
    "analytics", "backend", "contracts", "deploy", "docs", "integration", "mobile",
    "pipeline", "quality", "recommendations", "reports", "scripts",
    "sources", "web",
)
ROOT_FILES = (
    ".dockerignore", ".gitignore", ".gitlab-ci.yml", "AGENTS.md", "README.md",
    "requirements-dev.txt",
)
SKIP_DIRS = {
    ".git", ".runtime", ".venv", ".pytest_cache", "__pycache__",
    "node_modules", "dist", "test-results", "playwright-report", "artifacts",
    "bin", "obj", "Library", "Temp", "Builds", "Logs", "UserSettings",
}
ALLOWED_SUFFIXES = {
    ".asmdef", ".conf", ".cs", ".csproj", ".css", ".csv", ".dockerfile", ".html", ".ini",
    ".js", ".json", ".md", ".meta", ".mjs", ".props", ".py",
    ".pyi", ".ql", ".service", ".sh", ".sql", ".svg", ".timer",
    ".toml", ".ts", ".tsx", ".tsv", ".ttf", ".txt", ".uxml",
    ".uss", ".xml", ".yaml", ".yml",
}
DENIED_SUFFIXES = {
    ".b3dm", ".db", ".env", ".geojson", ".glb", ".gpkg",
    ".jsonl", ".kdbx", ".key", ".mbtiles", ".osm", ".p12",
    ".parquet", ".pbf", ".pem", ".pfx", ".sqlite", ".zip",
}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)
MAX_FILE_BYTES = 5 * 1024 * 1024
AUTH_TIMEOUT_SECONDS = 300
SHA_RE = re.compile(r"[a-f0-9]{40}")


def _git(*args: str, cwd: Path | None = None, capture: bool = False,
         options: tuple[str, ...] = (), env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *options, *args], cwd=cwd, check=True, text=True, env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.DEVNULL if env and env.get("NOVOSTROY_GIT_QUIET_AUTH") == "1" else None,
    )
    return result.stdout.strip() if capture else ""


def _write_publish_status(status: str, remote_sha: str | None = None) -> None:
    """Expose non-secret progress without changing the reviewed source commit."""
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    receipt["publish_status"] = status
    receipt["published"] = status == "pushed"
    receipt["remote_sha"] = remote_sha if status == "pushed" else None
    pending = RECEIPT.with_suffix(".json.tmp")
    pending.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    pending.replace(RECEIPT)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        del request, fp, code, msg, headers, newurl
        return None


def _connection_preflight() -> None:
    """Check TLS/host reachability without credentials or following redirects."""
    request = Request(REMOTE, method="HEAD")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            status, location = response.status, response.headers.get("Location")
    except HTTPError as exc:
        status, location = exc.code, exc.headers.get("Location")
    if 300 <= status < 400:
        target = urlsplit(urljoin(REMOTE, location or ""))
        if target.scheme != "https" or target.hostname != urlsplit(REMOTE).hostname:
            raise RuntimeError("GitLab redirected to a different host; token was not requested")
    if not 200 <= status < 500:
        raise RuntimeError(f"GitLab preflight returned HTTP {status}; token was not requested")


def _auth_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_TRACE") and not key.startswith("GIT_CONFIG_")
           and key != "GIT_CURL_VERBOSE"}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    env["SSH_ASKPASS"] = "/bin/false"
    return env


@contextmanager
def _token_credential_cache(token: str):
    """Place one path-scoped PAT in a short-lived in-memory Git cache."""
    directory = Path(tempfile.mkdtemp(prefix="auth-", dir=STAGING))
    directory.chmod(0o700)
    socket = directory / "cache.sock"
    options = (
        "-c", "credential.helper=",  # Reset any global persistent helpers.
        "-c", f"credential.helper=cache --socket={socket} --timeout={AUTH_TIMEOUT_SECONDS}",
        "-c", "credential.useHttpPath=true",
        "-c", "http.followRedirects=false",
        "-c", "http.extraHeader=",
    )
    env = _auth_environment()
    try:
        credentials = ("protocol=https\n"
                       "host=gitlab.spirit.culab.ru\n"
                       "path=hack-l2sh-2/novastroy.git\n"
                       "username=oauth2\n"
                       f"password={token}\n\n").encode("utf-8")
        result = subprocess.run(["git", *options, "credential", "approve"],
                                input=credentials, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, check=False)
        if result.returncode:
            raise RuntimeError("Could not initialize the temporary Git credential cache")
        check = subprocess.run(
            ["git", *options, "credential", "fill"],
            input=b"protocol=https\nhost=gitlab.spirit.culab.ru\npath=hack-l2sh-2/novastroy.git\n\n",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, check=False,
        )
        valid = check.returncode == 0 and (b"password=" + token.encode("utf-8") + b"\n") in check.stdout
        del check, credentials, token
        if not valid:
            raise RuntimeError("The temporary Git credential cache could not return the PAT")
        yield options, env
    finally:
        try:
            result = subprocess.run(["git", "credential-cache", f"--socket={socket}", "exit"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=env, check=False)
            if result.returncode:
                print("Warning: Git credential-cache exit failed; cache expires after 300 seconds",
                      file=sys.stderr)
        finally:
            shutil.rmtree(directory)


def _source_paths() -> list[Path]:
    """Return explicit code/document paths, rejecting links and unfamiliar files."""
    files = [ROOT / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        base = ROOT / name
        if not base.is_dir() or base.is_symlink():
            raise RuntimeError(f"Required source directory missing or linked: {base}")
        for directory, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for dirname in dirs:
                if (Path(directory) / dirname).is_symlink():
                    raise RuntimeError(f"Linked source directory: {Path(directory) / dirname}")
            for filename in sorted(names):
                path = Path(directory) / filename
                if filename in {".gitignore"}:
                    files.append(path)
                    continue
                if filename.startswith(".") or filename == "tsconfig.tsbuildinfo":
                    continue
                if path.is_symlink():
                    raise RuntimeError(f"Linked source file: {path}")
                if path.suffix.lower() in DENIED_SUFFIXES:
                    continue
                if path.suffix.lower() not in ALLOWED_SUFFIXES:
                    raise RuntimeError(f"Unreviewed source file type: {path}")
                files.append(path)
    result = sorted(set(files))
    for path in result:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Source file missing or linked: {path}")
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"assets", "data", ".runtime"}:
            raise RuntimeError(f"Private/generated path entered source snapshot: {relative}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError(f"Source file exceeds 5 MiB review cap: {relative}")
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in SECRET_PATTERNS):
            raise RuntimeError(f"Possible credential in source file: {relative}")
    return result


def _check_source_relative(relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RuntimeError(f"Unsafe tracked path in snapshot: {relative}")
    if any(ord(character) < 32 for character in str(relative)):
        raise RuntimeError(f"Source path contains control characters: {relative}")
    if str(relative) in ROOT_FILES:
        return
    if relative.parts[0] not in SOURCE_DIRS or len(relative.parts) < 2:
        raise RuntimeError(f"Unreviewed source root in snapshot: {relative}")
    if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[1:-1]):
        raise RuntimeError(f"Generated or hidden directory in snapshot: {relative}")
    if relative.name == ".gitignore":
        return
    if relative.name.startswith(".") or relative.suffix.lower() in DENIED_SUFFIXES \
            or relative.suffix.lower() not in ALLOWED_SUFFIXES:
        raise RuntimeError(f"Unreviewed source file in snapshot: {relative}")


def prepare(base_remote_sha: str | None = None) -> None:
    """Create a local branch1 commit and Git bundle, never using the network."""
    if base_remote_sha is not None and SHA_RE.fullmatch(base_remote_sha) is None:
        raise ValueError("The previous remote SHA must have 40 lowercase hex characters")
    if SNAPSHOT.exists():
        raise RuntimeError(f"Prepared snapshot already exists: {SNAPSHOT}; review it before replacing it")
    paths = _source_paths()
    STAGING.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.mkdir()
    try:
        for source in paths:
            destination = SNAPSHOT / source.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            destination.chmod(0o644)
        _git("init", "-b", BRANCH, str(SNAPSHOT))
        _git("add", "--all", cwd=SNAPSHOT)
        _git("commit", "-m", "Add Novostroy source snapshot", cwd=SNAPSHOT)
        _git("bundle", "create", str(BUNDLE), BRANCH, cwd=SNAPSHOT)
        committed = _git("rev-parse", "HEAD", cwd=SNAPSHOT, capture=True)
        receipt = {
            "branch": BRANCH,
            "commit": committed,
            "files": len(paths),
            "bundle_sha256": hashlib.sha256(BUNDLE.read_bytes()).hexdigest(),
            "snapshot_bytes": sum(path.stat().st_size for path in paths),
            "remote_target": REMOTE,
            "base_remote_sha": base_remote_sha,
            "published": False,
        }
        RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    except Exception:
        # Preserve failed staging for diagnosis; never touch the live checkout.
        raise
    print(f"Prepared {len(paths)} source files at {SNAPSHOT}")
    print(f"Local commit: {committed}; bundle: {BUNDLE}")
    print("No network request or push was made")


def _tracked_snapshot() -> list[Path]:
    if not (SNAPSHOT / ".git").is_dir():
        raise RuntimeError("Prepared source snapshot is missing; run prepare first")
    if _git("branch", "--show-current", cwd=SNAPSHOT, capture=True) != BRANCH:
        raise RuntimeError("Prepared snapshot is not on branch1")
    if _git("status", "--porcelain", cwd=SNAPSHOT, capture=True):
        raise RuntimeError("Prepared snapshot is dirty; review and commit it before publishing")
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if _git("rev-parse", "HEAD", cwd=SNAPSHOT, capture=True) != receipt["commit"]:
        raise RuntimeError("Prepared snapshot commit differs from reviewed receipt")
    if hashlib.sha256(BUNDLE.read_bytes()).hexdigest() != receipt["bundle_sha256"]:
        raise RuntimeError("Prepared bundle differs from reviewed receipt")
    base_remote_sha = receipt.get("base_remote_sha")
    if base_remote_sha is not None and SHA_RE.fullmatch(base_remote_sha) is None:
        raise RuntimeError("Reviewed receipt has an invalid previous remote SHA")
    data = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        cwd=SNAPSHOT, check=True, stdout=subprocess.PIPE,
    ).stdout
    paths = [Path(os.fsdecode(item)) for item in data.split(b"\0") if item]
    if len(paths) != receipt["files"]:
        raise RuntimeError("Prepared source file count differs from reviewed receipt")
    for relative in paths:
        _check_source_relative(relative)
        source = SNAPSHOT / relative
        if not source.is_file() or source.is_symlink() or SNAPSHOT.is_symlink():
            raise RuntimeError(f"Missing or linked tracked source: {relative}")
        ancestor = source.parent
        while ancestor != SNAPSHOT:
            if ancestor.is_symlink():
                raise RuntimeError(f"Linked source parent in snapshot: {relative}")
            ancestor = ancestor.parent
        if source.stat().st_size > MAX_FILE_BYTES or any(
            pattern.search(source.read_bytes()) for pattern in SECRET_PATTERNS
        ):
            raise RuntimeError(f"Unsafe tracked source: {relative}")
    return paths


def _switch_remote_branch(checkout: Path) -> None:
    remote_ref = f"refs/remotes/origin/{BRANCH}"
    if subprocess.run(["git", "show-ref", "--verify", "--quiet", remote_ref], cwd=checkout).returncode == 0:
        local = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{BRANCH}"],
            cwd=checkout,
        ).returncode == 0
        if local:
            _git("switch", BRANCH, cwd=checkout)
        else:
            _git("switch", "-c", BRANCH, "--track", f"origin/{BRANCH}", cwd=checkout)
        return
    refs = _git("for-each-ref", "--format=%(refname)", "refs/remotes/origin", cwd=checkout, capture=True)
    if not refs:
        _git("switch", "--orphan", BRANCH, cwd=checkout)
        return
    default = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=checkout, text=True, stdout=subprocess.PIPE,
    )
    if default.returncode or not default.stdout.strip().startswith("origin/"):
        raise RuntimeError("Remote has history but no branch1/default HEAD; choose its base branch explicitly")
    _git("switch", "-c", BRANCH, default.stdout.strip(), cwd=checkout)


def _blob_at(checkout: Path, revision: str, relative: Path) -> str | None:
    """Return the exact Git blob ID at a tree path, without reading file contents."""
    result = subprocess.run(
        ["git", "ls-tree", "-z", "--full-tree", revision, "--", str(relative)],
        cwd=checkout, check=True, stdout=subprocess.PIPE,
    ).stdout
    if not result:
        return None
    entry = result.removesuffix(b"\0")
    metadata, separator, name = entry.partition(b"\t")
    mode, kind, blob = metadata.split(b" ")
    if (not separator or name != os.fsencode(relative) or kind != b"blob"
            or mode not in {b"100644", b"100755"}):
        raise RuntimeError(f"Remote source path is not an ordinary file: {relative}")
    return blob.decode("ascii")


def _overlay_reviewed_paths(checkout: Path, paths: list[Path],
                            base_remote_sha: str | None) -> list[Path]:
    """Three-way overlay; preserve remote edits and stop on overlapping changes."""
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=checkout,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if head.returncode == 0:
        if base_remote_sha is None:
            raise RuntimeError("Existing remote history needs a reviewed base SHA before overlay")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_remote_sha, "HEAD"],
            cwd=checkout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if ancestor.returncode:
            raise RuntimeError("Remote branch1 does not descend from the reviewed base SHA")
        remote_tip = head.stdout.strip()
    elif base_remote_sha is not None:
        raise RuntimeError("Reviewed base SHA exists but remote branch has no history")
    else:
        remote_tip = None
    changed: list[Path] = []
    for relative in paths:
        desired = _git("hash-object", str(SNAPSHOT / relative),
                       cwd=checkout, capture=True)
        baseline = _blob_at(checkout, base_remote_sha, relative) if base_remote_sha else None
        current = _blob_at(checkout, remote_tip, relative) if remote_tip else None
        if current != baseline:
            if desired == baseline or desired == current:
                continue
            raise RuntimeError(f"Remote source file changed since reviewed base: {relative}")
        if desired == current:
            continue
        destination = checkout / relative
        if any(part.is_symlink() for part in (destination, *destination.parents)
               if part != checkout.parent):
            raise RuntimeError(f"Remote path is a symlink: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SNAPSHOT / relative, destination, follow_symlinks=False)
        destination.chmod(0o644)
        changed.append(relative)
    return changed


def publish_to_remote(remote: str = REMOTE, *, options: tuple[str, ...] = (),
                      env: dict[str, str] | None = None) -> str:
    """Three-way overlay reviewed source onto remote tip. CLI pins `remote` to REMOTE."""
    paths = _tracked_snapshot()
    base_remote_sha = json.loads(RECEIPT.read_text(encoding="utf-8")).get("base_remote_sha")
    if not remote or any(c.isspace() for c in remote):
        raise RuntimeError("Invalid remote target")
    STAGING.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="publish-work-", dir=STAGING) as temporary:
        checkout = Path(temporary) / "remote"
        _git("clone", "--no-checkout", remote, str(checkout), options=options, env=env)
        _switch_remote_branch(checkout)
        if _git("branch", "--show-current", cwd=checkout, capture=True) != BRANCH:
            raise RuntimeError("Remote checkout is not on branch1")
        changed = _overlay_reviewed_paths(checkout, paths, base_remote_sha)
        if changed:
            subprocess.run(["git", "add", "-f", "--", *(str(path) for path in changed)],
                           cwd=checkout, check=True)
        if _git("diff", "--cached", "--name-only", cwd=checkout, capture=True):
            _git("commit", "-m", "Add Novostroy source snapshot", cwd=checkout)
        committed = _git("rev-parse", "HEAD", cwd=checkout, capture=True)
        _git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=checkout,
             options=options, env=env)
        remote_head = _git("ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}",
                           cwd=checkout, capture=True, options=options, env=env)
        if remote_head != f"{committed}\trefs/heads/{BRANCH}":
            raise RuntimeError("Push returned but remote branch1 SHA did not match the local commit")
    print(f"Verified {BRANCH} on remote at {committed}")
    return committed


def publish_with_token() -> str:
    """Prompt once in a real terminal, publish, and erase the memory cache."""
    _tracked_snapshot()  # Fail locally before any network request or token prompt.
    _write_publish_status("started")
    try:
        _connection_preflight()
    except Exception:
        _write_publish_status("connection_failed")
        raise
    _write_publish_status("needs_token")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError("Run publish --token in your own interactive terminal; no token was requested")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("GitLab PAT (hidden input): ")
    except (getpass.GetPassWarning, EOFError) as exc:
        _write_publish_status("auth_failed")
        raise RuntimeError("Hidden terminal input failed; token was not used") from exc
    if not token or "\n" in token or "\r" in token:
        _write_publish_status("auth_failed")
        raise RuntimeError("A nonempty single-line GitLab PAT is required")
    try:
        with _token_credential_cache(token) as (options, env):
            token = ""
            try:
                _git("ls-remote", "--heads", REMOTE, BRANCH, capture=True,
                     options=options, env=env)
            except subprocess.CalledProcessError as exc:
                _write_publish_status("auth_failed")
                raise RuntimeError("GitLab rejected token authentication or repository access") from exc
            try:
                committed = publish_to_remote(REMOTE, options=options, env=env)
            except Exception:
                _write_publish_status("publish_failed")
                raise
            _write_publish_status("pushed", committed)
            return committed
    except RuntimeError:
        if json.loads(RECEIPT.read_text(encoding="utf-8")).get("publish_status") == "needs_token":
            _write_publish_status("auth_failed")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "publish"))
    parser.add_argument("--token", action="store_true", help="Prompt for a PAT in a terminal; never store it")
    parser.add_argument("--base-remote-sha", help="Last verified remote branch1 SHA for three-way overlay")
    args = parser.parse_args()
    if args.action == "prepare":
        if args.token:
            parser.error("--token applies only to publish")
        prepare(args.base_remote_sha)
    else:
        if args.base_remote_sha:
            parser.error("--base-remote-sha applies only to prepare")
        if not args.token:
            parser.error("Use publish --token from your own interactive terminal")
        publish_with_token()


if __name__ == "__main__":
    main()
