"""Prepare a bounded, source-bound private raster asset menu without fetching URLs.

Inputs are already captured public-page media with hashes and exact publisher
identities. SVGs are reduced to a tiny inert vector subset before conversion;
the browser never receives source SVG, raw URLs, or arbitrary file paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

from backend import developer_map as map_reader
from backend import developer_units as menu_reader
from sources.property.developer_map import (
    ROOT, _canonical, _file_sha, _fsync_dir, _rows, _sha, _write_private,
)


BASE = ROOT / "data/property/developer_units"
CAPTURE_ROOT = ROOT / ".runtime/research"
MAX_SOURCE_BYTES = 10_000_000
MAX_ASSET_BYTES = 10_000_000
MAX_DIMENSION = 4096
SVG_TAGS = {"svg", "g", "path", "rect", "polygon", "polyline", "circle",
            "ellipse", "line", "text", "tspan", "defs", "clipPath",
            "linearGradient", "radialGradient", "stop"}
SVG_ATTRIBUTES = {"x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r",
                  "rx", "ry", "width", "height", "viewBox", "version",
                  "transform", "points", "d", "style", "id", "class",
                  "fill", "stroke", "stroke-width", "opacity", "space",
                  "offset", "stop-color", "stop-opacity", "font-size",
                  "font-family", "text-anchor"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _private_source(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_relative_to(CAPTURE_ROOT):
        raise ValueError("Capture must remain in the private research tree")
    current = ROOT
    for part in path.relative_to(ROOT).parts:
        if part in {"", ".", ".."} or part.startswith(".") and part != ".runtime":
            raise ValueError("Unsafe capture path")
        current /= part
        if current.is_symlink():
            raise ValueError("Capture path has a symlink ancestor")
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Missing or oversized captured asset")
    return path


def _source_blob(item: dict[str, Any]) -> bytes:
    expected = item.get("sha256")
    if not isinstance(expected, str) or HEX64.fullmatch(expected) is None:
        raise ValueError("Missing exact capture SHA")
    blob = _private_source(item["local_path"]).read_bytes()
    if _sha(blob) != expected:
        raise ValueError("Captured media SHA mismatch")
    return blob


def _png_dimensions(blob: bytes) -> tuple[int, int]:
    if (len(blob) < 33 or blob[:8] != b"\x89PNG\r\n\x1a\n" or
            blob[12:16] != b"IHDR"):
        raise ValueError("Prepared asset is not PNG")
    width, height = struct.unpack_from(">II", blob, 16)
    if not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
        raise ValueError("Prepared PNG dimensions exceed bound")
    if len(blob) > MAX_ASSET_BYTES:
        raise ValueError("Prepared PNG exceeds 10 MB")
    return width, height


def _inert_svg(blob: bytes) -> bytes:
    if len(blob) > MAX_SOURCE_BYTES or b"<!DOCTYPE" in blob.upper() or b"<!ENTITY" in blob.upper():
        raise ValueError("SVG contains an external-document construct")
    root = ET.fromstring(blob)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("Captured vector is not SVG")
    dimensions = []
    for key in ("width", "height"):
        raw = root.attrib.get(key, "")
        if re.fullmatch(r"\d{1,5}(?:\.\d{1,4})?(?:px)?", raw) is None:
            raise ValueError("SVG has no bounded numeric dimensions")
        dimensions.append(float(raw.removesuffix("px")))
    width, height = dimensions
    if (not 1 <= width <= 10_000 or not 1 <= height <= 10_000 or
            not 0.1 <= height / width <= 2.5):
        raise ValueError("SVG aspect ratio exceeds bounded raster output")
    count = 0
    for node in root.iter():
        count += 1
        if count > 10_000:
            raise ValueError("SVG element count exceeds bound")
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in SVG_TAGS:
            raise ValueError("SVG contains an unsupported active element")
        for name, value in node.attrib.items():
            plain = name.rsplit("}", 1)[-1]
            if (plain not in SVG_ATTRIBUTES or len(value) > 100_000 or
                    any(term in value.casefold() for term in
                        ("url(", "javascript:", "http:", "https:", "data:", "@import", "expression("))):
                raise ValueError("SVG contains an external or active attribute")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _prepare_image(item: dict[str, Any]) -> bytes:
    blob = _source_blob(item)
    suffix = _private_source(item["local_path"]).suffix.lower()
    if suffix == ".png":
        _png_dimensions(blob)
        return blob
    if suffix != ".svg":
        raise ValueError("Only captured PNG or inert SVG plans are supported")
    sanitized = _inert_svg(blob)
    result = subprocess.run(["rsvg-convert", "--format", "png", "--width", "1600"],
                            input=sanitized, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False)
    if result.returncode != 0:
        raise ValueError("Safe SVG rasterization failed")
    _png_dimensions(result.stdout)
    return result.stdout


def _regions(hotspots_path: Path, item: dict[str, Any], floor_id: str,
             asset_sha: str) -> list[dict[str, Any]]:
    evidence = json.loads(_private_source(str(hotspots_path)).read_text(encoding="utf-8"))
    if (evidence.get("source_image_sha256") != item["sha256"] or
            evidence.get("source_image_url") != item["source_url"] or
            evidence.get("registry_id") != item["registry_id"] or
            str(evidence.get("section")) != str(item["section_label"]) or
            evidence.get("floor") != item["floor_number"] or
            evidence.get("geometry_basis") != "raster_traced_floor_plan" or
            evidence.get("reconstruction") is not True):
        raise ValueError("Floor regions are not tied to this exact source image/floor")
    result = []
    for source in evidence.get("regions", []):
        polygon = source.get("polygon_normalized_xy")
        if not isinstance(polygon, list):
            raise ValueError("Invalid traced floor region")
        region = {"region_id": "region_" + _sha(_canonical({
                      "floor_id": floor_id, "asset_sha256": asset_sha,
                      "source_region_id": source.get("region_id"), "polygon": polygon}))[:24],
                  "polygon": polygon, "source_label": source.get("source_label"),
                  "source_rooms": (str(source["source_rooms"])
                                   if source.get("source_rooms") is not None else None),
                  "source_area_m2": source.get("source_area_m2"),
                  "unit_id": None, "match_status": "unconfirmed",
                  "basis": "raster_traced_floor_plan", "reconstruction": True}
        result.append(region)
    return result


def _materialized(base_run_id: str, captures_path: Path, hotspots_path: Path | None,
                  map_run_id: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bytes],
                                            dict[str, Any]]:
    map_run = map_reader.load_staged(map_run_id)
    base = menu_reader.load_staged(base_run_id, map_run)
    run_dir = BASE / "runs" / base_run_id
    files = {name: list(_rows(run_dir / f"{name}.jsonl")) for name in
             ("buildings", "units", "floors", "plans", "unit_aliases")}
    by_unit_publisher = {(unit["source_group"], unit["publisher_unit_id"]): unit
                         for unit in files["units"]}
    floor_ids = {floor["floor_id"] for floor in files["floors"]}
    plans = {plan["plan_id"]: plan for plan in files["plans"]}
    assets: dict[str, bytes] = {}
    captures_file = _private_source(str(captures_path))
    captures = list(_rows(captures_file))
    if not captures or len(captures) > 20:
        raise ValueError("Media import requires 1..20 source-bound captures")
    for item in captures:
        item = dict(item)
        local_name = item.get("local_path")
        if not isinstance(local_name, str) or Path(local_name).name != local_name:
            raise ValueError("Media capture path must be a same-directory basename")
        item["local_path"] = str(captures_file.parent / local_name)
        if (item.get("source_building_id") != "639b34e2b176d8bf3ad3d6b1" or
                item.get("registry_id") not in
                map_run["by_registry"] or not isinstance(item.get("source_url"), str) or
                not item["source_url"].startswith("https://cdn.fsk.ru/")):
            raise ValueError("Media capture is not allowlisted to the source building")
        role = item.get("role")
        source_unit_id = (item.get("official_page_unit_id") if role == "floor_plan"
                          else item.get("publisher_unit_id"))
        unit = by_unit_publisher.get(("fsk", str(source_unit_id)))
        if unit is None or unit["registry_id"] != item["registry_id"]:
            raise ValueError("Media capture has no exact admitted publisher unit")
        if unit["publisher_building_id"] != item["source_building_id"]:
            raise ValueError("Media capture crosses source corpus")
        if role == "apartment_plan":
            matches = [plan for plan in plans.values()
                       if plan["unit_id"] == unit["unit_id"] and
                       plan["role"] == role and plan["source_url"] == item["source_url"]]
            if len(matches) != 1:
                raise ValueError("Captured unit plan URL lacks an exact menu source ref")
            plan = matches[0]
        elif role == "floor_plan":
            facts = item.get("source_page_facts") or {}
            section, floor = facts.get("section"), facts.get("floor")
            if (item.get("publisher_unit_id") is not None or
                    item.get("floor_section_label") != "corpus1_section2_floor5" or
                    str(section) != str(unit["section_label"]) or
                    floor != unit["floor_number"] or
                    item.get("source_page_url") !=
                    f"https://fsk.ru/the-lake/flats/{unit['publisher_unit_id']}"):
                raise ValueError("Floor image lacks an exact publisher unit page/floor binding")
            item["section_label"] = str(section)
            item["floor_number"] = floor
            floor_id = menu_reader._floor_id(map_run_id, unit["registry_id"],
                                              unit["section_label"], floor)
            if floor_id not in floor_ids:
                raise ValueError("Floor image lacks an exact menu floor")
            plan_id = "plan_" + _sha(_canonical({"floor_id": floor_id,
                                                  "url": item["source_url"]}))[:24]
            plan = {"plan_id": plan_id, "registry_id": unit["registry_id"],
                    "unit_id": None, "floor_id": floor_id, "role": "floor_plan",
                    "source_role": "floor_plan", "source_scope": "floor",
                    "source_field": "official_unit_page_on_floor_image",
                    "source_url": item["source_url"],
                    "rights_status": item.get("rights_status", "unknown"),
                    "source_evidence_sha256": _sha(_canonical(item)),
                    "asset_sha256": None, "asset_mime": None}
            if plan_id in plans:
                raise ValueError("Duplicate exact floor-plan identity")
            plans[plan_id] = plan
        else:
            raise ValueError("Captured media role is not an exact plan role")
        raster = _prepare_image(item)
        asset_sha = _sha(raster)
        assets[asset_sha] = raster
        plan["asset_sha256"] = asset_sha
        plan["asset_mime"] = "image/png"
        plan["capture_sha256"] = item["sha256"]
        plan["capture_observed_at"] = item.get("observed_at")
        plan["capture_rights_status"] = item.get("rights_status", "unknown")
        if role == "floor_plan" and hotspots_path is not None:
            plan["regions"] = _regions(hotspots_path, item, plan["floor_id"], asset_sha)
            plan["regions_asset_sha256"] = asset_sha
            plan["regions_evidence_sha256"] = _file_sha(_private_source(str(hotspots_path)))
    files["plans"] = sorted(plans.values(), key=lambda plan: plan["plan_id"])
    inputs = {"base_menu_run_id": base_run_id,
              "base_menu_manifest_sha256": _file_sha(run_dir / "manifest.json"),
              "captures_sha256": _file_sha(_private_source(str(captures_path))),
              "hotspots_sha256": (_file_sha(_private_source(str(hotspots_path)))
                                  if hotspots_path is not None else None)}
    return files, assets, inputs


def materialize(base_run_id: str, captures_path: Path, map_run_id: str,
                hotspots_path: Path | None = None, output_root: Path = BASE) -> dict[str, Any]:
    files, assets, inputs = _materialized(base_run_id, captures_path, hotspots_path, map_run_id)
    payloads = {name: b"".join(_canonical(row) + b"\n" for row in rows)
                for name, rows in files.items()}
    hashes = {name: _sha(payload) for name, payload in payloads.items()}
    base_manifest = json.loads((BASE / "runs" / base_run_id / "manifest.json").read_text())
    fingerprint = _sha(_canonical({"inputs": inputs, "outputs": hashes,
                                   "asset_sha256": sorted(assets)}))
    run_id = "developer-units-v1-" + fingerprint[:16]
    manifest = {**base_manifest, "run_id": run_id,
                "rules_version": "developer-unit-menu-v1+prepared-media-v1",
                **inputs,
                **{name + "_sha256": digest for name, digest in hashes.items()},
                **{name + "_total": len(rows) for name, rows in files.items()},
                "plan_assets_prepared": len(assets),
                "plan_asset_bytes": sum(map(len, assets.values())),
                "media_publication_basis": "user_authorized_source_bound_local_plans",
                "source_rights_status": "unknown"}
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs = output_root / "runs"
    runs.mkdir(exist_ok=True, mode=0o700)
    target = runs / run_id
    manifest_bytes = _canonical(manifest) + b"\n"
    if target.exists():
        if (target.is_symlink() or (target / "manifest.json").read_bytes() != manifest_bytes or
                any((target / f"{name}.jsonl").read_bytes() != payload
                    for name, payload in payloads.items()) or
                any(_file_sha(target / "assets" / f"{sha}.png") != sha for sha in assets)):
            raise ValueError("Immutable prepared menu ID collision")
        return manifest
    stage = Path(tempfile.mkdtemp(prefix=".stage-developer-unit-media-", dir=runs))
    os.chmod(stage, 0o700)
    try:
        asset_dir = stage / "assets"
        asset_dir.mkdir(mode=0o700)
        for sha, blob in assets.items():
            _write_private(asset_dir / f"{sha}.png", blob)
        for name, payload in payloads.items():
            _write_private(stage / f"{name}.jsonl", payload)
        _write_private(stage / "manifest.json", manifest_bytes)
        _fsync_dir(asset_dir)
        _fsync_dir(stage)
        os.replace(stage, target)
        _fsync_dir(runs)
    finally:
        if stage.exists():
            for path in (stage / "assets").glob("*"):
                path.unlink()
            (stage / "assets").rmdir()
            for path in stage.iterdir():
                path.unlink()
            stage.rmdir()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=BASE)
    sub = parser.add_subparsers(dest="command", required=True)
    item = sub.add_parser("materialize")
    item.add_argument("--base-menu-run-id", required=True)
    item.add_argument("--map-run-id", required=True)
    item.add_argument("--captures", required=True, type=Path)
    item.add_argument("--hotspots", type=Path)
    args = parser.parse_args()
    print(json.dumps(materialize(args.base_menu_run_id, args.captures, args.map_run_id,
                                 args.hotspots, args.output_root), sort_keys=True))


if __name__ == "__main__":
    main()
