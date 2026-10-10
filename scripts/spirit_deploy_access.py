#!/usr/bin/env python3
"""Import a Spirit service-account key and authorize dp in isolated local state.

This helper accepts only an explicit JSON key path or an explicit session JSONL
path. It never searches the filesystem for credentials and never prints key
material or raw command output. Optional diagnostics are strictly redacted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CONFIG_DIR = Path.home() / ".config" / "novostroy" / "spirit"
KEY_PATH = CONFIG_DIR / "service-account.json"
DP_DIR = CONFIG_DIR / "dp"
USER_DP_DIR = CONFIG_DIR / "user-dp"
TELEPORT_TENANT = "hack-l2sh-2"
TELEPORT_IDENTITY_PATH = CONFIG_DIR / "teleport-identity"
DEFAULT_DP = Path.home() / ".local" / "bin" / "dp"

SECRET_KIND = "ServiceAccountKeySecret"
KEY_FIELDS = frozenset(
    {
        "clientID",
        "clientSecret",
        "privateKey",
        "privateKeyID",
        "serviceAccountID",
        "serviceAccountName",
        "tenantKey",
        "tokenURI",
    }
)
SESSION_KIND_MARKER = b"ServiceAccountKeySecret"
SESSION_RECORD_MARKER = b"response_item"
MAX_KEY_BYTES = 256 * 1024
MAX_SESSION_CONTENT_BYTES = 2 * 1024 * 1024
HTTP_STATUS_PATTERNS = (
    re.compile(r"\bHTTP(?:/\d(?:\.\d)?)?\s*[: ]\s*([45]\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(?:response\s+)?status(?:\s+code)?\s*[:= ]\s*([45]\d{2})\b", re.IGNORECASE),
    re.compile(r"\b([45]\d{2})\b"),
)
SAFE_FAILURE_PHRASES = (
    ("unknown command", "unsupported command"),
    ("permission denied", "permission denied"),
    ("access denied", "access denied"),
    ("forbidden", "forbidden"),
    ("expired", "credential expired"),
)


class SafeAccessError(Exception):
    """An expected, non-sensitive error suitable for a short CLI message."""


def _path_stat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeAccessError("Could not inspect the requested path.") from exc


def ensure_private_dir(path: Path) -> Path:
    """Create a directory under the current home without following symlinks."""
    home = Path.home().resolve()
    target = Path(os.path.abspath(path))
    try:
        relative = target.relative_to(home)
    except ValueError as exc:
        raise SafeAccessError("Private state must stay under the current home directory.") from exc

    current = home
    parts = relative.parts
    if not parts:
        raise SafeAccessError("Refusing to use the home directory as private state.")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                info = current.lstat()
            except OSError as exc:
                raise SafeAccessError("Could not create private state directory.") from exc
        except OSError as exc:
            raise SafeAccessError("Could not inspect private state directory.") from exc

        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SafeAccessError("Private state path contains a symlink or non-directory.")
        if index == len(parts) - 1:
            if info.st_uid != os.getuid():
                raise SafeAccessError("Private state directory is not owned by the current user.")
            if stat.S_IMODE(info.st_mode) != 0o700:
                try:
                    current.chmod(0o700)
                except OSError as exc:
                    raise SafeAccessError("Could not restrict private state directory permissions.") from exc
    return target


def _normalize_key_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SafeAccessError("Service-account JSON must be an object.")

    if value.get("kind") == SECRET_KIND:
        attributes = value.get("attributes")
    elif "kind" in value or "attributes" in value:
        raise SafeAccessError("Service-account JSON envelope has an unsupported kind or shape.")
    else:
        attributes = value

    if not isinstance(attributes, dict) or set(attributes) != KEY_FIELDS:
        raise SafeAccessError("Service-account JSON does not match the expected key schema.")

    normalized: dict[str, str] = {}
    for field in KEY_FIELDS:
        item = attributes.get(field)
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise SafeAccessError("Service-account JSON contains a missing or invalid field.")
        normalized[field] = item
    # Spirit's downloaded ServiceAccountKeySecret uses this envelope; retain it
    # because dp consumes the official shape rather than a flattened key map.
    return {"kind": SECRET_KIND, "attributes": normalized}


def _read_regular_private_file(path: Path, *, require_private_mode: bool) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SafeAccessError("The explicitly selected key file does not exist.") from exc
    except OSError as exc:
        raise SafeAccessError("Could not inspect the explicitly selected key file.") from exc

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SafeAccessError("The explicitly selected key path must be a regular non-symlink file.")
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise SafeAccessError("The explicitly selected key file must be privately owned and unlinked.")
    if require_private_mode and stat.S_IMODE(info.st_mode) & 0o077:
        raise SafeAccessError("Restrict the source key file to owner-only permissions before importing it.")
    if info.st_size > MAX_KEY_BYTES:
        raise SafeAccessError("The explicitly selected key file is unexpectedly large.")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            data = source.read(MAX_KEY_BYTES + 1)
    except OSError as exc:
        raise SafeAccessError("Could not read the explicitly selected key file.") from exc
    if len(data) > MAX_KEY_BYTES:
        raise SafeAccessError("The explicitly selected key file is unexpectedly large.")
    return data


def load_key_file(path: Path) -> dict[str, Any]:
    raw = _read_regular_private_file(path.expanduser(), require_private_mode=True)
    try:
        return _normalize_key_document(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeAccessError("The explicitly selected key file is not valid JSON.") from exc


def load_key_from_session(path: Path) -> dict[str, Any]:
    """Read only user text objects whose top-level kind is the expected marker."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SafeAccessError("The configured session file is absent; no key was imported.") from exc
    except OSError as exc:
        raise SafeAccessError("Could not inspect the configured session file.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SafeAccessError("The configured session path is not a regular file owned by this user.")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SafeAccessError("Could not open the configured session file.") from exc

    latest: dict[str, Any] | None = None
    with os.fdopen(fd, "rb") as source:
        for line in source:
            if SESSION_KIND_MARKER not in line or SESSION_RECORD_MARKER not in line:
                continue
            if len(line) > MAX_SESSION_CONTENT_BYTES:
                continue
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("role") != "user":
                continue
            content = payload.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                text = item["text"]
                if len(text.encode("utf-8")) > MAX_SESSION_CONTENT_BYTES:
                    continue
                try:
                    decoded, _ = json.JSONDecoder().raw_decode(text.lstrip())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(decoded, dict) and decoded.get("kind") == SECRET_KIND:
                    latest = _normalize_key_document(decoded)

    if latest is None:
        raise SafeAccessError("No user-provided ServiceAccountKeySecret was found in the configured session.")
    return latest


def write_key_file(document: dict[str, Any], destination: Path, *, replace: bool) -> Path:
    destination = destination.expanduser()
    parent = ensure_private_dir(destination.parent)
    destination = parent / destination.name

    try:
        old = destination.lstat()
    except FileNotFoundError:
        old = None
    except OSError as exc:
        raise SafeAccessError("Could not inspect destination key path.") from exc
    if old is not None:
        if stat.S_ISLNK(old.st_mode) or not stat.S_ISREG(old.st_mode):
            raise SafeAccessError("Destination key path must be a regular non-symlink file.")
        if old.st_uid != os.getuid() or old.st_nlink != 1:
            raise SafeAccessError("Existing destination key file is not privately owned.")
        if not replace:
            raise SafeAccessError("A key file already exists; pass --replace to rotate it.")

    canonical_document = _normalize_key_document(document)
    encoded = (json.dumps(canonical_document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    temp_fd = -1
    temp_name: str | None = None
    try:
        temp_fd, temp_name = tempfile.mkstemp(prefix=".service-account-", dir=parent)
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "wb") as target:
            temp_fd = -1
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        temp_path = Path(temp_name)
        if replace:
            if destination.exists() and destination.is_symlink():
                raise SafeAccessError("Destination key path became a symlink.")
            os.replace(temp_path, destination)
        else:
            os.link(temp_path, destination, follow_symlinks=False)
            temp_path.unlink()
        destination.chmod(0o600)
    except FileExistsError as exc:
        raise SafeAccessError("A key file already exists; pass --replace to rotate it.") from exc
    except OSError as exc:
        raise SafeAccessError("Could not write the key into private local storage.") from exc
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def isolated_dp_environment() -> dict[str, str]:
    return _private_dp_environment(DP_DIR)


def user_dp_environment() -> dict[str, str]:
    """Use a separate private state directory for ordinary browser OAuth login."""
    return _private_dp_environment(USER_DP_DIR)


def _private_dp_environment(state_dir: Path) -> dict[str, str]:
    dp_dir = ensure_private_dir(state_dir)
    plugins = ensure_private_dir(dp_dir / "plugins")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DP_")}
    env.update(
        {
            "DP_WORKDIR": str(dp_dir),
            "DP_PLUGIN_WORKDIR": str(plugins),
            "DP_LOG_FILE": os.devnull,
            "DP_DEBUG_LOG_FILE": os.devnull,
            "DP_NO_WARNINGS": "true",
        }
    )
    return env


def user_login(dp_path: str | None) -> int:
    """Run dp's normal browser OAuth flow in a separate isolated user session."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("No graphical display is available to this process; run user-login from the active desktop terminal.",
              file=sys.stderr)
        return 2
    executable = resolve_dp(dp_path)
    env = user_dp_environment()
    print("Starting browser-based Spirit sign-in in the isolated user session.", flush=True)
    try:
        result = subprocess.run(
            [str(executable), "auth", "login"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        print("Browser sign-in timed out; isolated user-session status is unconfirmed. CLI output was suppressed.",
              file=sys.stderr)
        return 2
    except OSError:
        print("Could not start browser sign-in; CLI output was suppressed.", file=sys.stderr)
        return 2

    if result.returncode == 0:
        print(f"Browser sign-in completed in isolated user state at {USER_DP_DIR}; CLI output was suppressed.")
        return 0
    cause = classify_cli_failure(f"{result.stdout}\n{result.stderr}")
    detail = f"; {cause}" if cause else ""
    print(f"Browser sign-in failed (exit code {result.returncode}{detail}); CLI output was suppressed.",
          file=sys.stderr)
    return result.returncode or 1


def user_session_status(*, verify: bool = False) -> int:
    """Check the local token, optionally making two fixed read-only identity/access GETs."""
    try:
        if __package__:
            from .spirit_inventory import InventoryError, _token_from_dp
            from . import spirit_inventory as inventory
        else:
            from spirit_inventory import InventoryError, _token_from_dp
            import spirit_inventory as inventory
        token = _token_from_dp(user_session=True)
    except (ImportError, SafeAccessError):
        print("No usable cached user-session token; browser login is unconfirmed.")
        return 2
    except InventoryError:
        print("No usable cached user-session token; browser login is unconfirmed.")
        return 2
    if not verify:
        del token
        print(f"A local user-session token is cached in {USER_DP_DIR}; it was suppressed and is not API-validated.")
        return 0

    userinfo_path = "/api/v1/oauth2/userinfo"
    try:
        userinfo = inventory._get_json(token, userinfo_path)
    except InventoryError as exc:
        status = _http_status_from_safe_error(exc)
        print(f"userinfo read: {_read_scope_label(status)}")
        if status == 403:
            print("Stopped after definitive HTTP 403; no further read was attempted.")
            return 2
        return 2
    is_service_account, matches_target = _safe_identity_flags(userinfo)
    kind = "unknown" if is_service_account is None else ("service_account" if is_service_account else "user")
    account_match = "unknown" if matches_target is None else str(matches_target).lower()
    print("userinfo read: available (HTTP 200)")
    print(f"identity kind: {kind}; matches configured service account: {account_match}")

    tenant_path = "/api/v2/tenants/hack-l2sh-2/vmgroup?limit=100&offset=0"
    try:
        inventory._get_json(token, tenant_path)
    except InventoryError as exc:
        status = _http_status_from_safe_error(exc)
        print(f"target-tenant VM-group read: {_read_scope_label(status)}")
        if status == 403:
            print("Stopped after definitive HTTP 403; no further read was attempted.")
            return 2
        return 2
    finally:
        del token
    print("target-tenant VM-group read: available (HTTP 200)")
    return 0


def user_sa_metadata_status() -> int:
    """Read one known SA metadata resource using the confirmed tenant-key namespace."""
    try:
        if __package__:
            from .spirit_inventory import InventoryError, _token_from_dp
            from . import spirit_inventory as inventory
        else:
            from spirit_inventory import InventoryError, _token_from_dp
            import spirit_inventory as inventory
        token = _token_from_dp(user_session=True)
    except (ImportError, SafeAccessError):
        print("No usable cached user-session token; metadata checks were not attempted.")
        return 2
    except InventoryError:
        print("No usable cached user-session token; metadata checks were not attempted.")
        return 2

    tenant_id = inventory.METADATA_TENANT_KEY
    path = f"/api/v2/tenants/{tenant_id}/serviceaccounts/{inventory.OWN_SERVICE_ACCOUNT_ID}"
    try:
        try:
            result = inventory._get_json(token, path)
        except InventoryError as exc:
            status = _http_status_from_safe_error(exc)
            print(f"tenant-key namespace: {_read_scope_label(status)}")
            print("No UUID fallback or further request was attempted.")
            return 2
        resource_kind, id_match, tenant_match = _safe_resource_metadata(result, tenant_id)
        print("tenant-key namespace: available (HTTP 200); "
              f"resource_kind={resource_kind}; id_match={id_match}; tenant_match={tenant_match}")
    finally:
        del token
    return 0


def _safe_resource_metadata(value: Any, expected_tenant: str) -> tuple[str, str, str]:
    """Return only bounded labels and match booleans from a service-account response."""
    if not isinstance(value, dict):
        return "unknown", "unknown", "unknown"
    records: list[dict[str, Any]] = [value]
    for key in ("attributes", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            records.append(nested)
            attributes = nested.get("attributes")
            if isinstance(attributes, dict):
                records.append(attributes)

    resource_kind = "unknown"
    for record in records:
        for key in ("kind", "type"):
            kind = record.get(key)
            if isinstance(kind, str):
                normalized = re.sub(r"[^a-z]", "", kind.casefold())
                resource_kind = "service_account" if "serviceaccount" in normalized else "other"
                break
        if resource_kind != "unknown":
            break

    ids: list[str] = []
    tenant_ids: list[str] = []
    for record in records:
        for key in ("id", "serviceAccountID", "serviceAccountId", "service_account_id"):
            candidate = record.get(key)
            if isinstance(candidate, str):
                ids.append(candidate)
        for key in ("tenantID", "tenantId", "tenant_id", "tenantKey", "tenant_key"):
            candidate = record.get(key)
            if isinstance(candidate, str):
                tenant_ids.append(candidate)
        tenant = record.get("tenant")
        if isinstance(tenant, str):
            tenant_ids.append(tenant)
        elif isinstance(tenant, dict):
            for key in ("id", "key", "tenantID", "tenantId"):
                candidate = tenant.get(key)
                if isinstance(candidate, str):
                    tenant_ids.append(candidate)

    expected_id = "8ca85d21-1fdc-43c5-ac8e-eaa8a509e082"
    id_match = "unknown" if not ids else str(expected_id in ids and len(set(ids)) == 1).lower()
    tenant_match = "unknown" if not tenant_ids else str(expected_tenant in tenant_ids and len(set(tenant_ids)) == 1).lower()
    return resource_kind, id_match, tenant_match


def _safe_identity_flags(value: Any) -> tuple[bool | None, bool | None]:
    """Extract only identity booleans; never return profile values or names."""
    if not isinstance(value, dict):
        return None, None
    records = [value]
    for key in ("attributes", "identity", "principal", "user"):
        nested = value.get(key)
        if isinstance(nested, dict):
            records.append(nested)
    is_service_account: bool | None = None
    matches_target: bool | None = None
    for record in records:
        for key in ("isServiceAccount", "is_service_account"):
            flag = record.get(key)
            if isinstance(flag, bool):
                is_service_account = flag
                break
        for key in ("kind", "type", "principalType", "principal_type", "accountType"):
            kind = record.get(key)
            if isinstance(kind, str):
                normalized = re.sub(r"[^a-z]", "", kind.casefold())
                if "serviceaccount" in normalized or normalized == "serviceacc":
                    is_service_account = True
                    break
                if normalized in {"user", "humanuser", "useraccount"}:
                    is_service_account = False
                    break
        id_fields = ("serviceAccountID", "serviceAccountId", "service_account_id",
                     "accountID", "accountId", "account_id")
        for key in id_fields:
            identifier = record.get(key)
            if isinstance(identifier, str):
                matches_target = identifier == "8ca85d21-1fdc-43c5-ac8e-eaa8a509e082"
                if key.startswith("serviceAccount"):
                    is_service_account = True
                break
        if matches_target is None and is_service_account is True:
            identifier = record.get("id")
            if isinstance(identifier, str):
                matches_target = identifier == "8ca85d21-1fdc-43c5-ac8e-eaa8a509e082"
    if is_service_account is False and matches_target is None:
        matches_target = False
    return is_service_account, matches_target


def _http_status_from_safe_error(exc: Exception) -> int | None:
    match = re.search(r"\bHTTP (\d{3})\b", str(exc))
    return int(match.group(1)) if match else None


def _read_scope_label(status: int | None) -> str:
    if status is None:
        return "unavailable (safe error; details suppressed)"
    if status == 403:
        return "denied (HTTP 403)"
    if status == 401:
        return "unauthorized (HTTP 401)"
    return f"unavailable (HTTP {status})"


def resolve_dp(path: str | None) -> Path:
    if path:
        candidate = Path(path).expanduser()
    else:
        candidate = Path(shutil.which("dp") or DEFAULT_DP)
    try:
        info = candidate.stat()
    except OSError as exc:
        raise SafeAccessError("Spirit CLI `dp` was not found at the selected path.") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
        raise SafeAccessError("The selected dp path is not an executable regular file.")
    return candidate


def authenticate(key_path: Path, dp_path: str | None) -> int:
    load_key_file(key_path)
    executable = resolve_dp(dp_path)
    env = isolated_dp_environment()
    command = [str(executable), "auth", "service-acc", "--key-file", str(key_path.expanduser())]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        print("Authentication timed out; CLI output was suppressed.", file=sys.stderr)
        return 2
    except OSError:
        print("Could not start dp; CLI output was suppressed.", file=sys.stderr)
        return 2

    if result.returncode == 0:
        print(f"Authentication succeeded. Isolated dp state: {DP_DIR}")
        return 0
    print(f"Authentication failed (exit code {result.returncode}); CLI output was suppressed.", file=sys.stderr)
    return result.returncode or 1


def classify_cli_failure(output: str) -> str | None:
    """Return only an allowlisted failure label and an optional HTTP code."""
    lowered = output.casefold()
    phrase = next((label for needle, label in SAFE_FAILURE_PHRASES if needle in lowered), None)
    status = None
    for pattern in HTTP_STATUS_PATTERNS:
        match = pattern.search(output)
        if match:
            status = match.group(1)
            break
    if status and phrase:
        return f"{phrase} (HTTP {status})"
    if status:
        return f"HTTP {status}"
    return phrase


def _known_key_literals() -> tuple[str, ...]:
    """Load local credential literals only for in-memory diagnostic redaction."""
    try:
        document = load_key_file(KEY_PATH)
    except SafeAccessError:
        return ()
    attributes = document.get("attributes")
    if not isinstance(attributes, dict):
        return ()
    return tuple(
        value
        for value in attributes.values()
        if isinstance(value, str) and len(value) >= 4
    )


_PEM_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s\])}>]+")
_NAMED_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|"
    r"authorization|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[^\s,;]+"
)
_OPAQUE_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+~-]{24,}={0,2}(?![A-Za-z0-9])")
_DIAGNOSTIC_MARKER = re.compile(r"(?i)\b(?:error|failed?|failure|denied|forbidden|status|unauthorized)\b")
_DATED_TELEPORT_NAME = re.compile(r"teleport-identity-[0-9]{8}T[0-9]{6}Z\Z")


def redact_diagnostic_text(text: str, key_literals: tuple[str, ...] = ()) -> str:
    """Remove credentials and opaque values before any diagnostic is shown."""
    redacted = _PEM_PATTERN.sub("<redacted-pem>", text)
    for literal in sorted(set(key_literals), key=len, reverse=True):
        redacted = redacted.replace(literal, "<redacted-key>")
    redacted = _BEARER_PATTERN.sub("Bearer <redacted>", redacted)
    redacted = _JWT_PATTERN.sub("<redacted-jwt>", redacted)
    redacted = _URL_PATTERN.sub("<redacted-url>", redacted)
    redacted = _NAMED_SECRET_PATTERN.sub("credential=<redacted>", redacted)
    redacted = _OPAQUE_TOKEN_PATTERN.sub("<redacted-token>", redacted)
    return redacted


def safe_diagnostic_snippets(output: str, key_literals: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return a few short redacted error lines; suppress payload and structured output."""
    snippets: list[str] = []
    for raw_line in output.splitlines():
        if len(raw_line) > 4096:
            continue
        stripped = raw_line.strip()
        lowered = stripped.casefold()
        if not stripped or not _DIAGNOSTIC_MARKER.search(stripped):
            continue
        if any(marker in lowered for marker in ("response body", "payload", "request body")):
            continue
        # Structured data can carry arbitrary server response fields even when a key
        # name is not known, so do not print JSON-like lines at all.
        if "{" in stripped or "}" in stripped:
            continue
        redacted = redact_diagnostic_text(stripped, key_literals)
        redacted = " ".join(redacted.split())[:300]
        if redacted and redacted not in snippets:
            snippets.append(redacted)
        if len(snippets) == 3:
            break
    return tuple(snippets)


def _teleport_output_path(requested: str | Path | None) -> Path:
    output_dir = ensure_private_dir(TELEPORT_IDENTITY_PATH.parent)
    if requested is None:
        return output_dir / TELEPORT_IDENTITY_PATH.name
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        raise SafeAccessError("A renewed Teleport identity path must be absolute.")
    candidate = Path(os.path.abspath(candidate))
    if candidate.parent != output_dir or not _DATED_TELEPORT_NAME.fullmatch(candidate.name):
        raise SafeAccessError(
            "A renewed Teleport identity must use a new dated filename in the private Spirit config directory."
        )
    return candidate


def teleport_identity(dp_path: str | None, *, diagnose: bool = False,
                       output: str | Path | None = None) -> int:
    """Create a new Teleport identity without overwriting an existing file or exposing CLI output."""
    output_path = _teleport_output_path(output)
    try:
        output_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SafeAccessError("Could not inspect the Teleport identity output path.") from exc
    else:
        raise SafeAccessError("Teleport identity output already exists; refusing to overwrite it.")

    executable = resolve_dp(dp_path)
    env = isolated_dp_environment()
    command = [
        str(executable),
        "teleport",
        "identity",
        "--tenant",
        TELEPORT_TENANT,
        "--out",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        _remove_partial_identity(output_path)
        print("Teleport identity command timed out; CLI output was suppressed.", file=sys.stderr)
        return 2
    except OSError:
        _remove_partial_identity(output_path)
        print("Could not start the Teleport identity command; CLI output was suppressed.", file=sys.stderr)
        return 2

    if result.returncode != 0:
        _remove_partial_identity(output_path)
        cause = classify_cli_failure(f"{result.stdout}\n{result.stderr}")
        detail = f"; {cause}" if cause else ""
        print(
            f"Teleport identity failed (exit code {result.returncode}{detail}); CLI details were suppressed.",
            file=sys.stderr,
        )
        if diagnose:
            snippets = safe_diagnostic_snippets(
                f"{result.stdout}\n{result.stderr}", _known_key_literals()
            )
            if snippets:
                for snippet in snippets:
                    print(f"Sanitized diagnostic: {snippet}", file=sys.stderr)
            else:
                print("No allowlisted diagnostic lines matched.", file=sys.stderr)
        return result.returncode or 1

    try:
        info = output_path.lstat()
    except FileNotFoundError:
        print("Teleport identity command succeeded but created no output file; CLI output was suppressed.", file=sys.stderr)
        return 2
    except OSError:
        print("Could not inspect Teleport identity output; CLI output was suppressed.", file=sys.stderr)
        return 2
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        _remove_partial_identity(output_path)
        print("Teleport identity output is not a regular file owned by this user; CLI output was suppressed.", file=sys.stderr)
        return 2
    try:
        output_path.chmod(0o600)
        final_info = output_path.lstat()
    except OSError:
        print("Could not restrict Teleport identity permissions; CLI output was suppressed.", file=sys.stderr)
        return 2
    if stat.S_IMODE(final_info.st_mode) != 0o600:
        print("Teleport identity permissions are not private; CLI output was suppressed.", file=sys.stderr)
        return 2
    print(f"Teleport identity created privately at {output_path} (mode 0600).")
    return 0


def _remove_partial_identity(path: Path) -> None:
    """Remove only an owner-created regular file left at the preflight-absent path."""
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            path.chmod(0o600)
            path.unlink()
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely import a Spirit service-account key and authorize dp.")
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="Store a key from an explicit session JSONL or JSON file.")
    source = extract.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-session", metavar="SESSION_JSONL", type=Path, help="Read this explicit session JSONL path.")
    source.add_argument("--key-file", help="Explicit owner-only JSON key path; key contents are never accepted in argv.")
    extract.add_argument("--replace", action="store_true", help="Atomically replace an existing private key file.")

    imported = commands.add_parser("import", help="Copy an explicit service-account JSON into private local storage.")
    imported.add_argument("--key-file", required=True, help="Explicit owner-only JSON key path; key contents are never accepted in argv.")
    imported.add_argument("--replace", action="store_true", help="Atomically replace an existing private key file.")

    auth = commands.add_parser("auth", help="Authorize dp using the local private key and isolated state.")
    auth.add_argument("--key-file", default=str(KEY_PATH), help=f"Explicit key file (default: {KEY_PATH}).")
    auth.add_argument("--dp-bin", help="Optional path to the dp executable.")

    login = commands.add_parser("user-login", help="Sign in through ordinary browser OAuth in separate private state.")
    login.add_argument("--dp-bin", help="Optional path to the dp executable.")

    user_status = commands.add_parser(
        "user-status",
        help="Check a cached user token; optionally make fixed read-only identity/access checks.",
    )
    user_status.add_argument(
        "--verify",
        action="store_true",
        help="GET only userinfo and the configured tenant VM-group list; print sanitized status only.",
    )
    commands.add_parser(
        "user-sa-metadata",
        help="Compare fixed tenant-key/project-UUID paths for one service-account metadata resource.",
    )

    teleport = commands.add_parser("teleport-identity", help="Create a private Teleport identity for the configured tenant.")
    teleport.add_argument("--dp-bin", help="Optional path to the dp executable.")
    teleport.add_argument(
        "--output",
        help="Write a new dated identity in the private Spirit config directory; never overwrites the existing identity.",
    )
    teleport.add_argument("--diagnose", action="store_true", help="Show a few strictly redacted error lines on failure.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            attributes = load_key_from_session(args.from_session) if args.from_session else load_key_file(Path(args.key_file))
            key_path = write_key_file(attributes, KEY_PATH, replace=args.replace)
            print(f"Service-account key stored privately at {key_path} (mode 0600).")
            return 0
        if args.command == "import":
            attributes = load_key_file(Path(args.key_file))
            key_path = write_key_file(attributes, KEY_PATH, replace=args.replace)
            print(f"Service-account key imported privately at {key_path} (mode 0600).")
            return 0
        if args.command == "auth":
            return authenticate(Path(args.key_file).expanduser(), args.dp_bin)
        if args.command == "user-login":
            return user_login(args.dp_bin)
        if args.command == "user-status":
            return user_session_status(verify=args.verify)
        if args.command == "user-sa-metadata":
            return user_sa_metadata_status()
        if args.command == "teleport-identity":
            return teleport_identity(args.dp_bin, diagnose=args.diagnose, output=args.output)
    except SafeAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
