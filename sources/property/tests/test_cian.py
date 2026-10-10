"""CIAN private review import: real observations and synthetic parser boundaries."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sources.property.cian import (
    _validate_access_manifest,
    collect_authorized_pages,
    parse_authorized_html,
    parse_public_observations,
    write_review_run,
)


ROOT = Path(__file__).resolve().parents[3]
ACTUAL_OBSERVATIONS = ROOT / "data/property/cian_public_sample_20261010.json"
LISTING_URL = "https://www.cian.ru/sale/flat/325479540/"


def _rights(*, automated: bool = False, status: str = "cleared") -> dict[str, object]:
    return {
        "source": "cian", "agreement_reference": "synthetic-test-agreement",
        "local_processing_authorized": True, "permitted_storage": True,
        "automated_access_authorized": automated, "rights_status": status,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }


def _actual_payload() -> dict:
    return json.loads(ACTUAL_OBSERVATIONS.read_text(encoding="utf-8"))


@pytest.mark.skipif(not ACTUAL_OBSERVATIONS.is_file(), reason="local CIAN observation sample absent")
def test_two_actual_rendered_observations_are_private_review_not_offers() -> None:
    rows, skipped = parse_public_observations(ACTUAL_OBSERVATIONS)
    assert len(rows) == 2
    assert skipped == {"residential_complex": 1}
    by_id = {row["listing_id"]: row for row in rows}
    assert by_id["325479540"]["market_channel"] == "developer_primary"
    assert by_id["332989085"]["market_channel"] == "assignment"
    assert {row["market_kind"] for row in rows} == {"newbuild"}
    assert {row["price_kind"] for row in rows} == {"unit_asking"}
    assert {row["price"]["amount_rub"] for row in rows} == {38881323, 28590000}
    assert {row["source_updated_at"] for row in rows} == {None}
    for row in rows:
        assert row["media"] == [] and row["media_file_count"] == 0
        assert row["complex_id"] is row["unit_id"] is row["location"] is None
        assert row["rights_status"] == "unknown"
        assert "rights_not_cleared" in row["review_reasons"]


@pytest.mark.skipif(not ACTUAL_OBSERVATIONS.is_file(), reason="local CIAN observation sample absent")
def test_price_basis_rejects_from_conditional_per_square_metre_and_project_card() -> None:
    payload = _actual_payload()
    item = payload["observations"][0]
    payload["observations"] = [item]
    changed = copy.deepcopy(payload)
    changed["observations"][0]["displayed_price_basis"]["price_marked_as_from"] = True
    row = parse_public_observations(changed)[0][0]
    assert row["price_kind"] == "project_from" and row["price"] is None
    assert row["source_fields"]["displayed_price_basis"]["displayed_total_rub"] == 38881323
    assert row["source_fields"]["displayed_price_basis"]["price_marked_as_from"] is True

    changed = copy.deepcopy(payload)
    changed["observations"][0]["displayed_price_basis"][
        "unit_specific_discount_or_financing_condition_visible"] = True
    row = parse_public_observations(changed)[0][0]
    assert row["price_kind"] == "conditional" and row["price"] is None
    assert row["source_fields"]["displayed_price_basis"][
        "unit_specific_discount_or_financing_condition_visible"] is True

    changed = copy.deepcopy(payload)
    changed["observations"][0].pop("price_rub")
    changed["observations"][0]["displayed_price_basis"].pop("amount_rub")
    row = parse_public_observations(changed)[0][0]
    assert row["price_kind"] == "unknown" and row["price"] is None
    assert row["stated_price_per_sqm_rub"] == 553865


@pytest.mark.skipif(not ACTUAL_OBSERVATIONS.is_file(), reason="local CIAN observation sample absent")
def test_identity_source_dates_and_media_labels_are_strict() -> None:
    payload = _actual_payload()
    payload["observations"] = [payload["observations"][0]]
    for bad_url in ("https://evil.cian.ru/sale/flat/325479540/",
                    LISTING_URL + "?source=x", "http://www.cian.ru/sale/flat/325479540/"):
        changed = copy.deepcopy(payload)
        changed["observations"][0]["source_url"] = bad_url
        with pytest.raises(ValueError):
            parse_public_observations(changed)
    changed = copy.deepcopy(payload)
    changed["observations"][0]["source_updated_at"] = "2026-10-09T00:00:00Z"
    assert parse_public_observations(changed)[0][0]["source_updated_at"] == "2026-10-09T00:00:00Z"
    changed["observations"][0]["source_updated_at"] = "2026-10-11T00:00:00Z"
    with pytest.raises(ValueError):
        parse_public_observations(changed)
    changed = copy.deepcopy(payload)
    changed["observations"][0]["visible_media_labels"] = "Планировка"
    with pytest.raises(ValueError):
        parse_public_observations(changed)
    changed = copy.deepcopy(payload)
    changed["observed_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError):
        parse_public_observations(changed)


@pytest.mark.skipif(not ACTUAL_OBSERVATIONS.is_file(), reason="local CIAN observation sample absent")
def test_append_only_history_replay_and_report_artifacts(tmp_path: Path) -> None:
    rows, skipped = parse_public_observations(ACTUAL_OBSERVATIONS)
    first = write_review_run(rows, input_sha256="a" * 64, output_root=tmp_path, skipped=skipped)
    assert first["new_records"] == 2
    run = Path(first["path"])
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["records_total"] == 2
    assert manifest["capture_kinds"] == {"public_observation": 2}
    assert manifest["market_channels"] == {"assignment": 1, "developer_primary": 1}
    assert manifest["media_descriptors"] == manifest["media_files_downloaded"] == 0
    assert manifest["live_fetches"] == 0 and manifest["public_offer_imported"] is False
    assert "residential_complex: 1" in (run / "report.md").read_text()
    assert "asking_total_rub" in (run / "observations.csv").read_text()
    assert write_review_run(rows, input_sha256="a" * 64, output_root=tmp_path)[
        "status"] == "already_imported"
    newer = copy.deepcopy(rows[0])
    newer["observed_at"] = "2026-10-10T09:00:00Z"
    newer["price"]["amount_rub"] = 38881000
    second = write_review_run([newer], input_sha256="b" * 64, output_root=tmp_path)
    assert second["new_records"] == 1 and second["runs_total"] == 2
    assert (run / "observations.jsonl").is_file()
    index = json.loads((tmp_path / "index.json").read_text())
    assert len(index["observations"]) == 3


def test_authorized_synthetic_html_exact_offer_and_media(tmp_path: Path) -> None:
    html = f'''<html><head><link rel="canonical" href="{LISTING_URL}">
    <script type="application/ld+json">{{"@type":"Offer","url":"{LISTING_URL}",
    "price":38881323,"priceCurrency":"RUB"}}</script></head><body>
    <div>Тип жилья</div><div>Новостройка</div>
    <div>Условия сделки</div><div>долевое участие (214-ФЗ)</div>
    <div>Продавец</div><div>Застройщик</div>
    <div>Общая площадь 70.2 м²</div><div>Этаж 7 из 43</div>
    <img src="https://example.com/unit-plan.jpg" alt="Планировка квартиры"></body></html>'''
    path = tmp_path / "saved.html"
    path.write_text(html, encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unit_asking"
    assert row["price"]["amount_rub"] == 38881323
    assert row["market_channel"] == "developer_primary"
    assert row["area_sqm"] == 70.2 and row["floor"] == 7
    assert len(row["media"]) == 1 and row["media"][0]["role"] == "apartment_plan"
    assert row["media"][0]["fetch_status"] == "not_requested"
    assert row["media"][0]["public_display_allowed"] is False
    assert "rights_not_cleared" not in row["review_reasons"]


def test_synthetic_html_recommendation_from_price_and_per_m2_not_unit_price(tmp_path: Path) -> None:
    html = f'''<link rel="canonical" href="{LISTING_URL}">
    <script type="application/ld+json">{{"@graph":[
      {{"@type":"Offer","url":"https://www.cian.ru/sale/flat/999999999/",
        "price":90000000,"priceCurrency":"RUB"}}]}}</script>
    <meta property="product:price:amount" content="553865">
    <meta property="product:price:currency" content="RUB">
    <div>Цена от 553865</div><div>Ипотека возможна</div>'''
    path = tmp_path / "saved.html"
    path.write_text(html, encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unknown" and row["price"] is None
    assert row["media"] == []

    path.write_text('''<script type="application/ld+json">{"@type":"Offer",
    "url":"https://www.cian.ru/sale/flat/999999999/","price":90000000,
    "priceCurrency":"RUB"}</script>''', encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unknown" and row["price"] is None

    path.write_text(f'''<link rel="canonical" href="{LISTING_URL}">
    <meta property="product:price:amount" content="553865">
    <meta property="product:price:currency" content="RUB">
    <div>Цена за м²</div><div>553865 ₽</div>''', encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unknown" and row["price"] is None

    path.write_text(f'''<link rel="canonical" href="{LISTING_URL}">
    <meta property="product:price:amount" content="38881323">
    <meta property="product:price:currency" content="RUB">
    <div>Цена квартиры</div><div>38881323 ₽</div>''', encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unit_asking"
    assert row["price"]["amount_rub"] == 38881323

    path.write_text(f'''<link rel="canonical" href="{LISTING_URL}">
    <script type="application/ld+json">{{"@type":"Offer","url":"{LISTING_URL}",
    "price":38881323,"priceCurrency":"RUB",
    "priceSpecification":{{"@type":"UnitPriceSpecification"}}}}</script>''', encoding="utf-8")
    row = parse_authorized_html(path, source_url=LISTING_URL,
                                observed_at="2026-10-10T08:19:01Z", rights=_rights())
    assert row["price_kind"] == "unknown" and row["price"] is None


def test_automated_capture_fails_rights_gate_before_network(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not authorized"):
        collect_authorized_pages([LISTING_URL], rights=_rights(automated=False),
                                 output_root=tmp_path)
    assert not list(tmp_path.iterdir())
    _validate_access_manifest(_rights(automated=True), require_automated=True)
    for delay in (float("nan"), float("inf"), 60.0, 0.5):
        with pytest.raises(ValueError, match="delay"):
            collect_authorized_pages([LISTING_URL], rights=_rights(automated=True),
                                     output_root=tmp_path, delay_seconds=delay)
    assert not list(tmp_path.iterdir())
