"""Optional read-only PostGIS catalogue adapter; requires an explicit PG service.

The local prototype defaults to the empty real catalogue. A production operator
can configure a libpq service entry outside this repository and opt in using
NOVOSTROY_REAL_STORE=postgis. This module never imports the demo catalogue.
"""

from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def load_postgis_catalogue() -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostGIS mode requires psycopg; install backend/requirements-postgis.txt") from exc

    service = os.environ.get("NOVOSTROY_PG_SERVICE", "novostroy")
    if not service.replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("Invalid libpq service name")
    with psycopg.connect(f"service={service} connect_timeout=3") as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SELECT snapshot_id, captured_at FROM catalogue_snapshots ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1")
            latest = cursor.fetchone()
            cursor.execute("SELECT display_json FROM districts ORDER BY district_id")
            districts = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT display_json FROM complexes WHERE rights_status = 'cleared' ORDER BY complex_id")
            complexes = [row[0] for row in cursor.fetchall()]
            cursor.execute("""
                SELECT display_json FROM offers
                WHERE rights_status = 'cleared'
                  AND source_quality = 'dated_asking'
                  AND record_mode IN ('developer_offer', 'asking_price')
                  AND price_observed_at >= now() - interval '24 hours'
                  AND price_observed_at <= now()
                ORDER BY offer_id
            """)
            offers = [row[0] for row in cursor.fetchall()]

    allowed_complex_ids = {item["complex_id"] for item in complexes}
    offers = [item for item in offers if item.get("complex_id") in allowed_complex_ids
              and item.get("source", {}).get("record_mode") in {"developer_offer", "asking_price"}
              and item.get("source", {}).get("quality") == "dated_asking"
              and item.get("source", {}).get("rights_status") == "cleared"]
    payload = {"districts": districts, "complexes": complexes, "offers": offers}
    content_digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    import_id = latest[0] if latest is not None else "unversioned"
    captured_at = latest[1].isoformat() if latest is not None else datetime.now(timezone.utc).isoformat()
    return {
        "version": "1", "snapshot_id": f"postgis-{import_id}-{content_digest}",
        "snapshot_at": captured_at, "data_mode": "real",
        "districts": districts, "complexes": complexes, "offers": offers,
    }
