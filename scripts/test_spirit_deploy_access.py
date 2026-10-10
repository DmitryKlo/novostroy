"""Safe Teleport identity renewal checks for the local deployment helper."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import spirit_deploy_access as access


class TeleportIdentityTests(unittest.TestCase):
    def test_new_dated_identity_keeps_old_file_and_suppresses_cli_output(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="teleport-renew-test-", dir=access.CONFIG_DIR
        ) as temporary:
            directory = Path(temporary)
            old_identity = directory / "teleport-identity"
            old_identity.write_text("old synthetic identity", encoding="utf-8")
            old_identity.chmod(0o600)
            new_identity = directory / "teleport-identity-20261010T134500Z"
            commands: list[list[str]] = []

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                commands.append(command)
                self.assertEqual(kwargs["stdout"], access.subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], access.subprocess.PIPE)
                self.assertEqual(kwargs["env"]["DP_WORKDIR"], "/private/dp-state")
                Path(command[-1]).write_text("synthetic-new-identity", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="secret token", stderr="private data")

            output = io.StringIO()
            with patch.object(access, "TELEPORT_IDENTITY_PATH", old_identity), \
                 patch.object(access, "resolve_dp", return_value=Path("/usr/bin/dp")), \
                 patch.object(access, "isolated_dp_environment", return_value={"DP_WORKDIR": "/private/dp-state"}), \
                 patch.object(access.subprocess, "run", side_effect=fake_run), \
                 contextlib.redirect_stdout(output):
                result = access.teleport_identity(None, output=new_identity)

            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 1)
            self.assertIn("hack-l2sh-2", commands[0])
            self.assertEqual(commands[0][-2:], ["--out", str(new_identity)])
            self.assertEqual(old_identity.read_text(encoding="utf-8"), "old synthetic identity")
            self.assertEqual(stat.S_IMODE(old_identity.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(new_identity.stat().st_mode), 0o600)
            self.assertNotIn("secret token", output.getvalue())
            self.assertNotIn("private data", output.getvalue())

    def test_renewal_requires_dated_path_in_private_directory(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="teleport-renew-test-", dir=access.CONFIG_DIR
        ) as temporary:
            old_identity = Path(temporary) / "teleport-identity"
            with patch.object(access, "TELEPORT_IDENTITY_PATH", old_identity):
                with self.assertRaises(access.SafeAccessError):
                    access._teleport_output_path(Path(temporary) / "teleport-identity")
                with self.assertRaises(access.SafeAccessError):
                    access._teleport_output_path(Path(temporary) / "teleport-identity-20261010T1345Z")

    def test_existing_new_path_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="teleport-renew-test-", dir=access.CONFIG_DIR
        ) as temporary:
            directory = Path(temporary)
            old_identity = directory / "teleport-identity"
            new_identity = directory / "teleport-identity-20261010T134500Z"
            new_identity.write_text("sentinel", encoding="utf-8")
            with patch.object(access, "TELEPORT_IDENTITY_PATH", old_identity), \
                 patch.object(access, "resolve_dp", return_value=Path("/usr/bin/dp")), \
                 patch.object(access.subprocess, "run") as run:
                with self.assertRaises(access.SafeAccessError):
                    access.teleport_identity(None, output=new_identity)
            run.assert_not_called()
            self.assertEqual(new_identity.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
