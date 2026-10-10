"""Check active park walks against the same map and apartment snapshot.

The local Python 3.13 environment can stall its AnyIO worker pool, so this
checker invokes the FastAPI route function directly. A real HTTP check should
follow after the managed API is restarted with this source revision.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_DATA_MODE"] = "real"
os.environ["NOVOSTROY_REAL_STORE"] = "empty"
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api, developer_map, developer_experience  # noqa: E402


LAKE = "registry-ffc1b55b59b7001dabd36f2f"


def check() -> None:
    map_run = developer_map.resolve_active()
    assert map_run is not None and LAKE in map_run["by_registry"]
    experience = developer_experience.resolve_active(map_run)
    assert experience is not None
    menu = experience[0]
    schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/DeveloperParkWalk"})
    body = api.developer_building_park_walk(LAKE)
    validator.validate(body)
    assert body["map_run_id"] == map_run["manifest"]["run_id"]
    assert body["unit_run_id"] == menu["manifest"]["run_id"]
    assert body["status"] == "routed" and body["distance_m"] > 0
    assert body["estimated_minutes"] > 0 and body["access_verified"] is False
    assert body["endpoint_basis"] == "pedestrian_graph_near_park_boundary"
    assert body["boundary_gap_m"] <= 15
    serialized = json.dumps(body, ensure_ascii=False)
    assert "source_id" not in serialized and "source_url" not in serialized
    assert "straight_line_m" not in serialized
    print("PASS active park-walk: map/menu match, routed boundary approach, private source redacted")


if __name__ == "__main__":
    check()
