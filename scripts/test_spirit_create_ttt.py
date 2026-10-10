"""Offline safety tests for the exact one-attempt ttt create."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import spirit_create_ttt as target
from scripts import spirit_provision as provision


def _inventory() -> dict:
    return {"tenant_id": provision.TENANT_ID,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "vm_groups_truncated": False,
            "vm_groups": [{"id": provision.GROUP_ID, "name": "main-d",
                "availability_zone": "ru-central1-d", "status": "available",
                "quota_max": {"cpu": 4, "memory": 8, "disk": 60, "vms": 2},
                "quota_used": {"cpu": 0, "memory": 0, "disk": 0, "vms": 0},
                "vms": [{"id": "old", "name": "ttt", "status": "removed"}],
                "vms_truncated": False,
                "flavors": [{"id": provision.FLAVOR_ID, "name": "small", "cpu": 2,
                             "memory": 4096, "enabled": True, "public": True}],
                "flavors_truncated": False,
                "images": [{"id": provision.IMAGE_ID, "name": "Ubuntu Docker", "disk": 20}],
                "images_truncated": False}]}


class CreateTttTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name) / "deploy"
        for module, key, value in (
            (provision, "DEPLOY_DIR", base),
            (provision, "INVENTORY_PATH", base / "inventory.json"),
            (target, "PLAN_PATH", base / "create-ttt-plan.json"),
            (target, "ATTEMPT_PATH", base / "create-ttt-attempt.json"),
        ):
            mocked = patch.object(module, key, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        provision._write_private_json(provision.INVENTORY_PATH, _inventory())

    def test_plan_is_exact_and_immutable(self) -> None:
        plan = target.prepare_plan()
        body = plan["request"]["body"]
        self.assertEqual(body["attributes"]["name"], "ttt")
        self.assertEqual(body["attributes"]["volumes"]["contents"][0]["attributes"]["size"], 20)
        self.assertIs(body["attributes"]["withPublicIp"], False)
        self.assertEqual(plan["vm_group"]["id"], provision.GROUP_ID)
        with self.assertRaisesRegex(target.TttCreateError, "replacement is disabled"):
            target.prepare_plan()

    def test_current_name_or_disk_reservation_blocks_create(self) -> None:
        source = _inventory()
        source["vm_groups"][0]["vms"].append({"id": "current", "name": "ttt", "status": "failed"})
        with self.assertRaisesRegex(target.TttCreateError, "current VM"):
            target._select(source)
        source["vm_groups"][0]["vms"].pop()
        source["vm_groups"][0]["quota_used"]["disk"] = 20
        with self.assertRaisesRegex(target.TttCreateError, "not zero"):
            target._select(source)

    def test_one_post_and_no_replay_after_uncertain_result(self) -> None:
        plan = target.prepare_plan()
        with patch.object(target.inventory, "_token_from_dp", return_value="redacted"), \
             patch.object(target, "_fresh_preflight", return_value={"checked_at": "now"}), \
             patch.object(provision, "_post_once", side_effect=provision.ProvisionError("uncertain")) as post:
            with self.assertRaises(provision.ProvisionError):
                target.apply_plan(plan["plan_sha256"])
            with self.assertRaisesRegex(target.TttCreateError, "already exists"):
                target.apply_plan(plan["plan_sha256"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(provision._read_private_json(target.ATTEMPT_PATH)["state"], "indeterminate")


if __name__ == "__main__":
    unittest.main()
