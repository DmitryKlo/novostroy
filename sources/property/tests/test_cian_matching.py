"""Bounded, offline CIAN-to-map review cases; no network or public import."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from sources.property import cian_matching as matching


def _observation() -> dict:
    return {
        "observation_id": "cian-example-1", "source_url": "https://zhk-example-i.cian.ru/",
        "observed_at": "2026-10-10T10:00:00Z", "source_updated_at": None,
        "access_method": "search_index", "page_scope": "project",
        "project_name": "ЖК Пример", "source_claimed_use": "residential_project",
        "address_components": {"city": "Москва", "street": "Багратионовский проезд",
                               "house": "5А", "corpus": "2", "structure": None},
        "source_asserted_ids": [], "evidence_urls": [],
        "local_candidate": {"map_point_id": "mappt_" + "a" * 24,
                            "source_id": "osm:way:123", "source_version": 1,
                            "research_lead_only": True},
        "match_assessment": "exact_project_address_candidate_review_only",
    }


def _local() -> dict:
    return {"map_point_id": "mappt_" + "a" * 24,
            "source_record_id": "way/123/v1", "source_name": "ЖК Пример",
            "complex_label": "ЖК Пример", "address_tags": {
                "addr:street": "Багратионовский проезд", "addr:housenumber": "5А к2"}}


def test_exact_address_is_review_lead_only_and_corpus_is_symmetric() -> None:
    observation, local = _observation(), _local()
    assert matching._compare(observation, local, address_peer_count=1) == (
        "review", "exact_address_project_scope_requires_building_proof", "address_agreement_only")
    observation["address_components"]["corpus"] = "3"
    assert matching._compare(observation, local, address_peer_count=1)[1] == "corpus_conflict"
    observation["address_components"]["corpus"] = None
    assert matching._compare(observation, local, address_peer_count=1)[1] == "cian_corpus_unstated"
    observation["address_components"]["corpus"] = "2"
    local["address_tags"]["addr:housenumber"] = "5А"
    assert matching._compare(observation, local, address_peer_count=1)[1] == "local_corpus_unstated"
    local["address_tags"]["addr:housenumber"] = "5А к2"
    assert matching._compare(observation, local, address_peer_count=2)[1] == (
        "address_matches_multiple_local_objects")


def test_same_street_house_different_municipality_or_alias_is_not_a_match() -> None:
    observation, local = _observation(), _local()
    observation["address_components"]["municipality"] = "Даниловский"
    local["municipality"] = "Троицк"
    assert matching._compare(observation, local, address_peer_count=1)[1] == "municipality_conflict"
    local.pop("municipality")
    # Unknown locality does not become a confirmed link: even exact address is review-only.
    assert matching._compare(observation, local, address_peer_count=1)[0] == "review"
    observation["address_components"].update(house="16", corpus=None, structure="1")
    observation["address_components"]["street"] = "Кастанаевская ул."
    local["address_tags"] = {"addr:street": "Кастанаевская улица",
                             "addr:housenumber": "д16/1"}
    assert matching._compare(observation, local, address_peer_count=1)[1] == (
        "slash_address_semantics_unverified")


def test_project_name_alone_does_not_crosswalk_and_conflicts_reject_only_pair() -> None:
    observation, local = _observation(), _local()
    observation["address_components"].update(street=None, house=None, corpus=None)
    assert matching._compare(observation, local, address_peer_count=0)[0] == "manual"
    observation["address_components"].update(street="Городская улица", house="10")
    assert matching._compare(observation, local, address_peer_count=0)[1] == (
        "street_conflict_despite_shared_name")
    observation["match_assessment"] = "reject_candidate_pair_geographic_mismatch"
    assert matching._compare(observation, local, address_peer_count=0)[1] == (
        "reviewed_pair_geographic_mismatch")
    assert local["source_name"] == "ЖК Пример"  # No source object status is rewritten.


@pytest.mark.parametrize("url", [
    "http://www.cian.ru/", "https://www.cian.ru.evil.example/",
    "https://user@www.cian.ru/", "https://www.cian.ru:8443/",
    "https://www.cian.ru/path?token=private", "https://www.cian.ru/#fragment",
])
def test_unsafe_cian_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        matching._cian_url(url)


def test_review_input_requires_source_scope_and_bounded_unique_ids() -> None:
    observation = _observation()
    doc = {"schema": "cian-project-observations/1", "records": [observation]}
    assert matching._validate_observations(doc) == [observation]
    with pytest.raises(ValueError, match="Duplicate"):
        matching._validate_observations({**doc, "records": [observation, observation]})
    observation["local_candidate"]["research_lead_only"] = False
    with pytest.raises(ValueError, match="non-authoritative"):
        matching._validate_observations(doc)


def test_csv_formula_and_html_are_escaped() -> None:
    row = {"observation_id": "cian-one", "decision": "manual",
           "cian_project_name": '=HYPERLINK("bad")<script>alert(1)</script>',
           "cian_source_url": "https://www.cian.ru/", "cian_address": {},
           "cian_observed_at": "2026-10-10T10:00:00Z",
           "cian_access_method": "search_index", "cian_source_updated_at": None,
           "reason_code": "project_label_only_or_address_incomplete"}
    _jsonl, csv_body, html_body = matching._artifact_bytes([row])
    csv_row = next(csv.DictReader(io.StringIO(csv_body.decode("utf-8-sig"))))
    assert csv_row["cian_project_name"].startswith("'=HYPERLINK")
    assert b"<script>" not in html_body
    assert b"&lt;script&gt;" in html_body


def test_real_local_review_is_hashed_idempotent_and_private_when_present(tmp_path: Path) -> None:
    base = matching.ROOT / ".runtime/research/cian-matching"
    observations, local = base / "observations.json", base / "local-objects.json"
    if not (observations.is_file() and local.is_file() and
            Path("/tmp/novostroy-map-points-review.json").is_file()):
        pytest.skip("Pinned local research observations are not installed")
    output = matching.review(observations, local, tmp_path)
    assert matching.review(observations, local, tmp_path) == output
    schema = json.loads((matching.ROOT / "contracts/cian-matching.schema.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)
    assert manifest["review_rows_total"] == 7
    assert manifest["accepted_crosswalks"] == manifest["live_cian_fetches"] == 0
    assert manifest["decision_counts"] == {"review": 1, "manual": 5, "rejected": 1}
    row_schema = {"$schema": schema["$schema"], "$defs": schema["$defs"],
                  "$ref": "#/$defs/review_row"}
    rows = [json.loads(line) for line in (output / "review.jsonl").read_text().splitlines()]
    assert len(rows) == 7
    for row in rows:
        Draft202012Validator(row_schema, format_checker=FormatChecker()).validate(row)
        assert not row["accepted_crosswalk"] and not row["coordinates_changed"]
    (output / "review.csv").write_text("corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        matching.review(observations, local, tmp_path)
