"""The CI deploy entry point must fail closed before invoking Compose."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ci_deploy


SHA = "a" * 40


def _environment(release: Path) -> dict[str, str]:
    return {"CI_COMMIT_BRANCH": "branch1", "CI_COMMIT_REF_PROTECTED": "true",
            "CI_COMMIT_SHA": SHA, "NOVOSTROY_DEPLOY_TARGET": "trusted-vm",
            "NOVOSTROY_RELEASE_DIR": str(release)}


def test_manual_deploy_requires_target_and_checked_release_root(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    release = releases / "release-1"
    release.mkdir(parents=True)
    state = tmp_path / "deploy-control"
    state.mkdir()
    monkeypatch.setattr(ci_deploy, "RELEASES_ROOT", releases)
    monkeypatch.setattr(ci_deploy, "STATE_DIR", state)
    command = ci_deploy.deployment_command(_environment(release), "trusted-vm")
    assert command[-2:] == ["--image-tag", SHA]
    assert command[2] == "apply"
    with pytest.raises(ValueError, match="target"):
        ci_deploy.deployment_command(_environment(release), "different-vm")
    unprotected = _environment(release) | {"CI_COMMIT_REF_PROTECTED": "false"}
    with pytest.raises(ValueError, match="protected"):
        ci_deploy.deployment_command(unprotected, "trusted-vm")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="direct child"):
        ci_deploy.deployment_command(_environment(outside), "trusted-vm")
    linked = releases / "release-link"
    linked.symlink_to(release, target_is_directory=True)
    with pytest.raises(ValueError, match="direct child"):
        ci_deploy.deployment_command(_environment(linked), "trusted-vm")
