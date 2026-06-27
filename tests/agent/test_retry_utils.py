"""Tests for Retry-After parsing in agent.retry_utils."""

from __future__ import annotations

import time

import pytest

from agent.retry_utils import (
    _FailureCounter,
    extract_retry_after_seconds,
    jittered_backoff,
)


def test_extract_retry_after_seconds_integer():
    assert extract_retry_after_seconds("42") == 42.0


def test_extract_retry_after_seconds_integer_with_whitespace():
    assert extract_retry_after_seconds("  7  ") == 7.0


def test_extract_retry_after_seconds_http_date():
    # 10 seconds in the future
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 10))
    parsed = extract_retry_after_seconds(future)
    assert parsed is not None
    assert 9.0 <= parsed <= 10.0


def test_extract_retry_after_seconds_http_date_past():
    past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 5))
    assert extract_retry_after_seconds(past) == 0.0


def test_extract_retry_after_seconds_caps_at_300():
    parsed = extract_retry_after_seconds("600")
    assert parsed is not None
    assert parsed == 300.0


def test_extract_retry_after_seconds_invalid_returns_none():
    assert extract_retry_after_seconds("not-a-number-or-date") is None
    assert extract_retry_after_seconds("") is None
    assert extract_retry_after_seconds(None) is None


def test_jittered_backoff_increases_with_attempt():
    base = jittered_backoff(1, base_delay=1.0, max_delay=120.0)
    later = jittered_backoff(5, base_delay=1.0, max_delay=120.0)
    assert base < later


def test_jittered_backoff_respects_max_delay():
    assert jittered_backoff(100, base_delay=1.0, max_delay=30.0) <= 45.0


# ── _FailureCounter tests ─────────────────────────────────────────────


class TestFailureCounter:
    """Tests for the thread-safe consecutive-failure counter."""

    def test_initial_state(self):
        counter = _FailureCounter(threshold=3)
        assert counter.count == 0
        assert counter.is_tripped is False
        assert counter.remaining_cooldown == 0.0

    def test_threshold_trip(self):
        counter = _FailureCounter(threshold=3)
        # First two failures: not tripped
        assert counter.trip() is False
        assert counter.trip() is False
        # Third failure: trip
        assert counter.trip() is True
        assert counter.count == 3

    def test_no_cooldown_means_trip_stays_true_after_crossing(self):
        """A counter with cooldown=0 keeps returning True past threshold."""
        counter = _FailureCounter(threshold=2)
        assert counter.trip() is False  # 1
        assert counter.trip() is True  # 2 — threshold crossed
        # Without cooldown, trip() still returns True because the
        # counter count >= threshold (there's no cooldown period to
        # "expire").
        assert counter.trip() is True  # 3
        # is_tripped is False because cooldown=0 means no cooldown period
        assert counter.is_tripped is False

    def test_reset_on_success(self):
        counter = _FailureCounter(threshold=2, cooldown=30.0)
        counter.trip()  # count=1
        counter.trip()  # count=2, tripped
        assert counter.is_tripped
        counter.succeeded()
        assert counter.count == 0
        assert counter.is_tripped is False

    def test_reset_explicit(self):
        counter = _FailureCounter(threshold=3)
        for _ in range(3):
            counter.trip()
        assert counter.count == 3
        counter.reset()
        assert counter.count == 0
        assert counter.is_tripped is False

    def test_cooldown_blocks_trip(self):
        counter = _FailureCounter(threshold=2, cooldown=60.0)
        counter.trip()  # 1
        assert counter.trip() is True  # 2 — tripped
        assert counter.is_tripped is True
        assert counter.remaining_cooldown > 0

    def test_cooldown_expiry(self, monkeypatch):
        """After cooldown expires, is_tripped returns False."""
        fake_time = [1000.0]

        def _time():
            return fake_time[0]

        monkeypatch.setattr(time, "time", _time)

        counter = _FailureCounter(threshold=2, cooldown=30.0)
        counter.trip()
        counter.trip()
        assert counter.is_tripped is True

        # Advance past cooldown
        fake_time[0] = 1040.0
        assert counter.is_tripped is False
        assert counter.remaining_cooldown == 0.0

    def test_succeeded_resets_cooldown_period(self, monkeypatch):
        fake_time = [1000.0]

        def _time():
            return fake_time[0]

        monkeypatch.setattr(time, "time", _time)

        counter = _FailureCounter(threshold=2, cooldown=30.0)
        counter.trip()
        counter.trip()
        assert counter.is_tripped is True

        # Succeed mid-cooldown
        fake_time[0] = 1010.0
        counter.succeeded()
        assert counter.is_tripped is False
        assert counter.remaining_cooldown == 0.0

    def test_increment_returns_new_count(self):
        counter = _FailureCounter(threshold=5)
        assert counter.increment() == 1
        assert counter.increment() == 2
        assert counter.count == 2

    def test_threshold_validation(self):
        with pytest.raises(ValueError, match="threshold"):
            _FailureCounter(threshold=0)
        with pytest.raises(ValueError, match="threshold"):
            _FailureCounter(threshold=-1)

    def test_thread_safety(self):
        """Basic smoke: concurrent trip() calls don't corrupt state.

        With cooldown=0, every call past threshold returns True;
        this test verifies count integrity under concurrency.
        """
        import concurrent.futures

        counter = _FailureCounter(threshold=100)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(counter.trip) for _ in range(150)]
            concurrent.futures.wait(futures)
        results = [f.result() for f in futures]
        # Count of True results = calls after (and including) 100th
        true_count = sum(1 for r in results if r)
        assert true_count == 51  # calls 100-150 inclusive
        assert counter.count == 150

    def test_remaining_cooldown_returns_zero_when_not_tripped(self):
        counter = _FailureCounter(threshold=3, cooldown=30.0)
        assert counter.remaining_cooldown == 0.0
        counter.trip()
        assert counter.remaining_cooldown == 0.0
