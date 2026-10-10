"""Offline, bounded JSON-LD and media references from audited entry-page bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from sources.property.developer_harvest import atomic_bytes, canonical_json


MAX_JSONLD_BYTES = 200_000
MAX_ENTITIES_PER_PAGE = 100
PRICE_TEXT = re.compile(r"(?:\bот\s*)?(?:\d[\d\s\u00a0]{4,}|\d+(?:[.,]\d+)?\s*млн)\s*(?:₽|руб\.?)(?!\w)", re.I)


def _https_url(value: object, base: str) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    url = urljoin(base, value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active = False
        self.chunks: list[str] = []
        self.size = 0
        self.blocks: list[Any] = []
        self.visible_text: list[str] = []
        self.text_size = 0
        self.ignored_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.active = True
            self.chunks = []
            self.size = 0
        elif tag in {"script", "style"}:
            self.ignored_tag = tag

    def handle_data(self, data: str) -> None:
        if self.active:
            if self.size < MAX_JSONLD_BYTES:
                self.chunks.append(data)
                self.size += len(data)
        elif not self.ignored_tag and data.strip() and self.text_size < 500_000:
            self.visible_text.append(data)
            self.text_size += len(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self.ignored_tag:
            self.ignored_tag = None
        if tag == "script" and self.active:
            if self.size <= MAX_JSONLD_BYTES:
                try:
                    self.blocks.append(json.loads("".join(self.chunks)))
                except json.JSONDecodeError:
                    pass
            self.active = False
            self.chunks = []


def _entities(value: Any) -> list[dict]:
    output: list[dict] = []
    stack = [value]
    while stack and len(output) < MAX_ENTITIES_PER_PAGE:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item[:MAX_ENTITIES_PER_PAGE]))
        elif isinstance(item, dict):
            output.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(reversed(graph[:MAX_ENTITIES_PER_PAGE]))
    return output


def extract(body: bytes, page_url: str) -> dict:
    parser = JsonLdParser()
    parser.feed(body.decode("utf-8", "replace"))
    rows: list[dict] = []
    media: list[dict] = []
    seen_media: set[str] = set()
    for block in parser.blocks:
        for item in _entities(block):
            if len(rows) >= MAX_ENTITIES_PER_PAGE:
                break
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list):
                types = []
            raw_offers = item.get("offers")
            offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
            prices = []
            for offer in offers[:20]:
                if isinstance(offer, dict) and offer.get("price") is not None:
                    prices.append({"price": str(offer["price"])[:80],
                                   "currency": str(offer.get("priceCurrency") or "")[:10],
                                   "basis": "jsonld_offer_conditions_unverified"})
            image = item.get("image")
            images = image if isinstance(image, list) else [image]
            for one in images[:20]:
                url = _https_url(one, page_url)
                if url and url not in seen_media and len(media) < 100:
                    media.append({"url": url, "role": "jsonld_image_unclassified",
                                  "rights_status": "unknown", "public_display_allowed": False})
                    seen_media.add(url)
            name = item.get("name")
            rows.append({"types": [str(kind)[:80] for kind in types[:5]],
                         "name": name[:240] if isinstance(name, str) else None,
                         "url": _https_url(item.get("url"), page_url),
                         "offers": prices})
    text = " ".join(parser.visible_text)
    return {"jsonld_entities": rows, "jsonld_media_refs": media,
            "html_text_price_pattern_present": bool(PRICE_TEXT.search(text)),
            "interpretation": "entry_page_only_no_unit_identity_or_publication_right"}


def build(run_dir: Path) -> dict:
    ledger = run_dir / "requests.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    output: list[dict] = []
    for record in records:
        sha = record.get("body_sha256")
        if not sha:
            continue
        raw = (run_dir / "raw" / f"{sha}.body").read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError(f"audited body SHA mismatch: {sha}")
        evidence = extract(raw, record["final_url"])
        output.append({"host": record["host"], "source_url": record["final_url"],
                       "observed_at": record["at"], "raw_sha256": sha, **evidence})
    data = b"".join(canonical_json(row) + b"\n" for row in output)
    atomic_bytes(run_dir / "entry-evidence.jsonl", data)
    summary = {"schema_version": "developer-domain-entry-extract/1",
               "source_ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
               "entry_pages": len(output),
               "jsonld_entities": sum(len(row["jsonld_entities"]) for row in output),
               "jsonld_media_refs": sum(len(row["jsonld_media_refs"]) for row in output),
               "html_text_price_pages": sum(row["html_text_price_pattern_present"] for row in output),
               "output_sha256": hashlib.sha256(data).hexdigest(),
               "all_moscow_coverage_claimed": False,
               "public_display_allowed": False}
    atomic_bytes(run_dir / "entry-evidence-manifest.json", canonical_json(summary) + b"\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.run_dir), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
