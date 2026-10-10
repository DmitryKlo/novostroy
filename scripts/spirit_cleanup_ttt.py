#!/usr/bin/env python3
"""Delete only the newly failed, unaddressed `ttt` VM after exact review.

The plan and DELETE attempt use separate immutable files. Apply re-reads the
operation, VM and attached root volume before one DELETE. Verify uses GET only
to check the delete operation, VM/volume tombstones and released group quota.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

if __package__:
    from . import spirit_create_ttt as create
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
    from . import spirit_ttt_status as ttt_status
    from . import spirit_vm_status as status
else:
    import spirit_create_ttt as create
    import spirit_inventory as inventory
    import spirit_provision as provision
    import spirit_ttt_status as ttt_status
    import spirit_vm_status as status


PLAN_PATH = provision.DEPLOY_DIR / "cleanup-ttt-plan.json"
ATTEMPT_PATH = provision.DEPLOY_DIR / "cleanup-ttt-attempt.json"
POSTCHECK_PATH = provision.DEPLOY_DIR / "cleanup-ttt-postcheck.json"
EXPECTED_CREATE_SHA = "a77692c22c165c1e4886c03ec14dd2e5be836c7515fd51407887552c53a8b93e"
EXPECTED_OPERATION_ID = "4e9da137-8672-4bcf-af10-23eac622f2d8"
EXPECTED_VM_ID = "76ccb9a0-73c2-4048-8e29-ece3cb4bb0b3"
EXPECTED_VOLUME_ID = "187a26ac-1183-40c5-8eca-222ffc22cdb0"
MAX_REPORT_AGE = timedelta(minutes=30)


class CleanupTttError(Exception):
    """Sanitized operator-facing cleanup stop reason."""


class RejectedDelete(CleanupTttError):
    def __init__(self, http_status: int) -> None:
        self.http_status = http_status
        super().__init__(f"ttt DELETE returned HTTP {http_status}.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CleanupTttError("Creation evidence timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CleanupTttError("Creation evidence timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        raise CleanupTttError("Creation evidence timestamp lacks a timezone.")
    return parsed.astimezone(timezone.utc)


def _evidence(receipt: dict[str, Any], report: dict[str, Any], *, fresh: bool) -> dict[str, Any]:
    result = receipt.get("result")
    if (receipt.get("state") != "submitted"
            or receipt.get("plan_sha256") != EXPECTED_CREATE_SHA
            or receipt.get("tenant_id") != provision.TENANT_ID
            or receipt.get("vm_group_id") != provision.GROUP_ID
            or receipt.get("vm_name") != create.VM_NAME
            or receipt.get("root_volume_gb") != create.ROOT_DISK_GB
            or not isinstance(result, dict) or result.get("kind") != "Operation"
            or result.get("id") != EXPECTED_OPERATION_ID):
        raise CleanupTttError("Create receipt does not identify the reviewed failed ttt request.")
    if (report.get("tenant_id") != provision.TENANT_ID
            or report.get("vm_group_id") != provision.GROUP_ID
            or report.get("vm_name") != create.VM_NAME
            or report.get("operation_id") != EXPECTED_OPERATION_ID
            or report.get("operation_status") != "failed"
            or report.get("operation_resource_id") != EXPECTED_VM_ID
            or report.get("operation_read_error") or report.get("vm_read_error")
            or report.get("volume_read_error") or report.get("vm_found") is not True):
        raise CleanupTttError("Failed operation and VM are not fully confirmed.")
    vm = report.get("vm")
    volumes = report.get("volumes")
    if (not isinstance(vm, dict) or vm.get("id") != EXPECTED_VM_ID
            or vm.get("name") != create.VM_NAME or vm.get("status") != "failed"
            or any(vm.get(key) is not None for key in
                   ("private_ip", "public_ip", "fqdn", "teleport_app_host"))
            or vm.get("teleport_app_access") is not False
            or vm.get("with_public_ip") is not False):
        raise CleanupTttError("VM is not the exact failed, unaddressed creation result.")
    if (not isinstance(volumes, list) or len(volumes) != 1
            or not isinstance(volumes[0], dict)):
        raise CleanupTttError("Expected one explicitly linked root volume.")
    volume = volumes[0]
    if (volume.get("id") != EXPECTED_VOLUME_ID
            or volume.get("attached_vm_id") != EXPECTED_VM_ID
            or volume.get("association_basis") not in
               {"vm_detail_reference", "volume_vm_reference"}
            or volume.get("root") is not True or volume.get("status") != "error"
            or volume.get("size_gb_ui_basis") != create.ROOT_DISK_GB):
        raise CleanupTttError("Root volume differs from the exact failed creation.")
    start = _parse_time(receipt.get("attempted_at"))
    end = _parse_time(receipt.get("completed_at"))
    created = _parse_time(vm.get("created_at"))
    if not start - timedelta(seconds=2) <= created <= end + timedelta(minutes=1):
        raise CleanupTttError("VM creation time differs from this create attempt.")
    if fresh:
        observed = _parse_time(report.get("observed_at"))
        if not timedelta(0) <= _utcnow() - observed <= MAX_REPORT_AGE:
            raise CleanupTttError("Status report is stale; refresh read-only ttt status.")
    return {"operation_id": EXPECTED_OPERATION_ID, "operation_status": "failed",
            "vm_id": EXPECTED_VM_ID, "vm_status": "failed",
            "vm_created_at": vm["created_at"], "vm_has_network_address": False,
            "volume_id": EXPECTED_VOLUME_ID, "volume_status": "error",
            "volume_root": True, "volume_size_gb": create.ROOT_DISK_GB,
            "volume_attached_vm_id": EXPECTED_VM_ID}


def _delete_path() -> str:
    return (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
            f"/vm/{EXPECTED_VM_ID}")


def prepare_plan() -> dict[str, Any]:
    if (PLAN_PATH.exists() or PLAN_PATH.is_symlink()
            or ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink()):
        raise CleanupTttError("ttt cleanup plan or attempt already exists; replacement is disabled.")
    receipt = provision._read_private_json(create.ATTEMPT_PATH)
    report = provision._read_private_json(ttt_status.OUTPUT)
    evidence = _evidence(receipt, report, fresh=True)
    plan: dict[str, Any] = {
        "schema_version": 1, "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "create_plan_sha256": EXPECTED_CREATE_SHA,
        "create_attempted_at": receipt["attempted_at"],
        "status_observed_at": report["observed_at"],
        "status_sha256": provision._canonical_hash(report),
        "evidence": evidence,
        "request": {"method": "DELETE", "path": _delete_path(), "body": None},
        "safety": {"fresh_get_before_delete": True, "delete_attempts_max": 1,
                   "retry_on_uncertain_response": False,
                   "root_volume_delete_in_this_plan": False},
    }
    plan["plan_sha256"] = provision._canonical_hash(plan)
    provision._write_private_json(PLAN_PATH, plan, exclusive=True)
    return plan


def _delete_once(token: str, path: str) -> tuple[int, dict[str, Any] | None]:
    if path != _delete_path():
        raise CleanupTttError("DELETE path differs from the reviewed failed ttt VM.")
    context = ssl.create_default_context(cafile=str(inventory.SYSTEM_CA_BUNDLE)
                                         if inventory.SYSTEM_CA_BUNDLE.is_file() else None)
    connection = http.client.HTTPSConnection(inventory.API_HOST, timeout=30, context=context)
    try:
        connection.request("DELETE", path, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": "Novostroy exact failed-ttt cleanup/1"})
        response = connection.getresponse()
        if 400 <= response.status < 500:
            raise RejectedDelete(response.status)
        if response.status not in {200, 202, 204}:
            raise CleanupTttError(f"ttt DELETE response is uncertain (HTTP {response.status}).")
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise CleanupTttError("ttt DELETE response exceeded size limit; state is uncertain.")
        if not body:
            return response.status, None
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise CleanupTttError("ttt DELETE response shape is uncertain.")
        attrs = inventory._attributes(parsed)
        return response.status, {"kind": inventory._short_text(parsed.get("kind"), max_length=50),
                                 "id": inventory._identifier(attrs.get("id")),
                                 "status": inventory._short_text(attrs.get("status"), max_length=40)}
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException,
            UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupTttError("ttt DELETE transport or response is uncertain.") from exc
    finally:
        connection.close()


def apply_plan(reviewed_sha256: str) -> dict[str, Any]:
    plan = provision._read_private_json(PLAN_PATH)
    claimed = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (not isinstance(reviewed_sha256, str) or claimed != reviewed_sha256
            or claimed != provision._canonical_hash(unsigned)):
        raise CleanupTttError("Reviewed ttt cleanup plan SHA-256 does not match.")
    if plan.get("request") != {"method": "DELETE", "path": _delete_path(), "body": None}:
        raise CleanupTttError("Reviewed cleanup request differs from exact failed ttt VM.")
    if ATTEMPT_PATH.exists() or ATTEMPT_PATH.is_symlink():
        raise CleanupTttError("ttt DELETE attempt already exists; no retry is allowed.")
    receipt = provision._read_private_json(create.ATTEMPT_PATH)
    token = inventory._token_from_dp()
    current = ttt_status.read_status(token=token)
    evidence = _evidence(receipt, current, fresh=True)
    if evidence != plan.get("evidence"):
        raise CleanupTttError("Fresh failed-ttt evidence differs from reviewed plan.")
    attempt: dict[str, Any] = {
        "schema_version": 1, "state": "pending",
        "attempted_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "plan_sha256": claimed, "create_plan_sha256": EXPECTED_CREATE_SHA,
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_id": EXPECTED_VM_ID, "volume_id": EXPECTED_VOLUME_ID,
        "fresh_evidence": evidence, "http_status": None, "result": None,
    }
    provision._write_private_json(ATTEMPT_PATH, attempt, exclusive=True)
    try:
        http_status, response = _delete_once(token, _delete_path())
    except RejectedDelete as exc:
        attempt.update({"state": "rejected", "http_status": exc.http_status,
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(ATTEMPT_PATH, attempt)
        raise
    except CleanupTttError:
        attempt.update({"state": "indeterminate",
                        "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
        provision._write_private_json(ATTEMPT_PATH, attempt)
        raise
    attempt.update({"state": "submitted", "http_status": http_status,
                    "result": response,
                    "completed_at": _utcnow().isoformat().replace("+00:00", "Z")})
    provision._write_private_json(ATTEMPT_PATH, attempt)
    return attempt


def verify_cleanup(*, token: str | None = None) -> dict[str, Any]:
    attempt = provision._read_private_json(ATTEMPT_PATH)
    plan = provision._read_private_json(PLAN_PATH)
    if (attempt.get("plan_sha256") != plan.get("plan_sha256")
            or attempt.get("vm_id") != EXPECTED_VM_ID
            or attempt.get("volume_id") != EXPECTED_VOLUME_ID
            or attempt.get("state") not in {"submitted", "indeterminate"}):
        raise CleanupTttError("No matching ttt DELETE attempt is available to verify.")
    operation = attempt.get("result")
    operation_id = (inventory._identifier(operation.get("id"))
                    if isinstance(operation, dict) and operation.get("kind") == "Operation" else None)
    if operation_id is None:
        raise CleanupTttError("DELETE receipt does not identify an operation.")
    access_token = token or inventory._token_from_dp()
    detail = status._safe_operation(
        inventory._get_json(access_token, f"/api/v2/cloud-operations/{operation_id}"),
        operation_id, access_token)
    if detail["resource_id"] not in {None, EXPECTED_VM_ID}:
        raise CleanupTttError("DELETE operation points to a different VM.")
    groups, truncated = inventory._paged_groups(access_token, provision.TENANT_ID)
    if truncated:
        raise CleanupTttError("Group list is truncated; quota is uncertain.")
    selected = [group for group in groups if group.get("id") == provision.GROUP_ID]
    if len(selected) != 1:
        raise CleanupTttError("Exact VM group is absent after DELETE.")
    group = selected[0]
    vms, truncated = inventory._paged_vms(access_token, provision.TENANT_ID, provision.GROUP_ID)
    if truncated:
        raise CleanupTttError("VM list is truncated; deletion is uncertain.")
    matches = [vm for vm in vms if vm.get("id") == EXPECTED_VM_ID]
    if len(matches) > 1:
        raise CleanupTttError("Duplicate target VM records make deletion uncertain.")
    volumes: list[dict[str, Any]] = []
    total_seen = 0
    for page_number in range(3):
        path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/volume"
                f"?limit={inventory.PAGE_SIZE}&offset={page_number * inventory.PAGE_SIZE}")
        rows, total = inventory._page(inventory._get_json(access_token, path))
        total_seen += len(rows)
        volumes.extend(raw for raw in rows if inventory._identifier(
            inventory._attributes(raw).get("id")) == EXPECTED_VOLUME_ID)
        if len(rows) < inventory.PAGE_SIZE or (total is not None and total_seen >= total):
            break
    else:
        raise CleanupTttError("Volume list exceeds bounded coverage.")
    if len(volumes) > 1:
        raise CleanupTttError("Duplicate target volume records make deletion uncertain.")
    volume = None
    if volumes:
        path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                f"/volume/{EXPECTED_VOLUME_ID}")
        volume = status._safe_volume(inventory._get_json(access_token, path),
                                     EXPECTED_VOLUME_ID, EXPECTED_VM_ID,
                                     "exact_volume_id_after_delete")
    report: dict[str, Any] = {
        "schema_version": 1, "observed_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_id": EXPECTED_VM_ID, "vm_absent": not matches,
        "vm_status_if_present": matches[0].get("status") if matches else None,
        "vm_removed": not matches or matches[0].get("status") == "removed",
        "volume_id": EXPECTED_VOLUME_ID, "volume_absent": volume is None,
        "volume_if_present": volume,
        "volume_removed": volume is None or volume.get("status") == "removed",
        "cleanup_operation_id": operation_id,
        "cleanup_operation_status": detail["status"],
        "cleanup_operation_succeeded": detail["status"] in
        {"done", "completed", "success", "succeeded"},
        "group_quota_used_disk_gb": group.get("quota_used", {}).get("disk"),
        "group_quota_max_disk_gb": group.get("quota_max", {}).get("disk"),
    }
    provision._write_private_json(POSTCHECK_PATH, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Write immutable exact failed-ttt cleanup plan.")
    apply = commands.add_parser("apply", help="Fresh-check and send one reviewed DELETE.")
    apply.add_argument("--plan-sha256", required=True)
    commands.add_parser("verify", help="GET-only check operation, VM, volume and quota.")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = prepare_plan()
            print(f"Private exact-ttt cleanup plan saved: {PLAN_PATH}")
            print(f"Plan SHA-256 for review: {plan['plan_sha256']}")
            return 0
        if args.command == "apply":
            attempt = apply_plan(args.plan_sha256)
            print(f"One exact-ttt DELETE attempt saved: {ATTEMPT_PATH}")
            print(f"HTTP {attempt['http_status']}; state {attempt['state']}; "
                  f"operation ID {(attempt['result'] or {}).get('id') or 'not returned'}.")
            return 0
        report = verify_cleanup()
        print(f"Private ttt cleanup postcheck saved: {POSTCHECK_PATH}")
        print(f"Cleanup operation {report['cleanup_operation_status']}; "
              f"VM removed {report['vm_removed']}; volume removed {report['volume_removed']}; "
              f"group disk used {report['group_quota_used_disk_gb']} GB.")
        return 0
    except (CleanupTttError, status.StatusError, provision.ProvisionError,
            inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit ttt cleanup stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit ttt cleanup stopped: local filesystem error.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
