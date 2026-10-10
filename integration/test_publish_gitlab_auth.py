"""Offline checks for the temporary GitLab PAT transport."""

from __future__ import annotations

import getpass
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import publish_gitlab


class PublishTokenTests(unittest.TestCase):
    def test_git_environment_drops_tracing_and_config_injection(self) -> None:
        with patch.dict(os.environ, {
            "GIT_TRACE": "/tmp/trace", "GIT_TRACE_CURL": "1",
            "GIT_CURL_VERBOSE": "1", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "store",
        }):
            env = publish_gitlab._auth_environment()
        self.assertFalse(any(key.startswith("GIT_TRACE") for key in env))
        self.assertFalse(any(key.startswith("GIT_CONFIG_") for key in env))
        self.assertNotIn("GIT_CURL_VERBOSE", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "/bin/false")

    def test_cache_is_path_scoped_and_removed_even_if_exit_fails(self) -> None:
        dummy = "example-token-for-test"
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args: list[str], **kwargs):
            calls.append((args, kwargs))
            if args[-1] == "fill":
                return SimpleNamespace(returncode=0, stdout=(
                    "protocol=https\nhost=gitlab.spirit.culab.ru\n"
                    "path=hack-l2sh-2/novastroy.git\nusername=oauth2\n"
                    f"password={dummy}\n"
                ).encode())
            return SimpleNamespace(returncode=1 if args[-1] == "exit" else 0)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(publish_gitlab, "STAGING", Path(temporary)), \
                    patch.object(publish_gitlab.subprocess, "run", side_effect=fake_run):
                with publish_gitlab._token_credential_cache(dummy) as (options, env):
                    self.assertIn("credential.useHttpPath=true", options)
                    self.assertIn("http.followRedirects=false", options)
                    self.assertIn("credential.helper=", options)
                    self.assertNotIn(dummy, " ".join(options))
                    self.assertNotIn(dummy, str(env))
                    self.assertEqual(len(list(Path(temporary).glob("auth-*"))), 1)
                self.assertEqual(list(Path(temporary).glob("auth-*")), [])
        self.assertEqual([args[-1] for args, _ in calls], ["approve", "fill", "exit"])
        self.assertIn(b"path=hack-l2sh-2/novastroy.git\n", calls[0][1]["input"])
        self.assertEqual(calls[0][1]["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_echo_fallback_aborts_before_using_token(self) -> None:
        statuses: list[str] = []
        with patch.object(publish_gitlab, "_tracked_snapshot"), \
                patch.object(publish_gitlab, "_connection_preflight"), \
                patch.object(publish_gitlab, "_write_publish_status", side_effect=statuses.append), \
                patch.object(publish_gitlab.sys.stdin, "isatty", return_value=True), \
                patch.object(publish_gitlab.sys.stderr, "isatty", return_value=True), \
                patch.object(publish_gitlab.getpass, "getpass",
                             side_effect=getpass.GetPassWarning("no hidden input")), \
                patch.object(publish_gitlab, "_token_credential_cache") as cache:
            with self.assertRaisesRegex(RuntimeError, "Hidden terminal input failed"):
                publish_gitlab.publish_with_token()
        self.assertEqual(statuses, ["started", "needs_token", "auth_failed"])
        cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
