#!/usr/bin/env python3
"""Read only the exact newly requested `ttt` VM and its create operation."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

if __package__:
    from . import spirit_create_ttt as create
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
    from . import spirit_vm_status as status
else:
    import spirit_create_ttt as create
    import spirit_inventory as inventory
    import spirit_provision as provision
    import spirit_vm_status as status


OUTPUT = provision.DEPLOY_DIR / "create-ttt-status.json"


def read_status(*, token: str | None = None) -> dict[str, Any]:
    receipt = provision._read_private_json(create.ATTEMPT_PATH)
    if (receipt.get("tenant_id") != provision.TENANT_ID
            or receipt.get("vm_group_id") != provision.GROUP_ID
            or receipt.get("vm_name") != create.VM_NAME
            or receipt.get("root_volume_gb") != create.ROOT_DISK_GB
            or receipt.get("state") not in {"submitted", "pending", "indeterminate"}):
        raise status.StatusError("ttt create receipt does not match the exact target.")
    result = receipt.get("result")
    operation_id = (inventory._identifier(result.get("id"))
                    if isinstance(result, dict) and result.get("kind") == "Operation" else None)
    access_token = token or inventory._token_from_dp()
    report: dict[str, Any] = {
        "schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
        "vm_name": create.VM_NAME, "receipt_state": receipt["state"],
        "operation_id": operation_id, "operation_status": "unknown",
        "operation_resource_id": None, "operation_error_code": None,
        "operation_diagnostics": [], "operation_read_error": None,
        "vm_found": False, "vm_read_error": None, "vm": None,
        "volumes": [], "volume_read_error": None,
    }
    if operation_id:
        try:
            operation = status._safe_operation(
                inventory._get_json(access_token, f"/api/v2/cloud-operations/{operation_id}"),
                operation_id, access_token)
            report["operation_status"] = operation["status"] or "unknown"
            report["operation_resource_id"] = operation["resource_id"]
            report["operation_error_code"] = operation["error_code"]
            report["operation_diagnostics"] = operation["diagnostics"]
        except inventory.InventoryError as exc:
            report["operation_read_error"] = str(exc)
    try:
        vms, truncated = inventory._paged_vms(access_token, provision.TENANT_ID, provision.GROUP_ID)
        if truncated:
            raise status.StatusError("VM list is truncated; ttt identity is uncertain.")
        candidates = [vm for vm in vms if vm.get("name") == create.VM_NAME
                      and vm.get("status") != "removed"]
        resource_id = report["operation_resource_id"]
        selected = ([vm for vm in candidates if vm.get("id") == resource_id]
                    if resource_id else candidates)
        if len(selected) > 1:
            raise status.StatusError("More than one current VM is named ttt.")
        if resource_id and not selected and candidates:
            raise status.StatusError("Operation resource ID differs from the current ttt VM.")
    except inventory.InventoryError as exc:
        report["vm_read_error"] = str(exc)
        selected = []
    if selected:
        vm_id = selected[0]["id"]
        raw = inventory._get_json(
            access_token,
            f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm/{vm_id}")
        detail = status._safe_vm_detail(raw, vm_id)
        if detail["name"] != create.VM_NAME:
            raise status.StatusError("VM detail name differs from exact ttt target.")
        report["vm_found"] = True
        report["vm"] = detail
        report["volumes"], report["volume_read_error"] = status._linked_volumes(
            access_token, vm_id, raw)
    return report


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        report = read_status()
        if not report["operation_read_error"] and not report["vm_read_error"]:
            provision._write_private_json(OUTPUT, report)
    except (status.StatusError, provision.ProvisionError,
            inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit ttt status stopped: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit ttt status stopped: local filesystem error.", file=sys.stderr)
        return 2
    if report["operation_read_error"] or report["vm_read_error"]:
        print("Partial ttt read was not saved; prior status was preserved.")
        return 2
    print(f"Private ttt status saved: {OUTPUT}")
    vm = report["vm"]
    print(f"Operation {report['operation_status']}; VM "
          f"{vm['status'] if vm else 'not yet found'}; "
          f"private IP {vm['private_ip'] if vm and vm['private_ip'] else 'unknown'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
