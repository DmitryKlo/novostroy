"""Local-only checks that a prepared source cannot overwrite remote edits."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import publish_gitlab


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=directory, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def commit(directory: Path, subject: str) -> str:
    git(directory, "add", "--all")
    git(directory, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", subject)
    return git(directory, "rev-parse", "HEAD")


class SourceOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote"
        self.snapshot = self.root / "snapshot"
        self.remote.mkdir()
        self.snapshot.mkdir()
        git(self.remote, "init", "-q", "-b", "branch1")
        (self.remote / "README.md").write_text("base readme\n", encoding="utf-8")
        (self.remote / "docs.md").write_text("base docs\n", encoding="utf-8")
        self.base = commit(self.remote, "Base")
        self.snapshot_patch = patch.object(publish_gitlab, "SNAPSHOT", self.snapshot)
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)

    def test_remote_only_and_unchanged_source_edits_are_preserved(self) -> None:
        (self.remote / "README.md").write_text("remote edit\n", encoding="utf-8")
        (self.remote / "remote-only.md").write_text("keep me\n", encoding="utf-8")
        commit(self.remote, "Remote work")
        (self.snapshot / "README.md").write_text("base readme\n", encoding="utf-8")
        (self.snapshot / "docs.md").write_text("new docs\n", encoding="utf-8")

        changed = publish_gitlab._overlay_reviewed_paths(
            self.remote, [Path("README.md"), Path("docs.md")], self.base)

        self.assertEqual(changed, [Path("docs.md")])
        self.assertEqual((self.remote / "README.md").read_text(), "remote edit\n")
        self.assertEqual((self.remote / "docs.md").read_text(), "new docs\n")
        self.assertEqual((self.remote / "remote-only.md").read_text(), "keep me\n")

    def test_conflicting_edit_fails_before_copy(self) -> None:
        (self.remote / "README.md").write_text("remote edit\n", encoding="utf-8")
        commit(self.remote, "Remote work")
        (self.snapshot / "README.md").write_text("different local edit\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "changed since reviewed base"):
            publish_gitlab._overlay_reviewed_paths(
                self.remote, [Path("README.md")], self.base)
        self.assertEqual((self.remote / "README.md").read_text(), "remote edit\n")

    def test_existing_history_requires_a_reviewed_base(self) -> None:
        (self.snapshot / "README.md").write_text("local edit\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "reviewed base SHA"):
            publish_gitlab._overlay_reviewed_paths(
                self.remote, [Path("README.md")], None)

    def test_unrelated_base_is_rejected(self) -> None:
        (self.snapshot / "README.md").write_text("local edit\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "does not descend"):
            publish_gitlab._overlay_reviewed_paths(
                self.remote, [Path("README.md")], "0" * 40)

    def test_prepare_rejects_invalid_reviewed_base_before_staging(self) -> None:
        with self.assertRaisesRegex(ValueError, "40 lowercase hex"):
            publish_gitlab.prepare("not-a-reviewed-commit")


if __name__ == "__main__":
    unittest.main()
