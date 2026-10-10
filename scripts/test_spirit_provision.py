"""One-create safety checks without contacting Spirit or creating a VM."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_provision as provision


def _inventory() -> dict:
    return {"tenant_id": provision.TENANT_ID,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "vm_groups": [{
                "id": provision.GROUP_ID, "name": "main-d", "status": "available",
                "availability_zone": "ru-central1-d", "quota_max": {"cpu": 4, "memory": 8, "disk": 60, "vms": 2},
                "quota_used": {"cpu": 0, "memory": 0, "disk": 0, "vms": 0},
                "vms": [], "vms_truncated": False,
                "flavors": [{"id": provision.FLAVOR_ID, "name": "small", "cpu": 2, "memory": 4096,
                             "enabled": True, "public": True}], "flavors_truncated": False,
                "images": [{"id": provision.IMAGE_ID, "name": "Ubuntu 24.04 LTS + Docker", "disk": 20}],
                "images_truncated": False,
            }]}


class ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name) / "deploy"
        for key, path in (
            ("DEPLOY_DIR", base), ("INVENTORY_PATH", base / "inventory.json"),
            ("PLAN_PATH", base / "provision-plan.json"),
            ("ATTEMPT_PATH", base / "provision-attempt.json"),
        ):
            patcher = patch.object(provision, key, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        provision._write_private_json(provision.INVENTORY_PATH, _inventory())

    def test_review_plan_has_exact_reference_and_volume_wire_shape(self) -> None:
        plan = provision.prepare_plan()
        body = plan["request"]["body"]
        self.assertEqual(body["kind"], "CloudVm")
        self.assertEqual(body["attributes"]["flavor"], {
            "kind": "Reference", "resourceKind": "CloudFlavor",
            "identifier": {"name": "id", "value": provision.FLAVOR_ID}})
        self.assertEqual(body["attributes"]["volumes"], {
            "kind": "Collection", "contents": [{"kind": "CloudVolume", "attributes": {"size": 40}}]})
        self.assertIs(body["attributes"]["withPublicIp"], False)
        self.assertEqual(plan["vm_group"]["quota_remaining_before_create"]["memory"], 8)
        self.assertEqual(plan["plan_sha256"], provision._canonical_hash(
            {key: value for key, value in plan.items() if key != "plan_sha256"}))
        with self.assertRaisesRegex(provision.ProvisionError, "replacement is disabled"):
            provision.prepare_plan()
        self.assertEqual(provision._read_private_json(provision.PLAN_PATH)["plan_sha256"],
                         plan["plan_sha256"])

    def test_quota_or_existing_name_prevents_plan(self) -> None:
        source = _inventory()
        source["vm_groups"][0]["quota_max"]["memory"] = 3
        with self.assertRaisesRegex(provision.ProvisionError, "memory quota"):
            provision._target_from_inventory(source)
        source = _inventory()
        source["vm_groups"][0]["vms"] = [{"name": provision.VM_NAME.upper()}]
        with self.assertRaisesRegex(provision.ProvisionError, "already exists"):
            provision._target_from_inventory(source)

    def test_uncertain_post_is_recorded_and_never_retried(self) -> None:
        plan = provision.prepare_plan()
        with patch.object(provision.inventory, "_token_from_dp", return_value="secret-token"), \
             patch.object(provision, "_fresh_preflight", return_value={"checked_at": "2026-10-10T00:00:00Z"}), \
             patch.object(provision, "_post_once", side_effect=provision.ProvisionError("uncertain")) as post:
            with self.assertRaisesRegex(provision.ProvisionError, "uncertain"):
                provision.apply_plan(plan["plan_sha256"])
            with self.assertRaisesRegex(provision.ProvisionError, "already exists"):
                provision.apply_plan(plan["plan_sha256"])
        self.assertEqual(post.call_count, 1)
        receipt = json.loads(provision.ATTEMPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "indeterminate")
        self.assertNotIn("secret-token", json.dumps(receipt))

    def test_fresh_conflict_prevents_receipt_and_post(self) -> None:
        plan = provision.prepare_plan()
        group = _inventory()["vm_groups"][0]
        with patch.object(provision.inventory, "_paged_groups", return_value=([group], False)), \
             patch.object(provision.inventory, "_paged_vms", return_value=([{"name": provision.VM_NAME}], False)), \
             patch.object(provision.inventory, "_token_from_dp", return_value="secret-token"), \
             patch.object(provision, "_post_once") as post:
            with self.assertRaisesRegex(provision.ProvisionError, "already exists"):
                provision.apply_plan(plan["plan_sha256"])
        self.assertFalse(provision.ATTEMPT_PATH.exists())
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
