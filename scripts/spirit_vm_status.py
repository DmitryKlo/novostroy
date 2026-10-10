#!/usr/bin/env python3
"""Read one provisioned Spirit VM's safe status and network metadata.

Uses GET only. The provision receipt supplies the exact tenant, group, and VM
name. No credentials, key material, or raw API response is saved.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__:
    from . import spirit_inventory as inventory
    from . import spirit_provision as provision
    from .spirit_deploy_access import _known_key_literals, safe_diagnostic_snippets
else:
    import spirit_inventory as inventory
    import spirit_provision as provision
    from spirit_deploy_access import _known_key_literals, safe_diagnostic_snippets


OUTPUT = provision.DEPLOY_DIR / "vm-status.json"
CORRECTED_RECEIPT = provision.DEPLOY_DIR / "corrected-20gb-attempt.json"
CORRECTED_OUTPUT = provision.DEPLOY_DIR / "corrected-20gb-status.json"
DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
DIAGNOSTIC_KEYS = frozenset({"error", "errorMessage", "userError", "message", "reason", "detail", "details",
                             "description", "failure", "cause"})
CONTAINER_KEYS = frozenset({"attributes", "error", "failure", "cause", "result", "details",
                            "tasks", "contents"})
DIAGNOSTIC_LINE_RE = re.compile(r"(?i)\b(?:status|details|error|fail|denied|quota|capacity|resource|invalid|operation|task|cancel|rpc)\b")


class StatusError(Exception):
    """Sanitized operator-facing status error."""


def _ip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _domain(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(".")
    return value if DOMAIN_RE.fullmatch(value) and all(len(part) <= 63 for part in value.split(".")) else None


def _teleport_host(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlsplit(value if "://" in value else f"https://{value}")
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            return None
        return _domain(url.hostname)
    except ValueError:
        return None


def _safe_vm_detail(value: Any, expected_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatusError("VM detail has an unexpected shape.")
    attrs = inventory._attributes(value)
    identifier = inventory._identifier(attrs.get("id"))
    if identifier != expected_id:
        raise StatusError("VM detail ID differs from the selected VM.")
    port = attrs.get("teleportAppPort")
    return {
        "id": identifier,
        "name": inventory._short_text(attrs.get("name"), max_length=100),
        "status": inventory._short_text(attrs.get("status"), max_length=40),
        "public_ip": _ip(attrs.get("ip")),
        "private_ip": _ip(attrs.get("privateIp")),
        "fqdn": _domain(attrs.get("fqdn")),
        "with_public_ip": attrs.get("withPublicIp") if isinstance(attrs.get("withPublicIp"), bool) else None,
        "teleport_app_access": attrs.get("teleportAppAccess") if isinstance(attrs.get("teleportAppAccess"), bool) else None,
        "teleport_app_port": port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else None,
        "teleport_app_host": _teleport_host(attrs.get("teleportAppPublicAddr")),
        "created_at": inventory._short_text(attrs.get("createdAt"), max_length=40),
    }


def _reference_id(value: Any) -> str | None:
    if isinstance(value, str):
        return inventory._identifier(value)
    if not isinstance(value, dict):
        return None
    attrs = inventory._attributes(value)
    direct = inventory._identifier(attrs.get("id"))
    if direct:
        return direct
    identifier = value.get("identifier") or attrs.get("identifier")
    if isinstance(identifier, dict) and identifier.get("name") == "id":
        return inventory._identifier(identifier.get("value"))
    return None


def _vm_volume_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    collection = inventory._attributes(value).get("volumes")
    if isinstance(collection, dict):
        collection = collection.get("contents")
    if not isinstance(collection, list):
        return set()
    return {identifier for item in collection[:20] if (identifier := _reference_id(item))}


def _safe_volume(value: Any, expected_id: str, vm_id: str, basis: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatusError("Volume detail has an unexpected shape.")
    attrs = inventory._attributes(value)
    identifier = inventory._identifier(attrs.get("id"))
    if identifier != expected_id:
        raise StatusError("Volume detail ID differs from the selected volume.")
    attached_vm_id = _reference_id(attrs.get("vm"))
    if attached_vm_id and attached_vm_id != vm_id:
        raise StatusError("Volume detail is attached to a different VM.")
    size = attrs.get("size")
    return {"id": identifier, "status": inventory._short_text(attrs.get("status"), max_length=40),
            "size_gb_ui_basis": size if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= 100000 else None,
            "root": attrs.get("root") if isinstance(attrs.get("root"), bool) else None,
            "type": inventory._short_text(attrs.get("type"), max_length=40),
            "attached_vm_id": attached_vm_id, "association_basis": basis}


def _linked_volumes(token: str, vm_id: str, vm_detail: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Find volumes only with explicit VM references; cap list and detail GETs."""
    referenced = _vm_volume_ids(vm_detail)
    linked: dict[str, str] = {identifier: "vm_detail_reference" for identifier in referenced}
    total_seen = 0
    try:
        for page_number in range(3):
            path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/volume"
                    f"?limit={inventory.PAGE_SIZE}&offset={page_number * inventory.PAGE_SIZE}")
            rows, total = inventory._page(inventory._get_json(token, path))
            total_seen += len(rows)
            for raw in rows:
                attrs = inventory._attributes(raw)
                identifier = inventory._identifier(attrs.get("id"))
                if identifier and _reference_id(attrs.get("vm")) == vm_id:
                    linked.setdefault(identifier, "volume_vm_reference")
            if len(rows) < inventory.PAGE_SIZE or (total is not None and total_seen >= total):
                break
        else:
            return [], "Volume list exceeds the bounded page limit."
        if len(linked) > 10:
            return [], "More than ten volumes link to the target VM; detail is bounded."
        volumes = []
        for identifier, basis in sorted(linked.items()):
            path = (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}"
                    f"/volume/{identifier}")
            volumes.append(_safe_volume(inventory._get_json(token, path), identifier, vm_id, basis))
        return volumes, None
    except inventory.InventoryError as exc:
        return [], str(exc)


def _error_diagnostics(value: Any, token: str) -> list[str]:
    """Extract only bounded, redacted error text from known operation fields."""
    literals = (*_known_key_literals(), token)
    snippets: list[str] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 5 or len(snippets) >= 4:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if key in DIAGNOSTIC_KEYS and isinstance(child, str) and len(child) <= 1000:
                    for index, line in enumerate(child.splitlines()[:20]):
                        if index and not DIAGNOSTIC_LINE_RE.search(line):
                            continue
                        for item in safe_diagnostic_snippets(f"error: {line}", literals):
                            if item not in snippets and len(snippets) < 4:
                                snippets.append(item)
                elif key in CONTAINER_KEYS and isinstance(child, (dict, list)):
                    visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node[:5]:
                visit(child, depth + 1)

    visit(value, 0)
    return snippets


def _safe_operation(value: Any, expected_id: str, token: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StatusError("Operation detail has an unexpected shape.")
    attrs = inventory._attributes(value)
    identifier = inventory._identifier(attrs.get("id"))
    if identifier != expected_id:
        raise StatusError("Operation detail ID differs from the provision receipt.")
    metadata = attrs.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    result = attrs.get("result")
    error = result.get("error") if isinstance(result, dict) else None
    error_code = error.get("code") if isinstance(error, dict) else None
    diagnostics = _error_diagnostics({"result": result, "tasks": metadata.get("tasks")}, token)
    return {"id": identifier,
            "status": inventory._short_text(attrs.get("status"), max_length=40),
            "resource_id": inventory._identifier(metadata.get("resourceId")),
            "error_code": error_code if isinstance(error_code, str) and ERROR_CODE_RE.fullmatch(error_code) else None,
            "diagnostics": diagnostics}


def read_vm_status(*, token: str | None = None, corrected20: bool = False) -> dict[str, Any]:
    receipt = provision._read_private_json(CORRECTED_RECEIPT if corrected20 else provision.ATTEMPT_PATH)
    if (receipt.get("tenant_id") != provision.TENANT_ID
            or receipt.get("vm_group_id") != provision.GROUP_ID
            or receipt.get("vm_name") != provision.VM_NAME
            or receipt.get("state") not in {"pending", "submitted", "indeterminate"}):
        raise StatusError("Provision receipt does not match the one-VM target.")
    result = receipt.get("result")
    operation_id = (inventory._identifier(result.get("id"))
                    if isinstance(result, dict) and result.get("kind") == "Operation" else None)
    access_token = token or inventory._token_from_dp()
    report: dict[str, Any] = {
        "schema_version": 1,
        "receipt_mode": "corrected_20gb" if corrected20 else "initial_40gb",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tenant_id": provision.TENANT_ID,
        "vm_group_id": provision.GROUP_ID,
        "operation_id": operation_id,
        "operation_status": "unknown",
        "operation_resource_id": None,
        "operation_error_code": None,
        "operation_diagnostics": [],
        "operation_read_error": None,
        "vm_found": False,
        "vm_read_error": None,
        "vm": None,
        "volumes": [],
        "volume_read_error": None,
    }
    if operation_id:
        try:
            operation = _safe_operation(
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
            raise StatusError("VM list is truncated; target identity is uncertain.")
        candidates = [vm for vm in vms if vm.get("name") == provision.VM_NAME
                      and vm.get("status") != "removed"]
        resource_id = report["operation_resource_id"]
        selected = ([vm for vm in candidates if vm.get("id") == resource_id]
                    if resource_id else candidates)
        if len(selected) > 1:
            raise StatusError("More than one VM has the target name.")
        if resource_id and not selected and candidates:
            raise StatusError("Operation resource ID does not match the current named VM.")
    except inventory.InventoryError as exc:
        report["vm_read_error"] = str(exc)
        selected = []
    if selected:
        vm_id = selected[0]["id"]
        report["vm_found"] = True
        path = f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm/{vm_id}"
        try:
            raw_detail = inventory._get_json(access_token, path)
            detail = _safe_vm_detail(raw_detail, vm_id)
        except inventory.InventoryError as exc:
            report["vm_read_error"] = str(exc)
        else:
            if detail["name"] != provision.VM_NAME:
                raise StatusError("VM detail name differs from the provision target.")
            report["vm"] = detail
            report["volumes"], report["volume_read_error"] = _linked_volumes(
                access_token, vm_id, raw_detail)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corrected20", action="store_true",
                        help="Read the separate corrected 20-GB receipt and status output.")
    args = parser.parse_args(argv)
    output = CORRECTED_OUTPUT if args.corrected20 else OUTPUT
    try:
        report = read_vm_status(corrected20=args.corrected20)
        # A failed read must not replace a previously observed VM/operation
        # state with unknown values (for example when this process has no
        # network access but a different operator session does).
        if not report["operation_read_error"] and not report["vm_read_error"]:
            provision._write_private_json(output, report)
    except (StatusError, provision.ProvisionError, inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit VM status failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit VM status failed: local filesystem error.", file=sys.stderr)
        return 2
    if not report["operation_read_error"] and not report["vm_read_error"]:
        print(f"VM status saved privately: {output}")
    else:
        print("Partial read was not saved; prior VM status file was preserved.")
    if report["vm"]:
        vm = report["vm"]
        print(f"VM {vm['id']} status {vm['status'] or 'unknown'}; "
              f"private IP {vm['private_ip'] or 'unknown'}; public IP {vm['public_ip'] or 'none reported'}.")
    else:
        print("VM not yet present in the group list.")
    print(f"Operation status: {report['operation_status']}.")
    for field in ("operation_read_error", "vm_read_error", "volume_read_error"):
        if report[field]:
            print(f"Read limitation ({field}): {report[field]}", file=sys.stderr)
    return 3 if any(report[field] for field in ("operation_read_error", "vm_read_error", "volume_read_error")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
