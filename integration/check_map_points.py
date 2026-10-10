"""Check the combined point map in process, without TCP or browser claims.

Run from the repository root with
``rtk proxy .runtime/dev-env/bin/python integration/check_map_points.py``.
The two point groups remain distinct source records, not unique ЖК identities.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NOVOSTROY_EXPOSE_SOURCES"] = "0"

from backend import app as api  # noqa: E402
from backend.registry_walk import walk_minutes_for_registry  # noqa: E402
from integration.check_live import assert_public, validate  # noqa: E402


def request(path: str, *, if_none_match: str | None = None) -> Request:
    headers = [] if if_none_match is None else [
        (b"if-none-match", if_none_match.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": b"", "headers": headers})


def points(**filters: str | int | None) -> dict[str, Any]:
    args = {"query": None, "status": None, "kind": None,
            "district": None,
            "handover_year": None, "handover_quarter": None,
            "floors_min": None, "floors_max": None,
            "walk_metro_max_min": None, "walk_school_max_min": None,
            "walk_kindergarten_max_min": None, "walk_medical_max_min": None}
    args.update(filters)
    response = api.map_points(request("/v1/map/points"), **args)
    assert isinstance(response, Response) and response.status_code == 200
    assert response.headers.get("cache-control") == "private, max-age=0, must-revalidate"
    assert response.headers.get("etag", "").startswith('W/"')
    result = json.loads(response.body)
    validate("/v1/map/points", "get", result)
    assert_public(result)
    assert result["applied_filters"] == {key: value for key, value in args.items()
                                         if value is not None}
    items = result["items"]
    counts = result["counts"]
    assert counts["points"] == len(items)
    assert counts["registry_points"] == sum(item["point_type"] == "registry"
                                            for item in items)
    assert counts["mapped_construction_points"] == sum(
        item["point_type"] == "mapped_construction" for item in items)
    assert counts["coordinate_positions"] == len({tuple(item["location"])
                                                    for item in items})
    assert len({item["point_id"] for item in items}) == len(items)
    assert all("url" not in item and "geometry" not in item and "height" not in item
               for item in items)
    evidence = result["group_evidence"]
    assert evidence["schema_version"] == 1 and evidence["visibility"] == "public"
    assert evidence["registry_snapshot_id"] == result["registry_snapshot_id"]
    assert evidence["model_run_id"] == result["model_run_id"]
    point_ids = {item["point_id"] for item in items}
    grouped_ids: set[str] = set()
    group_ids: set[str] = set()
    for group in evidence["groups"]:
        assert re.fullmatch(r"group_[a-f0-9]{24}", group["group_id"])
        assert group["group_id"] not in group_ids
        group_ids.add(group["group_id"])
        members = set(group["point_ids"])
        assert len(members) == len(group["point_ids"]) >= 2
        assert members <= point_ids, "filtered map must not include a partial group"
        assert grouped_ids.isdisjoint(members), "a point cannot belong to two published groups"
        grouped_ids.update(members)
    return result


def check_registry_filter_parity(**filters: str | int) -> None:
    """The list, map, and point view must apply the same registry predicate."""
    defaults: dict[str, str | int | None] = {
        "query": None, "status": None, "kind": None,
        "district": None,
        "handover_year": None, "handover_quarter": None,
        "floors_min": None, "floors_max": None,
        "walk_metro_max_min": None, "walk_school_max_min": None,
        "walk_kindergarten_max_min": None, "walk_medical_max_min": None,
    }
    args = {**defaults, **filters}
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = api.registry_buildings(**args, limit=100, offset=offset)
        validate("/v1/registry/buildings", "get", page)
        assert_public(page)
        rows.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["records_total"]:
            break
        assert page["items"], "list pagination stopped before records_total"
    assert len(rows) == page["records_total"]
    mapped = api.registry_map(**args)
    validate("/v1/registry/map", "get", mapped)
    assert_public(mapped)
    selected = points(**filters)
    expected_filters = {key: value for key, value in filters.items() if value is not None}
    assert mapped["applied_filters"] == selected["applied_filters"] == expected_filters
    located_ids = {row["registry_id"] for row in rows if row["location"] is not None}
    assert mapped["counts"]["records"] == len(rows)
    assert mapped["counts"]["geocoded"] == len(located_ids)
    assert {item["registry_id"] for item in mapped["items"]} == located_ids
    assert {item["registry_id"] for item in selected["items"]} == located_ids
    assert selected["counts"]["registry_points"] == len(located_ids)
    if any(args[key] is not None for key in (
            "status", "kind", "district", "handover_year", "handover_quarter",
            "floors_min", "floors_max", "walk_metro_max_min", "walk_school_max_min",
            "walk_kindergarten_max_min", "walk_medical_max_min")):
        assert selected["counts"]["mapped_construction_points"] == 0
        assert selected["counts"]["excluded_mapped_by_registry_filters"] == 756
    for row in rows:
        if args["district"] is not None:
            assert api.active_registry()["district_names"].get(row["registry_id"]) == args["district"]
        if args["handover_year"] is not None:
            assert row["handover_year"] == args["handover_year"]
        if args["handover_quarter"] is not None:
            assert row["handover_quarter"] == args["handover_quarter"]
        if args["floors_min"] is not None or args["floors_max"] is not None:
            assert type(row["floors"]) is int
            assert args["floors_min"] is None or row["floors"] >= args["floors_min"]
            assert args["floors_max"] is None or row["floors"] <= args["floors_max"]
        if any(args[key] is not None for key in (
                "walk_metro_max_min", "walk_school_max_min",
                "walk_kindergarten_max_min", "walk_medical_max_min")):
            assert row["location"] is not None and row["location_quality"] == "verified"


def check() -> None:
    full = points()
    assert full["dataset_id"] == "map_points"
    assert full["dataset_total"] == full["counts"]["points"] == len(full["items"])
    assert full["counts"]["registry_points"] == full["counts"]["registry_points_total"]
    assert full["counts"]["mapped_construction_points"] == 756
    assert full["counts"]["excluded_mapped_by_registry_filters"] == 0
    assert full["registry_rights_status"] == "user_supplied_internal_use"
    assert "OpenStreetMap" in full["mapped_attribution"]
    first_response = api.map_points(request("/v1/map/points"), query=None,
                                    status=None, kind=None, district=None,
                                    handover_year=None, handover_quarter=None,
                                    floors_min=None, floors_max=None,
                                    walk_metro_max_min=None, walk_school_max_min=None,
                                    walk_kindergarten_max_min=None, walk_medical_max_min=None)
    cached = api.map_points(request("/v1/map/points", if_none_match=(
        first_response.headers["etag"])), query=None, status=None, kind=None,
        district=None, handover_year=None, handover_quarter=None,
        floors_min=None, floors_max=None, walk_metro_max_min=None,
        walk_school_max_min=None, walk_kindergarten_max_min=None,
        walk_medical_max_min=None)
    assert cached.status_code == 304

    registry = api.active_registry()
    models = api.active_model_run()
    source_rows = registry["items"]
    visible_rows = [row for row in source_rows if row["registry_id"] in registry["visible_ids"]]
    assert len(source_rows) == registry["visibility"]["source_total"] == 1473
    assert len(visible_rows) == registry["visibility"]["visible_total"]
    assert len(source_rows) - len(visible_rows) == registry["visibility"]["archived_total"]
    assert full["registry_source_total"] == len(source_rows)
    assert full["registry_archived_total"] == len(source_rows) - len(visible_rows)
    assert models["model_run_id"] == full["model_run_id"]
    assert registry["snapshot_id"] == full["registry_snapshot_id"]
    registry_by_id = {item["registry_id"]: item for item in source_rows}
    model_by_id = models["by_id"]
    registry_points = [item for item in full["items"] if item["point_type"] == "registry"]
    mapped_points = [item for item in full["items"]
                     if item["point_type"] == "mapped_construction"]
    assert sum(item["location"] is not None for item in source_rows) == 10
    assert len(registry_points) == len({item["registry_id"] for item in registry_points}) == sum(
        item["location"] is not None for item in visible_rows)
    assert all(item["registry_id"] in registry["visible_ids"] for item in registry_points)
    assert all(item["candidate_id"] is None and item["registry_id"] in registry_by_id
               and item["location"] == registry_by_id[item["registry_id"]]["location"]
               and item["latest_known_date"] == registry_by_id[item["registry_id"]]["latest_known_date"]
               and item["known_catalog_registry_id"] == item["registry_id"]
               and item["address"] == registry_by_id[item["registry_id"]]["address"]
               and (item["complex_label"] is None or
                    item["complex_label"] == registry_by_id[item["registry_id"]]["name"])
               for item in registry_points)
    assert len(mapped_points) == len({item["candidate_id"] for item in mapped_points}) == 756
    assert all(item["registry_id"] is None and item["candidate_id"] in model_by_id
               and item["known_catalog_registry_id"] is None
               and model_by_id[item["candidate_id"]]["source_group"] == "mapped_construction"
               and model_by_id[item["candidate_id"]]["ready_to_render"]
               for item in mapped_points)
    assert Counter(item["address_basis"] for item in mapped_points) == {
        "osm_street_house": 153, "osm_place_house": 6, "osm_street": 12,
        "osm_place": 32, None: 553}
    assert Counter(item["complex_label_basis"] for item in mapped_points) == {
        "osm_addr_place": 34, "osm_name": 6, "osm_parent_relation": 1,
        None: 715}
    assert full["dataset_total"] == len(registry_points) + len(mapped_points)

    # All markers are supplied by the map route, independent of list pagination.
    first_page = api.registry_buildings(query=None, status=None, kind=None, district=None,
                                        limit=48, offset=0)
    first_page_ids = {item["registry_id"] for item in first_page["items"]}
    assert len(first_page_ids) == 48
    assert first_page["dataset_total"] == len(visible_rows)
    assert first_page["source_total"] == len(source_rows)
    assert first_page["archived_total"] == len(source_rows) - len(visible_rows)
    assert all(item["registry_id"] in registry["visible_ids"] for item in first_page["items"])
    assert first_page["records_total"] == len(visible_rows)
    selected_mapped = mapped_points[0]
    archived_id = next(row["registry_id"] for row in source_rows
                       if row["registry_id"] not in registry["visible_ids"])
    try:
        api.registry_building_detail(archived_id)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("archived detail must not remain in ordinary catalogue")
    if registry_points:
        selected_registry = registry_points[0]
        assert api.registry_building_detail(selected_registry["registry_id"])["registry_id"] == (
            selected_registry["registry_id"])
    selected_detail = api.model_candidates_detail(
        selected_mapped["candidate_id"],
        request(f"/v1/models/candidates/{selected_mapped['candidate_id']}"))
    assert selected_detail.status_code == 200
    assert json.loads(selected_detail.body)["item"]["candidate_id"] == (
        selected_mapped["candidate_id"])

    # Coincident source records remain distinct IDs; visibility can remove either one.
    by_location: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for item in full["items"]:
        by_location.setdefault(tuple(item["location"]), []).append(item)
    assert full["counts"]["coordinate_positions"] == len(by_location)
    assert all(len({item["point_id"] for item in records}) == len(records)
               for records in by_location.values())

    for chosen in (*registry_points[:1], selected_mapped):
        filtered = points(query=chosen["name"])
        assert chosen["point_id"] in {item["point_id"] for item in filtered["items"]}
    kind = visible_rows[0]["record_kind"]
    by_kind = points(kind=kind)
    assert by_kind["counts"]["mapped_construction_points"] == 0
    assert by_kind["counts"]["excluded_mapped_by_registry_filters"] == 756
    status = next((record["status"] for record in visible_rows
                   if record["status"] is not None), None)
    assert status is not None
    by_status = points(status=status)
    assert by_status["counts"]["mapped_construction_points"] == 0
    assert by_status["counts"]["excluded_mapped_by_registry_filters"] == 756
    named_and_typed = points(query=selected_mapped["name"], kind=kind)
    assert named_and_typed["counts"]["mapped_construction_points"] == 0
    assert named_and_typed["counts"]["excluded_mapped_by_registry_filters"] >= 1
    try:
        points(kind="unverified_kind")
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("unknown kind must not be silently ignored")

    # User-facing registry filters are source facts, never inferred completion,
    # apartment characteristics, or OSM construction attributes.
    unfiltered_list = api.registry_buildings(query=None, status=None, kind=None, district=None,
                                             handover_year=None, handover_quarter=None,
                                             floors_min=None, floors_max=None,
                                             walk_metro_max_min=None, walk_school_max_min=None,
                                             walk_kindergarten_max_min=None, walk_medical_max_min=None,
                                             limit=100, offset=0)
    validate("/v1/registry/buildings", "get", unfiltered_list)
    facets = unfiltered_list["facets"]
    visible_districts = [registry["district_names"][row["registry_id"]] for row in visible_rows
                         if row["registry_id"] in registry["district_names"]]
    assert facets["district_coverage_count"] == len(visible_districts)
    assert facets["walking_origin_count"] == len(registry_points)
    assert facets["district_source_release"] == registry["district_basis"]["release"]
    assert {facet["value"]: facet["count"] for facet in facets["districts"]} == dict(
        Counter(visible_districts))
    assert facets["numeric_floors_count"] == sum(
        type(item["floors"]) is int for item in visible_rows)
    assert {int(facet["value"]): facet["count"] for facet in facets["handover_years"]} == dict(
        Counter(item["handover_year"] for item in visible_rows
                if item["handover_year"] is not None))
    year = next(item["handover_year"] for item in visible_rows
                if item["handover_year"] is not None)
    quarter = next(item["handover_quarter"] for item in visible_rows
                   if item["handover_quarter"] is not None)
    floor = next(item["floors"] for item in visible_rows
                 if type(item["floors"]) is int and 1 <= item["floors"] <= 300)
    check_registry_filter_parity(handover_year=year)
    check_registry_filter_parity(handover_quarter=quarter)
    check_registry_filter_parity(floors_min=floor, floors_max=floor)
    check_registry_filter_parity(handover_year=year, floors_min=1, floors_max=300)
    district = next(iter(registry["district_names"].values()))
    check_registry_filter_parity(district=district)
    for walk_key in ("walk_metro_max_min", "walk_school_max_min",
                     "walk_kindergarten_max_min", "walk_medical_max_min"):
        check_registry_filter_parity(**{walk_key: 180})
    assert set(walk_minutes_for_registry(registry)) == {
        item["registry_id"] for item in registry_points}
    for invalid in ({"handover_year": 1899}, {"handover_year": 2101},
                    {"handover_quarter": 5}, {"floors_min": 0},
                    {"floors_max": 301}, {"floors_min": 20, "floors_max": 19},
                    {"district": "x" * 161},
                    {"walk_metro_max_min": 0}, {"walk_school_max_min": 181},
                    {"walk_kindergarten_max_min": 0}, {"walk_medical_max_min": 181}):
        invalid_args = {"query": None, "status": None, "kind": None,
                        "district": None, "handover_year": None, "handover_quarter": None,
                        "floors_min": None, "floors_max": None,
                        "walk_metro_max_min": None, "walk_school_max_min": None,
                        "walk_kindergarten_max_min": None, "walk_medical_max_min": None,
                        **invalid}
        for call in (
            lambda: api.registry_buildings(**invalid_args, limit=48, offset=0),
            lambda: api.registry_map(**invalid_args),
            lambda: points(**invalid),
        ):
            try:
                call()
            except HTTPException as exc:
                assert exc.status_code == 422
            else:
                raise AssertionError(f"invalid filter must be rejected: {invalid}")

    print(f"PASS offline point map: source={len(source_rows)}, visible={len(visible_rows)}, "
          f"archived={len(source_rows) - len(visible_rows)}, "
          f"registry points={len(registry_points)}, mapped points={len(mapped_points)}, "
          "registry filter and privacy parity; no TCP/browser")


if __name__ == "__main__":
    check()
