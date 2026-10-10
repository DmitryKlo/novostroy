from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from analytics.store import AnalyticsStore, create_session_token


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_random_session_token_is_url_safe_and_high_entropy_shape() -> None:
    first = create_session_token()
    second = create_session_token()

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    assert first != second


def test_only_allowlisted_events_and_random_token_are_persisted(tmp_path) -> None:
    database = tmp_path / "analytics.sqlite3"
    token = create_session_token()
    store = AnalyticsStore(database)

    assert store.record_event("search", token)
    assert store.record_event("map_view", None)
    with pytest.raises(ValueError, match="event_type"):
        store.record_event("sale", token)
    with pytest.raises(ValueError, match="event_type"):
        store.record_event({"query": "private"}, token)
    with pytest.raises(ValueError, match="random URL-safe token"):
        store.record_event("search", "abc123")

    blob = database.read_bytes()
    assert token.encode("ascii") not in blob
    assert hashlib.sha256(token.encode("ascii")).hexdigest().encode("ascii") in blob
    assert b"sale" not in blob
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(analytics_events)")}
    assert columns == {"event_id", "occurred_at_us", "event_type", "session_hash"}
    metrics = store.metrics()
    assert metrics["event_counts"] == {"map_view": 1, "search": 1}
    assert metrics["session_linked_searches"] == 1


def test_no_event_properties_or_caller_timestamp_are_part_of_the_api(tmp_path) -> None:
    store = AnalyticsStore(tmp_path / "analytics.sqlite3")
    token = create_session_token()

    with pytest.raises(TypeError):
        store.record_event("search", token, query="купить двушку")
    with pytest.raises(TypeError):
        store.record_event("search", token, occurred_at=datetime.now(timezone.utc))


def test_search_to_offer_and_compare_quantiles_require_five_pairs(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    store = AnalyticsStore(tmp_path / "analytics.sqlite3", clock=clock)
    token = create_session_token()

    for delay in (1, 2, 3, 4, 5):
        store.record_event("search", token)
        clock.value += timedelta(seconds=delay)
        store.record_event("offer_view", token)
        clock.value += timedelta(seconds=1)
        store.record_event("compare", token)
    store.record_event("search", create_session_token())

    metrics = store.metrics()

    offer = metrics["time_to_result"]["search_to_offer_view"]
    compare = metrics["time_to_result"]["search_to_compare"]
    assert metrics["session_linked_searches"] == 6
    assert metrics["event_counts"] == {"compare": 5, "offer_view": 5, "search": 6}
    assert offer == {
        "starts": 6,
        "paired_events": 5,
        "unmatched_starts": 1,
        "status": "ready",
        "median_s": 3.0,
        "p90_s": 5.0,
    }
    assert compare["paired_events"] == 5
    assert compare["median_s"] == 4.0
    assert compare["p90_s"] == 6.0
    assert "sale" not in metrics["time_to_result"]
    assert "not a conversion-to-sale metric" in metrics["interpretation"]


def test_quantiles_are_suppressed_when_sample_is_too_small(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    store = AnalyticsStore(tmp_path / "analytics.sqlite3", clock=clock)
    token = create_session_token()
    store.record_event("search", token)
    clock.value += timedelta(seconds=12.3456)
    store.record_event("offer_view", token)

    result = store.metrics()["time_to_result"]["search_to_offer_view"]

    assert result["paired_events"] == 1
    assert result["status"] == "insufficient_data"
    assert result["median_s"] is None and result["p90_s"] is None


def test_searches_only_match_results_before_the_next_search(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    store = AnalyticsStore(tmp_path / "analytics.sqlite3", minimum_latency_samples=1, clock=clock)
    token = create_session_token()
    store.record_event("search", token)
    clock.value += timedelta(seconds=2)
    store.record_event("search", token)
    clock.value += timedelta(seconds=3)
    store.record_event("offer_view", token)

    result = store.metrics()["time_to_result"]["search_to_offer_view"]

    assert result["starts"] == 2
    assert result["paired_events"] == 1
    assert result["unmatched_starts"] == 1
    assert result["median_s"] == 3.0


def test_ttl_and_hard_row_cap_bound_sqlite_data(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    database = tmp_path / "analytics.sqlite3"
    store = AnalyticsStore(database, retention_days=30, max_events=3, clock=clock)

    for _ in range(5):
        store.record_event("map_view")
        clock.value += timedelta(seconds=1)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 3
    assert store.metrics()["event_counts"] == {"map_view": 3}

    clock.value += timedelta(days=31)
    assert store.metrics()["event_counts"] == {}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0] == 0


def test_naive_clock_and_invalid_bounds_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        AnalyticsStore(tmp_path / "bad.sqlite3", retention_days=31)
    with pytest.raises(ValueError, match="retention_days"):
        AnalyticsStore(tmp_path / "bad-fraction.sqlite3", retention_days=1.5)
    with pytest.raises(ValueError, match="max_events"):
        AnalyticsStore(tmp_path / "bad2.sqlite3", max_events=0)
    with pytest.raises(ValueError, match="max_events"):
        AnalyticsStore(tmp_path / "bad-fraction2.sqlite3", max_events=1.5)
    store = AnalyticsStore(tmp_path / "bad3.sqlite3", clock=lambda: datetime(2026, 10, 9))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record_event("search")
