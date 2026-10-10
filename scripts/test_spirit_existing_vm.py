"""Exact-name Spirit VM lookup checks without live API reads."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import spirit_existing_vm as existing
from scripts import spirit_inventory as inventory


class ExistingVmTests(unittest.TestCase):
    def test_exact_ttt_detail_omits_credentials(self) -> None:
        group = {"id": "group-1", "name": "main", "availability_zone": "ru-central1-d"}
        vm = {"id": "vm-ttt", "name": "ttt", "status": "active"}
        raw = {"id": "vm-ttt", "name": "ttt", "status": "active", "privateIp": "10.1.2.3",
               "withPublicIp": False, "privateKey": "SECRET", "password": "SECRET"}
        with patch.object(inventory, "_paged_groups", return_value=([group], False)), \
             patch.object(inventory, "_paged_vms", return_value=([vm], False)), \
             patch.object(inventory, "_get_json", return_value=raw), \
             patch.object(inventory, "_catalog", return_value=([], False)):
            report = existing.read_existing_vm(token="not-saved-token")
        self.assertEqual(report["vm"]["id"], "vm-ttt")
        self.assertEqual(report["vm"]["private_ip"], "10.1.2.3")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("not-saved-token", json.dumps(report))
        self.assertIsNone(report["filesystem_free_space"])

    def test_bounded_user_discovery_finds_exact_ttt_in_second_project(self) -> None:
        def groups(_token: str, tenant: str) -> tuple[list[dict], bool]:
            return ([{"id": tenant + "-group", "name": "group"}], False)

        def vms(_token: str, tenant: str, _group: str) -> tuple[list[dict], bool]:
            return ([{"id": "vm-ttt", "name": "ttt", "status": "active"}], False) \
                if tenant == "second" else ([], False)

        with patch.object(existing, "_project_keys", return_value=["first", "second"]), \
             patch.object(inventory, "_paged_groups", side_effect=groups), \
             patch.object(inventory, "_paged_vms", side_effect=vms), \
             patch.object(inventory, "_get_json", return_value={"id": "vm-ttt", "name": "ttt",
                                                              "status": "active"}), \
             patch.object(inventory, "_catalog", return_value=([], False)):
            report = existing.read_existing_vm(token="not-saved-token", user_session=True,
                                               discover_user_projects=True)
        self.assertEqual(report["tenant_id"], "second")
        self.assertEqual(report["projects_scanned"], 2)
        self.assertEqual(report["groups_scanned"], 2)

    def test_duplicate_exact_name_is_ambiguous(self) -> None:
        with patch.object(inventory, "_paged_groups", return_value=([{"id": "group-1"}], False)), \
             patch.object(inventory, "_paged_vms", return_value=([{"id": "vm-1", "name": "ttt"},
                                                                    {"id": "vm-2", "name": "ttt"}], False)):
            with self.assertRaisesRegex(existing.ExistingVmError, "exactly one"):
                existing.read_existing_vm(token="not-saved-token")


if __name__ == "__main__":
    unittest.main()
