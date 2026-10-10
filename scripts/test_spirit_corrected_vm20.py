"""Offline checks for one corrected 20-GB VM create after cleanup."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_cleanup_failed_vm as cleanup
from scripts import spirit_corrected_vm20 as corrected
from scripts import spirit_provision as provision


def _fixtures() -> tuple[dict, dict, dict]:
    now = datetime.now(timezone.utc).isoformat()
    cleanup_attempt = {"state": "submitted", "http_status": 202,
                       "completed_at": "2026-10-10T10:27:25Z",
                       "vm_id": cleanup.EXPECTED_VM_ID, "volume_id": cleanup.EXPECTED_VOLUME_ID,
                       "result": {"kind": "Operation", "id": "cleanup-operation-1"}}
    postcheck = {"observed_at": now, "tenant_id": provision.TENANT_ID,
                 "vm_group_id": provision.GROUP_ID,
                 "vm_id": cleanup.EXPECTED_VM_ID, "volume_id": cleanup.EXPECTED_VOLUME_ID,
                 "cleanup_operation_id": "cleanup-operation-1",
                 "cleanup_operation_status": "completed", "cleanup_operation_succeeded": True,
                 "vm_removed": True, "volume_removed": True,
                 "group_quota_used_disk_gb": 0}
    inventory = {"tenant_id": provision.TENANT_ID, "observed_at": now,
                 "vm_groups_truncated": False,
                 "vm_groups": [{"id": provision.GROUP_ID, "name": "main-d", "status": "available",
                    "quota_max": {"cpu": 4, "memory": 8, "disk": 60, "vms": 2},
                    "quota_used": {"cpu": 0, "memory": 0, "disk": 0, "vms": 0},
                    "vms": [{"id": cleanup.EXPECTED_VM_ID, "name": provision.VM_NAME,
                             "status": "removed"}], "vms_truncated": False,
                    "flavors": [{"id": provision.FLAVOR_ID, "name": "small", "cpu": 2,
                                 "memory": 4096, "enabled": True, "public": True}],
                    "flavors_truncated": False,
                    "images": [{"id": provision.IMAGE_ID, "name": "Ubuntu Docker",
                                "disk": 20}], "images_truncated": False}]}
    return cleanup_attempt, postcheck, inventory


class CorrectedVmTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name) / "deploy"
        for module, key, path in (
            (provision, "DEPLOY_DIR", base),
            (provision, "INVENTORY_PATH", base / "inventory.json"),
            (cleanup, "CLEANUP_ATTEMPT", base / "cleanup-attempt.json"),
            (cleanup, "POSTCHECK_REPORT", base / "cleanup-postcheck.json"),
            (corrected, "PLAN_PATH", base / "corrected-plan.json"),
            (corrected, "ATTEMPT_PATH", base / "corrected-attempt.json"),
        ):
            patcher = patch.object(module, key, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cleanup_attempt, self.postcheck, self.inventory = _fixtures()
        provision._write_private_json(cleanup.CLEANUP_ATTEMPT, self.cleanup_attempt)
        provision._write_private_json(cleanup.POSTCHECK_REPORT, self.postcheck)
        provision._write_private_json(provision.INVENTORY_PATH, self.inventory)

    def test_plan_changes_only_root_volume_size_and_cannot_be_replaced(self) -> None:
        plan = corrected.prepare_plan()
        expected = provision._create_body()
        self.assertEqual(expected["attributes"]["volumes"]["contents"][0]["attributes"]["size"], 40)
        expected["attributes"]["volumes"]["contents"][0]["attributes"]["size"] = 20
        self.assertEqual(plan["request"]["body"], expected)
        self.assertEqual(plan["selection"]["image"]["disk"], 20)
        with self.assertRaisesRegex(corrected.CorrectedCreateError, "replacement is disabled"):
            corrected.prepare_plan()

    def test_uncleared_disk_or_unrelated_same_name_blocks_plan(self) -> None:
        postcheck = copy.deepcopy(self.postcheck)
        postcheck["group_quota_used_disk_gb"] = 40
        with self.assertRaisesRegex(corrected.CorrectedCreateError, "not confirmed"):
            corrected._cleanup_ready(postcheck, self.cleanup_attempt, check_age=True)
        source = copy.deepcopy(self.inventory)
        source["vm_groups"][0]["vms"].append({"id": "other-vm", "name": provision.VM_NAME,
                                                 "status": "active"})
        with self.assertRaisesRegex(corrected.CorrectedCreateError, "unrelated"):
            corrected._select(source)

    def test_uncertain_post_records_one_attempt_and_never_retries(self) -> None:
        plan = corrected.prepare_plan()
        with patch.object(corrected.inventory, "_token_from_dp", return_value="not-saved-token"), \
             patch.object(corrected, "_fresh_preflight", return_value={"checked_at": "2026-10-10T10:30:00Z"}), \
             patch.object(provision, "_post_once", side_effect=provision.ProvisionError("uncertain")) as post:
            with self.assertRaisesRegex(provision.ProvisionError, "uncertain"):
                corrected.apply_plan(plan["plan_sha256"])
            with self.assertRaisesRegex(corrected.CorrectedCreateError, "already exists"):
                corrected.apply_plan(plan["plan_sha256"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(provision._read_private_json(corrected.ATTEMPT_PATH)["state"], "indeterminate")


if __name__ == "__main__":
    unittest.main()
