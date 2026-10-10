"""Read-only and output minimization checks for Spirit inventory."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import spirit_inventory as inventory


class _Response:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status = status
        self.body = json.dumps(body or {}).encode()

    def getheader(self, _name: str) -> str:
        return str(len(self.body))

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class _Connection:
    requests: list[tuple[str, str, dict]] = []
    status = 200

    def __init__(self, host: str, **_kwargs: object) -> None:
        assert host == inventory.API_HOST

    def request(self, method: str, path: str, *, headers: dict) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> _Response:
        return _Response(self.status, {"contents": [], "total": 0})

    def close(self) -> None:
        pass


class SpiritInventoryTests(unittest.TestCase):
    def test_token_stays_out_of_argv_and_environment(self) -> None:
        token = "a" * 64
        recorded: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
            recorded.append((command, kwargs["env"]))
            return SimpleNamespace(stdout=token + "\n", stderr="", returncode=0)

        with patch.object(inventory, "resolve_dp", return_value=Path("/usr/bin/dp")), \
             patch.object(inventory, "isolated_dp_environment", return_value={"DP_WORKDIR": "/tmp/private"}), \
             patch.object(inventory.subprocess, "run", side_effect=fake_run):
            self.assertEqual(inventory._token_from_dp(), token)
        command, env = recorded[0]
        self.assertEqual(command, ["/usr/bin/dp", "auth", "print-token"])
        self.assertNotIn(token, " ".join(command))
        self.assertNotIn(token, " ".join(env.values()))

    def test_fixed_get_allowlist_and_redirect_rejection(self) -> None:
        _Connection.requests = []
        _Connection.status = 302
        with patch.object(inventory.http.client, "HTTPSConnection", _Connection):
            with self.assertRaises(inventory.InventoryError):
                inventory._get_json("secret-token", "/api/v1/projects")
        self.assertEqual(len(_Connection.requests), 1)
        method, path, headers = _Connection.requests[0]
        self.assertEqual((method, path), ("GET", "/api/v1/projects"))
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        with self.assertRaises(inventory.InventoryError):
            inventory._get_json("secret-token", "https://evil.example/api/v1/projects")

    def test_report_drops_identity_ip_and_unrecognized_fields(self) -> None:
        responses = {
            f"/api/v2/serviceaccounts/{inventory.OWN_SERVICE_ACCOUNT_ID}/roles": {
                "contents": [{"kind": "Role", "attributes": {"name": "vm-reader", "secret": "hidden"}}], "total": 1},
            "/api/v1/oauth2/userinfo": {"email": "private@example.test", "access_token": "secret"},
            "/api/v1/projects": {"contents": [{"id": "project-1", "name": "Demo", "secret": "hidden"}], "total": 1},
            "/api/v2/tenants/hack-l2sh-2/vmgroup?limit=100&offset=0": {
                "contents": [{"id": "group-1", "name": "App", "vmCount": 1, "tenantKey": "private",
                              "quotaMax": {"cpu": 4, "memory": 8, "vms": 2, "unexpected": "hidden"},
                              "quotaUsed": {"cpu": 2, "memory": 4, "vms": 1}}], "total": 1},
            "/api/v2/tenants/hack-l2sh-2/vmgroup/group-1/vm?limit=100&offset=0": {
                "contents": [{"id": "vm-1", "name": "App VM", "status": "RUNNING", "ip": "198.51.100.5",
                              "privateIp": "10.0.0.5", "flavor": {"name": "small"}, "access_token": "secret"}], "total": 1},
            "/api/v2/cloud-flavors?limit=100&offset=0&vmGroupId=group-1": {
                "contents": [{"kind": "CloudFlavor", "attributes": {"id": "flavor-1", "name": "small",
                    "cpu": 2, "memory": 4, "enabled": True, "secret": "hidden"}}], "total": 1},
            "/api/v2/cloud-images?limit=100&offset=0&vmGroupId=group-1": {
                "contents": [{"id": "image-1", "name": "Linux", "disk": 20, "memory": 2}], "total": 1},
        }
        with patch.object(inventory, "_get_json", side_effect=lambda _token, path: responses[path]):
            report = inventory.collect_inventory("hack-l2sh-2", token="secret-token",
                                                 service_account_id=inventory.OWN_SERVICE_ACCOUNT_ID)
        encoded = json.dumps(report)
        self.assertNotIn("private@example.test", encoded)
        self.assertNotIn("198.51.100.5", encoded)
        self.assertNotIn("10.0.0.5", encoded)
        self.assertNotIn("secret", encoded)
        self.assertEqual(report["vm_groups"][0]["quota_max"]["cpu"], 4)
        self.assertEqual(report["vm_groups"][0]["vms"][0]["flavor"], "small")
        self.assertEqual(report["vm_groups"][0]["flavors"][0]["cpu"], 2)
        self.assertEqual(report["vm_groups"][0]["images"][0]["disk"], 20)
        self.assertEqual(report["service_account_roles"], ["vm-reader"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "private" / "inventory.json"
            with patch.object(inventory, "OUTPUT", output):
                inventory._write_private_report(report)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)

    def test_zero_vm_count_still_reads_failed_vm_list(self) -> None:
        group = inventory._group({"id": "group-1", "name": "main", "vmCount": 0})
        with patch.object(inventory, "_paged_groups", return_value=([group], False)), \
             patch.object(inventory, "_paged_vms", return_value=([{"id": "failed-1", "name": "novostroy-web",
                                                            "status": "failed"}], False)) as list_vms, \
             patch.object(inventory, "_catalog", return_value=([], False)), \
             patch.object(inventory, "_get_json", return_value={"contents": [], "total": 0}):
            report = inventory.collect_inventory("hack-l2sh-2", token="not-saved-token")
        list_vms.assert_called_once()
        self.assertEqual(report["vm_groups"][0]["vms"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
