"""Check the completed model-candidate run and public routes without TCP.

Run from the repository root with
``rtk proxy .runtime/dev-env/bin/python integration/check_model_routes.py``.
This invokes route functions in process; it does not prove proxy, browser or device loading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api  # noqa: E402
from backend import models as model_store  # noqa: E402
from integration.check_live import assert_public, validate  # noqa: E402


EXPECTED_INPUTS = {"registry": 1532, "mapped_construction": 756,
                   "external_event": 29}
EXPECTED_PRIVATE_SOURCES = {"estateline_accepted": 1473, "estateline_review": 59,
                            "osm_construction": 756, "minstroy_residential_event": 27,
                            "minstroy_text_audit": 2}


def check_attempt_coverage(run_id: str) -> None:
    run_dir = ROOT / "data/models" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_counts"] == EXPECTED_PRIVATE_SOURCES
    assert manifest["records_total"] == manifest["assessments_count"] == 2317
    with (run_dir / "assessments.jsonl").open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert len(rows) == len({row["candidate_id"] for row in rows}) == 2317
    assert all(isinstance(row.get("attempt_log"), dict) and row["attempt_log"] for row in rows)
    registry_keys = {"exact_osm_location_object_matches", "strict_address_key",
                     "strict_osm_address_matches", "verified_point_in_polygon_matches",
                     "validated_parts_parent_matches", "donor_geometry"}
    osm_keys = {"source_polygon", "typed_osm_identity", "validated_part_source_id_matches",
                "display_rights", "donor_geometry"}
    assert all(registry_keys <= row["attempt_log"].keys()
               for row in rows if row["entity_type"] == "registry_project")
    assert all(osm_keys <= row["attempt_log"].keys()
               for row in rows if row["entity_type"] == "mapped_construction")


def request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": b"", "headers": []})


def payload(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.status_code
    value = json.loads(response.body)
    assert isinstance(value, dict)
    return value


def listing(**filters: Any) -> dict[str, Any]:
    args = {"query": None, "decision": None, "reason_code": None,
            "source_group": None, "registry_id": None, "limit": 100, "offset": 0}
    args.update(filters)
    result = payload(api.model_candidates_list(request("/v1/models/candidates"), **args))
    validate("/v1/models/candidates", "get", result)
    assert_public(result)
    return result


def check_complex_previews(run_id: str) -> None:
    """A selected courtyard and multipart outline must match pinned source WKB."""
    import pyarrow.parquet as parquet
    from shapely import wkb
    from shapely.geometry import shape

    run_dir = ROOT / "data/models" / run_id
    with (run_dir / "source_evaluations.jsonl").open(encoding="utf-8") as stream:
        source_to_candidate = {
            row["source_record_id"]: row["candidate_id"]
            for row in (json.loads(line) for line in stream)
            if row["source_group"] == "osm_construction"
        }
    source_path = ROOT / "data/property/osm_construction" / (
        "osm_moscow_residential_construction_2026-10-08.parquet")
    source_rows = parquet.read_table(source_path, columns=[
        "osm_type", "osm_id", "osm_version", "geometry",
    ]).to_pylist()
    selected: dict[str, tuple[str, Any]] = {}
    for source_row in source_rows:
        source_id = (f"{source_row['osm_type']}/{source_row['osm_id']}/"
                     f"v{source_row['osm_version']}")
        candidate_id = source_to_candidate.get(source_id)
        if candidate_id is None:
            continue
        source_shape = wkb.loads(source_row["geometry"])
        if source_shape.geom_type == "MultiPolygon":
            selected.setdefault("MultiPolygon", (candidate_id, source_shape))
        elif source_shape.geom_type == "Polygon" and len(source_shape.interiors) > 0:
            selected.setdefault("courtyard", (candidate_id, source_shape))
        if len(selected) == 2:
            break
    assert set(selected) == {"MultiPolygon", "courtyard"}
    for kind, (candidate_id, source_shape) in selected.items():
        geometry = payload(api.model_candidates_geometry(
            candidate_id, request(f"/v1/models/candidates/{candidate_id}/geometry")))
        validate("/v1/models/candidates/{candidate_id}/geometry", "get", geometry)
        assert_public(geometry)
        preview = geometry["preview_geometry"]
        assert preview is not None
        if kind == "MultiPolygon":
            assert preview["type"] == "MultiPolygon"
            polygons = preview["coordinates"]
        else:
            assert preview["type"] == "Polygon" and len(preview["coordinates"]) > 1
            polygons = [preview["coordinates"]]
        rings = [ring for polygon in polygons for ring in polygon]
        assert sum(len(ring) for ring in rings) <= 512
        assert all(len(ring) >= 4 and ring[0] == ring[-1] for ring in rings)
        projected = shape(preview)
        assert projected.is_valid and projected.geom_type == source_shape.geom_type
        assert (projected.symmetric_difference(source_shape).area
                <= source_shape.area * 1e-8), kind
        assert all(abs(actual - expected) < 1e-6
                   for actual, expected in zip(geometry["bbox"], source_shape.bounds))


def check() -> None:
    summary = payload(api.model_candidates_summary(request("/v1/models/candidates/summary")))
    validate("/v1/models/candidates/summary", "get", summary)
    assert_public(summary)
    counts = summary["counts"]
    assert summary["run_status"] == "ready"
    check_attempt_coverage(summary["model_run_id"])
    check_complex_previews(summary["model_run_id"])
    assert counts["scanned"] == sum(EXPECTED_INPUTS.values()) == 2317
    assert sum(counts[key] for key in ("ready", "review", "rejected", "unavailable")) == 2317
    if "ready_depictions" in summary:
        assert sum(summary["ready_depictions"].values()) == counts["ready"]

    first = listing()
    assert first["dataset_total"] == first["records_total"] == 2317
    snapshot = (first["dataset_id"], first["registry_snapshot_id"], first["model_run_id"])
    assert snapshot == (summary["dataset_id"], summary["registry_snapshot_id"],
                        summary["model_run_id"])
    items: list[dict[str, Any]] = []
    for offset in range(0, 2317, 100):
        page = first if offset == 0 else listing(offset=offset)
        assert page["records_total"] == 2317 and page["offset"] == offset
        assert (page["dataset_id"], page["registry_snapshot_id"],
                page["model_run_id"]) == snapshot
        items.extend(page["items"])
    assert len(items) == len({row["candidate_id"] for row in items}) == 2317
    assert all("model_tileset" not in row and "selected_model" not in row
               for row in items)
    assert {group: sum(row["source_group"] == group for row in items)
            for group in EXPECTED_INPUTS} == EXPECTED_INPUTS
    assert sum(row["registry_id"] is not None for row in items) == 1473
    assert all(row["registry_id"] is None and row["project_link_status"] != "verified"
               for row in items if row["source_group"] == "mapped_construction")
    assert all(row["ready_to_render"] == (row["asset_readiness"] == "ready_to_render")
               for row in items)
    assert all(row["explanation_ru"] and row["reason_codes"]
               for row in items if row["decision"] != "ready")
    assert all(row["decision"] != "ready" or row["ready_to_render"]
               for row in items)
    assert all(row["render_height_m"] is not None and row["render_height_m"] > 0
               for row in items if row["depiction"] == "schematic_massing")
    assert all(row["render_height_m"] is None
               for row in items if row["depiction"] == "construction_outline")

    rejected = listing(decision="rejected")
    assert rejected["records_total"] == counts["rejected"]
    linked = next(row for row in items if row["registry_id"] is not None)
    linked_list = listing(registry_id=linked["registry_id"])
    assert linked_list["records_total"] >= 1
    assert all(row["registry_id"] == linked["registry_id"]
               for row in linked_list["items"])

    for chosen in (linked, next(row for row in items if row["ready_to_render"])):
        candidate_id = chosen["candidate_id"]
        detail = payload(api.model_candidates_detail(
            candidate_id, request(f"/v1/models/candidates/{candidate_id}")))
        validate("/v1/models/candidates/{candidate_id}", "get", detail)
        assert_public(detail)
        assert detail["item"]["candidate_id"] == candidate_id
        if chosen["ready_to_render"]:
            geometry = payload(api.model_candidates_geometry(
                candidate_id, request(f"/v1/models/candidates/{candidate_id}/geometry")))
            validate("/v1/models/candidates/{candidate_id}/geometry", "get", geometry)
            assert_public(geometry)
            assert geometry["model_tileset"]["url"].startswith("/assets/public-")
            assert detail["item"]["model_tileset"] == geometry["model_tileset"]
            assert detail["item"]["render_height_m"] == geometry["render_height_m"]
            assert detail["item"]["height_evidence"] == geometry["height_evidence"]
        else:
            assert detail["item"]["model_tileset"] is None

    print(f"PASS offline model routes: 2317 source rows, ready={counts['ready']}, "
          f"review={counts['review']}, rejected={counts['rejected']}, "
          f"unavailable={counts['unavailable']}; 1473 registry links")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged-manifest", type=Path,
                        help="validate an unpublished immutable run in process only")
    args = parser.parse_args()
    if args.staged_manifest is None:
        check()
    else:
        manifest_path = args.staged_manifest.resolve()
        if manifest_path.name != "manifest.json" or manifest_path.parent.parent != model_store.MODELS_ROOT:
            parser.error("staged manifest must be data/models/<run_id>/manifest.json")
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        run_id = manifest["run_id"]
        assert manifest_path.parent.name == run_id
        with tempfile.TemporaryDirectory(prefix="novostroy-model-check-") as temporary:
            pointer = Path(temporary) / "active.json"
            pointer.write_text(json.dumps({"run_id": run_id,
                                           "manifest_sha256": hashlib.sha256(raw).hexdigest()}),
                               encoding="utf-8")
            current_assets = model_store.asset_manifest()
            with patch.object(model_store, "ACTIVE_PATH", pointer), patch.object(
                model_store, "asset_manifest",
                lambda: {**current_assets, "project_models_root": manifest["public_root"]},
            ):
                check()
