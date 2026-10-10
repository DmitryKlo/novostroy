"""Rights-gated, dry-run-by-default importer for canonical developer offers.

This module writes only to the project's PostGIS tables. It accepts a libpq
service name, never a connection URL. The isolated `test_only` path is guarded
by a matching `property_test_*` service and schema and exists solely to run
synthetic integration fixtures through the real API code path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


_SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TEST_SOURCE_RE = re.compile(r"^synthetic_test_[A-Za-z0-9_-]+$")
_OFFER_FIELDS = (
    "offer_id", "unit_id", "building_id", "section_id", "floor_id",
    "complex_id", "district_id", "rooms", "area_sqm", "floor", "price",
    "source", "availability", "facts",
)
_SOURCE_FIELDS = (
    "name", "url", "quality", "record_mode", "observed_at",
    "source_updated_at", "rights_status", "attribution", "source_id",
    "source_record_id", "source_fields",
)
_FACT_STATUSES = {
    "observed", "derived", "estimated", "illustrative", "unknown", "stale", "conflict", "not_applicable",
}
_NULL_FACT_STATUSES = {"unknown", "stale", "conflict", "not_applicable"}


def _timestamp(value: Any, label: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _validate_rights_manifest(rights: Mapping[str, Any], *, test_only: bool) -> str:
    source_id = rights.get("source_id")
    basis = rights.get("access_basis")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("rights manifest must include a non-empty source_id")
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("rights manifest must include a non-empty access_basis")
    if rights.get("rights_status") != "cleared":
        raise ValueError("source rights_status must be explicitly cleared before import")
    if test_only:
        if rights.get("test_only") is not True or basis != "isolated_integration_test_only":
            raise ValueError("test-only import requires isolated_integration_test_only provenance")
        if not _TEST_SOURCE_RE.fullmatch(source_id):
            raise ValueError("test-only source_id must start with synthetic_test_")
        return source_id
    if rights.get("test_only") is True:
        raise ValueError("test-only rights cannot be used for a production import")
    if rights.get("permitted_storage") is not True or rights.get("permitted_display") is not True:
        raise ValueError("production import requires explicit storage and public-display permission")
    return source_id


def _validate_facts(
    facts: Any,
    source: Mapping[str, Any],
    *,
    record_index: int,
    test_only: bool,
) -> dict[str, dict[str, Any]]:
    if facts is None:
        return {}
    if not isinstance(facts, Mapping):
        raise ValueError(f"record {record_index} facts must be an object")
    validated: dict[str, dict[str, Any]] = {}
    expected_source_id = source.get("source_id")
    expected_record_id = source.get("source_record_id")
    expected_rights = source.get("rights_status")
    for key, raw_fact in facts.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"record {record_index} fact keys must be non-empty strings")
        if not isinstance(raw_fact, Mapping):
            raise ValueError(f"record {record_index} fact {key!r} must be an object")
        status = raw_fact.get("status")
        if status not in _FACT_STATUSES:
            raise ValueError(f"record {record_index} fact {key!r} has unsupported status")
        value = raw_fact.get("value")
        if status in _NULL_FACT_STATUSES and value is not None:
            raise ValueError(f"record {record_index} fact {key!r} status {status!r} requires null value")
        if status not in _NULL_FACT_STATUSES and value is None:
            raise ValueError(f"record {record_index} fact {key!r} status {status!r} requires a value")
        if status == "illustrative" and not test_only:
            raise ValueError(f"record {record_index} fact {key!r} is illustrative and cannot be imported as real")
        if not isinstance(raw_fact.get("unit"), str) or not isinstance(raw_fact.get("scope"), str):
            raise ValueError(f"record {record_index} fact {key!r} needs unit and scope strings")
        _timestamp(raw_fact.get("observed_at"), f"record {record_index} fact {key}.observed_at")
        provenance = raw_fact.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"record {record_index} fact {key!r} needs provenance")
        if provenance.get("source_id") != expected_source_id:
            raise ValueError(f"record {record_index} fact {key!r} source_id does not match source")
        if provenance.get("source_record_id") != expected_record_id:
            raise ValueError(f"record {record_index} fact {key!r} source_record_id does not match source")
        source_field = provenance.get("source_field")
        if source_field is not None and (not isinstance(source_field, str) or not source_field.strip()):
            raise ValueError(f"record {record_index} fact {key!r} source_field must be non-empty text or null")
        _timestamp(provenance.get("source_updated_at"), f"record {record_index} fact {key}.source_updated_at", optional=True)
        if provenance.get("rights_status") != expected_rights:
            raise ValueError(f"record {record_index} fact {key!r} rights_status does not match source")
        if status == "derived":
            inputs = provenance.get("inputs")
            formula = provenance.get("formula")
            if not isinstance(inputs, list) or not inputs or not isinstance(formula, str) or not formula.strip():
                raise ValueError(f"record {record_index} derived fact {key!r} needs input provenance and formula")
        validated[key] = dict(raw_fact)
    return validated


def validate_records(
    records: Iterable[Mapping[str, Any]],
    rights: Mapping[str, Any],
    *,
    test_only: bool = False,
) -> list[dict[str, Any]]:
    """Validate offer records and explicit rights without connecting to PostGIS."""
    source_id = _validate_rights_manifest(rights, test_only=test_only)
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(records, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"record {index} must be an object")
        if item.get("data_mode") != "real" or item.get("record_type") != "offer":
            raise ValueError(f"record {index} is not a real-mode canonical offer")
        offer_id = item.get("offer_id")
        complex_id = item.get("complex_id")
        source = item.get("source")
        if not isinstance(offer_id, str) or not offer_id.strip():
            raise ValueError(f"record {index} needs a non-empty offer_id")
        if not isinstance(complex_id, str) or not complex_id.strip():
            raise ValueError(f"record {index} needs an explicit complex_id")
        if not isinstance(source, Mapping):
            raise ValueError(f"record {index} needs a source object")
        if source.get("source_id") != source_id:
            raise ValueError(f"record {index} source_id does not match the rights manifest")
        if source.get("rights_status") != "cleared":
            raise ValueError(f"record {index} source rights are not explicitly cleared")
        source_rights = source.get("source_rights")
        if not isinstance(source_rights, Mapping):
            raise ValueError(f"record {index} must preserve its source_rights context")
        if source_rights.get("rights_status") != "cleared" or source_rights.get("access_basis") != rights.get("access_basis"):
            raise ValueError(f"record {index} source_rights do not match the cleared rights manifest")
        if test_only and "synthetic" not in str(source.get("name", "")).casefold():
            raise ValueError(f"record {index} test-only source must be visibly labeled synthetic")
        if source.get("record_mode") not in {"developer_offer", "asking_price"}:
            raise ValueError(f"record {index} is not an importable offer record")
        quality = source.get("quality")
        if quality not in {"dated_asking", "unknown"}:
            raise ValueError(f"record {index} has unsupported source quality")
        _timestamp(source.get("observed_at"), f"record {index} source.observed_at")
        _timestamp(source.get("source_updated_at"), f"record {index} source.source_updated_at", optional=True)
        if not isinstance(source.get("source_record_id"), str) or not source["source_record_id"].strip():
            raise ValueError(f"record {index} needs a source.source_record_id")
        raw_fields = source.get("source_fields", {})
        if not isinstance(raw_fields, Mapping):
            raise ValueError(f"record {index} source.source_fields must be an object")
        if isinstance(item.get("availability"), str) and item["availability"] not in {"available", "unavailable", "unknown"}:
            raise ValueError(f"record {index} has unsupported availability")

        price = item.get("price")
        if price is None:
            if quality != "unknown":
                raise ValueError(f"record {index} without a canonical price must have source quality unknown")
            amount = basis = price_observed = None
        else:
            if not isinstance(price, Mapping):
                raise ValueError(f"record {index} price must be an object or null")
            amount = price.get("amount_rub")
            basis = price.get("basis")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError(f"record {index} price.amount_rub must be a non-negative integer")
            if basis not in {"asking_total", "conditional_total"}:
                raise ValueError(f"record {index} price.basis is unsupported")
            price_observed = _timestamp(price.get("observed_at"), f"record {index} price.observed_at")
            if quality != "dated_asking":
                raise ValueError(f"record {index} with a price must have source quality dated_asking")

        key = (source_id, source["source_record_id"])
        if key in seen:
            raise ValueError(f"duplicate source record ID in batch: {key[1]}")
        seen.add(key)
        normalized = {field: item.get(field) for field in _OFFER_FIELDS}
        normalized["facts"] = _validate_facts(
            item.get("facts"), source, record_index=index, test_only=test_only
        )
        normalized["source"] = {field: source.get(field) for field in _SOURCE_FIELDS if field in source}
        normalized["source"]["rights_status"] = "cleared"
        normalized["_db"] = {
            "amount_rub": amount,
            "price_basis": basis,
            "price_observed_at": price_observed,
            "source_quality": quality,
            "source_record_id": source["source_record_id"],
            "source_fields": dict(raw_fields),
            "observed_at": _timestamp(source.get("observed_at"), f"record {index} source.observed_at"),
            "source_updated_at": _timestamp(source.get("source_updated_at"), f"record {index} source.source_updated_at", optional=True),
        }
        if test_only:
            normalized["source"]["name"] = f"SYNTHETIC INTEGRATION TEST ONLY — {source.get('name', 'test source')}"
            normalized["source"]["test_only"] = True
        validated.append(normalized)
    if not validated:
        raise ValueError("at least one canonical offer is required")
    return validated


def _canonical_digest(records: list[dict[str, Any]], rights: Mapping[str, Any]) -> str:
    payload = {"records": records, "rights": dict(rights)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _connect(service: str) -> Any:
    if not _SERVICE_RE.fullmatch(service):
        raise ValueError("invalid libpq service name")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostGIS import requires psycopg; install backend/requirements-postgis.txt") from exc
    # A service name is the only accepted connection mechanism; no DSN or URL.
    return psycopg.connect(f"service={service} connect_timeout=3")


def _verify_test_target(connection: Any, service: str) -> None:
    if not service.startswith("property_test_"):
        raise ValueError("test-only writes require a property_test_* libpq service")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_schema(), host(inet_server_addr()), inet_server_port()")
        database, schema, host, port = cursor.fetchone()
    if database != "novostroy" or schema != service or host != "127.0.0.1" or port != 55439:
        raise ValueError("test-only service must resolve to its matching schema on local 127.0.0.1:55439")


def import_records(
    records: Iterable[Mapping[str, Any]],
    rights: Mapping[str, Any],
    *,
    service: str,
    commit: bool = False,
    test_only: bool = False,
) -> dict[str, Any]:
    """Validate a batch, then optionally atomically upsert it through libpq service."""
    validated = validate_records(records, rights, test_only=test_only)
    digest = _canonical_digest(validated, rights)
    snapshot_id = f"property-{digest}"
    report = {
        "status": "dry_run" if not commit else "committed",
        "test_only": test_only,
        "record_count": len(validated),
        "snapshot_id": snapshot_id,
        "service": service,
    }
    if not commit:
        return report
    if service.startswith("property_test_") and not test_only:
        raise ValueError("property_test_* services require the explicit --test-only guard")

    connection = _connect(service)
    try:
        if test_only:
            _verify_test_target(connection, service)
        with connection:
            with connection.cursor() as cursor:
                complex_ids = sorted({record["complex_id"] for record in validated})
                cursor.execute(
                    "SELECT complex_id, rights_status FROM complexes WHERE complex_id = ANY(%s)",
                    (complex_ids,),
                )
                complex_rights = {row[0]: row[1] for row in cursor.fetchall()}
                missing = set(complex_ids) - set(complex_rights)
                if missing:
                    raise ValueError(f"missing pre-cleared complex rows: {', '.join(sorted(missing))}")
                uncleared = {key for key, value in complex_rights.items() if value != "cleared"}
                if uncleared:
                    raise ValueError(f"complex rights are not cleared: {', '.join(sorted(uncleared))}")

                now = datetime.now().astimezone()
                cursor.execute(
                    """INSERT INTO source_manifests
                           (source_id, source_url, access_basis, permitted_display,
                            permitted_storage, observed_at, source_updated_at, sha256, manifest_json)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (source_id) DO UPDATE SET
                           source_url=EXCLUDED.source_url,
                           access_basis=EXCLUDED.access_basis,
                           permitted_display=EXCLUDED.permitted_display,
                           permitted_storage=EXCLUDED.permitted_storage,
                           observed_at=EXCLUDED.observed_at,
                           source_updated_at=EXCLUDED.source_updated_at,
                           sha256=EXCLUDED.sha256,
                           manifest_json=EXCLUDED.manifest_json""",
                    (
                        rights["source_id"], rights.get("source_url"), rights["access_basis"],
                        not test_only, True, now, rights.get("source_updated_at"), digest,
                        json.dumps(
                            {
                                **dict(rights),
                                "permitted_storage": True,
                                "permitted_display": not test_only,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                cursor.execute(
                    "INSERT INTO catalogue_snapshots(snapshot_id,captured_at,source_manifest) VALUES (%s,%s,%s::jsonb) ON CONFLICT (snapshot_id) DO NOTHING",
                    (snapshot_id, now, json.dumps({"source_id": rights["source_id"], "sha256": digest}, sort_keys=True)),
                )
                for record in validated:
                    source = record["source"]
                    db = record["_db"]
                    display = {key: record.get(key) for key in _OFFER_FIELDS}
                    display["data_mode"] = "real"
                    cursor.execute(
                        """INSERT INTO offers
                               (offer_id, complex_id, district_id, amount_rub, price_basis,
                                price_observed_at, record_mode, rights_status, source_quality,
                                source_record_id, source_fields, display_json)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'cleared',%s,%s,%s::jsonb,%s::jsonb)
                           ON CONFLICT (offer_id) DO UPDATE SET
                               complex_id=EXCLUDED.complex_id,
                               district_id=EXCLUDED.district_id,
                               amount_rub=EXCLUDED.amount_rub,
                               price_basis=EXCLUDED.price_basis,
                               price_observed_at=EXCLUDED.price_observed_at,
                               record_mode=EXCLUDED.record_mode,
                               rights_status=EXCLUDED.rights_status,
                               source_quality=EXCLUDED.source_quality,
                               source_record_id=EXCLUDED.source_record_id,
                               source_fields=EXCLUDED.source_fields,
                               display_json=EXCLUDED.display_json
                           WHERE offers.source_record_id=EXCLUDED.source_record_id
                             AND offers.display_json->'source'->>'source_id' = EXCLUDED.display_json->'source'->>'source_id'""",
                        (
                            record["offer_id"], record["complex_id"], record.get("district_id"),
                            db["amount_rub"], db["price_basis"], db["price_observed_at"],
                            source["record_mode"], db["source_quality"], db["source_record_id"],
                            json.dumps(db["source_fields"], ensure_ascii=False, sort_keys=True),
                            json.dumps(display, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(f"offer_id collision from another source: {record['offer_id']}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return report


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"line {line_number} must be a JSON object")
        records.append(item)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Canonical offer JSONL from the local adapter")
    parser.add_argument("--rights-json", type=Path, required=True, help="Explicit source permission manifest")
    parser.add_argument("--service", default=os.environ.get("NOVOSTROY_PG_SERVICE", "novostroy"), help="libpq service name only")
    parser.add_argument("--test-only", action="store_true", help="Permit synthetic fixtures only in property_test_* schema/service")
    parser.add_argument("--commit", action="store_true", help="Commit a validated batch; otherwise perform a dry run")
    args = parser.parse_args(argv)
    if "=" in args.service or "://" in args.service:
        parser.error("--service accepts a libpq service name, not a DSN or URL")
    try:
        records = _read_jsonl(args.input)
        rights = json.loads(args.rights_json.read_text(encoding="utf-8"))
        if not isinstance(rights, dict):
            raise ValueError("rights manifest must be a JSON object")
        report = import_records(records, rights, service=args.service, commit=args.commit, test_only=args.test_only)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
