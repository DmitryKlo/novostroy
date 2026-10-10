"""Offline identity checks for one created ttt operation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_create_ttt as create
from scripts import spirit_provision as provision
from scripts import spirit_ttt_status as target
from scripts import spirit_vm_status as status


class TttStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        deploy = Path(temporary.name) / "deploy"
        for module, name, path in (
            (provision, "DEPLOY_DIR", deploy),
            (create, "ATTEMPT_PATH", deploy / "create-ttt-attempt.json"),
        ):
            mocked = patch.object(module, name, path)
            mocked.start()
            self.addCleanup(mocked.stop)
        provision._write_private_json(create.ATTEMPT_PATH, {
            "tenant_id": provision.TENANT_ID, "vm_group_id": provision.GROUP_ID,
            "vm_name": "ttt", "root_volume_gb": 20, "state": "submitted",
            "result": {"kind": "Operation", "id": "op-1"},
        })

    def test_status_uses_operation_resource_id(self) -> None:
        operation = {"kind": "Operation", "attributes": {
            "id": "op-1", "status": "completed",
            "metadata": {"resourceId": "vm-new"}}}
        vm = {"kind": "CloudVm", "attributes": {
            "id": "vm-new", "name": "ttt", "status": "active",
            "privateIp": "10.0.0.2", "withPublicIp": False}}

        def get(_token: str, path: str) -> dict:
            return operation if path.endswith("/op-1") else vm

        with patch.object(target.inventory, "_get_json", side_effect=get), \
             patch.object(target.inventory, "_paged_vms", return_value=([
                 {"id": "vm-old", "name": "ttt", "status": "removed"},
                 {"id": "vm-new", "name": "ttt", "status": "active"}], False)), \
             patch.object(status, "_linked_volumes", return_value=([], None)):
            report = target.read_status(token="redacted")
        self.assertEqual(report["operation_resource_id"], "vm-new")
        self.assertEqual(report["vm"]["id"], "vm-new")
        self.assertEqual(report["vm"]["private_ip"], "10.0.0.2")

    def test_receipt_for_other_vm_is_rejected_before_network(self) -> None:
        receipt = provision._read_private_json(create.ATTEMPT_PATH)
        receipt["vm_name"] = "other"
        provision._write_private_json(create.ATTEMPT_PATH, receipt)
        with patch.object(target.inventory, "_get_json") as get:
            with self.assertRaisesRegex(status.StatusError, "exact target"):
                target.read_status(token="redacted")
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
