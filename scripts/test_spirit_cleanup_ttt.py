"""Offline target and replay checks for the exact failed-ttt cleanup."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_cleanup_ttt as cleanup
from scripts import spirit_create_ttt as create
from scripts import spirit_provision as provision
from scripts import spirit_ttt_status as ttt_status


def _fixture() -> tuple[dict, dict]:
    now = datetime.now(timezone.utc).isoformat()
    receipt = {"state": "submitted", "plan_sha256": cleanup.EXPECTED_CREATE_SHA,
               "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
               "vm_name": create.VM_NAME, "root_volume_gb": create.ROOT_DISK_GB,
               "result": {"kind": "Operation", "id": cleanup.EXPECTED_OPERATION_ID},
               "attempted_at": now, "completed_at": now}
    report = {"tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
              "vm_name": create.VM_NAME, "observed_at": now,
              "operation_id": cleanup.EXPECTED_OPERATION_ID,
              "operation_status": "failed", "operation_resource_id": cleanup.EXPECTED_VM_ID,
              "operation_read_error": None, "vm_read_error": None,
              "volume_read_error": None, "vm_found": True,
              "vm": {"id": cleanup.EXPECTED_VM_ID, "name": create.VM_NAME,
                     "status": "failed", "created_at": now, "private_ip": None,
                     "public_ip": None, "fqdn": None, "teleport_app_host": None,
                     "teleport_app_access": False, "with_public_ip": False},
              "volumes": [{"id": cleanup.EXPECTED_VOLUME_ID,
                           "attached_vm_id": cleanup.EXPECTED_VM_ID,
                           "association_basis": "vm_detail_reference",
                           "root": True, "status": "error", "size_gb_ui_basis": 20}]}
    return receipt, report


class CleanupTttTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        deploy = Path(temporary.name) / "deploy"
        for module, name, value in (
            (provision, "DEPLOY_DIR", deploy),
            (create, "ATTEMPT_PATH", deploy / "create-ttt-attempt.json"),
            (ttt_status, "OUTPUT", deploy / "create-ttt-status.json"),
            (cleanup, "PLAN_PATH", deploy / "cleanup-ttt-plan.json"),
            (cleanup, "ATTEMPT_PATH", deploy / "cleanup-ttt-attempt.json"),
        ):
            mocked = patch.object(module, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.receipt, self.report = _fixture()
        provision._write_private_json(create.ATTEMPT_PATH, self.receipt)
        provision._write_private_json(ttt_status.OUTPUT, self.report)

    def test_exact_plan_and_identity_guards(self) -> None:
        plan = cleanup.prepare_plan()
        self.assertEqual(plan["request"], {"method": "DELETE", "path": cleanup._delete_path(),
                                           "body": None})
        self.assertFalse(plan["safety"]["root_volume_delete_in_this_plan"])
        wrong = copy.deepcopy(self.report)
        wrong["volumes"][0]["attached_vm_id"] = "other-vm"
        with self.assertRaisesRegex(cleanup.CleanupTttError, "Root volume differs"):
            cleanup._evidence(self.receipt, wrong, fresh=False)

    def test_delete_is_not_repeated_after_uncertain_response(self) -> None:
        plan = cleanup.prepare_plan()
        with patch.object(cleanup.inventory, "_token_from_dp", return_value="redacted"), \
             patch.object(ttt_status, "read_status", return_value=self.report), \
             patch.object(cleanup, "_delete_once", side_effect=cleanup.CleanupTttError("uncertain")) as delete:
            with self.assertRaises(cleanup.CleanupTttError):
                cleanup.apply_plan(plan["plan_sha256"])
            with self.assertRaisesRegex(cleanup.CleanupTttError, "already exists"):
                cleanup.apply_plan(plan["plan_sha256"])
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(provision._read_private_json(cleanup.ATTEMPT_PATH)["state"], "indeterminate")


if __name__ == "__main__":
    unittest.main()
