"""Gate a manual deployment to the named, preconfigured local runner host."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
RELEASES_ROOT = Path("/srv/novostroy/releases")
STATE_DIR = Path("/srv/novostroy/deploy-control")


def deployment_command(environment: Mapping[str, str], hostname: str) -> list[str]:
    if (environment.get("CI_COMMIT_BRANCH") != "branch1"
            or environment.get("CI_COMMIT_REF_PROTECTED") != "true"):
        raise ValueError("Deployment requires the protected branch1 ref")
    sha = environment.get("CI_COMMIT_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("Deployment requires a full source commit SHA")
    target = environment.get("NOVOSTROY_DEPLOY_TARGET", "")
    if not target or target != hostname:
        raise ValueError("Trusted deploy target does not match this runner host")
    release_text = environment.get("NOVOSTROY_RELEASE_DIR", "")
    release = Path(release_text)
    if (not release.is_absolute() or release.parent != RELEASES_ROOT
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release.name)
            or not release.is_dir() or release.is_symlink()):
        raise ValueError("Select an existing direct child of /srv/novostroy/releases")
    if not STATE_DIR.is_dir() or STATE_DIR.is_symlink():
        raise ValueError("Private deploy-control directory is not configured")
    return [sys.executable, str(ROOT / "deploy/apply_release.py"), "apply",
            "--release-dir", str(release), "--state-dir", str(STATE_DIR),
            "--image-tag", sha]


def main() -> None:
    command = deployment_command(os.environ, socket.gethostname().split(".")[0])
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
