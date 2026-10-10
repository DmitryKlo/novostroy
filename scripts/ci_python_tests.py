"""Run deterministic source-only checks with bounded time and visible skips."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CHECKS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("contract registry", (PYTHON, "contracts/build_registry.py", "--check"), 120),
    ("acceptance cases", (PYTHON, "integration/run_acceptance.py"), 120),
    ("recommendation units", (PYTHON, "-m", "unittest", "discover", "-s",
                              "recommendations", "-p", "test_*.py"), 180),
    ("source-only backend API and geometry fixtures", (
        PYTHON, "-m", "pytest", "-q", "-ra", "--strict-markers",
        "backend/tests/test_generation.py", "backend/tests/test_models_api.py",
        "backend/tests/test_developer_map_api.py",
        "backend/tests/test_developer_units_api.py",
        "backend/tests/test_developer_models_api.py",
        "backend/tests/test_map_point_groups.py", "backend/tests/test_city_geometry.py",
        "backend/tests/test_parts_geometry.py::test_interval_extrusion_uses_top_not_top_plus_min_height",
    ), 90),
    ("cartography geometry fixtures", (
        PYTHON, "-m", "pytest", "-q", "-ra", "--strict-markers",
        "backend/tests/test_cartographic_layers.py::test_pastel_surface_triangulation_preserves_holes_and_disjoint_parts",
        "backend/tests/test_cartographic_layers.py::test_layer_offsets_and_ribbon_are_distinct",
    ), 60),
    ("property, facade, and maintenance fixtures", (
        PYTHON, "-m", "pytest", "-q", "-ra", "--strict-markers",
        "sources/property/tests", "sources/facades/tests", "pipeline/maintenance/tests",
    ), 120),
    ("deployment and CI fixtures", (
        PYTHON, "-m", "pytest", "-q", "-ra", "--strict-markers",
        "integration/test_ci_source_guard.py", "integration/test_ci_deploy.py",
        "integration/test_deploy_release.py", "integration/test_publish_gitlab_overlay.py",
        "integration/test_publish_gitlab_auth.py",
        "integration/test_criteria_producer.py", "integration/test_dev_startup.py",
        "scripts/tests/test_package_deploy_data.py",
        "scripts/tests/test_agent_data_bundle.py",
    ), 90),
)


def main() -> None:
    for label, command, seconds in CHECKS:
        print(f"CI check: {label}", flush=True)
        try:
            subprocess.run(command, cwd=ROOT, check=True, timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(f"CI check timed out after {seconds}s: {label}") from exc
    print("source-only Python checks PASS", flush=True)


if __name__ == "__main__":
    main()
