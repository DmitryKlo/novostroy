"""Concurrency controls for the single local nearby geodata worker."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from backend import nearby


@pytest.fixture(autouse=True)
def _clear_worker_response_state():
    with nearby._RESPONSE_CACHE_LOCK:
        nearby._RESPONSE_CACHE.clear()
        nearby._WORKER_INFLIGHT.clear()
    yield
    with nearby._RESPONSE_CACHE_LOCK:
        nearby._RESPONSE_CACHE.clear()
        nearby._WORKER_INFLIGHT.clear()


def _fake_exchange(counter: list[int], lock: threading.Lock, delay: float = 0.02):
    def exchange(request: dict[str, Any], _deadline: float) -> dict[str, Any]:
        with lock:
            counter[0] += 1
            call_number = counter[0]
        time.sleep(delay)
        return {"status": "ok", "request": request, "call_number": call_number,
                "items": [1, 2]}

    return exchange


def test_identical_parallel_requests_share_worker_result_and_return_copies(monkeypatch) -> None:
    counter = [0]
    lock = threading.Lock()
    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    monkeypatch.setattr(nearby, "_worker_exchange_locked", _fake_exchange(counter, lock, 0.08))
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _index: nearby._worker_request(request), range(6)))

    assert counter == [1]
    assert all(result["status"] == "ok" for result in results)
    assert all(result["call_number"] == 1 for result in results)
    results[0]["items"].append(3)
    assert nearby._worker_request(request)["items"] == [1, 2]
    assert counter == [1]


def test_distinct_parallel_requests_wait_in_bounded_admission_without_busy_failure(monkeypatch) -> None:
    counter = [0]
    lock = threading.Lock()
    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    # Keep the old admission wait value so this test specifically verifies
    # that the larger bounded slot pool, not a long arbitrary queue wait, is
    # what accepts this browser-sized concurrent burst.
    monkeypatch.setattr(nearby, "WORKER_ADMISSION_WAIT_SECONDS", 0.1)
    # Hold the serialized worker beyond the former 100 ms admission wait so
    # a three-slot semaphore would deterministically reject later callers.
    monkeypatch.setattr(nearby, "_worker_exchange_locked", _fake_exchange(counter, lock, 0.15))

    requests = [{"kind": "point", "point_id": f"point-{index}", "lon": 37.5,
                 "lat": 55.6, "education": "all"} for index in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(nearby._worker_request, requests))

    assert counter == [6]
    assert [result["request"]["point_id"] for result in results] == [
        f"point-{index}" for index in range(6)]


def test_response_cache_expires_and_invalidates_on_source_signature(monkeypatch) -> None:
    counter = [0]
    lock = threading.Lock()
    signature = ["snapshot-a"]
    monkeypatch.setattr(nearby, "_input_signature", lambda: tuple(signature))
    monkeypatch.setattr(nearby, "_worker_exchange_locked", _fake_exchange(counter, lock, 0))
    request = {"kind": "viewport", "bbox": [37.5, 55.6, 37.51, 55.61],
               "layers": ["metro"], "education": "all", "limit": 10}

    nearby._worker_request(request)
    nearby._worker_request(request)
    assert counter == [1]

    monkeypatch.setattr(nearby, "WORKER_RESPONSE_CACHE_TTL_SECONDS", 0.001)
    signature[0] = "snapshot-b"
    nearby._worker_request(request)
    assert counter == [2]

    time.sleep(0.01)
    nearby._worker_request(request)
    assert counter == [3]


def test_failed_worker_result_is_not_cached(monkeypatch) -> None:
    calls = [0]

    def exchange(_request: dict[str, Any], _deadline: float) -> dict[str, Any]:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("sanitized worker failure")
        return {"status": "ok"}

    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    monkeypatch.setattr(nearby, "_worker_exchange_locked", exchange)
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}

    with pytest.raises(RuntimeError, match="sanitized worker failure"):
        nearby._worker_request(request)
    assert nearby._worker_request(request) == {"status": "ok"}
    assert calls == [2]


def test_parallel_waiters_share_failure_and_next_request_retries(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = [0]

    def failing_exchange(_request: dict[str, Any], _deadline: float) -> dict[str, Any]:
        calls[0] += 1
        started.set()
        assert release.wait(timeout=1.0)
        raise RuntimeError("sanitized worker failure")

    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    monkeypatch.setattr(nearby, "_worker_exchange_locked", failing_exchange)
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}
    key = nearby._worker_cache_key(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(nearby._worker_request, request)]
        assert started.wait(timeout=1.0)
        futures.extend(pool.submit(nearby._worker_request, request) for _ in range(3))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with nearby._RESPONSE_CACHE_LOCK:
                flight = nearby._WORKER_INFLIGHT.get(key)
                waiter_count = flight.waiters if flight else 0
            if waiter_count == 3:
                break
            time.sleep(0.001)
        assert waiter_count == 3
        release.set()
        messages = []
        for future in futures:
            with pytest.raises(RuntimeError, match="sanitized worker failure") as error:
                future.result(timeout=1.0)
            messages.append(str(error.value))

    assert calls == [1]
    assert messages == ["sanitized worker failure"] * 4

    monkeypatch.setattr(nearby, "_worker_exchange_locked",
                        lambda _request, _deadline: {"status": "ok"})
    assert nearby._worker_request(request) == {"status": "ok"}


def test_saturated_admission_fails_after_short_bounded_wait(monkeypatch) -> None:
    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    monkeypatch.setattr(nearby, "WORKER_ADMISSION_WAIT_SECONDS", 0.02)
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}
    acquired = 0
    try:
        for _ in range(nearby.WORKER_MAX_ADMITTED_REQUESTS):
            assert nearby._QUEUE.acquire(blocking=False)
            acquired += 1
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="queue is busy"):
            nearby._worker_request(request)
        assert time.monotonic() - started < 0.25
    finally:
        for _ in range(acquired):
            nearby._QUEUE.release()


def test_cache_hit_is_served_before_queue_admission(monkeypatch) -> None:
    calls = [0]
    lock = threading.Lock()
    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    monkeypatch.setattr(nearby, "_worker_exchange_locked", _fake_exchange(calls, lock, 0))
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}
    expected = nearby._worker_request(request)
    acquired = 0
    try:
        for _ in range(nearby.WORKER_MAX_ADMITTED_REQUESTS):
            assert nearby._QUEUE.acquire(blocking=False)
            acquired += 1
        assert nearby._worker_request(request) == expected
        assert calls == [1]
    finally:
        for _ in range(acquired):
            nearby._QUEUE.release()


def test_singleflight_waiter_limit_rejects_excess_duplicate_waiters(monkeypatch) -> None:
    from concurrent.futures import Future

    monkeypatch.setattr(nearby, "_input_signature", lambda: ("fixed-source-snapshot",))
    request = {"kind": "park", "lon": 37.5, "lat": 55.6, "point_id": "point-x"}
    key = nearby._worker_cache_key(request)
    flight = nearby._WorkerFlight(Future(), waiters=nearby.MAX_SINGLEFLIGHT_WAITERS)
    with nearby._RESPONSE_CACHE_LOCK:
        nearby._WORKER_INFLIGHT[key] = flight
    with pytest.raises(RuntimeError, match="queue is busy"):
        nearby._worker_request(request)
