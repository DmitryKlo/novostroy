"""Narrow calls into the already audited local source adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sources.property.adapters import parse_developer_yrl
from sources.property.importer import validate_records
from sources.property.minstroy_registry import (
    DATASETS as MINSTROY_DATASETS,
    SCHEMA_VERSION as MINSTROY_SCHEMA_VERSION,
    parse_registry_workbook,
)
from sources.social.osm_adapter import CLASSIFICATION_VERSION, normalize_elements
from sources.social.enrichment import enrich_social_batch
from sources.social.taxonomy import TAXONOMY_VERSION


ADAPTER_VERSIONS = {
    "developer_yrl_file": "developer-yrl-local-v1",
    "osm_poi_file": CLASSIFICATION_VERSION,
    "city_parquet_reference": "overture-parquet-reference-v2",
    "social_enrichment_fixture": f"social-enrichment-{TAXONOMY_VERSION}-v1",
    "minstroy_registry_xls": MINSTROY_SCHEMA_VERSION,
}


@dataclass(frozen=True)
class Batch:
    records: list[dict[str, Any]]
    coverage: dict[str, Any]
    observed_at: str | None
    etag: str | None = None
    last_modified: str | None = None


def _field_coverage(records: list[dict[str, Any]], fields: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        parts = field.split(".")
        populated = 0
        for record in records:
            value: Any = record
            for part in parts:
                value = value.get(part) if isinstance(value, Mapping) else None
            if value is not None:
                populated += 1
        result[field] = {"populated": populated, "missing": len(records) - populated}
    return result


def developer_yrl_file(path: Path, job: Mapping[str, Any], manifest: Mapping[str, Any] | None,
                       rights: Mapping[str, Any] | None, previous_etag: str | None) -> Batch:
    if rights is None:
        raise ValueError("A rights manifest is required for YRL processing")
    mode = job.get("data_mode")
    records = parse_developer_yrl(
        path, source_id=job["source_id"], rights=rights, data_mode=mode,
        observed_at=job.get("observed_at"),
    )
    if mode == "real":
        validate_records(records, rights)
    return Batch(
        records=records,
        coverage={"status": "illustrative" if mode == "demo" else "source_rows_only",
                  "record_count": len(records),
                  "fields": _field_coverage(records, ("offer_id", "unit_id", "complex_id", "district_id", "rooms", "area_sqm", "price.amount_rub", "availability"))},
        observed_at=job.get("observed_at"),
    )


def osm_poi_file(path: Path, job: Mapping[str, Any], manifest: Mapping[str, Any] | None,
                 rights: Mapping[str, Any] | None, previous_etag: str | None) -> Batch:
    if manifest is None or manifest.get("coverage") != "sample":
        raise ValueError("OSM sample requires its pinned source manifest")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("OSM raw sample must contain elements")
    observed_at = manifest.get("retrieved_at")
    if not isinstance(observed_at, str):
        raise ValueError("OSM manifest has no retrieval time")
    records = normalize_elements(payload["elements"], observed_at=observed_at)
    categories: dict[str, int] = {}
    for record in records:
        category = record.get("category") or "unclassified"
        categories[category] = categories.get(category, 0) + 1
    return Batch(records, {
        "status": "sample_only", "record_count": len(records),
        "categories": categories,
        "fields": _field_coverage(records, ("source_id", "category", "location", "entrance", "ownership", "quality")),
        "license": manifest.get("license"), "attribution": manifest.get("required_attribution"),
    }, observed_at)


def city_parquet_reference(path: Path, job: Mapping[str, Any], manifest: Mapping[str, Any] | None,
                           rights: Mapping[str, Any] | None, previous_etag: str | None) -> Batch:
    """Inventory a local clipped Parquet export; never rebuild or copy tiles."""
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    if not {"id", "geometry"}.issubset(names):
        raise ValueError("City Parquet must contain id and geometry")
    row_count = parquet.metadata.num_rows
    fields: dict[str, Any] = {}
    physical_columns = {parquet.schema.column(index).path: index
                        for index in range(parquet.metadata.row_group(0).num_columns)}
    for field in ("id", "geometry", "height", "num_floors", "min_height"):
        if field not in names:
            fields[field] = {"populated": 0, "missing": row_count}
            continue
        index = physical_columns.get(field)
        if index is None:
            fields[field] = {"populated": None, "missing": None}
            continue
        known_nulls = 0
        complete_statistics = True
        for group in range(parquet.metadata.num_row_groups):
            stats = parquet.metadata.row_group(group).column(index).statistics
            if stats is None or stats.null_count is None or not 0 <= stats.null_count <= parquet.metadata.row_group(group).num_rows:
                complete_statistics = False
                break
            known_nulls += stats.null_count
        fields[field] = ({"populated": row_count - known_nulls, "missing": known_nulls}
                         if complete_statistics else {"populated": None, "missing": None})
    record = {"record_type": "city_export_reference", "source_id": job["source_id"],
              "row_count": row_count, "source_release": (manifest or {}).get("source_release")}
    return Batch([record], {"status": "bounded_export_reference", "record_count": row_count, "fields": fields,
                            "geometry_quality": "footprints_only", "height_status": "partial"},
                 (manifest or {}).get("observed_at"))


def social_enrichment_fixture(path: Path, job: Mapping[str, Any], manifest: Mapping[str, Any] | None,
                              rights: Mapping[str, Any] | None, previous_etag: str | None) -> Batch:
    """Exercise POI enrichment at a visibly fictional anchor, outside the API."""
    if job.get("data_mode") != "illustrative_fixture" or manifest is None or manifest.get("coverage") != "sample":
        raise ValueError("Social enrichment fixture requires sample provenance")
    anchor = job.get("synthetic_anchor")
    if not isinstance(anchor, list) or len(anchor) != 2:
        raise ValueError("Synthetic anchor needs [lon,lat]")
    pois = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pois, list):
        raise ValueError("Normalized POI source must be a list")
    records = enrich_social_batch(
        [{"complex_id": "synthetic-origin-for-pipeline-only", "coordinates": anchor,
          "access_kind": "center_approx", "source_id": "synthetic_fixture"}],
        pois, poi_source_version=manifest.get("normalized_sha256"), poi_coverage="sample",
        max_pedestrian_requests=0, max_transit_requests=0,
    )
    for record in records:
        record["fixture_only"] = True
        for fact in record.get("facts", {}).values():
            if fact.get("value") is not None:
                fact["status"] = "illustrative"
                fact["scope"] = "synthetic_origin_" + str(fact.get("scope", "sample"))
                fact.setdefault("provenance", {})["synthetic_origin"] = True
    fact_values = list(records[0].get("facts", {}).values()) if records else []
    populated = sum(fact.get("value") is not None for fact in fact_values)
    return Batch(records, {
        "status": "illustrative_fixture_only", "record_count": len(records),
        "fact_fields": len(fact_values), "populated_fact_fields": populated,
        "unknown_fact_fields": len(fact_values) - populated,
        "source_coverage": "sample", "routed_requests": 0,
    }, manifest.get("retrieved_at"))


def minstroy_registry_xls(path: Path, job: Mapping[str, Any], manifest: Mapping[str, Any] | None,
                          rights: Mapping[str, Any] | None, previous_etag: str | None) -> Batch:
    """Preserve public permit events separately from Moscow complexes and offers."""
    dataset_kind = job.get("dataset_kind")
    if dataset_kind not in MINSTROY_DATASETS or MINSTROY_DATASETS[dataset_kind]["source_id"] != job.get("source_id"):
        raise ValueError("Unknown Minstroy dataset kind")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("datasets"), list):
        raise ValueError("Official registry source manifest is required")
    dataset = next((entry for entry in manifest["datasets"] if entry.get("source_id") == job["source_id"]), None)
    if not isinstance(dataset, Mapping) or dataset.get("rights_status") != "cleared":
        raise ValueError("Registry source rights are not cleared")
    observed_at = dataset.get("captured_at") or dataset.get("retrieved_at")
    if not isinstance(observed_at, str):
        raise ValueError("Registry retrieval date is missing")
    records = parse_registry_workbook(path, dataset_kind=dataset_kind,
                                      observed_at=observed_at, residential_only=True)
    if any(record.get("record_type") != "registry_event" or record.get("source", {}).get("record_mode") != "registry_status"
           for record in records):
        raise ValueError("Registry adapter emitted an offer-shaped record")
    fields = ("registry_event.permit_number", "registry_event.event_date",
              "project_reference.name", "project_reference.applicant",
              "project_reference.address", "project_reference.floor_count", "project_reference.price_rub")
    return Batch(records, {
        "status": "registry_events_only", "record_count": len(records),
        "raw_row_count": dataset.get("raw_record_count"), "moscow_joins": 0,
        "fields": _field_coverage(records, fields),
        "rights_status": "cleared", "published_version_date": dataset.get("published_version_date"),
    }, observed_at)


ADAPTERS = {
    "developer_yrl_file": developer_yrl_file,
    "osm_poi_file": osm_poi_file,
    "city_parquet_reference": city_parquet_reference,
    "social_enrichment_fixture": social_enrichment_fixture,
    "minstroy_registry_xls": minstroy_registry_xls,
}
