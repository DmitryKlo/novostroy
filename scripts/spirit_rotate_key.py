#!/usr/bin/env python3
"""Create one replacement key for the configured Spirit service account.

The script is deliberately two-step: ``plan`` writes a private, reviewable
request; ``apply`` requires its SHA-256, records a pending ledger before one
POST, and never retries an uncertain request. It does not revoke old keys.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import ssl
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import spirit_inventory as inventory
    from .spirit_deploy_access import (
        CONFIG_DIR,
        KEY_PATH,
        SafeAccessError,
        ensure_private_dir,
        load_key_file,
        _normalize_key_document,
        write_key_file,
    )
else:
    import spirit_inventory as inventory
    from spirit_deploy_access import (
        CONFIG_DIR,
        KEY_PATH,
        SafeAccessError,
        ensure_private_dir,
        load_key_file,
        _normalize_key_document,
        write_key_file,
    )


API_HOST = "spirit.culab.ru"
TENANT_ID = "hack-l2sh-2"
SERVICE_ACCOUNT_ID = "8ca85d21-1fdc-43c5-ac8e-eaa8a509e082"
DESCRIPTION = "Novostroy deployment CI 2026-10-10"
PLAN_PATH = CONFIG_DIR / "key-rotation-plan.json"
LEDGER_PATH = CONFIG_DIR / "key-rotation-pending.json"
NEXT_KEY_PATH = CONFIG_DIR / "service-account-next.json"
USER_PLAN_PATH = CONFIG_DIR / "key-rotation-user-plan.json"
USER_LEDGER_PATH = CONFIG_DIR / "key-rotation-user-pending.json"
USER_NEXT_KEY_PATH = CONFIG_DIR / "service-account-next-user.json"
MAX_PLAN_AGE = timedelta(days=1)
MAX_RESPONSE_BYTES = 256 * 1024


class RotationError(Exception):
    """Sanitized operator-facing error; never contains an API body or key."""


class RejectedCreate(RotationError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Spirit rejected key creation with HTTP {status}.")


class UncertainCreate(RotationError):
    def __init__(self, status: int | None = None) -> None:
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Key creation outcome is uncertain{suffix}; the pending ledger blocks retries.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_rotation_dir() -> Path:
    directory = ensure_private_dir(CONFIG_DIR)
    if directory != CONFIG_DIR:
        raise RotationError("Private rotation directory did not resolve as expected.")
    return directory


def _require_absent(path: Path, description: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RotationError(f"Could not inspect the {description} path.") from exc
    raise RotationError(f"The {description} already exists; refusing to replace or retry it.")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: dict[str, Any], *, exclusive: bool) -> None:
    directory = _ensure_rotation_dir()
    if path.parent != directory:
        raise RotationError("Rotation state path is outside the private configuration directory.")
    if exclusive:
        _require_absent(path, "rotation state")
    else:
        try:
            old = path.lstat()
        except OSError as exc:
            raise RotationError("Could not inspect existing rotation state.") from exc
        if (stat.S_ISLNK(old.st_mode) or not stat.S_ISREG(old.st_mode)
                or old.st_uid != os.getuid() or old.st_nlink != 1
                or stat.S_IMODE(old.st_mode) != 0o600):
            raise RotationError("Existing rotation state has unsafe type or ownership.")

    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".key-rotation-", suffix=".json", dir=directory)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            Path(temporary).unlink()
        else:
            os.replace(temporary, path)
        _fsync_directory(directory)
    except FileExistsError as exc:
        raise RotationError("Rotation state appeared concurrently; no request was sent.") from exc
    except OSError as exc:
        raise RotationError("Could not persist private rotation state safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RotationError("The reviewed private rotation plan is missing or unreadable.") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 64 * 1024):
        raise RotationError("Private rotation plan has unsafe type, owner, permissions, or size.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RotationError("Private rotation plan is invalid.") from exc
    if not isinstance(value, dict):
        raise RotationError("Private rotation plan has an invalid object shape.")
    return value


def _current_account() -> dict[str, str]:
    try:
        document = load_key_file(KEY_PATH)
    except SafeAccessError as exc:
        raise RotationError("The current private service-account key is unavailable.") from exc
    attributes = document.get("attributes")
    if not isinstance(attributes, dict):
        raise RotationError("The current key has an invalid service-account envelope.")
    service_account_id = attributes.get("serviceAccountID")
    tenant_key = attributes.get("tenantKey")
    if service_account_id != SERVICE_ACCOUNT_ID:
        raise RotationError("The current key does not match the reviewed service account.")
    if not isinstance(tenant_key, str) or not tenant_key:
        raise RotationError("The current key does not identify its tenant.")
    # Keep the opaque tenantKey only in process memory for response comparison.
    return {"serviceAccountID": service_account_id, "tenantKey": tenant_key}


def _request_path() -> str:
    return f"/api/v2/tenants/{TENANT_ID}/serviceaccounts/{SERVICE_ACCOUNT_ID}/keys"


def _rotation_paths(user_session: bool) -> tuple[Path, Path, Path, str]:
    if user_session:
        return USER_PLAN_PATH, USER_LEDGER_PATH, USER_NEXT_KEY_PATH, "user"
    return PLAN_PATH, LEDGER_PATH, NEXT_KEY_PATH, "service_account"


def _plan_body(expires_at: str) -> dict[str, Any]:
    return {
        "kind": "ServiceAccountKey",
        "attributes": {"description": DESCRIPTION, "expiresAt": expires_at},
    }


def prepare_plan(*, user_session: bool = False) -> dict[str, Any]:
    plan_path, ledger_path, next_key_path, auth_profile = _rotation_paths(user_session)
    _ensure_rotation_dir()
    _require_absent(plan_path, "key rotation plan")
    _require_absent(ledger_path, "key rotation ledger")
    _require_absent(next_key_path, "replacement key file")
    _current_account()
    created_at = _utcnow()
    plan: dict[str, Any] = {
        "schema_version": 1,
        "created_at": _utc_text(created_at),
        "auth_profile": auth_profile,
        "tenant_id": TENANT_ID,
        "service_account_id": SERVICE_ACCOUNT_ID,
        "request": {
            "method": "POST",
            "host": API_HOST,
            "path": _request_path(),
            "body": _plan_body(_utc_text(created_at + timedelta(days=30))),
        },
        "safety": {
            "max_post_attempts": 1,
            "retry_on_uncertain_response": False,
            "old_key_revocation": "not_requested",
        },
    }
    plan["plan_sha256"] = _canonical_hash(plan)
    _write_private_json(plan_path, plan, exclusive=True)
    return plan


def _validate_plan(plan: dict[str, Any], reviewed_sha256: str, *, user_session: bool = False) -> None:
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (not isinstance(reviewed_sha256, str) or len(reviewed_sha256) != 64
            or claimed != reviewed_sha256 or claimed != _canonical_hash(unsigned)):
        raise RotationError("The reviewed plan SHA-256 does not match the private plan.")
    _plan_path, _ledger_path, _next_key_path, auth_profile = _rotation_paths(user_session)
    stored_profile = plan.get("auth_profile", "service_account")
    if (plan.get("schema_version") != 1 or stored_profile != auth_profile
            or plan.get("tenant_id") != TENANT_ID
            or plan.get("service_account_id") != SERVICE_ACCOUNT_ID):
        raise RotationError("The private plan does not match the selected auth profile and service account.")
    request = plan.get("request")
    if not isinstance(request, dict):
        raise RotationError("The private plan request differs from the fixed key-creation contract.")
    body = request.get("body")
    attributes = body.get("attributes") if isinstance(body, dict) else None
    expires_at = attributes.get("expiresAt") if isinstance(attributes, dict) else None
    if request != {
        "method": "POST", "host": API_HOST, "path": _request_path(),
        "body": _plan_body(expires_at) if isinstance(expires_at, str) else None,
    }:
        raise RotationError("The private plan request differs from the fixed key-creation contract.")
    created_at_text = plan.get("created_at")
    try:
        created_at = datetime.fromisoformat(str(created_at_text).replace("Z", "+00:00"))
        expires_at_value = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RotationError("The private plan timestamp is invalid.") from exc
    now = _utcnow()
    age = now - created_at
    if (created_at.tzinfo is None or expires_at_value.tzinfo is None
            or age < timedelta(0) or age > MAX_PLAN_AGE
            or expires_at_value != created_at + timedelta(days=30)):
        raise RotationError("The private plan is stale or has an invalid 30-day expiry.")


def _create_key_once(token: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    attributes = body.get("attributes")
    expires_at = attributes.get("expiresAt") if isinstance(attributes, dict) else None
    if (path != _request_path() or not isinstance(expires_at, str)
            or body != _plan_body(expires_at)):
        raise RotationError("Request differs from the fixed key-creation contract.")
    context = ssl.create_default_context(
        cafile=str(inventory.SYSTEM_CA_BUNDLE) if inventory.SYSTEM_CA_BUNDLE.is_file() else None
    )
    connection = http.client.HTTPSConnection(API_HOST, timeout=30, context=context)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        connection.request("POST", path, body=encoded, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Novostroy service-account key rotation/1",
        })
        response = connection.getresponse()
        status = response.status
        if 400 <= status < 500:
            raise RejectedCreate(status)
        if status not in {200, 201}:
            raise UncertainCreate(status)
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise UncertainCreate(status)
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if not data or len(data) > MAX_RESPONSE_BYTES:
            raise UncertainCreate(status)
        try:
            return status, json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UncertainCreate(status) from exc
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
        raise UncertainCreate() from exc
    finally:
        connection.close()


def _validate_created_key(value: Any, current: dict[str, str]) -> dict[str, Any]:
    try:
        key_document = _normalize_key_document(value)
    except SafeAccessError as exc:
        raise RotationError("Spirit returned a key response with an unexpected schema.") from exc
    attributes = key_document["attributes"]
    if (attributes.get("serviceAccountID") != SERVICE_ACCOUNT_ID
            or attributes.get("tenantKey") != current["tenantKey"]):
        raise RotationError("Spirit returned a key for a different service account or tenant.")
    return key_document


def apply_plan(reviewed_sha256: str, *, user_session: bool = False) -> dict[str, Any]:
    plan_path, ledger_path, next_key_path, auth_profile = _rotation_paths(user_session)
    _ensure_rotation_dir()
    if ledger_path.exists() or ledger_path.is_symlink():
        raise RotationError("A rotation attempt already exists; no second POST is allowed.")
    _require_absent(next_key_path, "replacement key file")
    plan = _read_private_json(plan_path)
    _validate_plan(plan, reviewed_sha256, user_session=user_session)
    current = _current_account()
    token = inventory._token_from_dp(user_session=user_session)

    created_at = _utc_text(_utcnow())
    ledger: dict[str, Any] = {
        "schema_version": 1,
        "state": "pending",
        "created_at": created_at,
        "auth_profile": auth_profile,
        "plan_sha256": plan["plan_sha256"],
        "tenant_id": TENANT_ID,
        "service_account_id": SERVICE_ACCOUNT_ID,
        "request_path": _request_path(),
        "expires_at": plan["request"]["body"]["attributes"]["expiresAt"],
        "http_status": None,
        "retry_allowed": False,
        "old_key_revocation": "not_requested",
    }
    _write_private_json(ledger_path, ledger, exclusive=True)
    try:
        status, response = _create_key_once(token, _request_path(), plan["request"]["body"])
    except RejectedCreate as exc:
        ledger.update({"state": "rejected", "http_status": exc.status,
                       "completed_at": _utc_text(_utcnow())})
        _write_private_json(ledger_path, ledger, exclusive=False)
        raise
    except UncertainCreate as exc:
        ledger.update({"state": "indeterminate", "http_status": exc.status,
                       "completed_at": _utc_text(_utcnow())})
        _write_private_json(ledger_path, ledger, exclusive=False)
        raise

    ledger.update({"state": "response_received", "http_status": status,
                   "completed_at": _utc_text(_utcnow())})
    _write_private_json(ledger_path, ledger, exclusive=False)
    try:
        next_document = _validate_created_key(response, current)
    except RotationError:
        ledger.update({"state": "created_response_invalid", "completed_at": _utc_text(_utcnow())})
        _write_private_json(ledger_path, ledger, exclusive=False)
        raise
    try:
        write_key_file(next_document, next_key_path, replace=False)
    except SafeAccessError as exc:
        ledger.update({"state": "created_key_not_saved", "completed_at": _utc_text(_utcnow())})
        _write_private_json(ledger_path, ledger, exclusive=False)
        raise RotationError("Spirit created a key but it could not be saved privately; do not retry.") from exc
    ledger.update({"state": "key_saved", "replacement_key_path": str(next_key_path),
                   "completed_at": _utc_text(_utcnow())})
    _write_private_json(ledger_path, ledger, exclusive=False)
    return ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="Write a private, reviewable key-create plan without API mutation.")
    plan_parser.add_argument("--user-session", action="store_true",
                             help="Use the separate browser OAuth user session (distinct plan and ledger).")
    apply = commands.add_parser("apply", help="Send exactly one reviewed key-create POST.")
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--user-session", action="store_true",
                        help="Use the separate browser OAuth user session (distinct plan and ledger).")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan_path, _ledger_path, _next_key_path, auth_profile = _rotation_paths(args.user_session)
            plan = prepare_plan(user_session=args.user_session)
            print(f"Private {auth_profile} key-creation plan saved at {plan_path}.")
            print(f"Review this plan and pass its SHA-256: {plan['plan_sha256']}")
            return 0
        _plan_path, _ledger_path, next_key_path, auth_profile = _rotation_paths(args.user_session)
        ledger = apply_plan(args.plan_sha256, user_session=args.user_session)
        print(f"Replacement key saved privately at {next_key_path}; auth profile {auth_profile}; ledger state {ledger['state']}.")
        print("The old key was not revoked. Validate the replacement with the isolated dp auth command.")
        return 0
    except RejectedCreate as exc:
        print(f"Key creation rejected: HTTP {exc.status}; response body suppressed; no retry.", file=sys.stderr)
        return 2
    except (RotationError, inventory.InventoryError, SafeAccessError) as exc:
        print(f"Key rotation stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Key rotation stopped: local filesystem error; no raw details emitted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
