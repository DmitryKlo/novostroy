"""Run a synthetic-only adapter -> PostGIS -> FastAPI acceptance slice.

All test data is committed only to a new `property_test_*` schema and a
service whose search_path points to that schema. The main `novostroy` service
is used only to create that new schema. No public application tables are
created or seeded.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from sources.property.adapters import parse_developer_yrl
from sources.property.importer import import_records, validate_records


ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / ".runtime"
RUN_TOKEN = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
SERVICE = f"property_test_luna_{RUN_TOKEN}"
SCHEMA = SERVICE
SERVICE_FILE = RUNTIME / f"{SERVICE}.pg_service.conf"
PORT = 55439


def _run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT, env=env)


def _source_rights() -> dict[str, str]:
    return {
        "access_basis": "isolated_integration_test_only",
        "local_storage": "test_only_schema",
        "public_display": "test_client_only",
        "redistribution": "not_applicable",
        "rights_status": "cleared",
    }


def _parse_test_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rights = json.loads(
        (ROOT / "sources/property/tests/fixtures/rights.integration-test-only.json").read_text(encoding="utf-8")
    )
    observed = datetime.now(timezone.utc).replace(microsecond=0)
    records = parse_developer_yrl(
        ROOT / "sources/property/tests/fixtures/developer_feed.integration.synthetic.xml",
        source_id=rights["source_id"],
        rights=_source_rights(),
        data_mode="real",
        observed_at=observed.isoformat(),
    )
    for record in records:
        record["source"]["name"] = "SYNTHETIC INTEGRATION TEST ONLY — never a real listing"
    stale = next(record for record in records if "STALE" in record["offer_id"])
    stale["price"]["observed_at"] = (observed - timedelta(hours=48)).isoformat()
    return records, rights


def _create_service_file() -> None:
    if not re.fullmatch(r"property_test_[a-z0-9_]+", SERVICE):
        raise RuntimeError("unsafe test service name")
    if SERVICE_FILE.exists():
        raise RuntimeError(f"refusing to reuse existing test service file: {SERVICE_FILE}")
    config = (
        "[novostroy]\n"
        "host=127.0.0.1\n"
        f"port={PORT}\n"
        "dbname=novostroy\n"
        "user=pirojban\n"
        "connect_timeout=3\n"
        f"passfile={RUNTIME / 'postgis.pgpass'}\n\n"
        f"[{SERVICE}]\n"
        "host=127.0.0.1\n"
        f"port={PORT}\n"
        "dbname=novostroy\n"
        "user=pirojban\n"
        "connect_timeout=3\n"
        f"passfile={RUNTIME / 'postgis.pgpass'}\n"
        f"options=-csearch_path={SCHEMA},public\n"
    )
    fd = os.open(SERVICE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(config)
        output.flush()
        os.fsync(output.fileno())


def _create_schema() -> None:
    with psycopg.connect("service=novostroy connect_timeout=3") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (SCHEMA,))
            if cursor.fetchone():
                raise RuntimeError(f"refusing to overwrite existing test schema {SCHEMA}")
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))


def _seed_dimensions(env: dict[str, str]) -> None:
    psql = str(RUNTIME / "postgis-env" / "bin" / "psql")
    _run(
        [psql, "-X", "-v", "ON_ERROR_STOP=1", f"service={SERVICE}", "-f", "pipeline/postgis_schema.sql"],
        env=env,
    )
    district = {
        "district_id": "SYNTHETIC-INTEGRATION-DISTRICT-001",
        "name": "SYNTHETIC TEST DISTRICT — fictional",
        "boundary_version": "SYNTHETIC-TEST-ONLY-v1",
    }
    complex_id = "SYNTHETIC-INTEGRATION-COMPLEX-001"
    district_display = {**district, "geometry": None}
    complex_display = {
        "complex_id": complex_id,
        "name": "SYNTHETIC TEST COMPLEX — not a property",
        "district_id": district["district_id"],
        "location": [37.6173, 55.7558],
        "developer": "SYNTHETIC TEST ONLY",
        "model_quality": "none",
        "source": {
            "name": "SYNTHETIC INTEGRATION TEST ONLY",
            "quality": "illustrative",
            "record_mode": "demo",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "rights_status": "demo",
        },
    }
    with psycopg.connect(f"service={SERVICE} connect_timeout=3") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO districts(district_id,boundary_version,name,geometry,display_json)
                   VALUES (%s,%s,%s,ST_Multi(ST_GeomFromText(%s,4326)),%s::jsonb)""",
                (
                    district["district_id"], district["boundary_version"], district["name"],
                    "POLYGON((37.5 55.6,37.8 55.6,37.8 55.9,37.5 55.9,37.5 55.6))",
                    json.dumps(district_display, ensure_ascii=False),
                ),
            )
            cursor.execute(
                """INSERT INTO complexes(complex_id,district_id,location,rights_status,display_json)
                   VALUES (%s,%s,ST_SetSRID(ST_MakePoint(37.6173,55.7558),4326),'cleared',%s::jsonb)""",
                (complex_id, district["district_id"], json.dumps(complex_display, ensure_ascii=False)),
            )


def _insert_adversarial_test_rows(records: list[dict[str, Any]]) -> None:
    fresh = next(record for record in records if "FRESH" in record["offer_id"])
    unknown_price = next(record for record in records if "UNKNOWN-PRICE" in record["offer_id"])
    with psycopg.connect(f"service={SERVICE} connect_timeout=3") as connection:
        with connection.cursor() as cursor:
            # Defense-in-depth fixture: the importer itself refuses this row.
            uncleared = copy.deepcopy(fresh)
            uncleared["offer_id"] = "SYNTHETIC-INTEGRATION-UNCLEARED-004"
            uncleared["source"]["source_record_id"] = uncleared["offer_id"]
            uncleared["source"]["rights_status"] = "unknown"
            uncleared["source"]["source_rights"]["rights_status"] = "unknown"
            _insert_raw(cursor, uncleared, table_rights="unknown")

            # Deliberately inconsistent row tests that a budget filter cannot
            # treat missing amount as zero; the public importer rejects it.
            no_price = copy.deepcopy(unknown_price)
            no_price["offer_id"] = "SYNTHETIC-INTEGRATION-MISSING-PRICE-005"
            no_price["source"]["source_record_id"] = no_price["offer_id"]
            no_price["source"]["quality"] = "dated_asking"
            no_price["price"] = None
            _insert_raw(cursor, no_price, table_rights="cleared", table_quality="dated_asking", override_observed_at=datetime.now(timezone.utc))


def _insert_raw(
    cursor: Any,
    record: dict[str, Any],
    *,
    table_rights: str,
    table_quality: str | None = None,
    override_observed_at: datetime | None = None,
) -> None:
    source = record["source"]
    price = record.get("price")
    amount = price.get("amount_rub") if isinstance(price, dict) else None
    basis = price.get("basis") if isinstance(price, dict) else None
    price_at = price.get("observed_at") if isinstance(price, dict) else None
    display = {key: record.get(key) for key in (
        "offer_id", "unit_id", "building_id", "section_id", "floor_id", "complex_id",
        "district_id", "rooms", "area_sqm", "floor", "price", "source", "availability",
    )}
    display["data_mode"] = "real"
    cursor.execute(
        """INSERT INTO offers(offer_id,complex_id,district_id,amount_rub,price_basis,
                              price_observed_at,record_mode,rights_status,source_quality,
                              source_record_id,source_fields,display_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
        (
            record["offer_id"], record["complex_id"], record.get("district_id"), amount, basis,
            override_observed_at or price_at, source["record_mode"], table_rights,
            table_quality or source["quality"], source["source_record_id"],
            json.dumps(source.get("source_fields", {}), ensure_ascii=False),
            json.dumps(display, ensure_ascii=False),
        ),
    )


def _exercise_api(env: dict[str, str], fresh_id: str) -> dict[str, Any]:
    previous = {key: os.environ.get(key) for key in (
        "PGSERVICEFILE", "NOVOSTROY_PG_SERVICE", "NOVOSTROY_REAL_STORE", "NOVOSTROY_DATA_MODE",
    )}
    os.environ.update(env)
    try:
        from fastapi.testclient import TestClient
        from backend.app import app

        with TestClient(app) as client:
            health = client.get("/v1/health")
            assert health.status_code == 200, health.text
            assert health.json()["data_mode"] == "real"
            assert health.json()["snapshot_id"].startswith("postgis-property-")

            matching = client.post("/v1/search", json={"filters": {"budget_max_rub": 20000000}})
            assert matching.status_code == 200, matching.text
            assert matching.json()["count"] == 1
            assert [item["offer_id"] for item in matching.json()["items"]] == [fresh_id]

            missing_price = client.post("/v1/search", json={"filters": {"budget_max_rub": 1}})
            assert missing_price.status_code == 200 and missing_price.json()["count"] == 0

            valid_but_unmatched_district = client.post(
                "/v1/search", json={"filters": {"district_ids": ["SYNTHETIC-INTEGRATION-DISTRICT-001"]}}
            )
            assert valid_but_unmatched_district.status_code == 200
            assert valid_but_unmatched_district.json()["count"] == 0

            unknown_district = client.post(
                "/v1/search", json={"filters": {"district_ids": ["SYNTHETIC-UNKNOWN-DISTRICT"]}}
            )
            assert unknown_district.status_code == 422
            assert unknown_district.json()["detail"]["code"] == "unknown_district"

            assert client.get(f"/v1/offers/{fresh_id}").status_code == 200
            assert client.get("/v1/offers/SYNTHETIC-INTEGRATION-STALE-002").status_code == 404
            assert client.get("/v1/offers/SYNTHETIC-INTEGRATION-UNCLEARED-004").status_code == 404
            assert client.get("/v1/offers/SYNTHETIC-INTEGRATION-MISSING-PRICE-005").status_code == 404
            return {
                "health_eligible_offer_count": health.json()["counts"]["offers"],
                "budget_match_count": matching.json()["count"],
                "unknown_price_budget_match_count": missing_price.json()["count"],
                "known_district_match_count": valid_but_unmatched_district.json()["count"],
                "unknown_district_status": unknown_district.status_code,
                "stale_uncleared_missing_price_detail_status": 404,
            }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    records, rights = _parse_test_records()
    demo_rights = json.loads((ROOT / "sources/property/tests/fixtures/rights.synthetic.json").read_text(encoding="utf-8"))
    demo_records = parse_developer_yrl(
        ROOT / "sources/property/tests/fixtures/developer_feed.synthetic.xml",
        source_id="synthetic_demo",
        rights=demo_rights,
        data_mode="demo",
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        validate_records(demo_records, rights, test_only=True)
    except ValueError as exc:
        demo_rejection = str(exc)
    else:
        raise AssertionError("demo adapter output unexpectedly passed real-mode import validation")

    uncleared = copy.deepcopy(records[0])
    uncleared["source"]["rights_status"] = "unknown"
    uncleared["source"]["source_rights"]["rights_status"] = "unknown"
    try:
        validate_records([uncleared], rights, test_only=True)
    except ValueError as exc:
        rights_rejection = str(exc)
    else:
        raise AssertionError("uncleared record unexpectedly passed import validation")

    _create_service_file()
    os.environ["PGSERVICEFILE"] = str(SERVICE_FILE)
    _create_schema()
    test_env = dict(os.environ)
    test_env["PGSERVICEFILE"] = str(SERVICE_FILE)
    _seed_dimensions(test_env)
    report = import_records(records, rights, service=SERVICE, commit=True, test_only=True)
    _insert_adversarial_test_rows(records)
    api_result = _exercise_api(
        {
            "PGSERVICEFILE": str(SERVICE_FILE),
            "NOVOSTROY_PG_SERVICE": SERVICE,
            "NOVOSTROY_REAL_STORE": "postgis",
            "NOVOSTROY_DATA_MODE": "real",
        },
        next(record["offer_id"] for record in records if "FRESH" in record["offer_id"]),
    )
    with psycopg.connect(f"service={SERVICE} connect_timeout=3") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM offers")
            stored_rows = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM offers WHERE rights_status='cleared' AND source_quality='dated_asking' AND price_observed_at >= now() - interval '24 hours'")
            fresh_rows = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM catalogue_snapshots")
            snapshot_rows = cursor.fetchone()[0]
    print(json.dumps({
        "result": "PASS",
        "synthetic_only": True,
        "schema": SCHEMA,
        "service": SERVICE,
        "test_schema_rows": stored_rows,
        "fresh_cleared_dated_rows_in_test_schema": fresh_rows,
        "immutable_snapshots": snapshot_rows,
        "importer": report,
        "api": api_result,
        "demo_import_rejection": demo_rejection,
        "uncleared_import_rejection": rights_rejection,
        "service_file": str(SERVICE_FILE),
        "cleanup": "schema and service file intentionally retained only under ignored local .runtime for inspection; public novostroy service not seeded",
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
