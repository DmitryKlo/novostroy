"""Offline checks for the isolated browser OAuth Git transport."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import publish_gitlab_gcm


class PublishGcmTests(unittest.TestCase):
    def test_browser_mode_keeps_git_prompts_disabled(self) -> None:
        with patch.dict(os.environ, {"GIT_TRACE": "/tmp/trace"}), \
                patch.object(publish_gitlab_gcm, "user_dp_environment",
                             return_value=dict(os.environ)):
            env = publish_gitlab_gcm._isolated_environment(
                Path("/tmp/gcm/git-credential-manager"), Path("/tmp/private/gitconfig"))
        self.assertEqual(env["GCM_INTERACTIVE"], "1")
        self.assertEqual(env["GCM_GUI_PROMPT"], "0")
        self.assertEqual(env["GCM_GITLAB_AUTHMODES"], "browser")
        self.assertEqual(env["GCM_CREDENTIAL_STORE"], "none")
        self.assertNotIn("GIT_TERMINAL_PROMPT", env)
        self.assertNotIn("GIT_TRACE", env)

    def test_probe_reports_category_without_echoing_credential(self) -> None:
        secret = "not-a-real-oauth-code"
        result = SimpleNamespace(returncode=128, stdout="", stderr=(
            "fatal: could not read Username for https://gitlab.spirit.culab.ru/"
            f"?code={secret}: terminal prompts disabled"))
        with patch.object(publish_gitlab_gcm.subprocess, "run", return_value=result) as run:
            with self.assertRaises(RuntimeError) as caught:
                publish_gitlab_gcm._gitlab_access_probe(options=(), env={})
        self.assertIn("credential helper returned no username", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(run.call_args.kwargs["stdin"], publish_gitlab_gcm.subprocess.DEVNULL)

    def test_dp_failure_shows_only_redacted_bounded_diagnostics(self) -> None:
        secret = "not-a-real-access-token"
        result = SimpleNamespace(returncode=1, stdout="", stderr=(
            "Error: OAuth request https://example.test/?code=" + secret + " failed\n"
            "response body: {\"token\":\"" + secret + "\"}"))
        with patch.object(publish_gitlab_gcm.subprocess, "run", return_value=result), \
                patch.object(publish_gitlab_gcm, "_known_key_literals", return_value=()):
            with self.assertRaises(RuntimeError) as caught:
                publish_gitlab_gcm._configure_git(
                    Path("/bin/true"), {}, Path("/tmp/nonexistent-gitconfig"))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("response body", str(caught.exception))
        self.assertIn("<redacted-url>", str(caught.exception))

    def test_private_config_audit_reports_only_allowlisted_presence(self) -> None:
        result = SimpleNamespace(returncode=0, stdout=(
            "credential.helper\n"
            "credential.https://gitlab.spirit.culab.ru.gitlabdevclientid\n"
            "credential.https://gitlab.spirit.culab.ru.gitlabdevclientsecret\n"
            "credential.https://other.example.test.provider\n"))
        with patch.object(publish_gitlab_gcm.subprocess, "run", return_value=result):
            summary = publish_gitlab_gcm._config_audit(Path("/tmp/private/gitconfig"))
        self.assertIn("helper=True", summary)
        self.assertIn("host_client_id=True", summary)
        self.assertIn("host_client_secret=True", summary)
        self.assertNotIn("other.example.test", summary)


if __name__ == "__main__":
    unittest.main()
