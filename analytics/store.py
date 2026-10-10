"""Bounded, privacy-limited event aggregation for local product analytics.

Only allowlisted event names and a SHA-256 digest of a high-entropy random
session token are persisted. The caller must never pass query text, offer IDs,
properties, device identifiers, or client-supplied timestamps.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any


EVENT_TYPES = frozenset(
    {"search", "map_view", "offer_view", "compare", "recommendation_view"}
)
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_EVENTS = 100_000
DEFAULT_MIN_LATENCY_SAMPLES = 5
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,80}\Z", re.ASCII)
_MICROSECONDS_PER_SECOND = 1_000_000


def create_session_token() -> str:
    """Create a random, URL-safe session token with 256 bits of entropy.

    Keep it in application memory for one app session. Do not derive it from
    an account, device, browser, site, or user identifier.
    """
    return secrets.token_urlsafe(32)


def _timestamp_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * _MICROSECONDS_PER_SECOND)


def _validate_event_type(event_type: str) -> str:
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of: {', '.join(sorted(EVENT_TYPES))}")
    return event_type


def _session_digest(session_token: str | None) -> str | None:
    if session_token is None:
        return None
    if not isinstance(session_token, str) or not _TOKEN_PATTERN.fullmatch(session_token):
        raise ValueError("session_token must be a random URL-safe token of 32 to 80 characters")
    return hashlib.sha256(session_token.encode("ascii")).hexdigest()


def _percentile_nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


class AnalyticsStore:
    """SQLite store with 30-day TTL, a hard row cap, and aggregate-only output."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_events: int = DEFAULT_MAX_EVENTS,
        minimum_latency_samples: int = DEFAULT_MIN_LATENCY_SAMPLES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(retention_days) is not int or not 1 <= retention_days <= DEFAULT_RETENTION_DAYS:
            raise ValueError(f"retention_days must be between 1 and {DEFAULT_RETENTION_DAYS}")
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if type(minimum_latency_samples) is not int or minimum_latency_samples < 1:
            raise ValueError("minimum_latency_samples must be a positive integer")
        self.path = Path(path)
        self.retention_days = retention_days
        self.max_events = max_events
        self.minimum_latency_samples = minimum_latency_samples
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS analytics_events (
                    event_id INTEGER PRIMARY KEY,
                    occurred_at_us INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('search','map_view','offer_view','compare','recommendation_view')
                    ),
                    session_hash TEXT
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS analytics_events_time_idx "
                "ON analytics_events(occurred_at_us)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS analytics_events_session_time_idx "
                "ON analytics_events(session_hash, occurred_at_us, event_id)"
            )

    def _prune(self, connection: sqlite3.Connection, now_us: int) -> None:
        cutoff_us = now_us - self.retention_days * 24 * 60 * 60 * _MICROSECONDS_PER_SECOND
        connection.execute("DELETE FROM analytics_events WHERE occurred_at_us < ?", (cutoff_us,))
        connection.execute(
            """DELETE FROM analytics_events
               WHERE event_id IN (
                   SELECT event_id FROM analytics_events
                   ORDER BY occurred_at_us DESC, event_id DESC
                   LIMIT -1 OFFSET ?
               )""",
            (self.max_events,),
        )

    def record_event(
        self,
        event_type: str,
        session_token: str | None = None,
    ) -> bool:
        """Persist one allowlisted event; no properties or caller time are accepted."""
        clean_type = _validate_event_type(event_type)
        digest = _session_digest(session_token)
        now_us = _timestamp_us(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, now_us)
            connection.execute(
                "INSERT INTO analytics_events(occurred_at_us,event_type,session_hash) VALUES (?,?,?)",
                (now_us, clean_type, digest),
            )
            self._prune(connection, now_us)
        return True

    def metrics(self) -> dict[str, Any]:
        """Return counts and paired search-to-result timing, never sale conversion."""
        now_us = _timestamp_us(self._clock())
        cutoff_us = now_us - self.retention_days * 24 * 60 * 60 * _MICROSECONDS_PER_SECOND
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, now_us)
            event_counts = dict(
                connection.execute(
                    "SELECT event_type, COUNT(*) FROM analytics_events "
                    "WHERE occurred_at_us >= ? AND occurred_at_us <= ? GROUP BY event_type",
                    (cutoff_us, now_us),
                ).fetchall()
            )
            rows = connection.execute(
                "SELECT session_hash,event_type,occurred_at_us,event_id "
                "FROM analytics_events WHERE session_hash IS NOT NULL "
                "AND occurred_at_us >= ? AND occurred_at_us <= ? "
                "ORDER BY session_hash,occurred_at_us,event_id",
                (cutoff_us, now_us),
            ).fetchall()

        durations: dict[str, list[float]] = {
            "search_to_offer_view": [],
            "search_to_compare": [],
        }
        session_starts = 0
        events_by_session: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for session_hash, event_type, occurred_at_us, _event_id in rows:
            events_by_session[session_hash].append((event_type, occurred_at_us))

        target_types = {
            "offer_view": "search_to_offer_view",
            "compare": "search_to_compare",
        }
        for session_events in events_by_session.values():
            search_positions = [
                index for index, (event_type, _time) in enumerate(session_events)
                if event_type == "search"
            ]
            session_starts += len(search_positions)
            for position, start_index in enumerate(search_positions):
                start_us = session_events[start_index][1]
                end_index = search_positions[position + 1] if position + 1 < len(search_positions) else len(session_events)
                found: set[str] = set()
                for event_type, event_us in session_events[start_index + 1:end_index]:
                    metric_name = target_types.get(event_type)
                    if metric_name is not None and metric_name not in found:
                        durations[metric_name].append((event_us - start_us) / _MICROSECONDS_PER_SECOND)
                        found.add(metric_name)

        return {
            "window_days": self.retention_days,
            "event_counts": event_counts,
            "session_linked_searches": session_starts,
            "time_to_result": {
                metric_name: self._latency_summary(values, session_starts)
                for metric_name, values in durations.items()
            },
            "minimum_latency_samples": self.minimum_latency_samples,
            "interpretation": (
                "Elapsed time between recorded UI events among session-linked searches; "
                "this is not a conversion-to-sale metric."
            ),
        }

    def _latency_summary(self, values: list[float], starts: int) -> dict[str, Any]:
        enough_data = len(values) >= self.minimum_latency_samples
        return {
            "starts": starts,
            "paired_events": len(values),
            "unmatched_starts": starts - len(values),
            "status": "ready" if enough_data else "insufficient_data",
            "median_s": round(median(values), 3) if enough_data else None,
            "p90_s": round(_percentile_nearest_rank(values, 0.9), 3) if enough_data else None,
        }
