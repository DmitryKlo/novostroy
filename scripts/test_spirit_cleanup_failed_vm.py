"""One-resource failed-VM cleanup gates; no live DELETE is sent."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_cleanup_failed_vm as cleanup
from scripts import spirit_provision as provision
from scripts import spirit_vm_status as status


def _fixtures() -> tuple[dict, dict]:
    now = datetime.now(timezone.utc).isoformat()
    receipt = {"state": "submitted", "plan_sha256": cleanup.EXPECTED_PROVISION_SHA,
               "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
               "vm_name": provision.VM_NAME, "attempted_at": "2026-10-10T10:11:03.501828Z",
               "completed_at": "2026-10-10T10:11:03.575845Z",
               "result": {"kind": "Operation", "id": cleanup.EXPECTED_OPERATION_ID}}
    report = {"tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
              "observed_at": now, "operation_id": cleanup.EXPECTED_OPERATION_ID,
              "operation_status": "failed", "operation_resource_id": cleanup.EXPECTED_VM_ID,
              "operation_read_error": None, "vm_read_error": None,
              "volume_read_error": None, "vm_found": True,
              "vm": {"id": cleanup.EXPECTED_VM_ID, "name": provision.VM_NAME,
                     "status": "failed", "created_at": "2026-10-10T10:11:03.540235Z",
                     "private_ip": None, "public_ip": None, "fqdn": None,
                     "teleport_app_host": None, "teleport_app_access": False,
                     "with_public_ip": False},
              "volumes": [{"id": cleanup.EXPECTED_VOLUME_ID, "status": "error", "root": True,
                           "size_gb_ui_basis": 40, "attached_vm_id": cleanup.EXPECTED_VM_ID,
                           "association_basis": "vm_detail_reference"}]}
    return receipt, report


class FailedVmCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name) / "deploy"
        for module, key, path in (
            (provision, "DEPLOY_DIR", base),
            (provision, "ATTEMPT_PATH", base / "provision-attempt.json"),
            (cleanup, "CLEANUP_PLAN", base / "cleanup-plan.json"),
            (cleanup, "CLEANUP_ATTEMPT", base / "cleanup-attempt.json"),
            (cleanup, "STATUS_REPORT", base / "vm-status.json"),
            (cleanup, "POSTCHECK_REPORT", base / "cleanup-postcheck.json"),
        ):
            patcher = patch.object(module, key, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        receipt, report = _fixtures()
        self.receipt, self.report = receipt, report
        provision._write_private_json(provision.ATTEMPT_PATH, receipt)
        provision._write_private_json(cleanup.STATUS_REPORT, report)

    def test_plan_exact_failed_vm_only_and_immutable(self) -> None:
        plan = cleanup.prepare_cleanup_plan()
        self.assertEqual(plan["request"], {"method": "DELETE", "body": None,
            "path": (f"/api/v2/tenants/{provision.TENANT_ID}/vmgroup/"
                     f"{provision.GROUP_ID}/vm/{cleanup.EXPECTED_VM_ID}")})
        self.assertIs(plan["safety"]["root_volume_delete_in_this_plan"], False)
        with self.assertRaisesRegex(cleanup.CleanupError, "replacement is disabled"):
            cleanup.prepare_cleanup_plan()

    def test_address_or_other_volume_blocks_cleanup(self) -> None:
        changed = copy.deepcopy(self.report)
        changed["vm"]["private_ip"] = "10.0.0.2"
        with self.assertRaisesRegex(cleanup.CleanupError, "unaddressed"):
            cleanup._validate_evidence(self.receipt, changed, fresh=True)
        changed = copy.deepcopy(self.report)
        changed["volumes"].append(copy.deepcopy(changed["volumes"][0]))
        with self.assertRaisesRegex(cleanup.CleanupError, "exactly one"):
            cleanup._validate_evidence(self.receipt, changed, fresh=True)

    def test_uncertain_delete_is_recorded_and_not_retried(self) -> None:
        plan = cleanup.prepare_cleanup_plan()
        with patch.object(cleanup.inventory, "_token_from_dp", return_value="not-saved-token"), \
             patch.object(status, "read_vm_status", return_value=self.report), \
             patch.object(cleanup, "_delete_vm_once", side_effect=cleanup.CleanupError("uncertain")) as delete:
            with self.assertRaisesRegex(cleanup.CleanupError, "uncertain"):
                cleanup.apply_cleanup_plan(plan["plan_sha256"])
            with self.assertRaisesRegex(cleanup.CleanupError, "already exists"):
                cleanup.apply_cleanup_plan(plan["plan_sha256"])
        self.assertEqual(delete.call_count, 1)
        attempt = provision._read_private_json(cleanup.CLEANUP_ATTEMPT)
        self.assertEqual(attempt["state"], "indeterminate")
        self.assertEqual(attempt["vm_id"], cleanup.EXPECTED_VM_ID)

    def test_verify_reports_attached_error_volume_without_deleting_it(self) -> None:
        plan = cleanup.prepare_cleanup_plan()
        provision._write_private_json(cleanup.CLEANUP_ATTEMPT, {
            "state": "submitted", "plan_sha256": plan["plan_sha256"],
            "vm_id": cleanup.EXPECTED_VM_ID, "volume_id": cleanup.EXPECTED_VOLUME_ID,
            "result": {"kind": "Operation", "id": "cleanup-operation-1"}})
        group = {"id": provision.GROUP_ID, "quota_used": {"disk": 40},
                 "quota_max": {"disk": 60}}
        volume = {"id": cleanup.EXPECTED_VOLUME_ID, "status": "error", "size": 40,
                  "root": True, "vm": {"kind": "Reference", "identifier": {
                      "name": "id", "value": cleanup.EXPECTED_VM_ID}}}

        def get(_token: str, path: str) -> dict:
            if "cloud-operations" in path:
                return {"id": "cleanup-operation-1", "status": "done",
                        "metadata": {"resourceId": cleanup.EXPECTED_VM_ID}}
            return volume if path.endswith(cleanup.EXPECTED_VOLUME_ID) else {
                "contents": [volume], "total": 1}

        with patch.object(cleanup.inventory, "_token_from_dp", return_value="not-saved-token"), \
             patch.object(cleanup.inventory, "_paged_groups", return_value=([group], False)), \
             patch.object(cleanup.inventory, "_paged_vms", return_value=([], False)), \
             patch.object(cleanup.inventory, "_get_json", side_effect=get):
            report = cleanup.verify_cleanup()
        self.assertTrue(report["vm_absent"])
        self.assertTrue(report["vm_removed"])
        self.assertFalse(report["volume_absent"])
        self.assertEqual(report["volume_if_present"]["status"], "error")
        self.assertFalse(report["volume_removed"])
        self.assertTrue(report["cleanup_operation_succeeded"])
        self.assertEqual(report["group_quota_used_disk_gb"], 40)

    def test_corrected20_plan_is_separate_and_targets_only_second_vm(self) -> None:
        base = cleanup.CLEANUP_PLAN.parent
        second_receipt = copy.deepcopy(self.receipt)
        second_receipt.update({"plan_sha256": cleanup.CORRECTED_PROVISION_SHA,
                               "root_volume_gb": 20,
                               "attempted_at": "2026-10-10T10:31:38.029565Z",
                               "completed_at": "2026-10-10T10:31:38.091551Z",
                               "result": {"kind": "Operation", "id": cleanup.CORRECTED_OPERATION_ID}})
        second_status = copy.deepcopy(self.report)
        second_status.update({"receipt_mode": "corrected_20gb",
                              "operation_id": cleanup.CORRECTED_OPERATION_ID,
                              "operation_resource_id": cleanup.CORRECTED_VM_ID})
        second_status["vm"].update({"id": cleanup.CORRECTED_VM_ID,
                                    "created_at": "2026-10-10T10:31:38.063777Z"})
        second_status["volumes"][0].update({"id": cleanup.CORRECTED_VOLUME_ID,
                                              "attached_vm_id": cleanup.CORRECTED_VM_ID,
                                              "size_gb_ui_basis": 20})
        paths = {"CORRECTED_PLAN": base / "cleanup-corrected20-plan.json",
                 "CORRECTED_ATTEMPT": base / "cleanup-corrected20-attempt.json",
                 "CORRECTED_RECEIPT": base / "corrected-20gb-attempt.json",
                 "CORRECTED_STATUS": base / "corrected-20gb-status.json",
                 "CORRECTED_POSTCHECK": base / "cleanup-corrected20-postcheck.json"}
        with patch.multiple(cleanup, **paths):
            provision._write_private_json(cleanup.CORRECTED_RECEIPT, second_receipt)
            provision._write_private_json(cleanup.CORRECTED_STATUS, second_status)
            plan = cleanup.prepare_cleanup_plan(corrected20=True)
            self.assertEqual(plan["evidence"]["vm_id"], cleanup.CORRECTED_VM_ID)
            self.assertEqual(plan["evidence"]["volume_size_gb"], 20)
            self.assertTrue(plan["request"]["path"].endswith(cleanup.CORRECTED_VM_ID))
            self.assertFalse(cleanup.CLEANUP_PLAN.exists())


if __name__ == "__main__":
    unittest.main()
