#!/usr/bin/env python3
"""Plan and submit at most one exact `ttt` VM create in Spirit main-d.

The plan is made from a recent private inventory. Apply refreshes group, VM,
flavor, image and quota data before writing an exclusive attempt ledger. No
existing VM is deleted or restarted, and an uncertain POST is never repeated.
"""

from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
else:
    import spirit_inventory as inventory
    import spirit_provision as provision


PLAN_PATH = provision.DEPLOY_DIR / "create-ttt-plan.json"
ATTEMPT_PATH = provision.DEPLOY_DIR / "create-ttt-attempt.json"
VM_NAME = "ttt"
ROOT_DISK_GB = 20
GROUP_NAME = "main-d"
ZONE = "ru-central1-d"


class TttCreateError(Exception):
    """Sanitized operator-facing stop reason."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _recent_inventory(source: dict[str, Any]) -> None:
    value = source.get("observed_at")
    if not isinstance(value, str):
        raise TttCreateError("Inventory timestamp is missing.")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TttCreateError("Inventory timestamp is invalid.") from exc
    if observed.tzinfo is None:
        raise TttCreateError("Inventory timestamp lacks a timezone.")
    age = (_utcnow() - observed.astimezone(timezone.utc)).total_seconds()
    if not 0 <= age <= provision.MAX_INVENTORY_AGE_SECONDS:
        raise TttCreateError("Inventory is stale; refresh read-only inventory first.")


def _body() -> dict[str, Any]:
    body = copy.deepcopy(provision._create_body())
    body["attributes"]["name"] = VM_NAME
    body["attributes"]["volumes"]["contents"][0]["attributes"]["size"] = ROOT_DISK_GB
    return body


def _select(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if source.get("tenant_id") != provision.TENANT_ID or source.get("vm_groups_truncated"):
        raise TttCreateError("Tenant or VM group inventory is incomplete.")
    groups = [item for item in source.get("vm_groups", [])
              if isinstance(item, dict) and item.get("id") == provision.GROUP_ID]
    if len(groups) != 1:
        raise TttCreateError("Exact main-d VM group is missing or ambiguous.")
    group = groups[0]
    if (group.get("name") != GROUP_NAME or group.get("availability_zone") != ZONE
            or group.get("status") != "available"):
        raise TttCreateError("Exact main-d group is unavailable or changed.")
    if any(group.get(key) for key in ("vms_truncated", "flavors_truncated", "images_truncated")):
        raise TttCreateError("VM or catalog listing is truncated.")
    if not all(isinstance(group.get(key), list) for key in ("vms", "flavors", "images")):
        raise TttCreateError("VM or catalog listing is missing.")
    current = [vm for vm in group["vms"] if isinstance(vm, dict)
               and str(vm.get("name", "")).casefold() == VM_NAME.casefold()
               and vm.get("status") != "removed"]
    if current:
        raise TttCreateError("A current VM named ttt exists; create stopped.")
    flavors = [item for item in group["flavors"] if isinstance(item, dict)
               and item.get("id") == provision.FLAVOR_ID]
    images = [item for item in group["images"] if isinstance(item, dict)
              and item.get("id") == provision.IMAGE_ID]
    if len(flavors) != 1 or len(images) != 1:
        raise TttCreateError("Exact flavor or image is missing or ambiguous.")
    flavor, image = flavors[0], images[0]
    if (flavor.get("cpu") != 2 or flavor.get("memory") != 4096
            or flavor.get("enabled") is not True or flavor.get("public") is False
            or image.get("disk") != 20):
        raise TttCreateError("Selected 2-CPU/4-GiB flavor or Ubuntu image catalog changed.")
    # Group quota is separate from the provider's zone SSD ceiling. Prior
    # creates failed on that upstream limit despite available group quota.
    maximum, used = group.get("quota_max") or {}, group.get("quota_used") or {}
    for key, needed in {"cpu": 2, "memory": 4, "disk": ROOT_DISK_GB, "vms": 1}.items():
        limit, consumed = maximum.get(key), used.get(key)
        if (isinstance(limit, bool) or not isinstance(limit, (int, float))
                or isinstance(consumed, bool) or not isinstance(consumed, (int, float))
                or limit - consumed < needed):
            raise TttCreateError(f"Group lacks verified free {key} quota.")
    if used.get("disk") != 0:
        raise TttCreateError("Group disk reservation is not zero after prior cleanup.")
    return group, flavor, image


def prepare_plan() -> dict[str, Any]:
    if (PLAN_PATH.exists() or PLAN_PATH.is_symlink()
            or ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink()):
        raise TttCreateError("ttt plan or attempt already exists; replacement is disabled.")
    source = provision._read_private_json(provision.INVENTORY_PATH)
    _recent_inventory(source)
    group, flavor, image = _select(source)
    path = f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm"
    body = _body()
    plan: dict[str, Any] = {
        "schema_version": 1,
        "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "inventory_observed_at": source["observed_at"],
        "tenant_id": provision.TENANT_ID,
        "vm_group": {"id": provision.GROUP_ID, "name": GROUP_NAME,
                     "availability_zone": ZONE,
                     "quota_max": group["quota_max"], "quota_used": group["quota_used"]},
        "selection": {"vm_name": VM_NAME, "flavor": flavor, "image": image,
                      "root_volume_gb": ROOT_DISK_GB, "public_ip": False, "gpu": False,
                      "image_disk_unit": "unverified"},
        "request": {"method": "POST", "path": path, "body": body},
        "request_sha256": provision._canonical_hash(body),
        "safety": {"create_attempts_max": 1, "retry_on_uncertain_response": False,
                   "preflight": "fresh exact group, name, catalog, quota",
                   "provider_zone_ssd_capacity": "unverified; prior 20-GB request failed"},
    }
    plan["plan_sha256"] = provision._canonical_hash(plan)
    provision._write_private_json(PLAN_PATH, plan, exclusive=True)
    return plan


def _fresh_preflight(token: str) -> dict[str, Any]:
    groups, truncated = inventory._paged_groups(token, provision.TENANT_ID)
    if truncated:
        raise TttCreateError("Fresh VM group listing is truncated.")
    matches = [group for group in groups if group.get("id") == provision.GROUP_ID]
    if len(matches) != 1:
        raise TttCreateError("Exact main-d group is missing from fresh API data.")
    group = matches[0]
    group["vms"], group["vms_truncated"] = inventory._paged_vms(
        token, provision.TENANT_ID, provision.GROUP_ID)
    group["flavors"], group["flavors_truncated"] = inventory._catalog(
        token, provision.GROUP_ID, "flavors")
    group["images"], group["images_truncated"] = inventory._catalog(
        token, provision.GROUP_ID, "images")
    _select({"tenant_id": provision.TENANT_ID, "vm_groups": [group],
             "vm_groups_truncated": False})
    return {"checked_at": _utcnow().isoformat().replace("+00:00", "Z"),
            "group_status": group["status"], "current_ttt_count": 0,
            "quota_used": group["quota_used"]}


def apply_plan(reviewed_sha256: str) -> dict[str, Any]:
    if not isinstance(reviewed_sha256, str) or len(reviewed_sha256) != 64:
        raise TttCreateError("Apply requires the reviewed plan SHA-256.")
    plan = provision._read_private_json(PLAN_PATH)
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if claimed != reviewed_sha256 or claimed != provision._canonical_hash(unsigned):
        raise TttCreateError("Reviewed ttt plan SHA-256 does not match.")
    expected_path = f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm"
    expected_request = {"method": "POST", "path": expected_path, "body": _body()}
    if (plan.get("request") != expected_request
            or plan.get("request_sha256") != provision._canonical_hash(_body())
            or plan.get("tenant_id") != provision.TENANT_ID
            or plan.get("vm_group", {}).get("id") != provision.GROUP_ID):
        raise TttCreateError("Reviewed request differs from the exact one-VM ttt target.")
    if ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise TttCreateError("ttt create attempt already exists; no second POST is allowed.")
    token = inventory._token_from_dp()
    preflight = _fresh_preflight(token)
    attempt: dict[str, Any] = {
        "schema_version": 1, "state": "pending",
        "attempted_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "plan_sha256": claimed, "request_sha256": plan["request_sha256"],
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_name": VM_NAME, "root_volume_gb": ROOT_DISK_GB,
        "preflight": preflight, "http_status": None, "result": None,
    }
    provision._write_private_json(ATTEMPT_PATH, attempt, exclusive=True)
    try:
        status, response = provision._post_once(token, expected_path, _body())
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
    attempt.update({"state": "submitted", "http_status": status,
                    "result": provision._result_summary(response),
                    "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
    provision._write_private_json(ATTEMPT_PATH, attempt)
    return attempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Save one immutable private ttt create plan.")
    apply = commands.add_parser("apply", help="Fresh-check and submit one reviewed ttt create.")
    apply.add_argument("--plan-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = prepare_plan()
            print(f"Private ttt plan saved: {PLAN_PATH}")
            print(f"Plan SHA-256 for review: {plan['plan_sha256']}")
            return 0
        attempt = apply_plan(args.plan_sha256)
        print(f"One ttt create attempt saved: {ATTEMPT_PATH}")
        print(f"HTTP {attempt['http_status']}; state {attempt['state']}; "
              f"operation ID {attempt['result']['id'] or 'not returned'}.")
        return 0
    except (TttCreateError, provision.ProvisionError, inventory.InventoryError,
            inventory.SafeAccessError) as exc:
        print(f"Spirit ttt create stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit ttt create stopped: local filesystem error.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
