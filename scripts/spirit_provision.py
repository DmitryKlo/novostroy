#!/usr/bin/env python3
"""Plan one Spirit VM and apply one reviewed create request at most once.

Plan mode is offline. Apply mode checks current group quota, available catalog
entries, and name conflicts with GET requests before writing a private attempt
ledger and issuing one POST. A pending or uncertain attempt is never retried.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import ssl
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import spirit_inventory as inventory
else:
    import spirit_inventory as inventory


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / ".runtime/deploy"
INVENTORY_PATH = DEPLOY_DIR / "inventory.json"
PLAN_PATH = DEPLOY_DIR / "provision-plan.json"
ATTEMPT_PATH = DEPLOY_DIR / "provision-attempt.json"

TENANT_ID = "hack-l2sh-2"
GROUP_ID = "1cf41dbd-b6d8-425a-9bcd-894e9bf64878"
FLAVOR_ID = "bb650ad1-91dd-458a-957e-2abef36dc091"
IMAGE_ID = "b38f0504-54ec-4e51-bcd0-2870db520ef9"
VM_NAME = "novostroy-web"
DISK_GB = 40
MAX_INVENTORY_AGE_SECONDS = 1800
MAX_RESPONSE_BYTES = 1024 * 1024


class ProvisionError(Exception):
    """A sanitized operator-facing error with no API response body."""


class RejectedCreate(ProvisionError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"VM create request returned HTTP {status}.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProvisionError("Required private deployment file is missing.") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 2 * 1024 * 1024):
        raise ProvisionError("Private deployment file has unsafe ownership, mode, or size.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("Private deployment file is unreadable or invalid.") from exc
    if not isinstance(value, dict):
        raise ProvisionError("Private deployment file has an invalid object shape.")
    return value


def _private_directory() -> Path:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = DEPLOY_DIR.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ProvisionError("Private deployment directory is unsafe.")
    DEPLOY_DIR.chmod(0o700)
    return DEPLOY_DIR


def _write_private_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    parent = _private_directory()
    if path.parent != parent:
        raise ProvisionError("Output path is outside the private deployment directory.")
    if exclusive and path.exists():
        raise ProvisionError("A provision attempt already exists; no second POST is allowed.")
    descriptor, temporary = tempfile.mkstemp(prefix=".provision-", suffix=".json", dir=parent)
    try:
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
            if path.exists() and (path.is_symlink() or not path.is_file()):
                raise ProvisionError("Existing deployment output path is unsafe.")
            os.replace(temporary, path)
    except FileExistsError as exc:
        raise ProvisionError("A provision attempt already exists; no second POST is allowed.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _reference(resource_kind: str, identifier: str) -> dict[str, Any]:
    return {"kind": "Reference", "resourceKind": resource_kind,
            "identifier": {"name": "id", "value": identifier}}


def _create_body() -> dict[str, Any]:
    return {"kind": "CloudVm", "attributes": {
        "name": VM_NAME,
        "flavor": _reference("CloudFlavor", FLAVOR_ID),
        "image": _reference("CloudImage", IMAGE_ID),
        "withGpu": False,
        "withPublicIp": False,
        "volumes": {"kind": "Collection", "contents": [
            {"kind": "CloudVolume", "attributes": {"size": DISK_GB}}]},
    }}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remaining(group: dict[str, Any], flavor: dict[str, Any]) -> dict[str, float]:
    maximum = group.get("quota_max") or {}
    used = group.get("quota_used") or {}
    cpu = flavor.get("cpu")
    memory_mib = flavor.get("memory")
    if (not isinstance(cpu, (int, float)) or isinstance(cpu, bool) or cpu <= 0
            or not isinstance(memory_mib, (int, float)) or isinstance(memory_mib, bool) or memory_mib <= 0):
        raise ProvisionError("Selected flavor has incomplete CPU or memory metadata.")
    # Cloud Manager catalog memory uses MiB; quota memory uses GiB.
    required = {"cpu": float(cpu), "memory": float(math.ceil(memory_mib / 1024)),
                "disk": float(DISK_GB), "vms": 1.0}
    for key, need in required.items():
        ceiling, consumed = maximum.get(key), used.get(key)
        if (not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool)
                or not isinstance(consumed, (int, float)) or isinstance(consumed, bool)
                or ceiling - consumed < need):
            raise ProvisionError(f"VM group lacks verified free {key} quota.")
    return {key: maximum[key] - used[key] for key in required}


def _target_from_inventory(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if source.get("tenant_id") != TENANT_ID:
        raise ProvisionError("Inventory tenant does not match the selected tenant.")
    groups = [group for group in source.get("vm_groups", [])
              if isinstance(group, dict) and group.get("id") == GROUP_ID]
    if len(groups) != 1:
        raise ProvisionError("Selected VM group is absent or ambiguous in inventory.")
    group = groups[0]
    if group.get("status") != "available":
        raise ProvisionError("Selected VM group is not available.")
    flavors = [item for item in group.get("flavors", [])
               if isinstance(item, dict) and item.get("id") == FLAVOR_ID]
    images = [item for item in group.get("images", [])
              if isinstance(item, dict) and item.get("id") == IMAGE_ID]
    if len(flavors) != 1 or len(images) != 1:
        raise ProvisionError("Selected flavor or image is absent or ambiguous.")
    flavor, image = flavors[0], images[0]
    if flavor.get("enabled") is not True or flavor.get("public") is False:
        raise ProvisionError("Selected flavor is disabled or inaccessible.")
    # The catalog exposes image.disk=20 but the inspected UI does not prove its
    # unit, so it is reported for review and not treated as a capacity proof.
    _remaining(group, flavor)
    if any(isinstance(vm, dict) and str(vm.get("name", "")).casefold() == VM_NAME.casefold()
           for vm in group.get("vms", [])):
        raise ProvisionError("A VM with the selected name already exists.")
    if group.get("vms_truncated") or group.get("flavors_truncated") or group.get("images_truncated"):
        raise ProvisionError("Inventory is truncated for the selected VM group.")
    return group, flavor, image


def prepare_plan() -> dict[str, Any]:
    if PLAN_PATH.exists() or PLAN_PATH.is_symlink() or ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise ProvisionError("A reviewed VM plan or provision attempt already exists; plan replacement is disabled.")
    source = _read_private_json(INVENTORY_PATH)
    observed = source.get("observed_at")
    try:
        observed_at = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvisionError("Inventory timestamp is invalid.") from exc
    age = (_utcnow() - observed_at).total_seconds()
    if not 0 <= age <= MAX_INVENTORY_AGE_SECONDS:
        raise ProvisionError("Inventory is stale; refresh read-only inventory first.")
    group, flavor, image = _target_from_inventory(source)
    request_path = f"/api/v2/tenants/{TENANT_ID}/vmgroup/{GROUP_ID}/vm"
    request_body = _create_body()
    plan: dict[str, Any] = {
        "schema_version": 1,
        "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "inventory_observed_at": observed,
        "tenant_id": TENANT_ID,
        "vm_group": {"id": GROUP_ID, "name": group.get("name"),
                     "availability_zone": group.get("availability_zone"),
                     "quota_remaining_before_create": _remaining(group, flavor)},
        "selection": {"vm_name": VM_NAME, "flavor": flavor, "image": image,
                      "root_volume_gb": DISK_GB, "public_ip": False, "gpu": False,
                      "unit_evidence": {"flavor_memory": "MiB", "group_memory_quota": "GiB",
                                        "volume_size": "GB", "image_disk": "unverified"}},
        "request": {"method": "POST", "path": request_path, "body": request_body},
        "request_sha256": _canonical_hash(request_body),
        "safety": {"create_attempts_max": 1, "retry_on_uncertain_response": False,
                   "preflight": "fresh group quota, VM name, flavor, image"},
    }
    plan["plan_sha256"] = _canonical_hash(plan)
    _write_private_json(PLAN_PATH, plan)
    return plan


def _fresh_preflight(token: str, plan: dict[str, Any]) -> dict[str, Any]:
    groups, truncated = inventory._paged_groups(token, TENANT_ID)
    if truncated:
        raise ProvisionError("Current VM group listing is truncated.")
    selected = [group for group in groups if group.get("id") == GROUP_ID]
    if len(selected) != 1:
        raise ProvisionError("Selected VM group is missing in the current API response.")
    group = selected[0]
    if group.get("status") != "available":
        raise ProvisionError("Selected VM group is no longer available.")
    vms, vm_truncated = inventory._paged_vms(token, TENANT_ID, GROUP_ID)
    if vm_truncated:
        raise ProvisionError("Current VM list is truncated; name uniqueness is unknown.")
    if any(str(vm.get("name", "")).casefold() == VM_NAME.casefold() for vm in vms):
        raise ProvisionError("A VM with the planned name already exists; no POST was sent.")
    flavors, flavor_truncated = inventory._catalog(token, GROUP_ID, "flavors")
    images, image_truncated = inventory._catalog(token, GROUP_ID, "images")
    if flavor_truncated or image_truncated:
        raise ProvisionError("Current flavor or image catalog is truncated.")
    group["flavors"], group["images"], group["vms"] = flavors, images, vms
    _target_from_inventory({"tenant_id": TENANT_ID, "vm_groups": [group]})
    flavor = next(item for item in flavors if item["id"] == FLAVOR_ID)
    return {"checked_at": _utcnow().isoformat().replace("+00:00", "Z"),
            "quota_remaining_before_create": _remaining(group, flavor),
            "existing_vm_count": len(vms)}


def _post_once(token: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    expected = f"/api/v2/tenants/{TENANT_ID}/vmgroup/{GROUP_ID}/vm"
    if path != expected:
        raise ProvisionError("Create path differs from the reviewed VM target.")
    context = ssl.create_default_context(cafile=str(inventory.SYSTEM_CA_BUNDLE)
                                         if inventory.SYSTEM_CA_BUNDLE.is_file() else None)
    connection = http.client.HTTPSConnection(inventory.API_HOST, timeout=30, context=context)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    try:
        connection.request("POST", path, body=encoded, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "Novostroy one-VM provision/1"})
        response = connection.getresponse()
        status = response.status
        if 400 <= status < 500:
            raise RejectedCreate(status)
        if status not in {200, 201, 202}:
            raise ProvisionError(f"VM create response was uncertain (HTTP {status}).")
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ProvisionError("VM create response exceeded the size limit; status is uncertain.")
        return status, json.loads(content) if content else None
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("VM create transport or response was uncertain.") from exc
    finally:
        connection.close()


def _result_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"kind": None, "id": None, "status": None}
    attributes = inventory._attributes(value)
    return {"kind": inventory._short_text(value.get("kind"), max_length=60),
            "id": inventory._identifier(attributes.get("id")),
            "status": inventory._short_text(attributes.get("status"), max_length=60)}


def apply_plan(reviewed_sha256: str) -> dict[str, Any]:
    if not isinstance(reviewed_sha256, str) or len(reviewed_sha256) != 64:
        raise ProvisionError("Apply requires the reviewed plan SHA-256.")
    plan = _read_private_json(PLAN_PATH)
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if claimed != _canonical_hash(unsigned) or claimed != reviewed_sha256:
        raise ProvisionError("Reviewed plan SHA-256 does not match the current plan.")
    if plan.get("request", {}).get("body") != _create_body():
        raise ProvisionError("Reviewed request differs from the one-VM target.")
    if ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise ProvisionError("A provision attempt already exists; no second POST is allowed.")
    token = inventory._token_from_dp()
    try:
        preflight = _fresh_preflight(token, plan)
    except inventory.InventoryError as exc:
        raise ProvisionError(str(exc)) from exc
    attempt: dict[str, Any] = {
        "schema_version": 1, "state": "pending", "attempted_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "plan_sha256": claimed, "request_sha256": plan["request_sha256"],
        "tenant_id": TENANT_ID, "vm_group_id": GROUP_ID, "vm_name": VM_NAME,
        "preflight": preflight, "http_status": None, "result": None,
    }
    _write_private_json(ATTEMPT_PATH, attempt, exclusive=True)
    try:
        status, response = _post_once(token, plan["request"]["path"], plan["request"]["body"])
    except RejectedCreate as exc:
        attempt.update({"state": "rejected", "http_status": exc.status,
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        _write_private_json(ATTEMPT_PATH, attempt)
        raise
    except ProvisionError:
        attempt.update({"state": "indeterminate",
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        _write_private_json(ATTEMPT_PATH, attempt)
        raise
    attempt.update({"state": "submitted", "http_status": status,
                    "result": _result_summary(response),
                    "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
    _write_private_json(ATTEMPT_PATH, attempt)
    return attempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Write a private, reviewable VM create plan without API mutation.")
    apply = commands.add_parser("apply", help="Fresh-check and send exactly one reviewed create request.")
    apply.add_argument("--plan-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = prepare_plan()
            print(f"One-VM plan saved privately: {PLAN_PATH}")
            print(f"Plan SHA-256 for review: {plan['plan_sha256']}")
            return 0
        attempt = apply_plan(args.plan_sha256)
        print(f"One create attempt recorded privately: {ATTEMPT_PATH}")
        print(f"HTTP {attempt['http_status']}; state {attempt['state']}; "
              f"response {attempt['result']['kind'] or 'kind unknown'} "
              f"ID {attempt['result']['id'] or 'not in response'}.")
        return 0
    except (ProvisionError, inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit VM provision stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit VM provision stopped: local filesystem error.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
