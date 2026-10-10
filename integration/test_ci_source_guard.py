"""Source-only CI boundary checks using synthetic Git trees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci_source_guard import REQUIRED_DOCKER_EXCLUSIONS, validate_checkout


def _stage(root: Path, *paths: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", *paths], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def source_checkout(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".dockerignore").write_text(
        "\n".join(sorted(REQUIRED_DOCKER_EXCLUSIONS)) + "\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/tool.py").write_text("print('safe')\n", encoding="utf-8")
    _stage(tmp_path, ".dockerignore", "scripts/tool.py")
    return tmp_path


def test_source_guard_accepts_only_reviewable_tracked_code(source_checkout: Path) -> None:
    assert validate_checkout(source_checkout)["tracked_files"] == 2


@pytest.mark.parametrize("name", ["data/private.json", "assets/tile.glb",
                                   ".runtime/secret.json", "scripts/raw.jsonl"])
def test_source_guard_rejects_private_or_generated_tracked_path(
        source_checkout: Path, name: str) -> None:
    path = source_checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fixture\n", encoding="utf-8")
    _stage(source_checkout, name)
    with pytest.raises((RuntimeError, ValueError)):
        validate_checkout(source_checkout)


def test_source_guard_rejects_secret_without_echoing_value(source_checkout: Path) -> None:
    value = "glpat-" + "A" * 25
    (source_checkout / "scripts/tool.py").write_text(value, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        validate_checkout(source_checkout)
    assert "scripts/tool.py" in str(error.value)
    assert value not in str(error.value)


def test_source_guard_rejects_link_and_missing_docker_exclusion(source_checkout: Path) -> None:
    link = source_checkout / "scripts/linked.py"
    link.symlink_to(source_checkout / "scripts/tool.py")
    _stage(source_checkout, "scripts/linked.py")
    with pytest.raises(ValueError, match="link"):
        validate_checkout(source_checkout)
    link.unlink()
    subprocess.run(["git", "-C", str(source_checkout), "rm", "--cached", "-q", "scripts/linked.py"],
                   check=True)
    (source_checkout / ".dockerignore").write_text("data\nassets\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Docker context"):
        validate_checkout(source_checkout)
    (source_checkout / ".dockerignore").write_text(
        "\n".join(sorted(REQUIRED_DOCKER_EXCLUSIONS)) + "\n!data/**\n", encoding="utf-8")
    with pytest.raises(ValueError, match="re-inclusion"):
        validate_checkout(source_checkout)
