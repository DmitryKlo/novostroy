# Product event analytics

## Purpose and current status

This module measures a small, allowlisted set of user-interface actions and the elapsed time from a search submission to a later offer view or comparison. It does not identify users, inspect query text, or measure purchases. A `search → offer_view` or `search → compare` pair is an interaction timing sample, **not a conversion-to-sale metric**.

No real user metrics have been collected yet. Unit-test events use an isolated temporary SQLite database and are synthetic test data; they must not be reported as product usage. Until the backend wiring is deployed and real events arrive, `/v1/metrics` should report empty counts and insufficient timing data.

## Event and session contract

The only accepted event types are `search`, `map_view`, `offer_view`, `compare`, and `recommendation_view`. The HTTP payload should contain only:

```json
{"event_type":"search","session_id":"<random-url-safe-token>"}
```

Generate a 32-byte cryptographically random URL-safe token for one in-memory application session. A web client must keep it in JavaScript memory, not cookies, local storage, or a cross-site identifier. A native client should not derive it from a device, account, or installation ID. The server hashes this token with SHA-256 before storage. A missing token still contributes to event counts but cannot contribute to time-to-result metrics.

Do not send or store raw query text, filters, offer or complex IDs, properties, device data, referrers, or user identifiers. The analytics store intentionally has no API for event properties or client-supplied timestamps. The backend owns timestamps at event receipt.

Backend call shape:

```python
store = AnalyticsStore(ROOT / "data/runtime/analytics.sqlite3")
store.record_event(event_type=request.event_type, session_token=request.session_id)
payload = store.metrics()
```

The store validates the event allowlist and random-token shape; it persists only event type, server UTC time, and the token digest. No event IDs or digests appear in the metrics response.

## Retention and limits

The default store keeps events for at most 30 days and at most 100,000 rows. It deletes expired rows on writes and metric reads, then removes oldest rows if the hard cap is exceeded. SQLite remains under `data/runtime/`, which is excluded from source control. The module uses only the Python standard library.

## Metric definitions

`event_counts` reports accepted events in the rolling retention window. Funnel timing includes only searches with a session token. For each search, the matching window ends at the next search in that same session. Within that window, the first `offer_view` and first `compare` are each paired independently with the search; later duplicate views of the same type are not paired again to it. This prevents one result from being attributed to several earlier searches.

For `search_to_offer_view` and `search_to_compare`, `starts` is the number of session-linked searches, `paired_events` is the number of observed pairs, and `unmatched_starts` is the remainder. Median and P90 elapsed seconds are returned only with at least five pairs; P90 uses the nearest-rank definition. Below that threshold, both quantiles are `null` and status is `insufficient_data`. These are descriptive timings among observed pairs; no sale, purchase, or user-conversion claim is made.

## Validation

```bash
rtk python -m py_compile analytics/store.py analytics/tests/test_store.py
rtk pytest -q analytics/tests/test_store.py
```

Tests use temporary SQLite files and synthetic event sequences. They check token hashing, allowlisting, matching rules, quantile suppression, TTL, and the row cap.
