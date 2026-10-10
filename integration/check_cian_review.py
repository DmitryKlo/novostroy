"""Audit the private CIAN review run without CIAN network access.

Run once from the repository root:
``rtk proxy .runtime/dev-env/bin/python integration/check_cian_review.py``.
The two actual inputs are human-observed rendered page facts. HTML/media parser
exercises below are synthetic fixtures, never a live crawl or licensed feed.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources.property.cian import (  # noqa: E402
    collect_authorized_pages,
    parse_authorized_html,
    parse_public_observations,
)
from sources.property.cian_media import parse_cian_media  # noqa: E402


SAMPLE = ROOT / "data/property/cian_public_sample_20261010.json"
REVIEW = ROOT / "data/property/cian/review-v1"
SCHEMA = ROOT / "contracts/cian-review.schema.json"
EXPECTED = {"325479540": (38881323, "developer_primary", 553865),
            "332989085": (28590000, "assignment", 516998)}


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def rights(*, automated: bool = False) -> dict[str, object]:
    return {"source": "cian", "agreement_reference": "synthetic-fixture-only",
            "local_processing_authorized": True, "permitted_storage": True,
            "automated_access_authorized": automated, "rights_status": "cleared",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}


def check_artifacts() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    index = json.loads((REVIEW / "index.json").read_text(encoding="utf-8"))
    assert index["schema_version"] == "cian-review-index/1"
    assert index["runs"] == ["cian-review-f022d4daa722d203"]
    run = REVIEW / "runs" / index["runs"][0]
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    validator.validate(manifest)
    sample_bytes = SAMPLE.read_bytes()
    observations_bytes = (run / "observations.jsonl").read_bytes()
    assert manifest["input_sha256"] == digest(sample_bytes)
    assert manifest["observations_sha256"] == digest(observations_bytes)
    assert manifest["run_id"] == "cian-review-" + digest(observations_bytes)[:16]
    rows = [json.loads(line) for line in observations_bytes.splitlines()]
    assert len(rows) == manifest["records_total"] == 2
    for row in rows:
        validator.validate(row)
    parsed, skipped = parse_public_observations(SAMPLE)
    assert rows == parsed and skipped == {"residential_complex": 1}
    assert manifest["non_unit_pages_skipped"] == skipped
    assert manifest["capture_kinds"] == {"public_observation": 2}
    assert manifest["market_channels"] == {"assignment": 1, "developer_primary": 1}
    assert manifest["price_kinds"] == {"unit_asking": 2}
    assert manifest["rights_statuses"] == {"unknown": 2}
    assert manifest["live_fetches"] == manifest["media_descriptors"] == (
        manifest["media_files_downloaded"]) == 0
    assert manifest["public_offer_imported"] is False
    assert len(index["observations"]) == 2
    by_id = {row["listing_id"]: row for row in rows}
    assert set(by_id) == set(EXPECTED)
    for listing_id, (amount, channel, per_sqm) in EXPECTED.items():
        row = by_id[listing_id]
        assert row["market_kind"] == "newbuild" and row["market_channel"] == channel
        assert row["price_kind"] == "unit_asking"
        assert row["price"] == {"amount_rub": amount, "basis": "asking_total",
                                "observed_at": row["observed_at"]}
        assert row["stated_price_per_sqm_rub"] == per_sqm
        assert row["source_fields"]["displayed_price_basis"]["currency"] == "RUB"
        assert row["source_fields"]["displayed_price_basis"]["unit"] == "listing_total"
        assert row["complex_id"] is row["unit_id"] is row["location"] is None
        assert row["availability"] == "unknown" and row["rights_status"] == "unknown"
        assert row["media"] == [] and row["media_file_count"] == 0
        assert {"no_explicit_complex_id", "no_explicit_unit_id", "rights_not_cleared"} <= (
            set(row["review_reasons"]))
        assert row["source_capture_sha256"] == manifest["input_sha256"]
    fingerprints = {entry["listing_id"]: entry["fingerprint"]
                    for entry in index["observations"]}
    for listing_id, row in by_id.items():
        fact = {key: value for key, value in row.items()
                if key not in {"review_reasons", "source_capture_sha256"}}
        assert fingerprints[listing_id] == digest(canonical(fact))
    with (run / "observations.csv").open(newline="", encoding="utf-8") as stream:
        csv_rows = list(csv.DictReader(stream))
    assert len(csv_rows) == 2
    assert {row["listing_id"]: int(row["asking_total_rub"]) for row in csv_rows} == (
        {key: value[0] for key, value in EXPECTED.items()})
    assert all(int(row["media_descriptors"]) == int(row["media_files_downloaded"]) == 0
               for row in csv_rows)
    assert "Live automated fetches in this import: 0" in (
        run / "report.md").read_text(encoding="utf-8")
    assert stat.S_IMODE(run.stat().st_mode) & 0o077 == 0
    assert all(stat.S_IMODE((run / filename).stat().st_mode) & 0o077 == 0 for filename in (
        "manifest.json", "observations.jsonl", "observations.csv", "report.md"))


def check_negative_price_and_timestamp() -> None:
    source = json.loads(SAMPLE.read_text(encoding="utf-8"))
    source["observations"] = [source["observations"][0]]
    modified = copy.deepcopy(source)
    modified["observations"][0]["displayed_price_basis"]["price_marked_as_from"] = True
    row = parse_public_observations(modified)[0][0]
    assert row["price_kind"] == "project_from" and row["price"] is None
    assert row["source_fields"]["displayed_price_basis"]["displayed_total_rub"] == 38881323
    modified = copy.deepcopy(source)
    modified["observations"][0]["displayed_price_basis"][
        "unit_specific_discount_or_financing_condition_visible"] = True
    row = parse_public_observations(modified)[0][0]
    assert row["price_kind"] == "conditional" and row["price"] is None
    modified = copy.deepcopy(source)
    modified["observations"][0]["displayed_price_basis"].pop("amount_rub")
    modified["observations"][0].pop("price_rub")
    row = parse_public_observations(modified)[0][0]
    assert row["price_kind"] == "unknown" and row["price"] is None
    assert row["stated_price_per_sqm_rub"] == 553865
    modified = copy.deepcopy(source)
    modified["observations"][0]["source_updated_at"] = "2026-10-09T00:00:00Z"
    row = parse_public_observations(modified)[0][0]
    assert row["source_updated_at"] == "2026-10-09T00:00:00Z"
    modified["observations"][0]["source_updated_at"] = "2026-10-11T00:00:00Z"
    try:
        parse_public_observations(modified)
    except ValueError:
        pass
    else:
        raise AssertionError("source update after capture must fail")
    modified = copy.deepcopy(source)
    modified["observed_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    try:
        parse_public_observations(modified)
    except ValueError:
        pass
    else:
        raise AssertionError("future capture must fail")


def check_synthetic_html_and_media() -> None:
    listing = "https://www.cian.ru/sale/flat/325479540/"
    html = f'''<html><head><link rel="canonical" href="{listing}">
    <script type="application/ld+json">{{"@type":"Offer","url":"{listing}",
    "price":12345678,"priceCurrency":"RUB"}}</script>
    <script type="application/ld+json">{{"@type":"Offer",
    "url":"https://www.cian.ru/sale/flat/999999999/",
    "price":99999999,"priceCurrency":"RUB"}}</script></head>
    <body><div>Тип жилья</div><div>Новостройка</div>
    <div>Условия сделки</div><div>долевое участие (214-ФЗ)</div>
    <div>Продавец</div><div>Застройщик</div>
    <div>Общая площадь 51.2 м²</div><div>Этаж 4 из 19</div></body></html>'''
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.html"
        path.write_text(html, encoding="utf-8")
        row = parse_authorized_html(path, source_url=listing,
                                    observed_at="2026-10-10T08:19:01Z", rights=rights())
        assert row["price_kind"] == "unit_asking" and row["price"]["amount_rub"] == 12345678
        assert row["market_channel"] == "developer_primary"
        assert row["media"] == []
        try:
            collect_authorized_pages([listing], rights=rights(automated=False),
                                     output_root=Path(directory) / "blocked")
        except ValueError:
            pass
        else:
            raise AssertionError("unapproved network collection must fail before requests")
        assert not (Path(directory) / "blocked").exists()
        for invalid_delay in (float("inf"), 1000000.0):
            try:
                collect_authorized_pages([listing], rights=rights(automated=True),
                                         output_root=Path(directory) / "blocked-delay",
                                         delay_seconds=invalid_delay)
            except ValueError:
                pass
            else:
                raise AssertionError("unbounded collector delay must fail before requests")
        assert not (Path(directory) / "blocked-delay").exists()

    media = parse_cian_media({"page_kind": "unit_listing", "listing_id": "325479540",
                              "media_items": [
                                  {"url": "https://images.example.com/one.jpg",
                                   "source_field": "Gallery/0", "caption": "ЖК Шагал"},
                                  {"url": "https://images.example.com/two.jpg",
                                   "source_field": "Photos/1", "caption": "Рендер фасада"}]},
                             source_url=listing, observed_at="2026-10-10T08:19:01Z",
                             rights_status="unknown")
    assert [(item["role"], item["visual_kind"]) for item in media] == [
        ("unknown", "unknown"), ("facade_image", "render")]
    assert all(item["fetch_status"] == "not_requested" and
               item["public_display_allowed"] is False for item in media)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["$defs"]["MediaCandidate"],
                                     format_checker=FormatChecker())
    for item in media:
        validator.validate(item)


def check_public_boundary() -> None:
    from backend import app as api

    paths = {getattr(route, "path", "") for route in api.app.routes}
    assert not any("cian" in path.casefold() for path in paths)
    assets = json.loads((ROOT / "assets/manifest.json").read_text(encoding="utf-8"))
    assert "cian" not in json.dumps(assets, ensure_ascii=False).casefold()
    public_schema = json.loads((ROOT / "contracts/schemas.json").read_text(encoding="utf-8"))
    real_source = public_schema["$defs"]["SearchResult"]["allOf"][0]["then"]["properties"][
        "items"]["items"]["properties"]["source"]["properties"]
    assert real_source["rights_status"] == {"const": "cleared"}


def check() -> None:
    check_artifacts()
    check_negative_price_and_timestamp()
    check_synthetic_html_and_media()
    check_public_boundary()
    print("PASS private CIAN review: 2 dated observations, 1 project page skipped, "
          "0 live fetches/public offers/media files; synthetic parser gates pass")


if __name__ == "__main__":
    check()
