"""Private source identity must not survive the default public projection."""

from __future__ import annotations

from backend import public


def test_public_payload_strips_source_identity_and_page_evidence(monkeypatch) -> None:
    monkeypatch.setattr(public, "EXPOSE_SOURCES", False)
    value = {
        "status": "modeled_forecast",
        "source_identity": {"model_basis": "private-model-name",
                            "source_response_sha256": "a" * 64,
                            "source_url": "https://private.example/response"},
        "source_page_url": "https://private.example/page",
        "source_response_sha256": "b" * 64,
        "nested": [{"source_page_url": "https://private.example/nested",
                    "value": 42}],
    }
    assert public.public_payload(value) == {
        "status": "modeled_forecast", "nested": [{"value": 42}]}
