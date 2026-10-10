"""The bounded media check uses private refs and never treats a URL as a license."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sources.property.developer_harvest import canonical_json, digest
from sources.property.developer_media_probe import (
    MEDIA_HOSTS,
    ROLE_PLAN,
    MediaResponse,
    detected_media_type,
    probe,
    select_samples,
)


def _delivery(tmp_path: Path) -> Path:
    rows = []
    for provider, roles in ROLE_PLAN.items():
        host = sorted(MEDIA_HOSTS[provider])[0]
        for role in roles:
            rows.append({
                "provider": provider,
                "role": role,
                "scope": "unit",
                "url": f"https://{host}/{provider}/{role}.png",
                "observation_id": f"{provider}-{role}",
                "source_page_url": f"https://{provider}.example/catalog",
                "rights_status": "unknown",
                "public_display_allowed": False,
            })
    raw = b"".join(canonical_json(row) + b"\n" for row in rows)
    (tmp_path / "media_refs.jsonl").write_bytes(raw)
    (tmp_path / "manifest.json").write_bytes(canonical_json({
        "schema_version": "developer-delivery/1",
        "files": {"media_refs": {"path": "media_refs.jsonl",
                                 "sha256": digest(raw), "records": len(rows)}},
    }) + b"\n")
    return tmp_path


def test_sample_is_typed_bounded_private_and_hash_checked(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    plan = probe(delivery, tmp_path / "out", live=False)
    assert plan["network_attempts"] == 0
    assert len(plan["sample"]) == 12
    assert {row["provider"] for row in plan["sample"]} == set(ROLE_PLAN)
    assert not (tmp_path / "out").exists()
    refs = [json.loads(line) for line in (delivery / "media_refs.jsonl").read_text().splitlines()]
    refs.append({**refs[0], "url": "http://127.0.0.1/internal"})
    refs.append({**refs[0], "url": "https://cdn.a101.ru/unlicensed.png",
                 "public_display_allowed": True})
    assert len(select_samples(refs)) == 12
    (delivery / "media_refs.jsonl").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="hash/count"):
        probe(delivery, tmp_path / "out", live=False)


def test_denial_stops_host_and_resume_does_not_refetch(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    calls = []

    def fetch(url: str, allowed_hosts: set[str]) -> MediaResponse:
        calls.append(url)
        if "a101" in url:
            return MediaResponse(403, b"", "text/html", url, "http_403")
        if "fsk" in url:
            return MediaResponse(200, b"<html>not a plan</html>", "text/html", url)
        return MediaResponse(200, b"\x89PNG\r\n\x1a\nimage", "image/png", url)

    output = tmp_path / "probe"
    result = probe(delivery, output, live=True, fetch=fetch, sleep=lambda _: None)
    assert len(result["results"]) == 12
    assert result["network_attempts"] == 10
    assert result["blocked_hosts"] == ["cdn.a101.ru"]
    assert sum(row["fetch_status"] == "skipped_blocked_host"
               for row in result["results"]) == 2
    assert all(row["public_display_allowed"] is False for row in result["results"])
    assert all(row["fetch_status"] == "not_image_or_type_mismatch"
               for row in result["results"] if row["provider"] == "fsk")
    assert len(list((output / "raw").iterdir())) == 1  # identical bytes deduplicate
    assert probe(delivery, output, live=True, fetch=fetch) == result
    assert len(calls) == 10


def test_magic_does_not_accept_html_or_extension_claim() -> None:
    assert detected_media_type(b"<html><img src='plan.jpg'></html>") is None
    assert detected_media_type(b"\xff\xd8\xffsome jpeg bytes") == "image/jpeg"
