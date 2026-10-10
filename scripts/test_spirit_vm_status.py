"""Read-only, minimized VM status checks without network or credentials reads."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_inventory as inventory
from scripts import spirit_provision as provision
from scripts import spirit_vm_status as status


class VmStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name) / "deploy"
        for module, key, path in (
            (provision, "DEPLOY_DIR", base),
            (provision, "ATTEMPT_PATH", base / "provision-attempt.json"),
            (status, "OUTPUT", base / "vm-status.json"),
            (status, "CORRECTED_RECEIPT", base / "corrected-20gb-attempt.json"),
            (status, "CORRECTED_OUTPUT", base / "corrected-20gb-status.json"),
        ):
            patcher = patch.object(module, key, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        provision._write_private_json(provision.ATTEMPT_PATH, {
            "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
            "vm_name": provision.VM_NAME, "state": "submitted",
            "result": {"kind": "Operation", "id": "operation-1", "status": "new"},
        })

    def test_detail_keeps_only_safe_network_and_status_fields(self) -> None:
        raw = {"kind": "CloudVm", "attributes": {
            "id": "vm-1", "name": provision.VM_NAME, "status": "running",
            "ip": "203.0.113.8", "privateIp": "10.0.0.5", "fqdn": "vm.example.test",
            "teleportAppAccess": True, "teleportAppPort": 443,
            "teleportAppPublicAddr": "https://vm.example.test/private?token=secret",
            "volumes": {"kind": "Collection", "contents": [{"kind": "Reference",
                "identifier": {"name": "id", "value": "volume-1"}}]},
            "privateKey": "SECRET-KEY", "credentials": {"password": "SECRET"}}}
        operation = {"kind": "Operation", "attributes": {
            "id": "operation-1", "status": "done", "metadata": {"resourceId": "vm-1"},
            "result": {"secret": "SECRET-RESULT", "error": {"code": "NONE",
                "message": "Volume creation failed: Bearer not-saved-token"}}}}
        responses = {
            "/api/v2/cloud-operations/operation-1": operation,
            f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/vm/vm-1": raw,
            f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/volume?limit=100&offset=0": {
                "contents": [{"id": "volume-1", "vm": {"kind": "Reference",
                    "identifier": {"name": "id", "value": "vm-1"}}}], "total": 1},
            f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/{provision.GROUP_ID}/volume/volume-1": {
                "id": "volume-1", "status": "failed", "size": 40, "root": True,
                "vm": {"kind": "Reference", "identifier": {"name": "id", "value": "vm-1"}},
                "secret": "SECRET-VOLUME"},
        }
        with patch.object(inventory, "_paged_vms", return_value=([{"id": "vm-1", "name": provision.VM_NAME}], False)), \
             patch.object(inventory, "_get_json", side_effect=lambda _token, path: responses[path]) as get:
            report = status.read_vm_status(token="not-saved-token")
        self.assertEqual(len(get.call_args_list), 4)
        self.assertEqual(report["operation_id"], "operation-1")
        self.assertEqual(report["operation_status"], "done")
        self.assertEqual(report["operation_resource_id"], "vm-1")
        self.assertEqual(report["vm"]["private_ip"], "10.0.0.5")
        self.assertEqual(report["volumes"][0]["size_gb_ui_basis"], 40)
        self.assertEqual(report["volumes"][0]["association_basis"], "vm_detail_reference")
        self.assertIsNone(report["vm"]["teleport_app_host"])
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("not-saved-token", json.dumps(report))
        self.assertIn("Volume creation failed", report["operation_diagnostics"][0])
        self.assertIn("<redacted>", report["operation_diagnostics"][0])

    def test_pending_vm_is_explicit_without_guessing_network(self) -> None:
        with patch.object(inventory, "_paged_vms", return_value=([], False)), \
             patch.object(inventory, "_get_json", return_value={"id": "operation-1", "status": "new"}) as get:
            report = status.read_vm_status(token="not-saved-token")
        self.assertIsNone(report["vm"])
        self.assertIs(report["vm_found"], False)
        self.assertEqual(report["operation_status"], "new")
        get.assert_called_once()

    def test_identity_mismatch_refuses_detail(self) -> None:
        def get(_token: str, path: str) -> dict:
            return {"id": "operation-1", "status": "done"} if "cloud-operations" in path else {
                "id": "vm-other", "name": provision.VM_NAME}

        with patch.object(inventory, "_paged_vms", return_value=([{"id": "vm-1", "name": provision.VM_NAME}], False)), \
             patch.object(inventory, "_get_json", side_effect=get):
            with self.assertRaisesRegex(status.StatusError, "ID differs"):
                status.read_vm_status(token="not-saved-token")

    def test_operation_status_survives_vm_list_failure(self) -> None:
        with patch.object(inventory, "_paged_vms", side_effect=inventory.InventoryError("HTTP 403")), \
             patch.object(inventory, "_get_json", return_value={"id": "operation-1", "status": "running"}):
            report = status.read_vm_status(token="not-saved-token")
        self.assertEqual(report["operation_status"], "running")
        self.assertEqual(report["vm_read_error"], "HTTP 403")
        self.assertIsNone(report["vm"])

    def test_multiline_rpc_details_are_redacted_and_bounded(self) -> None:
        value = {"id": "operation-1", "status": "failed", "result": {
            "error": {"message": "RPC failed:\nstatus = StatusCode.UNKNOWN\n"
                                  "details = quota exhausted for disk 40GB; Bearer secret-token"}}}
        operation = status._safe_operation(value, "operation-1", "secret-token")
        lines = operation["diagnostics"]
        self.assertLessEqual(len(lines), 4)
        self.assertTrue(any("quota exhausted" in line for line in lines))
        self.assertNotIn("secret-token", json.dumps(operation))

    def test_partial_read_preserves_previous_status_file(self) -> None:
        status.OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        status.OUTPUT.write_text('{"operation_status":"failed"}', encoding="utf-8")
        partial = {"operation_read_error": "transport error", "vm_read_error": "transport error",
                   "volume_read_error": None, "operation_status": "unknown", "vm": None}
        with patch.object(status, "read_vm_status", return_value=partial):
            self.assertEqual(status.main([]), 3)
        self.assertEqual(json.loads(status.OUTPUT.read_text(encoding="utf-8")),
                         {"operation_status": "failed"})

    def test_corrected_mode_selects_operation_resource_not_old_tombstone(self) -> None:
        provision._write_private_json(status.CORRECTED_RECEIPT, {
            "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
            "vm_name": provision.VM_NAME, "state": "submitted",
            "result": {"kind": "Operation", "id": "corrected-operation-1"}})
        old = {"id": "old-vm", "name": provision.VM_NAME, "status": "removed"}
        new = {"id": "new-vm", "name": provision.VM_NAME, "status": "creating"}

        def get(_token: str, path: str) -> dict:
            if "cloud-operations" in path:
                return {"id": "corrected-operation-1", "status": "running",
                        "metadata": {"resourceId": "new-vm"}}
            return {"id": "new-vm", "name": provision.VM_NAME, "status": "creating"}

        with patch.object(inventory, "_paged_vms", return_value=([old, new], False)), \
             patch.object(inventory, "_get_json", side_effect=get), \
             patch.object(status, "_linked_volumes", return_value=([], None)):
            report = status.read_vm_status(token="not-saved-token", corrected20=True)
        self.assertEqual(report["receipt_mode"], "corrected_20gb")
        self.assertEqual(report["vm"]["id"], "new-vm")
        self.assertEqual(report["operation_resource_id"], "new-vm")


if __name__ == "__main__":
    unittest.main()
