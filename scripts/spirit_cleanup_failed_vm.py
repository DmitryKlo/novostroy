#!/usr/bin/env python3
"""Review and delete only the failed VM created by this one provision attempt.

The plan is offline and immutable. Apply refreshes operation, VM, and volume
metadata with GETs, writes an attempt ledger before one DELETE, and never
retries a rejected or uncertain request. Root-volume cleanup is separate.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
    from . import spirit_vm_status as status
else:
    import spirit_inventory as inventory
    import spirit_provision as provision
    import spirit_vm_status as status


CLEANUP_PLAN = provision.DEPLOY_DIR / "cleanup-failed-vm-plan.json"
CLEANUP_ATTEMPT = provision.DEPLOY_DIR / "cleanup-failed-vm-attempt.json"
STATUS_REPORT = provision.DEPLOY_DIR / "vm-status.json"
POSTCHECK_REPORT = provision.DEPLOY_DIR / "cleanup-failed-vm-postcheck.json"
EXPECTED_OPERATION_ID = "6235c2ad-c30d-44ca-8b86-cb4f1ea4a439"
EXPECTED_VM_ID = "02999bae-c0f8-4def-8ab7-32124936f920"
EXPECTED_VOLUME_ID = "8e909cb6-cf33-42ce-b42c-7bc0afb904f4"
EXPECTED_PROVISION_SHA = "7f7045f0eaacc055b89a76bf21d0e4bd39539eefe10ce652b4a572504ca8f91c"
CORRECTED_PLAN = provision.DEPLOY_DIR / "cleanup-corrected20-plan.json"
CORRECTED_ATTEMPT = provision.DEPLOY_DIR / "cleanup-corrected20-attempt.json"
CORRECTED_POSTCHECK = provision.DEPLOY_DIR / "cleanup-corrected20-postcheck.json"
CORRECTED_RECEIPT = provision.DEPLOY_DIR / "corrected-20gb-attempt.json"
CORRECTED_STATUS = provision.DEPLOY_DIR / "corrected-20gb-status.json"
CORRECTED_OPERATION_ID = "f206ac5a-d96c-46c7-b1b9-912a10472456"
CORRECTED_VM_ID = "0d2866cb-3e3b-4b8e-839a-c418ca8d71ce"
CORRECTED_VOLUME_ID = "b2d9c2ca-6bc3-4a7e-b01c-52c8da2ee674"
CORRECTED_PROVISION_SHA = "3ed75bd125856d10f87e70316248f64f177ef02a26588c95e5e7a021eba9c15d"
MAX_REPORT_AGE = timedelta(minutes=30)


class CleanupError(Exception):
    """A sanitized operator-facing cleanup error."""


class RejectedDelete(CleanupError):
    def __init__(self, http_status: int) -> None:
        self.http_status = http_status
        super().__init__(f"Failed-VM DELETE returned HTTP {http_status}.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CleanupError("Creation evidence has an invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CleanupError("Creation evidence has an invalid timestamp.") from exc
    if parsed.tzinfo is None:
        raise CleanupError("Creation evidence timestamp lacks a timezone.")
    return parsed.astimezone(timezone.utc)


def _target(corrected20: bool) -> dict[str, Any]:
    if corrected20:
        return {"plan": CORRECTED_PLAN, "attempt": CORRECTED_ATTEMPT,
                "postcheck": CORRECTED_POSTCHECK, "receipt": CORRECTED_RECEIPT,
                "status": CORRECTED_STATUS, "operation_id": CORRECTED_OPERATION_ID,
                "vm_id": CORRECTED_VM_ID, "volume_id": CORRECTED_VOLUME_ID,
                "provision_sha": CORRECTED_PROVISION_SHA, "disk_gb": 20}
    return {"plan": CLEANUP_PLAN, "attempt": CLEANUP_ATTEMPT,
            "postcheck": POSTCHECK_REPORT, "receipt": provision.ATTEMPT_PATH,
            "status": STATUS_REPORT, "operation_id": EXPECTED_OPERATION_ID,
            "vm_id": EXPECTED_VM_ID, "volume_id": EXPECTED_VOLUME_ID,
            "provision_sha": EXPECTED_PROVISION_SHA, "disk_gb": provision.DISK_GB}


def _validate_evidence(receipt: dict[str, Any], report: dict[str, Any], *, fresh: bool,
                       corrected20: bool = False) -> dict[str, Any]:
    target = _target(corrected20)
    result = receipt.get("result")
    if (receipt.get("state") != "submitted" or receipt.get("plan_sha256") != target["provision_sha"]
            or receipt.get("tenant_id") != provision.TENANT_ID
            or receipt.get("vm_group_id") != provision.GROUP_ID
            or receipt.get("vm_name") != provision.VM_NAME
            or not isinstance(result, dict) or result.get("kind") != "Operation"
            or result.get("id") != target["operation_id"]
            or (corrected20 and receipt.get("root_volume_gb") != 20)):
        raise CleanupError("Provision receipt does not identify the failed one-VM creation.")
    if (report.get("tenant_id") != provision.TENANT_ID
            or report.get("vm_group_id") != provision.GROUP_ID
            or report.get("operation_id") != target["operation_id"]
            or report.get("operation_status") != "failed"
            or report.get("operation_resource_id") != target["vm_id"]
            or (corrected20 and report.get("receipt_mode") != "corrected_20gb")
            or report.get("operation_read_error") or report.get("vm_read_error")
            or report.get("volume_read_error") or report.get("vm_found") is not True):
        raise CleanupError("Failed operation and VM are not fully confirmed by read-only evidence.")
    vm = report.get("vm")
    volumes = report.get("volumes")
    if (not isinstance(vm, dict) or vm.get("id") != target["vm_id"]
            or vm.get("name") != provision.VM_NAME or vm.get("status") != "failed"
            or any(vm.get(key) is not None for key in ("private_ip", "public_ip", "fqdn", "teleport_app_host"))
            or vm.get("teleport_app_access") is not False
            or vm.get("with_public_ip") is not False):
        raise CleanupError("VM is not the expected failed, unaddressed provision result.")
    if (not isinstance(volumes, list) or len(volumes) != 1
            or not isinstance(volumes[0], dict)):
        raise CleanupError("Expected exactly one explicitly linked root volume.")
    volume = volumes[0]
    if (volume.get("id") != target["volume_id"]
            or volume.get("attached_vm_id") != target["vm_id"]
            or volume.get("association_basis") not in {"vm_detail_reference", "volume_vm_reference"}
            or volume.get("root") is not True or volume.get("status") != "error"
            or volume.get("size_gb_ui_basis") != target["disk_gb"]):
        raise CleanupError("Linked root volume no longer matches the failed creation.")
    start = _parse_time(receipt.get("attempted_at"))
    end = _parse_time(receipt.get("completed_at"))
    created = _parse_time(vm.get("created_at"))
    if not start - timedelta(seconds=2) <= created <= end + timedelta(minutes=1):
        raise CleanupError("VM creation time does not match the one create attempt.")
    if fresh:
        observed = _parse_time(report.get("observed_at"))
        if not timedelta(0) <= _utcnow() - observed <= MAX_REPORT_AGE:
            raise CleanupError("Read-only VM status report is stale; refresh it before planning.")
    return {"operation_id": target["operation_id"], "operation_status": "failed",
            "vm_id": target["vm_id"], "vm_status": "failed", "vm_created_at": vm["created_at"],
            "vm_has_network_address": False,
            "volume_id": target["volume_id"], "volume_status": "error",
            "volume_root": True, "volume_size_gb": target["disk_gb"],
            "volume_attached_vm_id": target["vm_id"]}


def prepare_cleanup_plan(*, corrected20: bool = False) -> dict[str, Any]:
    target = _target(corrected20)
    if (target["plan"].exists() or target["plan"].is_symlink()
            or target["attempt"].exists() or target["attempt"].is_symlink()):
        raise CleanupError("A reviewed cleanup plan or DELETE attempt already exists; replacement is disabled.")
    receipt = provision._read_private_json(target["receipt"])
    report = provision._read_private_json(target["status"])
    evidence = _validate_evidence(receipt, report, fresh=True, corrected20=corrected20)
    delete_path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                   f"/vm/{target['vm_id']}")
    plan: dict[str, Any] = {
        "schema_version": 1,
        "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "provision_plan_sha256": target["provision_sha"],
        "provision_attempted_at": receipt["attempted_at"],
        "status_report_observed_at": report["observed_at"],
        "status_report_sha256": provision._canonical_hash(report),
        "evidence": evidence,
        "request": {"method": "DELETE", "path": delete_path, "body": None},
        "safety": {"fresh_get_before_delete": True, "delete_attempts_max": 1,
                   "retry_on_uncertain_response": False,
                   "root_volume_delete_in_this_plan": False,
                   "post_delete_check": "confirm VM absent; then inspect volume and group quota separately"},
    }
    plan["plan_sha256"] = provision._canonical_hash(plan)
    provision._write_private_json(target["plan"], plan, exclusive=True)
    return plan


def _delete_vm_once(token: str, path: str, *, corrected20: bool = False) -> tuple[int, dict[str, Any] | None]:
    target = _target(corrected20)
    expected = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                f"/vm/{target['vm_id']}")
    if path != expected:
        raise CleanupError("DELETE path differs from the reviewed failed VM.")
    context = ssl.create_default_context(cafile=str(inventory.SYSTEM_CA_BUNDLE)
                                         if inventory.SYSTEM_CA_BUNDLE.is_file() else None)
    connection = http.client.HTTPSConnection(inventory.API_HOST, timeout=30, context=context)
    try:
        connection.request("DELETE", path, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": "Novostroy failed-VM cleanup/1"})
        response = connection.getresponse()
        if 400 <= response.status < 500:
            raise RejectedDelete(response.status)
        if response.status not in {200, 202, 204}:
            raise CleanupError(f"Failed-VM DELETE response is uncertain (HTTP {response.status}).")
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise CleanupError("Failed-VM DELETE response exceeded the size limit; state is uncertain.")
        if not body:
            return response.status, None
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise CleanupError("Failed-VM DELETE response has an unexpected shape; state is uncertain.")
        attrs = inventory._attributes(parsed)
        return response.status, {"kind": inventory._short_text(parsed.get("kind"), max_length=50),
                                 "id": inventory._identifier(attrs.get("id")),
                                 "status": inventory._short_text(attrs.get("status"), max_length=40)}
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError("Failed-VM DELETE transport or response is uncertain.") from exc
    finally:
        connection.close()


def apply_cleanup_plan(reviewed_sha256: str, *, corrected20: bool = False) -> dict[str, Any]:
    target = _target(corrected20)
    plan = provision._read_private_json(target["plan"])
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not isinstance(reviewed_sha256, str) or claimed != reviewed_sha256 or claimed != provision._canonical_hash(unsigned):
        raise CleanupError("Reviewed cleanup plan SHA-256 does not match the current plan.")
    expected_path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                     f"/vm/{target['vm_id']}")
    if plan.get("request") != {"method": "DELETE", "path": expected_path, "body": None}:
        raise CleanupError("Reviewed cleanup request differs from the exact failed VM target.")
    if target["attempt"].exists() or target["attempt"].is_symlink():
        raise CleanupError("A failed-VM DELETE attempt already exists; no second attempt is allowed.")
    receipt = provision._read_private_json(target["receipt"])
    token = inventory._token_from_dp()
    current = status.read_vm_status(token=token, corrected20=corrected20)
    evidence = _validate_evidence(receipt, current, fresh=True, corrected20=corrected20)
    if evidence != plan.get("evidence"):
        raise CleanupError("Fresh failed-VM evidence differs from the reviewed cleanup plan.")
    attempt: dict[str, Any] = {
        "schema_version": 1, "state": "pending",
        "attempted_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "plan_sha256": claimed, "provision_plan_sha256": target["provision_sha"],
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_id": target["vm_id"], "volume_id": target["volume_id"],
        "fresh_evidence": evidence, "http_status": None, "result": None,
    }
    provision._write_private_json(target["attempt"], attempt, exclusive=True)
    try:
        http_status, response = _delete_vm_once(token, expected_path, corrected20=corrected20)
    except RejectedDelete as exc:
        attempt.update({"state": "rejected", "http_status": exc.http_status,
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(target["attempt"], attempt)
        raise
    except CleanupError:
        attempt.update({"state": "indeterminate",
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(target["attempt"], attempt)
        raise
    attempt.update({"state": "submitted", "http_status": http_status, "result": response,
                    "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
    provision._write_private_json(target["attempt"], attempt)
    return attempt


def verify_cleanup(*, token: str | None = None, corrected20: bool = False) -> dict[str, Any]:
    target = _target(corrected20)
    attempt = provision._read_private_json(target["attempt"])
    if (attempt.get("plan_sha256") != provision._read_private_json(target["plan"]).get("plan_sha256")
            or attempt.get("vm_id") != target["vm_id"]
            or attempt.get("volume_id") != target["volume_id"]
            or attempt.get("state") not in {"submitted", "indeterminate"}):
        raise CleanupError("No matching failed-VM DELETE attempt is available to verify.")
    token = token or inventory._token_from_dp()
    operation = attempt.get("result")
    operation_id = (inventory._identifier(operation.get("id"))
                    if isinstance(operation, dict) and operation.get("kind") == "Operation" else None)
    if operation_id is None:
        raise CleanupError("DELETE receipt does not identify a Cloud Operation.")
    operation_detail = status._safe_operation(
        inventory._get_json(token, f"/api/v2/cloud-operations/{operation_id}"),
        operation_id, token)
    if operation_detail["resource_id"] not in {None, target["vm_id"]}:
        raise CleanupError("DELETE operation points to a different resource.")
    groups, group_truncated = inventory._paged_groups(token, provision.TENANT_ID)
    if group_truncated:
        raise CleanupError("Group list is truncated; quota state is uncertain.")
    selected_groups = [group for group in groups if group.get("id") == provision.GROUP_ID]
    if len(selected_groups) != 1:
        raise CleanupError("Target VM group is absent or ambiguous after DELETE.")
    group = selected_groups[0]
    vms, vm_truncated = inventory._paged_vms(token, provision.TENANT_ID, provision.GROUP_ID)
    if vm_truncated:
        raise CleanupError("VM list is truncated; deletion state is uncertain.")
    matching_vms = [vm for vm in vms if vm.get("id") == target["vm_id"]]
    if len(matching_vms) > 1:
        raise CleanupError("Duplicate target VM records make deletion state uncertain.")
    volumes: list[dict[str, Any]] = []
    total_seen = 0
    for page_number in range(3):
        path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/volume"
                f"?limit={inventory.PAGE_SIZE}&offset={page_number * inventory.PAGE_SIZE}")
        rows, total = inventory._page(inventory._get_json(token, path))
        total_seen += len(rows)
        volumes.extend(item for item in rows if inventory._identifier(
            inventory._attributes(item).get("id")) == target["volume_id"])
        if len(rows) < inventory.PAGE_SIZE or (total is not None and total_seen >= total):
            break
    else:
        raise CleanupError("Volume list exceeds the bounded page limit.")
    if len(volumes) > 1:
        raise CleanupError("Duplicate target volume records make deletion state uncertain.")
    volume = None
    if volumes:
        path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                f"/volume/{target['volume_id']}")
        volume = status._safe_volume(inventory._get_json(token, path), target["volume_id"],
                                     target["vm_id"], "exact_volume_id_after_delete")
    report = {
        "schema_version": 1,
        "observed_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_id": target["vm_id"], "vm_absent": not matching_vms,
        "vm_status_if_present": matching_vms[0].get("status") if matching_vms else None,
        "vm_removed": not matching_vms or matching_vms[0].get("status") == "removed",
        "volume_id": target["volume_id"], "volume_absent": volume is None,
        "volume_if_present": volume,
        "volume_removed": volume is None or volume.get("status") == "removed",
        "cleanup_operation_id": operation_id,
        "cleanup_operation_status": operation_detail["status"],
        "cleanup_operation_succeeded": operation_detail["status"] in {"done", "completed", "success", "succeeded"},
        "group_quota_used_disk_gb": group.get("quota_used", {}).get("disk"),
        "group_quota_max_disk_gb": group.get("quota_max", {}).get("disk"),
    }
    provision._write_private_json(target["postcheck"], report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corrected20", action="store_true",
                        help="Target only the separate failed 20-GB corrected VM attempt.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Write one immutable, reviewable failed-VM DELETE plan.")
    apply = commands.add_parser("apply", help="Fresh-check and send one reviewed failed-VM DELETE.")
    apply.add_argument("--plan-sha256", required=True)
    commands.add_parser("verify", help="Read only VM, root volume, and group disk quota after DELETE.")
    args = parser.parse_args(argv)
    target = _target(args.corrected20)
    try:
        if args.command == "plan":
            plan = prepare_cleanup_plan(corrected20=args.corrected20)
            print(f"Failed-VM cleanup plan saved privately: {target['plan']}")
            print(f"Plan SHA-256 for review: {plan['plan_sha256']}")
        elif args.command == "apply":
            attempt = apply_cleanup_plan(args.plan_sha256, corrected20=args.corrected20)
            print(f"One failed-VM DELETE attempt recorded privately: {target['attempt']}")
            print(f"HTTP {attempt['http_status']}; state {attempt['state']}; "
                  "confirm VM disappearance and root-volume state with GETs.")
        else:
            report = verify_cleanup(corrected20=args.corrected20)
            print(f"Read-only cleanup check saved privately: {target['postcheck']}")
            print(f"VM absent: {report['vm_absent']}; volume absent: {report['volume_absent']}; "
                  f"VM removed: {report['vm_removed']}; volume removed: {report['volume_removed']}; "
                  f"cleanup operation: {report['cleanup_operation_status']}; "
                  f"group disk used: {report['group_quota_used_disk_gb']} GB.")
        return 0
    except (CleanupError, provision.ProvisionError, inventory.InventoryError,
            inventory.SafeAccessError, status.StatusError) as exc:
        print(f"Spirit failed-VM cleanup stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit failed-VM cleanup stopped: local filesystem error.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
