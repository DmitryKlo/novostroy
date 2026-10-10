"""Build the two deployment images from one reviewed, clean Git bundle checkout.

The `check` action is read-only. `build` tags both images with the exact source
commit and sets a revision label that apply_release checks before local deploy.
No GitLab or Spirit credentials are used.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.apply_release import IMAGE_REPOSITORIES, SHA_RE, _verify_local_images  # noqa: E402
from scripts.ci_source_guard import validate_checkout  # noqa: E402


def _output(*command: str) -> str:
    return subprocess.run(command, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def check_source(source_sha: str) -> dict[str, int | str]:
    if SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("Source SHA must be a full 40-character lowercase Git commit")
    if not (ROOT / ".git").is_dir():
        raise RuntimeError("Build from the checkout cloned from the reviewed source bundle")
    if _output("git", "rev-parse", "--verify", "HEAD") != source_sha:
        raise RuntimeError("Checkout HEAD differs from the reviewed source commit")
    if _output("git", "branch", "--show-current") != "branch1":
        raise RuntimeError("The source checkout must be on branch1")
    if _output("git", "status", "--porcelain"):
        raise RuntimeError("The source checkout is dirty; review it before building")
    result = validate_checkout(ROOT)
    return {"source_sha": source_sha, **result}


def build(source_sha: str) -> None:
    checked = check_source(source_sha)
    subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    for repository, dockerfile in zip(
        IMAGE_REPOSITORIES, ("deploy/api.Dockerfile", "deploy/web.Dockerfile"), strict=True
    ):
        image = f"{repository}:{source_sha}"
        subprocess.run(
            ["docker", "build", "--label", f"org.opencontainers.image.revision={source_sha}",
             "--tag", image, "--file", dockerfile, "."],
            cwd=ROOT, check=True,
        )
    _verify_local_images(source_sha)
    print(f"Built both reviewed local images for {checked['source_sha']}; "
          f"{checked['tracked_files']} source files checked")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "build"))
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if args.action == "check":
        checked = check_source(args.source_sha)
        print(f"Source checkout PASS: {checked['source_sha']}; "
              f"{checked['tracked_files']} files")
    else:
        build(args.source_sha)


if __name__ == "__main__":
    main()
