#!/usr/bin/env python3
"""Plan one corrected 20-GB-root Spirit VM after verified cleanup.

This is a new request with separate immutable plan and attempt ledgers. It is
allowed only after the old failed VM/root volume are removed, the cleanup
operation completed, and the group disk reservation returned to zero. Apply
refreshes those facts with GETs and makes at most one POST.
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import spirit_cleanup_failed_vm as cleanup
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
else:
    import spirit_cleanup_failed_vm as cleanup
    import spirit_inventory as inventory
    import spirit_provision as provision


PLAN_PATH = provision.DEPLOY_DIR / "corrected-20gb-plan.json"
ATTEMPT_PATH = provision.DEPLOY_DIR / "corrected-20gb-attempt.json"
ROOT_DISK_GB = 20
MAX_EVIDENCE_AGE = timedelta(minutes=30)
SUCCESS_STATES = frozenset({"done", "completed", "success", "succeeded"})


class CorrectedCreateError(Exception):
    """Sanitized operator-facing corrected-create error."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CorrectedCreateError("Evidence timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorrectedCreateError("Evidence timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        raise CorrectedCreateError("Evidence timestamp lacks a timezone.")
    return parsed.astimezone(timezone.utc)


def _cleanup_ready(postcheck: dict[str, Any], attempt: dict[str, Any], *, check_age: bool) -> None:
    result = attempt.get("result")
    operation_id = inventory._identifier(result.get("id")) if isinstance(result, dict) else None
    if (attempt.get("state") != "submitted" or attempt.get("http_status") not in {200, 202, 204}
            or attempt.get("vm_id") != cleanup.EXPECTED_VM_ID
            or attempt.get("volume_id") != cleanup.EXPECTED_VOLUME_ID
            or not isinstance(result, dict) or result.get("kind") != "Operation"
            or operation_id is None):
        raise CorrectedCreateError("Failed-VM cleanup attempt is not a submitted exact-target operation.")
    if (postcheck.get("tenant_id") != provision.TENANT_ID
            or postcheck.get("vm_group_id") != provision.GROUP_ID
            or postcheck.get("vm_id") != cleanup.EXPECTED_VM_ID
            or postcheck.get("volume_id") != cleanup.EXPECTED_VOLUME_ID
            or postcheck.get("cleanup_operation_id") != operation_id
            or postcheck.get("cleanup_operation_status") not in SUCCESS_STATES
            or postcheck.get("cleanup_operation_succeeded") is not True
            or postcheck.get("vm_removed") is not True
            or postcheck.get("volume_removed") is not True
            or postcheck.get("group_quota_used_disk_gb") != 0):
        raise CorrectedCreateError("Cleanup operation, tombstones, or released disk quota are not confirmed.")
    observed = _time(postcheck.get("observed_at"))
    if observed < _time(attempt.get("completed_at")):
        raise CorrectedCreateError("Cleanup check predates the DELETE attempt completion.")
    if check_age and not timedelta(0) <= _utcnow() - observed <= MAX_EVIDENCE_AGE:
        raise CorrectedCreateError("Cleanup check is stale; refresh it with read-only verify.")


def _remaining_20(group: dict[str, Any], flavor: dict[str, Any]) -> dict[str, int | float]:
    maximum, used = group.get("quota_max") or {}, group.get("quota_used") or {}
    cpu, memory_mib = flavor.get("cpu"), flavor.get("memory")
    if (not isinstance(cpu, (int, float)) or isinstance(cpu, bool) or cpu <= 0
            or not isinstance(memory_mib, (int, float)) or isinstance(memory_mib, bool)
            or memory_mib <= 0):
        raise CorrectedCreateError("Selected flavor has incomplete CPU or memory metadata.")
    required = {"cpu": cpu, "memory": math.ceil(memory_mib / 1024),
                "disk": ROOT_DISK_GB, "vms": 1}
    remaining: dict[str, int | float] = {}
    for key, need in required.items():
        limit, consumption = maximum.get(key), used.get(key)
        if (not isinstance(limit, (int, float)) or isinstance(limit, bool)
                or not isinstance(consumption, (int, float)) or isinstance(consumption, bool)
                or limit - consumption < need):
            raise CorrectedCreateError(f"Group lacks verified free {key} quota for the corrected VM.")
        remaining[key] = limit - consumption
    if used.get("disk") != 0:
        raise CorrectedCreateError("Previous 40-GB disk reservation has not returned to zero.")
    return remaining


def _select(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if source.get("tenant_id") != provision.TENANT_ID or source.get("vm_groups_truncated"):
        raise CorrectedCreateError("Tenant or VM group inventory is incomplete.")
    groups = [item for item in source.get("vm_groups", [])
              if isinstance(item, dict) and item.get("id") == provision.GROUP_ID]
    if len(groups) != 1 or groups[0].get("status") != "available":
        raise CorrectedCreateError("Selected VM group is absent, ambiguous, or unavailable.")
    group = groups[0]
    if group.get("vms_truncated") or group.get("flavors_truncated") or group.get("images_truncated"):
        raise CorrectedCreateError("Selected VM, flavor, or image listing is truncated.")
    same_name = [vm for vm in group.get("vms", [])
                 if isinstance(vm, dict) and str(vm.get("name", "")).casefold() == provision.VM_NAME.casefold()]
    if any(vm.get("id") != cleanup.EXPECTED_VM_ID or vm.get("status") != "removed"
           for vm in same_name):
        raise CorrectedCreateError("An active or unrelated VM holds the selected name.")
    flavors = [item for item in group.get("flavors", [])
               if isinstance(item, dict) and item.get("id") == provision.FLAVOR_ID]
    images = [item for item in group.get("images", [])
              if isinstance(item, dict) and item.get("id") == provision.IMAGE_ID]
    if len(flavors) != 1 or len(images) != 1:
        raise CorrectedCreateError("Selected flavor or image is absent or ambiguous.")
    flavor, image = flavors[0], images[0]
    if flavor.get("enabled") is not True or flavor.get("public") is False:
        raise CorrectedCreateError("Selected flavor is disabled or inaccessible.")
    if image.get("disk") != ROOT_DISK_GB:
        raise CorrectedCreateError("Selected image catalog disk value is not 20.")
    _remaining_20(group, flavor)
    return group, flavor, image


def _body_20() -> dict[str, Any]:
    body = copy.deepcopy(provision._create_body())
    body["attributes"]["volumes"]["contents"][0]["attributes"]["size"] = ROOT_DISK_GB
    return body


def prepare_plan() -> dict[str, Any]:
    if PLAN_PATH.exists() or PLAN_PATH.is_symlink() or ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise CorrectedCreateError("A corrected plan or create attempt already exists; replacement is disabled.")
    cleanup_attempt = provision._read_private_json(cleanup.CLEANUP_ATTEMPT)
    postcheck = provision._read_private_json(cleanup.POSTCHECK_REPORT)
    _cleanup_ready(postcheck, cleanup_attempt, check_age=True)
    source = provision._read_private_json(provision.INVENTORY_PATH)
    inventory_at = _time(source.get("observed_at"))
    if (inventory_at < _time(postcheck.get("observed_at"))
            or not timedelta(0) <= _utcnow() - inventory_at <= MAX_EVIDENCE_AGE):
        raise CorrectedCreateError("Inventory must be refreshed after verified cleanup.")
    group, flavor, image = _select(source)
    path = f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm"
    body = _body_20()
    original_body = provision._create_body()
    if (body["attributes"]["volumes"]["contents"][0]["attributes"]["size"] != ROOT_DISK_GB
            or original_body["attributes"]["volumes"]["contents"][0]["attributes"]["size"] != 40):
        raise CorrectedCreateError("Corrected request body is not the reviewed 40-to-20-GB change.")
    plan: dict[str, Any] = {
        "schema_version": 1, "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "previous_provision_plan_sha256": cleanup.EXPECTED_PROVISION_SHA,
        "cleanup_plan_sha256": cleanup_attempt.get("plan_sha256"),
        "cleanup_operation_id": postcheck["cleanup_operation_id"],
        "cleanup_observed_at": postcheck["observed_at"],
        "inventory_observed_at": source["observed_at"],
        "selection": {"vm_name": provision.VM_NAME, "flavor": flavor, "image": image,
                      "root_volume_gb": ROOT_DISK_GB,
                      "group_quota_remaining_before_create": _remaining_20(group, flavor),
                      "public_ip": False, "gpu": False},
        "difference_from_first_request": {"root_volume_gb": {"old": 40, "new": ROOT_DISK_GB}},
        "request": {"method": "POST", "path": path, "body": body},
        "request_sha256": provision._canonical_hash(body),
        "safety": {"create_attempts_max": 1, "retry_on_uncertain_response": False,
                   "requires_cleanup_operation_success": True,
                   "requires_removed_vm_and_volume": True,
                   "provider_zone_capacity": "unverified; 20 GB may still be rejected"},
    }
    plan["plan_sha256"] = provision._canonical_hash(plan)
    provision._write_private_json(PLAN_PATH, plan, exclusive=True)
    return plan


def _fresh_preflight(token: str) -> dict[str, Any]:
    cleanup_report = cleanup.verify_cleanup(token=token)
    cleanup_attempt = provision._read_private_json(cleanup.CLEANUP_ATTEMPT)
    _cleanup_ready(cleanup_report, cleanup_attempt, check_age=True)
    groups, group_truncated = inventory._paged_groups(token, provision.TENANT_ID)
    if group_truncated:
        raise CorrectedCreateError("Fresh VM group listing is truncated.")
    selected = [group for group in groups if group.get("id") == provision.GROUP_ID]
    if len(selected) != 1:
        raise CorrectedCreateError("Selected VM group is missing in fresh API data.")
    group = selected[0]
    group["vms"], group["vms_truncated"] = inventory._paged_vms(
        token, provision.TENANT_ID, provision.GROUP_ID)
    group["flavors"], group["flavors_truncated"] = inventory._catalog(
        token, provision.GROUP_ID, "flavors")
    group["images"], group["images_truncated"] = inventory._catalog(
        token, provision.GROUP_ID, "images")
    _, flavor, image = _select({"tenant_id": provision.TENANT_ID,
                                "vm_groups": [group], "vm_groups_truncated": False})
    return {"checked_at": _utcnow().isoformat().replace("+00:00", "Z"),
            "cleanup_operation_status": cleanup_report["cleanup_operation_status"],
            "vm_removed": cleanup_report["vm_removed"],
            "volume_removed": cleanup_report["volume_removed"],
            "group_disk_used_gb": group["quota_used"]["disk"],
            "quota_remaining_before_create": _remaining_20(group, flavor),
            "image_catalog_disk_value": image["disk"]}


def apply_plan(reviewed_sha256: str) -> dict[str, Any]:
    plan = provision._read_private_json(PLAN_PATH)
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not isinstance(reviewed_sha256, str) or claimed != reviewed_sha256 or claimed != provision._canonical_hash(unsigned):
        raise CorrectedCreateError("Reviewed corrected plan SHA-256 does not match the current plan.")
    expected_path = f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm"
    if plan.get("request") != {"method": "POST", "path": expected_path, "body": _body_20()}:
        raise CorrectedCreateError("Reviewed corrected request differs from the one 20-GB VM target.")
    if ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise CorrectedCreateError("A corrected create attempt already exists; no second POST is allowed.")
    token = inventory._token_from_dp()
    preflight = _fresh_preflight(token)
    attempt: dict[str, Any] = {
        "schema_version": 1, "state": "pending",
        "attempted_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "plan_sha256": claimed, "request_sha256": plan["request_sha256"],
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_name": provision.VM_NAME, "root_volume_gb": ROOT_DISK_GB,
        "preflight": preflight, "http_status": None, "result": None,
    }
    provision._write_private_json(ATTEMPT_PATH, attempt, exclusive=True)
    try:
        http_status, response = provision._post_once(token, expected_path, _body_20())
    except provision.RejectedCreate as exc:
        attempt.update({"state": "rejected", "http_status": exc.status,
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(ATTEMPT_PATH, attempt)
        raise
    except provision.ProvisionError:
        attempt.update({"state": "indeterminate",
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(ATTEMPT_PATH, attempt)
        raise
    attempt.update({"state": "submitted", "http_status": http_status,
                    "result": provision._result_summary(response),
                    "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
    provision._write_private_json(ATTEMPT_PATH, attempt)
    return attempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Write one immutable 20-GB request plan after verified cleanup.")
    apply = commands.add_parser("apply", help="Fresh-check and send exactly one reviewed corrected VM create.")
    apply.add_argument("--plan-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = prepare_plan()
            print(f"Corrected 20-GB VM plan saved privately: {PLAN_PATH}")
            print(f"Plan SHA-256 for review: {plan['plan_sha256']}")
        else:
            attempt = apply_plan(args.plan_sha256)
            print(f"One corrected VM create attempt recorded privately: {ATTEMPT_PATH}")
            print(f"HTTP {attempt['http_status']}; state {attempt['state']}; "
                  f"response {attempt['result']['kind'] or 'kind unknown'} "
                  f"ID {attempt['result']['id'] or 'not in response'}.")
        return 0
    except (CorrectedCreateError, cleanup.CleanupError, provision.ProvisionError,
            inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit corrected VM create stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit corrected VM create stopped: local filesystem error.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
