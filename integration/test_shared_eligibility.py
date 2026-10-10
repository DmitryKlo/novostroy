"""An explicitly unavailable offer cannot reappear in any active API path."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import backend.app as api_module
from backend.app import app


ROOT = Path(__file__).resolve().parents[1]


def test_unavailable_offer_excluded_from_search_map_detail_compare(monkeypatch) -> None:
    catalogue = json.loads((ROOT / "data/demo/catalog.json").read_text(encoding="utf-8"))
    unavailable = catalogue["offers"][0]
    unavailable["availability"] = "unavailable"
    allowed = catalogue["offers"][1]
    monkeypatch.setattr(api_module, "load_catalogue", lambda: catalogue)
    client = TestClient(app)

    searched = client.post("/v1/search", json={"filters": {"limit": 100}})
    assert searched.status_code == 200
    assert searched.json()["count"] == len(catalogue["offers"]) - 1
    assert unavailable["offer_id"] not in {item["offer_id"] for item in searched.json()["items"]}

    mapped = client.get("/v1/map", params={"complex_ids": unavailable["complex_id"]})
    assert mapped.status_code == 200
    assert mapped.json()["counts"]["offers"] == sum(
        offer["complex_id"] == unavailable["complex_id"]
        for offer in catalogue["offers"]
    ) - 1

    assert client.get(f"/v1/offers/{unavailable['offer_id']}").status_code == 404
    comparison = client.post("/v1/compare", json={
        "offer_ids": [allowed["offer_id"], unavailable["offer_id"]]
    })
    assert comparison.status_code == 404
