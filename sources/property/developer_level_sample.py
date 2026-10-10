"""Offline sample of source-asserted Level SSR units from one audited homepage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from sources.property.developer_harvest import atomic_bytes, canonical_json


NUXT_DATA = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
FIELDS = ("pk", "realty_pk", "project", "project_id", "project_short_slug",
          "building_id", "building_number", "building_name", "section_id",
          "section_title", "floor", "number", "room", "flat_category", "area",
          "price", "old_price", "discount", "show_price", "status_modified",
          "sales_start", "metro_city", "district", "plan", "floor_plan",
          "plan_3d_visualization")
MEDIA_FIELDS = {"plan": "apartment_plan", "floor_plan": "floor_plan",
                "plan_3d_visualization": "visualized_apartment_plan"}


def _ref(values: list, pointer: object) -> object:
    if not isinstance(pointer, int) or pointer < 0 or pointer >= len(values):
        return None
    return values[pointer]


def _dict(values: list, pointer: object) -> dict:
    value = _ref(values, pointer)
    if not isinstance(value, dict):
        raise ValueError("Nuxt object reference missing")
    return value


def _https(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def parse_sample(body: bytes, source_url: str, observed_at: str) -> tuple[list[dict], dict]:
    match = NUXT_DATA.search(body.decode("utf-8", "replace"))
    if not match:
        raise ValueError("source Nuxt payload missing")
    values = json.loads(match.group(1))
    if not isinstance(values, list) or len(values) > 100_000:
        raise ValueError("invalid or oversized Nuxt pool")
    root = _dict(values, 3)
    projects = _dict(values, root.get("projects-moscow"))
    flats = _dict(values, projects.get("flatsResultsRes"))
    items = _ref(values, flats.get("results"))
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("invalid bounded SSR unit list")
    rows: list[dict] = []
    for pointer in items:
        source = _dict(values, pointer)
        fields = {key: _ref(values, source.get(key)) for key in FIELDS if key in source}
        unit_id = fields.get("pk")
        if not isinstance(unit_id, int) or unit_id <= 0:
            raise ValueError("publisher unit ID missing")
        media = []
        for key, role in MEDIA_FIELDS.items():
            url = _https(fields.get(key))
            if url:
                media.append({"url": url, "role": role, "scope": "unit",
                              "rights_status": "unknown", "public_display_allowed": False,
                              "source_field": key})
        rows.append({"provider": "level", "record_type": "unit_sample",
                     "publisher_unit_id": str(unit_id),
                     "publisher_project_id": str(fields["project_id"]) if fields.get("project_id") is not None else None,
                     "publisher_building_id": str(fields["building_id"]) if fields.get("building_id") is not None else None,
                     "publisher_section_id": str(fields["section_id"]) if fields.get("section_id") is not None else None,
                     "project_name": fields.get("project"), "building_label": fields.get("building_name"),
                     "section_label": fields.get("section_title"), "unit_number": fields.get("number"),
                     "floor": fields.get("floor"), "area_m2": fields.get("area"),
                     "rooms_source_enum": fields.get("room"),
                     "price_rub": fields.get("price") if fields.get("show_price") is True else None,
                     "old_price_rub": fields.get("old_price"), "discount_source": fields.get("discount"),
                     "price_basis": "homepage_ssr_displayed_conditions_unverified",
                     "source_updated_at": fields.get("status_modified"),
                     "source_updated_at_basis": "publisher_status_modified_field_not_verified_update_time",
                     "source_city_assertion": fields.get("metro_city"),
                     "source_url": source_url, "source_detail_url": None,
                     "observed_at": observed_at, "media": media,
                     "rights_status": "unknown", "public_display_allowed": False,
                     "source_fields": fields})
    if len({row["publisher_unit_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate publisher unit IDs in SSR sample")
    total = _ref(values, flats.get("count"))
    projects_res = _dict(values, projects.get("resultsRes"))
    next_url = _ref(values, flats.get("next"))
    info = {"catalog_total_claimed": total if isinstance(total, int) else None,
            "project_count_claimed": _ref(values, projects_res.get("count")),
            "source_next_url": next_url if isinstance(next_url, str) else None,
            "source_next_url_fetched": False,
            "sample_rows": len(rows)}
    return rows, info


def build(audit_dir: Path) -> dict:
    ledger = [json.loads(line) for line in (audit_dir / "requests.jsonl").read_text(
        encoding="utf-8").splitlines()]
    matches = [item for item in ledger if item.get("host") == "level.ru" and
               item.get("status") == 200 and item.get("body_sha256")]
    if len(matches) != 1:
        raise ValueError("expected one audited Level homepage")
    event = matches[0]
    raw = (audit_dir / "raw" / f"{event['body_sha256']}.body").read_bytes()
    if hashlib.sha256(raw).hexdigest() != event["body_sha256"]:
        raise ValueError("Level raw SHA mismatch")
    rows, info = parse_sample(raw, event["final_url"], event["at"])
    output = audit_dir / "supplemental-level"
    payload = b"".join(canonical_json(row) + b"\n" for row in rows)
    atomic_bytes(output / "unit_sample.jsonl", payload)
    manifest = {"schema_version": "level-homepage-ssr-sample/1",
                "source_url": event["final_url"], "observed_at": event["at"],
                "source_sha256": event["body_sha256"],
                "unit_sample_sha256": hashlib.sha256(payload).hexdigest(),
                "public_display_allowed": False,
                "all_catalog_units_fetched": False, **info}
    atomic_bytes(output / "manifest.json", canonical_json(manifest) + b"\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.audit_dir), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
