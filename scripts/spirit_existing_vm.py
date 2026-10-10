#!/usr/bin/env python3
"""Read only the existing Spirit VM named `ttt` and its safe connection data.

Queries bounded tenant VM groups, requires one exact-name match, then reads
that VM's detail. It never calls credentials, prints tokens, or dumps raw API
responses. Filesystem free space and installed services require host access.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
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


TARGET_NAME = "ttt"
MAX_SEARCH_GROUPS = 10
MAX_SEARCH_PROJECTS = 50
MAX_TOTAL_SEARCH_GROUPS = 100
MAX_LINKED_VOLUMES = 8
OUTPUT = provision.DEPLOY_DIR / "ttt-status.json"


class ExistingVmError(Exception):
    """Sanitized exact-target inventory error."""


def _volume_ids(raw_detail: Any) -> list[str]:
    identifiers = status._vm_volume_ids(raw_detail)
    if len(identifiers) > MAX_LINKED_VOLUMES:
        raise ExistingVmError("VM has more linked volumes than the bounded detail limit.")
    return sorted(identifiers)


def _flavor(token: str, group_id: str, raw_detail: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    attrs = inventory._attributes(raw_detail)
    flavor_id = status._reference_id(attrs.get("flavor"))
    flavor_name = inventory._short_text(attrs.get("flavor"), max_length=100)
    if isinstance(attrs.get("flavor"), dict):
        flavor_name = inventory._short_text(inventory._attributes(attrs["flavor"]).get("name"), max_length=100)
    try:
        catalog, truncated = inventory._catalog(token, group_id, "flavors")
    except inventory.InventoryError as exc:
        return None, str(exc)
    if truncated:
        return None, "Flavor catalog is truncated; CPU and memory are unknown."
    matches = ([item for item in catalog if item.get("id") == flavor_id]
               if flavor_id else [item for item in catalog if item.get("name") == flavor_name]
               if flavor_name else [])
    if len(matches) != 1:
        return None, "VM flavor is not uniquely linked to the current catalog."
    item = matches[0]
    return {"id": item["id"], "name": item["name"], "cpu": item["cpu"],
            "memory_mib": item["memory"],
            "association_basis": "vm_flavor_id" if flavor_id else "unique_vm_flavor_name"}, None


def _project_keys(token: str) -> list[str]:
    rows, total = inventory._page(inventory._get_json(token, "/api/v1/projects"))
    if len(rows) > MAX_SEARCH_PROJECTS or (total is not None and total > len(rows)):
        raise ExistingVmError("Accessible project list exceeds bounded discovery coverage.")
    keys = [project["key"] for raw in rows
            if (project := inventory._project(raw)) is not None and project.get("key")]
    unique = list(dict.fromkeys(keys))
    if len(unique) != len(rows):
        raise ExistingVmError("Some accessible projects lack unique tenant keys.")
    return unique


def read_existing_vm(*, tenant_id: str | None = None, token: str | None = None,
                     user_session: bool = False, discover_user_projects: bool = False) -> dict[str, Any]:
    if discover_user_projects and (tenant_id is not None or not user_session):
        raise ExistingVmError("Bounded project discovery requires the isolated user session and no tenant ID.")
    selected_tenant = tenant_id or provision.TENANT_ID
    if not discover_user_projects and inventory._identifier(selected_tenant) is None:
        raise ExistingVmError("Tenant key is invalid.")
    access_token = token or inventory._token_from_dp(user_session=user_session)
    tenants = _project_keys(access_token) if discover_user_projects else [selected_tenant]
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    groups_scanned = 0
    inaccessible_projects = 0
    for tenant in tenants:
        try:
            groups, group_truncated = inventory._paged_groups(access_token, tenant)
        except inventory.InventoryError:
            if discover_user_projects:
                inaccessible_projects += 1
                continue
            raise
        if group_truncated or len(groups) > MAX_SEARCH_GROUPS:
            raise ExistingVmError("Tenant VM group search exceeds the bounded coverage limit.")
        groups_scanned += len(groups)
        if groups_scanned > MAX_TOTAL_SEARCH_GROUPS:
            raise ExistingVmError("Cross-project VM group search exceeds the total bounded limit.")
        for group in groups:
            try:
                vms, vm_truncated = inventory._paged_vms(access_token, tenant, group["id"])
            except inventory.InventoryError:
                if discover_user_projects:
                    inaccessible_projects += 1
                    continue
                raise
            if vm_truncated:
                raise ExistingVmError("VM list is truncated; exact-name match is uncertain.")
            matches.extend((tenant, group, vm) for vm in vms if vm.get("name") == TARGET_NAME
                           and vm.get("status") != "removed")
    if len(matches) != 1:
        raise ExistingVmError("Expected exactly one current VM named ttt in the bounded tenant search.")
    tenant, group, listed = matches[0]
    vm_id = listed["id"]
    group_id = group["id"]
    path = f"/api/v2/tenants/{tenant}/vmgroup/{group_id}/vm/{vm_id}"
    raw = inventory._get_json(access_token, path)
    detail = status._safe_vm_detail(raw, vm_id)
    if detail["name"] != TARGET_NAME:
        raise ExistingVmError("VM detail name differs from the exact target ttt.")
    flavor, flavor_error = _flavor(access_token, group_id, raw)
    volumes: list[dict[str, Any]] = []
    volume_error: str | None = None
    try:
        for volume_id in _volume_ids(raw):
            volume_path = (f"/api/v2/tenants/{tenant}/vmgroup/{group_id}"
                           f"/volume/{volume_id}")
            volumes.append(status._safe_volume(
                inventory._get_json(access_token, volume_path), volume_id, vm_id,
                "vm_detail_reference"))
    except inventory.InventoryError as exc:
        volume_error = str(exc)
        volumes = []
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tenant_id": tenant,
        "lookup_basis": "bounded_user_projects" if discover_user_projects else "explicit_tenant",
        "projects_scanned": len(tenants), "groups_scanned": groups_scanned,
        "inaccessible_scopes": inaccessible_projects,
        "group": {"id": group_id, "name": group.get("name"),
                  "availability_zone": group.get("availability_zone"),
                  "status": group.get("status")},
        "vm": detail,
        "flavor": flavor, "flavor_read_error": flavor_error,
        "volumes": volumes, "volume_read_error": volume_error,
        "filesystem_free_space": None,
        "filesystem_free_space_basis": "requires authenticated in-VM df read",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--tenant-id", help="Exact known tenant/project key; defaults to hack-l2sh-2.")
    scope.add_argument("--discover-user-projects", action="store_true",
                       help="Bounded exact-name search across projects visible to the isolated user session.")
    parser.add_argument("--user-session", action="store_true",
                        help="Use the separate browser user-dp token instead of the service-account token.")
    args = parser.parse_args(argv)
    try:
        report = read_existing_vm(tenant_id=args.tenant_id,
                                  user_session=args.user_session or args.discover_user_projects,
                                  discover_user_projects=args.discover_user_projects)
        provision._write_private_json(OUTPUT, report)
    except (ExistingVmError, status.StatusError, provision.ProvisionError,
            inventory.InventoryError, inventory.SafeAccessError) as exc:
        print(f"Spirit ttt read failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit ttt read failed: local filesystem error.", file=sys.stderr)
        return 2
    vm = report["vm"]
    print(f"Private ttt report saved: {OUTPUT}")
    print(f"VM ID {vm['id']}; status {vm['status'] or 'unknown'}; "
          f"zone {report['group']['availability_zone'] or 'unknown'}; "
          f"private IP {vm['private_ip'] or 'unknown'}; "
          f"public IP {vm['public_ip'] or 'none reported'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
