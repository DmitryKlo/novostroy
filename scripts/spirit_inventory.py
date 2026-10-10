#!/usr/bin/env python3
"""Read a bounded Spirit project and VM inventory into private local JSON.

The access token is obtained from isolated ``dp`` state and stays in this
process's memory. HTTP requests are GET-only to the fixed Spirit HTTPS host.
Raw API bodies, identities, credentials, IP addresses and token output are
never saved or printed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .spirit_deploy_access import (
        SafeAccessError,
        isolated_dp_environment,
        resolve_dp,
        user_dp_environment,
    )
else:
    from spirit_deploy_access import SafeAccessError, isolated_dp_environment, resolve_dp, user_dp_environment


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".runtime/deploy/inventory.json"
API_HOST = "spirit.culab.ru"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
MAX_BODY_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 100
MAX_GROUP_PAGES = 5
MAX_VM_PAGES = 3
MAX_GROUPS = 200
MAX_VMS_PER_GROUP = 200
MAX_CATALOG_GROUPS = 10
OWN_SERVICE_ACCOUNT_ID = "8ca85d21-1fdc-43c5-ac8e-eaa8a509e082"
METADATA_TENANT_KEY = "hack-l2sh-2"
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{24,8192}\Z")


class InventoryError(Exception):
    """A sanitized error that cannot contain an API body or token."""


def _identifier(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63:
        return str(value)
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten Spirit's `{kind, attributes}` wrapper without retaining it."""
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        return item
    result = dict(attributes)
    for key in ("id", "uuid"):
        if key not in result and key in item:
            result[key] = item[key]
    return result


def _short_text(value: Any, *, max_length: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable()).strip()
    return cleaned[:max_length] if cleaned else None


def _nonnegative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _quota(value: Any) -> dict[str, int | float | None]:
    source = value if isinstance(value, dict) else {}
    return {key: _nonnegative_number(source.get(key))
            for key in ("cpu", "memory", "vms", "disk", "publicIp", "gpu")}


def _page(value: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)], len(value)
    if not isinstance(value, dict):
        raise InventoryError("Unexpected API page shape.")
    for key in ("contents", "items", "projects"):
        data = value.get(key)
        if isinstance(data, list):
            total = value.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                total = None
            return [item for item in data if isinstance(item, dict)], total
    raise InventoryError("Unexpected API page shape.")


def _token_from_dp(*, user_session: bool = False) -> str:
    executable = resolve_dp(None)
    env = user_dp_environment() if user_session else isolated_dp_environment()
    try:
        result = subprocess.run(
            [str(executable), "auth", "print-token"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError("Could not read the isolated Spirit access token.") from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not TOKEN_RE.fullmatch(token):
        raise InventoryError("Isolated Spirit authentication is unavailable.")
    return token


def _allowed_path(path: str) -> bool:
    if path in {"/api/v1/projects", "/api/v1/oauth2/userinfo"}:
        return True
    if path == (f"/api/v2/tenants/{METADATA_TENANT_KEY}/serviceaccounts/"
                f"{OWN_SERVICE_ACCOUNT_ID}"):
        return True
    return bool(re.fullmatch(
        r"/api/v2/tenants/[A-Za-z0-9_-]+/vmgroup\?limit=(?:100)&offset=[0-9]+"
        r"|/api/v2/tenants/[A-Za-z0-9_-]+/vmgroup/[A-Za-z0-9_-]+/vm\?limit=(?:100)&offset=[0-9]+"
        r"|/api/v2/tenants/[A-Za-z0-9_-]+/vmgroup/[A-Za-z0-9_-]+/vm/[A-Za-z0-9_-]+"
        r"|/api/v2/tenants/[A-Za-z0-9_-]+/vmgroup/[A-Za-z0-9_-]+/volume\?limit=(?:100)&offset=[0-9]+"
        r"|/api/v2/tenants/[A-Za-z0-9_-]+/vmgroup/[A-Za-z0-9_-]+/volume/[A-Za-z0-9_-]+"
        r"|/api/v2/serviceaccounts/[A-Za-z0-9_-]+/roles"
        r"|/api/v2/cloud-operations/[A-Za-z0-9_-]+"
        r"|/api/v2/cloud-flavors\?limit=(?:100)&offset=[0-9]+&vmGroupId=[A-Za-z0-9_-]+"
        r"|/api/v2/cloud-images\?limit=(?:100)&offset=[0-9]+&vmGroupId=[A-Za-z0-9_-]+",
        path,
    ))


def _get_json(token: str, path: str) -> Any:
    if not _allowed_path(path):
        raise InventoryError("Requested path is outside the read-only API allowlist.")
    context = ssl.create_default_context(cafile=str(SYSTEM_CA_BUNDLE) if SYSTEM_CA_BUNDLE.is_file() else None)
    connection = http.client.HTTPSConnection(API_HOST, timeout=20, context=context)
    try:
        connection.request("GET", path, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Novostroy read-only inventory/1",
        })
        response = connection.getresponse()
        if response.status != 200:
            # http.client never follows redirects. Do not inspect or report
            # response bodies because they may contain private account data.
            raise InventoryError(f"Spirit inventory GET returned HTTP {response.status}.")
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise InventoryError("Spirit inventory page exceeds the size limit.")
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise InventoryError("Spirit inventory page exceeds the size limit.")
        return json.loads(body)
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise InventoryError("Spirit inventory GET failed or returned invalid JSON.") from exc
    finally:
        connection.close()


def _project(item: dict[str, Any]) -> dict[str, Any] | None:
    item = _attributes(item)
    identifier = _identifier(item.get("id")) or _identifier(item.get("key"))
    if identifier is None:
        return None
    return {"id": identifier, "key": _identifier(item.get("key")),
            "name": _short_text(item.get("name")),
            "status": _short_text(item.get("status"), max_length=40)}


def _vm(item: dict[str, Any]) -> dict[str, Any] | None:
    item = _attributes(item)
    identifier = _identifier(item.get("id"))
    if identifier is None:
        return None
    flavor = item.get("flavor")
    image = item.get("image")
    return {"id": identifier, "name": _short_text(item.get("name")),
            "status": _short_text(item.get("status"), max_length=40),
            "flavor": _short_text(flavor if isinstance(flavor, str) else flavor.get("name") if isinstance(flavor, dict) else None),
            "image": _short_text(image if isinstance(image, str) else image.get("name") if isinstance(image, dict) else None),
            "with_gpu": item.get("withGpu") if isinstance(item.get("withGpu"), bool) else None,
            "with_public_ip": item.get("withPublicIp") if isinstance(item.get("withPublicIp"), bool) else None}


def _group(item: dict[str, Any]) -> dict[str, Any] | None:
    item = _attributes(item)
    identifier = _identifier(item.get("id"))
    if identifier is None:
        return None
    count = item.get("vmCount")
    return {"id": identifier, "name": _short_text(item.get("name")),
            "status": _short_text(item.get("status"), max_length=40),
            "availability_zone": _short_text(item.get("availabilityZone"), max_length=80),
            "vm_count_reported": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
            "quota_max": _quota(item.get("quotaMax")), "quota_used": _quota(item.get("quotaUsed")),
            "vms": [], "vms_truncated": False,
            "flavors": [], "flavors_truncated": False,
            "images": [], "images_truncated": False}


def _paged_groups(token: str, tenant_id: str) -> tuple[list[dict[str, Any]], bool]:
    groups: list[dict[str, Any]] = []
    total: int | None = None
    for page_number in range(MAX_GROUP_PAGES):
        path = f"/api/v2/tenants/{tenant_id}/vmgroup?limit={PAGE_SIZE}&offset={page_number * PAGE_SIZE}"
        records, total = _page(_get_json(token, path))
        for item in records:
            sanitized = _group(item)
            if sanitized is not None and len(groups) < MAX_GROUPS:
                groups.append(sanitized)
        if len(records) < PAGE_SIZE or (total is not None and (page_number + 1) * PAGE_SIZE >= total):
            break
        if len(groups) >= MAX_GROUPS:
            break
    truncated = (total is not None and total > len(groups)) or len(groups) >= MAX_GROUPS
    return groups, truncated


def _paged_vms(token: str, tenant_id: str, group_id: str) -> tuple[list[dict[str, Any]], bool]:
    vms: list[dict[str, Any]] = []
    total: int | None = None
    for page_number in range(MAX_VM_PAGES):
        path = (f"/api/v2/tenants/{tenant_id}/vmgroup/{group_id}/vm"
                f"?limit={PAGE_SIZE}&offset={page_number * PAGE_SIZE}")
        records, total = _page(_get_json(token, path))
        for item in records:
            sanitized = _vm(item)
            if sanitized is not None and len(vms) < MAX_VMS_PER_GROUP:
                vms.append(sanitized)
        if len(records) < PAGE_SIZE or (total is not None and (page_number + 1) * PAGE_SIZE >= total):
            break
        if len(vms) >= MAX_VMS_PER_GROUP:
            break
    truncated = (total is not None and total > len(vms)) or len(vms) >= MAX_VMS_PER_GROUP
    return vms, truncated


def _catalog_item(kind: str, item: dict[str, Any]) -> dict[str, Any] | None:
    item = _attributes(item)
    identifier = _identifier(item.get("id"))
    if identifier is None:
        return None
    if kind == "flavors":
        return {"id": identifier, "name": _short_text(item.get("name")),
                "platform": _short_text(item.get("platform"), max_length=80),
                "cpu": _nonnegative_number(item.get("cpu")),
                "memory": _nonnegative_number(item.get("memory")),
                "public": item.get("public") if isinstance(item.get("public"), bool) else None,
                "enabled": item.get("enabled") if isinstance(item.get("enabled"), bool) else None}
    return {"id": identifier, "name": _short_text(item.get("name")),
            "disk": _nonnegative_number(item.get("disk")),
            "memory": _nonnegative_number(item.get("memory"))}


def _catalog(token: str, group_id: str, kind: str) -> tuple[list[dict[str, Any]], bool]:
    if kind not in {"flavors", "images"} or _identifier(group_id) is None:
        raise InventoryError("Catalog selection is invalid.")
    path_kind = "cloud-flavors" if kind == "flavors" else "cloud-images"
    items: list[dict[str, Any]] = []
    total: int | None = None
    for page_number in range(3):
        path = f"/api/v2/{path_kind}?limit={PAGE_SIZE}&offset={page_number * PAGE_SIZE}&vmGroupId={group_id}"
        records, total = _page(_get_json(token, path))
        for record in records:
            sanitized = _catalog_item(kind, record)
            if sanitized is not None and len(items) < 200:
                items.append(sanitized)
        if len(records) < PAGE_SIZE or (total is not None and (page_number + 1) * PAGE_SIZE >= total):
            break
        if len(items) >= 200:
            break
    return items, (total is not None and total > len(items)) or len(items) >= 200


def _own_roles(token: str, service_account_id: str) -> list[str]:
    if _identifier(service_account_id) is None:
        raise InventoryError("Service-account ID is invalid.")
    result = _get_json(token, f"/api/v2/serviceaccounts/{service_account_id}/roles")
    if isinstance(result, list):
        entries = result
    elif isinstance(result, dict):
        entries = next((result[key] for key in ("contents", "roles", "items")
                        if isinstance(result.get(key), list)), None)
        if entries is None:
            raise InventoryError("Unexpected service-account roles shape.")
    else:
        raise InventoryError("Unexpected service-account roles shape.")
    labels: list[str] = []
    for entry in entries[:100]:
        if isinstance(entry, str):
            label = _short_text(entry, max_length=120)
        elif isinstance(entry, dict):
            attrs = _attributes(entry)
            role = attrs.get("role")
            if isinstance(role, dict):
                attrs = _attributes(role)
            label = (_short_text(attrs.get("name"), max_length=120)
                     or _short_text(attrs.get("key"), max_length=120))
        else:
            label = None
        if label and label not in labels:
            labels.append(label)
    return labels


def diagnose_group_shape(tenant_id: str) -> dict[str, Any]:
    """Return field names and types only; never field values or identities."""
    if _identifier(tenant_id) is None:
        raise InventoryError("Tenant ID is invalid.")
    page = _get_json(_token_from_dp(),
                     f"/api/v2/tenants/{tenant_id}/vmgroup?limit={PAGE_SIZE}&offset=0")
    records, total = _page(page)

    def types(value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        return {str(key)[:64]: type(item).__name__ for key, item in value.items()
                if isinstance(key, str) and len(key) <= 64}

    first = records[0] if records else None
    attrs = first.get("attributes") if isinstance(first, dict) else None
    flat = _attributes(first) if isinstance(first, dict) else None
    return {"page_fields": types(page), "total": total, "returned_count": len(records),
            "first_record_fields": types(first), "first_attributes_fields": types(attrs),
            "first_flat_fields": types(flat),
            "quota_max_fields": types(flat.get("quotaMax")) if isinstance(flat, dict) else None,
            "quota_used_fields": types(flat.get("quotaUsed")) if isinstance(flat, dict) else None}


def collect_inventory(tenant_id: str, *, token: str | None = None,
                      service_account_id: str | None = None) -> dict[str, Any]:
    if _identifier(tenant_id) is None:
        raise InventoryError("Tenant ID is invalid.")
    access_token = token or _token_from_dp()
    report: dict[str, Any] = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "api_origin": f"https://{API_HOST}",
        "tenant_id": tenant_id,
        "userinfo_authorized": False,
        "projects": [], "projects_truncated": False,
        "vm_groups": [], "vm_groups_truncated": False,
        "service_account_roles": [], "service_account_roles_status": "not_requested",
        "read_errors": [],
    }
    if service_account_id:
        try:
            report["service_account_roles"] = _own_roles(access_token, service_account_id)
            report["service_account_roles_status"] = "available"
        except InventoryError as exc:
            report["service_account_roles_status"] = "unavailable"
            report["read_errors"].append({"scope": "own_service_account_roles", "message": str(exc)})
    try:
        _get_json(access_token, "/api/v1/oauth2/userinfo")
        report["userinfo_authorized"] = True
    except InventoryError as exc:
        report["read_errors"].append({"scope": "userinfo", "message": str(exc)})
    try:
        project_rows, total = _page(_get_json(access_token, "/api/v1/projects"))
        report["projects"] = [project for item in project_rows if (project := _project(item)) is not None][:MAX_GROUPS]
        report["projects_truncated"] = total is not None and total > len(report["projects"])
    except InventoryError as exc:
        report["read_errors"].append({"scope": "projects", "message": str(exc)})
    try:
        groups, truncated = _paged_groups(access_token, tenant_id)
        report["vm_groups"] = groups
        report["vm_groups_truncated"] = truncated
    except InventoryError as exc:
        report["read_errors"].append({"scope": "vm_groups", "message": str(exc)})
        groups = []
    # vmCount can be zero while a failed create has left a failed VM and a
    # reserved root volume. Read a bounded set of groups even at count zero.
    for group in groups[:MAX_CATALOG_GROUPS]:
        try:
            group["vms"], group["vms_truncated"] = _paged_vms(access_token, tenant_id, group["id"])
        except InventoryError as exc:
            group["vm_read_error"] = str(exc)
            report["read_errors"].append({"scope": "group_vms", "message": str(exc)})
    for group in groups[MAX_CATALOG_GROUPS:]:
        group["vms_truncated"] = True
    for group in groups[:MAX_CATALOG_GROUPS]:
        for kind in ("flavors", "images"):
            try:
                group[kind], group[f"{kind}_truncated"] = _catalog(access_token, group["id"], kind)
            except InventoryError as exc:
                group[f"{kind}_read_error"] = str(exc)
                report["read_errors"].append({"scope": f"group_{kind}", "message": str(exc)})
    return report


def _write_private_report(report: dict[str, Any]) -> None:
    parent = OUTPUT.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InventoryError("Inventory output directory is unsafe.")
    parent.chmod(0o700)
    if OUTPUT.exists() and (OUTPUT.is_symlink() or not OUTPUT.is_file()):
        raise InventoryError("Existing inventory output path is unsafe.")
    descriptor, temporary = tempfile.mkstemp(prefix=".inventory-", suffix=".json", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, OUTPUT)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="hack-l2sh-2", help="Known Spirit tenant key (default: hack-l2sh-2).")
    parser.add_argument("--service-account-id", default=OWN_SERVICE_ACCOUNT_ID,
                        help="Read roles for this one known service account; use an empty value to skip.")
    parser.add_argument("--diagnose-shape", action="store_true", help="Print only VM group field names and value types; no values.")
    args = parser.parse_args(argv)
    try:
        if args.diagnose_shape:
            print(json.dumps(diagnose_group_shape(args.tenant_id), ensure_ascii=False, sort_keys=True))
            return 0
        report = collect_inventory(args.tenant_id, service_account_id=args.service_account_id or None)
        _write_private_report(report)
    except (SafeAccessError, InventoryError) as exc:
        print(f"Spirit inventory failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Spirit inventory failed: local filesystem error.", file=sys.stderr)
        return 2
    print(f"Read-only Spirit inventory saved privately: {OUTPUT}")
    print(f"Projects: {len(report['projects'])}; VM groups: {len(report['vm_groups'])}; "
          f"VMs: {sum(len(group['vms']) for group in report['vm_groups'])}.")
    for issue in report["read_errors"]:
        print(f"Read limitation ({issue['scope']}): {issue['message']}", file=sys.stderr)
    return 3 if report["read_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
