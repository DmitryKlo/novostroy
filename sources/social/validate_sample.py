"""Build and validate the checked-in bounded OSM POI demonstration sample."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

if __package__:
    from .osm_adapter import CLASSIFICATION_VERSION, classify_osm_tags, normalize_elements
else:
    from osm_adapter import CLASSIFICATION_VERSION, classify_osm_tags, normalize_elements


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data/social"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_dataset(manifest_name: str) -> int:
    manifest = json.loads((DATA_DIR / manifest_name).read_text(encoding="utf-8"))
    raw_path = DATA_DIR / manifest["raw_file"]
    normalized_path = DATA_DIR / manifest["normalized_file"]
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert manifest.get("source_kind") == "osm"
    assert manifest.get("coverage") == "sample"
    assert sha256(raw_path) == manifest["raw_sha256"], f"raw checksum mismatch: {manifest_name}"
    assert raw_path.stat().st_size == manifest["raw_bytes"], f"raw byte count mismatch: {manifest_name}"
    elements = raw.get("elements", [])
    assert len(elements) == manifest["element_count"] > 0, f"sample count mismatch: {manifest_name}"
    assert raw.get("osm3s", {}).get("timestamp_osm_base") == manifest["osm_base_timestamp"]
    normalized = normalize_elements(elements, observed_at=manifest["retrieved_at"])
    assert len(normalized) == len(elements)
    ids = [record["source_id"] for record in normalized]
    assert len(ids) == len(set(ids)), "duplicate OSM IDs"
    for source, record in zip(elements, normalized, strict=True):
        assert record["source_id"] == f'{source["type"]}/{source["id"]}'
        assert record["osm_version"] == source["version"]
        assert record["osm_timestamp"] == source["timestamp"]
        assert record["tags_raw"] == source.get("tags", {})
        assert record["classification_version"] == CLASSIFICATION_VERSION
        assert record["source_kind"] == "osm" and record["coverage"] == "sample"
        assert record["location"] is not None, "sample feature lacks source coordinate"
        assert record["entrance"] is None
        assert record["ownership"] is None and record["quality"] is None
        assert record["district_id"] is None
        if source["type"] == "node":
            assert record["location"]["role"] == "osm_node"
            assert record["location"]["coordinates"] == [source["lon"], source["lat"]]
            assert record["location"]["approximate"] is False
        else:
            assert record["location"]["role"] == "overpass_center"
            assert record["location"]["approximate"] is True

    category_counts = dict(Counter(record["category"] for record in normalized if record["category"]))
    expected_counts = manifest.get("observed_classification_counts")
    if expected_counts is not None:
        assert category_counts == expected_counts, f"category counts mismatch: {manifest_name}"

    normalized_text = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    normalized_bytes = normalized_text.encode("utf-8")
    digest = hashlib.sha256(normalized_bytes).hexdigest()
    if manifest.get("normalized_sha256") not in (None, "pending"):
        assert len(normalized_bytes) == manifest["normalized_bytes"], f"normalized byte count mismatch: {manifest_name}"
        assert digest == manifest["normalized_sha256"], f"normalized checksum mismatch: {manifest_name}"
    normalized_path.write_bytes(normalized_bytes)
    print(f"{manifest_name}: {len(normalized)} features, categories={category_counts or {'unclassified': len(normalized)}}, sha256={digest}")
    return len(normalized)


def main() -> None:
    total = validate_dataset("manifest.json") + validate_dataset("poi_manifest.json")
    assert classify_osm_tags({"amenity": "clinic"}) == ("healthcare", "amenity=clinic")
    assert classify_osm_tags({"amenity": "unknown_place"}) == (None, None)
    assert classify_osm_tags({"amenity": "pharmacy", "shop": "supermarket"}) == (None, None)
    print(f"validated {total} real OSM source features; entrance/ownership/quality/district remain unknown")


if __name__ == "__main__":
    main()
