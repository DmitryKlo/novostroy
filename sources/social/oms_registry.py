"""Build a small, source-backed OMS status sidecar from curated registry rows.

The adapter deliberately accepts normalized official evidence as input. It does
not scrape an undocumented endpoint or infer OMS status from OSM tags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from shapely import from_wkb


SERVICE_SCOPE = "participates_in_territorial_oms_program_not_all_services"


def _normalize_component(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFC", value).casefold()
    return " ".join(value.split()) or None


def _normalize_street(value: str | None) -> str | None:
    """Normalize the explicit `ул.` prefix and `улица` prefix/suffix forms."""
    value = _normalize_component(value)
    if not value:
        return None
    value = re.sub(r"^ул\.?\s+", "улица ", value)
    if value.startswith("улица "):
        return value
    if value.endswith(" улица"):
        return f"улица {value[:-len(' улица')]}"
    return value


def _normalize_house(value: str | None) -> tuple[str, str | None] | None:
    """Split a complete house key and retain an explicit mapped component."""
    value = _normalize_component(value)
    if not value:
        return None
    match = re.fullmatch(r"(\d+[a-zа-я]?)\s*([кс])\s*(\d+[a-zа-я]?)", value)
    if match:
        return match.group(1), f"{match.group(2)}{match.group(3)}"
    if re.fullmatch(r"\d+[a-zа-я]?", value):
        return value, None
    return None


def _official_name_core(value: str | None) -> str | None:
    value = _normalize_component(value)
    if not value:
        return None
    # Remove only a single matching pair of quotes around the official org
    # name. Normalize the explicit legal number separator `№ 8` vs `№8`.
    value = value.strip('"«»„“”')
    return re.sub(r"№\s+(\d+)", r"№\1", value)


def _org_core(organization_name: str) -> str | None:
    match = re.search(r"«([^»]+)»", organization_name)
    if not match:
        return None
    return _official_name_core(match.group(1))


def _branch_number(value: str | None) -> str | None:
    value = _normalize_component(value)
    if not value:
        return None
    match = re.search(r"филиал\s*№\s*(\d+)", value)
    return match.group(1) if match else None


def _clinic_number(value: str | None) -> str | None:
    value = _official_name_core(value)
    if not value:
        return None
    match = re.search(r"поликлиника\s*№\s*(\d+)", value)
    return match.group(1) if match else None


def _identity(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["osm_type"]), int(row["osm_id"])


def _candidate_matches(tags: dict[str, Any], record: dict[str, Any]) -> bool:
    address = record["address"]
    osm_street = _normalize_street(tags.get("addr:street"))
    source_street = _normalize_street(address.get("street"))
    if not osm_street or osm_street != source_street:
        return False

    osm_house = _normalize_house(tags.get("addr:housenumber"))
    source_house = _normalize_house(address.get("house_number"))
    if not osm_house or not source_house:
        return False
    corpus = _normalize_component(address.get("corpus"))
    if corpus:
        corpus_marker = _normalize_component(address.get("corpus_marker")) or "к"
        if source_house[1] is not None:
            return False
        source_house = (source_house[0], f"{corpus_marker}{corpus}")
    if osm_house != source_house:
        return False

    source_core = _org_core(record["organization_name"])
    source_clinic_no = _clinic_number(source_core)
    osm_clinic_numbers = {
        number
        for number in (
            _clinic_number(tags.get("name")),
            _clinic_number(tags.get("official_name")),
        )
        if number
    }
    branch_no = _normalize_component(record.get("branch_number"))
    osm_branch_numbers = {
        number
        for number in (
            _branch_number(tags.get("name")),
            _branch_number(tags.get("official_name")),
        )
        if number
    }
    city = _normalize_component(tags.get("addr:city"))
    clinic_tagged = (
        _normalize_component(tags.get("amenity")) == "clinic"
        or _normalize_component(tags.get("healthcare")) == "clinic"
    )
    osm_name_aliases = {
        normalized
        for alias in record.get("osm_name_aliases", [])
        if (normalized := _official_name_core(alias))
    }
    observed_osm_names = {
        normalized
        for normalized in (
            _official_name_core(tags.get("name")),
            _official_name_core(tags.get("official_name")),
        )
        if normalized
    }
    exact_alias_match = bool(osm_name_aliases & observed_osm_names)
    numbered_clinic_match = bool(
        source_clinic_no
        and source_clinic_no in osm_clinic_numbers
        and (not branch_no or not osm_branch_numbers or branch_no in osm_branch_numbers)
        and (branch_no is not None or not osm_branch_numbers)
    )
    return bool(
        (exact_alias_match or numbered_clinic_match)
        and (city is None or city == "москва")
        and clinic_tagged
    )


def build_sidecar(
    *,
    poi_path: Path,
    poi_manifest_path: Path,
    evidence_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    poi_manifest = json.loads(poi_manifest_path.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, int, str], dict[str, Any]] = defaultdict(dict)

    table = pq.read_table(
        poi_path,
        columns=["geometry", "osm_type", "osm_id", "osm_version", "category", "tags_json"],
    )
    for row in table.to_pylist():
        if row["category"] != "clinic":
            continue
        key = (str(row["osm_type"]), int(row["osm_id"]), str(row["category"]))
        tags = json.loads(row["tags_json"])
        geometry = from_wkb(row["geometry"])
        geometry_rank = {
            "Polygon": 3,
            "MultiPolygon": 3,
            "Point": 2,
            "LineString": 1,
            "MultiLineString": 1,
        }.get(geometry.geom_type, 0)
        # The local export may carry both line and polygon representations of
        # one OSM way. Keep one identity and prefer its area geometry.
        if (
            not grouped[key]
            or geometry_rank > grouped[key]["geometry_rank"]
            or (geometry_rank == grouped[key]["geometry_rank"] and len(tags) > len(grouped[key]["tags"]))
        ):
            grouped[key] = {
                "tags": tags,
                "version": int(row["osm_version"]),
                "geometry": geometry,
                "geometry_rank": geometry_rank,
            }

    records: list[dict[str, Any]] = []
    for evidence_row in evidence["records"]:
        if evidence_row.get("oms_effective_year") != 2026:
            raise ValueError("Only the dated 2026 OMS evidence row is accepted here")
        if not evidence_row.get("oms_source_url") or not evidence_row.get("facility_source_url"):
            raise ValueError("Official OMS and facility address sources are required")
        matches = [
            (key, feature)
            for key, feature in grouped.items()
            if _candidate_matches(feature["tags"], evidence_row)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one exact OSM clinic match for {evidence_row['organization_code']}; "
                f"found {len(matches)}"
            )

        (osm_type, osm_id, category), feature = matches[0]
        geometry = feature["geometry"]
        if geometry.geom_type == "Point":
            point = geometry
            location_method = "source_point"
            location_quality = "source_point_access_unverified"
        else:
            point = geometry.representative_point()
            location_method = "point_on_surface"
            location_quality = "approximate_footprint"
        output_record = {
                "osm_source_id": f"osm:{osm_type}:{osm_id}:{category}",
                "osm_version": feature["version"],
                "poi_table_sha256": poi_manifest["output"]["sha256"],
                "osm_geometry_type": geometry.geom_type,
                "location": [point.x, point.y],
                "location_method": location_method,
                "location_quality": location_quality,
                "oms_status": "verified",
                "organization_code": str(evidence_row["organization_code"]),
                "organization_name": evidence_row["organization_name"],
                "facility_name": evidence_row["facility_name"],
                "oms_registry_reference": evidence_row.get("oms_registry_reference"),
                "matched_address": evidence_row["address"]["display"],
                "official_source_url": evidence_row["oms_source_url"],
                "facility_address_source_url": evidence_row["facility_source_url"],
                "official_snapshot_at": evidence_row["facility_snapshot_at"],
                "effective_year": evidence_row["oms_effective_year"],
                "match_method": "unique_exact_name_address",
                "service_scope": SERVICE_SCOPE,
            }
        for input_key, output_key in (
            ("facility_source_reference", "facility_address_source_reference"),
            ("specialties", "specialties"),
            ("specialties_source_url", "specialties_source_url"),
            ("specialties_source_reference", "specialties_source_reference"),
        ):
            if evidence_row.get(input_key) is not None:
                output_record[output_key] = evidence_row[input_key]
        aliases = {
            normalized
            for alias in evidence_row.get("osm_name_aliases", [])
            if (normalized := _official_name_core(alias))
        }
        for tag_key in ("name", "official_name"):
            observed_name = _official_name_core(feature["tags"].get(tag_key))
            if observed_name and observed_name in aliases:
                output_record["matched_osm_name"] = feature["tags"][tag_key]
                break
        records.append(output_record)

    sidecar = {
        "schema_version": 1,
        "source_snapshot": {
            "osm_file": poi_manifest["source"]["file"],
            "osm_sha256": poi_manifest["source"]["sha256"],
            "osm_snapshot_timestamp": poi_manifest["source"]["osm_snapshot_timestamp"],
            "poi_table_sha256": poi_manifest["output"]["sha256"],
            "osm_license": poi_manifest["rights"]["license"],
            "osm_attribution": poi_manifest["rights"]["attribution"],
            "official_evidence_file": evidence_path.as_posix(),
            "official_snapshot_at": evidence["snapshot_at"],
        },
        "records": records,
    }
    manifest = {
        "dataset": "verified OMS program participation assertions joined to OSM clinic features",
        "created_at": evidence["snapshot_at"],
        "source_manifest": poi_manifest_path.as_posix(),
        "source_manifest_sha256": _sha256(poi_manifest_path),
        "evidence_file": evidence_path.as_posix(),
        "evidence_sha256": _sha256(evidence_path),
        "output_sha256": None,
        "records_total": len(records),
        "rights": {
            "osm": "ODbL-1.0; retain attribution and comply with share-alike obligations",
            "official_sources": "Normalized factual assertions from public official pages; no bulk-reuse license or API was verified.",
        },
        "limitations": evidence.get("limitations", []),
    }
    return sidecar, manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(encoded)
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pois", type=Path, default=Path("data/social/osm_moscow_social_pois_261008.parquet"))
    parser.add_argument("--poi-manifest", type=Path, default=Path("data/social/osm_moscow_social_pois_261008.manifest.json"))
    parser.add_argument("--evidence", type=Path, default=Path("data/social/oms_registry_records_2026.json"))
    parser.add_argument("--output", type=Path, default=Path("data/social/oms_verified_clinics_2026.json"))
    parser.add_argument("--manifest", type=Path, default=Path("data/social/oms_verified_clinics_2026.manifest.json"))
    args = parser.parse_args()

    sidecar, manifest = build_sidecar(
        poi_path=args.pois,
        poi_manifest_path=args.poi_manifest,
        evidence_path=args.evidence,
    )
    manifest["output_sha256"] = hashlib.sha256(
        (json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    ).hexdigest()
    _write_json(args.output, sidecar)
    _write_json(args.manifest, manifest)
    print(f"wrote {len(sidecar['records'])} verified OMS rows to {args.output}")


if __name__ == "__main__":
    main()
