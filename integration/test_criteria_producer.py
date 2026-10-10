"""A source adapter fact must pass the same registry filter consumed by the API."""

from __future__ import annotations

import json
from pathlib import Path

from recommendations.engine import search
from sources.property.adapters import parse_developer_yrl


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "sources/property/tests/fixtures"


def test_demo_yrl_area_and_exact_price_per_sqm_reach_engine() -> None:
    rights = json.loads((FIXTURES / "rights.synthetic.json").read_text(encoding="utf-8"))
    [record] = parse_developer_yrl(
        FIXTURES / "developer_feed.synthetic.xml", source_id="synthetic_test",
        rights=rights, data_mode="demo", observed_at="2026-10-09T00:00:00Z",
    )
    assert record["facts"]["unit.area_sqm"]["unit"] == "m²"
    request = {"filters": {"predicates": [
        {"key": "unit.area_sqm", "op": "min", "value": "55.5"},
        {"key": "unit.area_sqm", "op": "max", "value": "55.5"},
        {"key": "offer.price_per_sqm_rub", "op": "min", "value": "177000000"},
    ]}}
    result = search([record], request, snapshot_at="2026-10-09T00:00:00Z")
    assert result["count"] == 1
    assert result["items"][0]["offer_id"] == "SYNTHETIC-DEMO-OFFER-001"
