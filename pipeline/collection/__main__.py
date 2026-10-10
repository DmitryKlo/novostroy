"""Run reviewed collection jobs; default is offline read-only dry-run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.collection.runner import DEFAULT_CONFIG, DEFAULT_STATE_DIR, run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--apply", action="store_true", help="Commit immutable local snapshots and checkpoints")
    parser.add_argument("--allow-real", action="store_true", help="Also permit explicitly rights-cleared local real jobs")
    parser.add_argument("--allow-network", action="store_true", help="Opt in to reviewed public HTTP jobs")
    parser.add_argument("--offline", action="store_true", help="Explicitly assert the default offline-only mode")
    parser.add_argument("--job", action="append", help="Run only named jobs; may be repeated")
    args = parser.parse_args()
    if args.offline and args.allow_network:
        parser.error("--offline conflicts with --allow-network")
    result = run(args.config, args.state_dir, apply=args.apply, allow_real=args.allow_real,
                 allow_network=args.allow_network,
                 selected_jobs=set(args.job) if args.job else None)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if result["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
